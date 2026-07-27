"""飞书企业自建应用：长连接收消息、回复指令并主动推送卡片。"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

import httpx

from piaoxingqiu_auto.adapters.cards import build_card

log = logging.getLogger("piaoxingqiu.feishu")

OPEN_API_BASE = "https://open.feishu.cn/open-apis"
TOKEN_REFRESH_MARGIN = 60
MESSAGE_DEDUPE_TTL = 3600.0
WS_SHUTDOWN_TIMEOUT = 5.0


class FeishuError(RuntimeError):
    pass


@dataclass(frozen=True)
class IncomingCommand:
    message_id: str
    chat_id: str
    sender_open_id: str
    text: str
    is_admin: bool


class FeishuGateway:
    def __init__(
        self,
        config: dict,
        command_queue: asyncio.Queue[IncomingCommand] | None = None,
    ):
        self.app_id = str(config.get("feishu_app_id") or "").strip()
        self.app_secret = str(config.get("feishu_app_secret") or "").strip()
        self.default_chat_id = str(config.get("feishu_default_chat_id") or "").strip()
        raw_admins = config.get("feishu_admin_open_ids") or []
        if not isinstance(raw_admins, list):
            raise FeishuError("feishu_admin_open_ids 必须是字符串数组")
        self.admin_open_ids = {
            str(item).strip() for item in raw_admins if str(item).strip()
        }
        if not self.app_id or not self.app_secret:
            raise FeishuError("缺少 feishu_app_id 或 feishu_app_secret")

        self.command_queue = command_queue
        self._http = httpx.AsyncClient(timeout=15)
        self._token = ""
        self._token_expires_at = 0.0
        self._token_lock = asyncio.Lock()
        self._processed_messages: dict[str, float] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._event_tasks: set[asyncio.Task] = set()
        self._closing = threading.Event()
        self._sdk_loop: asyncio.AbstractEventLoop | None = None
        self._ws_client: Any = None
        self._ws_thread: threading.Thread | None = None
        self._ws_stopped: asyncio.Future[None] | None = None

    async def aclose(self) -> None:
        self._closing.set()
        tasks = list(self._event_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        sdk_loop = self._sdk_loop
        ws_client = self._ws_client
        disconnect_future = None
        if sdk_loop is not None and not sdk_loop.is_closed():
            disconnect = getattr(ws_client, "_disconnect", None)
            if disconnect is not None and sdk_loop.is_running():
                try:
                    # lark-oapi 1.7.1 没有公开 stop API，只提供内部异步断连方法。
                    disconnect_future = asyncio.run_coroutine_threadsafe(
                        disconnect(), sdk_loop
                    )
                    await asyncio.wait_for(
                        asyncio.wrap_future(disconnect_future),
                        timeout=WS_SHUTDOWN_TIMEOUT,
                    )
                except TimeoutError:
                    if disconnect_future is not None:
                        disconnect_future.cancel()
                    log.warning("飞书长连接断开超时，将强制停止 SDK 事件循环")
                except RuntimeError:
                    # 线程可能恰好已自行退出并关闭循环。
                    pass
                except Exception:
                    log.exception("主动断开飞书长连接失败")
            with suppress(RuntimeError):
                sdk_loop.call_soon_threadsafe(sdk_loop.stop)

        thread = self._ws_thread
        if thread is not None and thread.is_alive():
            await asyncio.to_thread(thread.join, WS_SHUTDOWN_TIMEOUT)
            if thread.is_alive():
                log.warning("飞书长连接线程未在 %.0f 秒内退出", WS_SHUTDOWN_TIMEOUT)

        stopped = self._ws_stopped
        if stopped is not None and not stopped.done():
            stopped.set_result(None)
        await self._http.aclose()

    # ---- 应用身份与消息 API ----

    async def _tenant_access_token(self, force_refresh: bool = False) -> str:
        now = time.monotonic()
        if not force_refresh and self._token and now < self._token_expires_at:
            return self._token
        async with self._token_lock:
            now = time.monotonic()
            if not force_refresh and self._token and now < self._token_expires_at:
                return self._token
            response = await self._http.post(
                f"{OPEN_API_BASE}/auth/v3/tenant_access_token/internal",
                json={"app_id": self.app_id, "app_secret": self.app_secret},
            )
            payload = self._response_json(response, "获取 tenant_access_token")
            if (
                response.is_error
                or payload.get("code") != 0
                or not payload.get("tenant_access_token")
            ):
                raise FeishuError(
                    self._api_error("获取 tenant_access_token", response, payload)
                )
            expires_in = int(payload.get("expire") or 7200)
            self._token = payload["tenant_access_token"]
            self._token_expires_at = now + max(1, expires_in - TOKEN_REFRESH_MARGIN)
            return self._token

    async def _post_api(self, path: str, payload: dict) -> dict:
        for attempt in range(2):
            token = await self._tenant_access_token(force_refresh=attempt > 0)
            response = await self._http.post(
                f"{OPEN_API_BASE}{path}",
                headers={"Authorization": f"Bearer {token}"},
                json=payload,
            )
            data = self._response_json(response, "调用飞书 API")
            if response.is_success and data.get("code") == 0:
                return data
            if attempt == 0 and data.get("code") in (99991663, 99991668):
                self._token = ""
                continue
            raise FeishuError(self._api_error("飞书 API", response, data))
        raise FeishuError("飞书访问凭证刷新失败")

    @staticmethod
    def _response_json(response: httpx.Response, action: str) -> dict:
        try:
            data = response.json()
        except ValueError as exc:
            raise FeishuError(
                f"{action}失败：HTTP {response.status_code} 返回非 JSON 响应"
            ) from exc
        if not isinstance(data, dict):
            raise FeishuError(f"{action}失败：HTTP {response.status_code} 响应格式错误")
        return data

    @staticmethod
    def _api_error(action: str, response: httpx.Response, data: dict) -> str:
        log_id = response.headers.get("X-Tt-Logid", "")
        suffix = f" log_id={log_id}" if log_id else ""
        return (
            f"{action}失败：HTTP {response.status_code} "
            f"code={data.get('code')} {data.get('msg')}{suffix}"
        )

    async def send_card(self, title: str, body_md: str, template: str = "red") -> bool:
        if not self.default_chat_id:
            log.error("未配置 feishu_default_chat_id，无法主动推送")
            return False
        try:
            await self._post_api(
                "/im/v1/messages?receive_id_type=chat_id",
                {
                    "receive_id": self.default_chat_id,
                    "msg_type": "interactive",
                    "content": json.dumps(
                        build_card(title, body_md, template), ensure_ascii=False
                    ),
                },
            )
            return True
        except Exception as exc:
            log.error("应用机器人推送失败: %s", exc)
            return False

    async def reply_text(self, message_id: str, text: str) -> bool:
        try:
            await self._post_api(
                f"/im/v1/messages/{message_id}/reply",
                {
                    "msg_type": "text",
                    "content": json.dumps({"text": text}, ensure_ascii=False),
                },
            )
            return True
        except Exception as exc:
            log.error("回复飞书消息失败 [%s]: %s", message_id, exc)
            return False

    async def reply_image(self, message_id: str, content: bytes) -> bool:
        """上传验证码图片并回复当前私聊消息。"""
        try:
            image_key = await self._upload_image(content)
            await self._post_api(
                f"/im/v1/messages/{message_id}/reply",
                {
                    "msg_type": "image",
                    "content": json.dumps({"image_key": image_key}),
                },
            )
            return True
        except Exception as exc:
            log.error("回复飞书图片失败 [%s]: %s", message_id, exc)
            return False

    async def _upload_image(self, content: bytes) -> str:
        for attempt in range(2):
            token = await self._tenant_access_token(force_refresh=attempt > 0)
            response = await self._http.post(
                f"{OPEN_API_BASE}/im/v1/images",
                headers={"Authorization": f"Bearer {token}"},
                data={"image_type": "message"},
                files={"image": ("captcha.png", content, "image/png")},
            )
            payload = self._response_json(response, "上传飞书图片")
            image_key = (payload.get("data") or {}).get("image_key")
            if response.is_success and payload.get("code") == 0 and image_key:
                return str(image_key)
            if attempt == 0 and payload.get("code") in (99991663, 99991668):
                self._token = ""
                continue
            raise FeishuError(self._api_error("上传飞书图片", response, payload))
        raise FeishuError("飞书访问凭证刷新失败")

    # ---- 长连接事件 ----

    async def run_forever(self) -> None:
        if self.command_queue is None:
            raise FeishuError("未配置指令队列，无法启动飞书长连接")
        if self._closing.is_set():
            raise FeishuError("飞书长连接已关闭，不能重复启动")
        if self._ws_thread is not None and self._ws_thread.is_alive():
            raise FeishuError("飞书长连接已经在运行")
        self._loop = asyncio.get_running_loop()
        stopped = self._loop.create_future()
        self._ws_stopped = stopped

        def start_client() -> None:
            sdk_loop: asyncio.AbstractEventLoop | None = None
            ws_client: Any = None
            try:
                # lark-oapi 1.7.1 的 ws.Client 使用模块级事件循环，必须在实际
                # 运行 start() 的线程内创建并绑定，否则会与主 asyncio 循环冲突。
                sdk_loop = asyncio.new_event_loop()
                asyncio.set_event_loop(sdk_loop)
                self._sdk_loop = sdk_loop
                try:
                    import lark_oapi as lark  # type: ignore[import-untyped]
                    import lark_oapi.ws.client as ws_client_module  # type: ignore[import-untyped]
                except ImportError as exc:
                    raise FeishuError(
                        "未安装 lark-oapi，请先执行 python -m pip install -e ."
                    ) from exc
                ws_client_module.loop = sdk_loop

                def on_message(data) -> None:
                    try:
                        if self._closing.is_set():
                            return
                        payload = json.loads(lark.JSON.marshal(data))
                        self._call_main_loop(self._dispatch_payload, payload)
                    except Exception:
                        log.exception("解析飞书消息事件失败")

                handler = (
                    lark.EventDispatcherHandler.builder("", "")
                    .register_p2_im_message_receive_v1(on_message)
                    .build()
                )
                ws_client = lark.ws.Client(
                    self.app_id,
                    self.app_secret,
                    event_handler=handler,
                    log_level=lark.LogLevel.INFO,
                )
                self._ws_client = ws_client
                if self._closing.is_set():
                    return
                log.info("正在建立飞书长连接")
                ws_client.start()
            except BaseException as exc:
                if isinstance(exc, FeishuError):
                    failure = exc
                else:
                    detail = str(exc) or type(exc).__name__
                    failure = FeishuError(f"飞书长连接失败：{detail}")
                self._call_main_loop(self._finish_ws, stopped, failure)
            else:
                self._call_main_loop(
                    self._finish_ws, stopped, FeishuError("飞书长连接意外停止")
                )
            finally:
                try:
                    if sdk_loop is not None and not sdk_loop.is_closed():
                        pending = asyncio.all_tasks(sdk_loop)
                        for task in pending:
                            task.cancel()
                        if pending and not sdk_loop.is_running():
                            drained = asyncio.gather(*pending, return_exceptions=True)
                            # close() 可能在 start() 刚要运行的间隙排入 stop；
                            # 首次 drain 会消费 stop，第二次再完成任务回收。
                            for _ in range(2):
                                try:
                                    sdk_loop.run_until_complete(drained)
                                    break
                                except RuntimeError:
                                    if drained.done():
                                        break
                except Exception:
                    log.exception("清理飞书 SDK 事件循环失败")
                finally:
                    if sdk_loop is not None and not sdk_loop.is_closed():
                        sdk_loop.close()
                    if self._sdk_loop is sdk_loop:
                        self._sdk_loop = None
                    if self._ws_client is ws_client:
                        self._ws_client = None

        # SDK 的 start() 是阻塞调用。使用 daemon 线程可以让 systemd 停止服务时
        # 不被默认线程池的 shutdown 等待卡住；事件仍通过 call_soon_threadsafe 回主循环。
        thread = threading.Thread(
            target=start_client,
            name="feishu-ws",
            daemon=True,
        )
        self._ws_thread = thread
        thread.start()
        try:
            await stopped
        finally:
            if self._ws_stopped is stopped:
                self._ws_stopped = None

    @staticmethod
    def _finish_ws(future: asyncio.Future, exc: BaseException) -> None:
        if not future.done():
            future.set_exception(exc)

    def _call_main_loop(self, callback, *args) -> None:
        loop = self._loop
        if self._closing.is_set() or loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(callback, *args)
        except RuntimeError:
            if not self._closing.is_set():
                log.exception("投递飞书长连接事件失败")

    def _dispatch_payload(self, payload: dict) -> None:
        if self._closing.is_set():
            return
        task = asyncio.create_task(self._handle_event_payload(payload))
        self._event_tasks.add(task)
        task.add_done_callback(self._event_task_done)

    def _event_task_done(self, task: asyncio.Task) -> None:
        self._event_tasks.discard(task)
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:
            log.exception("处理飞书消息事件失败")

    async def _handle_event_payload(self, payload: dict) -> None:
        """处理已反序列化的飞书消息事件。"""
        event = payload.get("event") or payload
        sender = event.get("sender") or {}
        if sender.get("sender_type") != "user":
            return
        message = event.get("message") or {}
        if message.get("message_type") != "text":
            return

        message_id = str(message.get("message_id") or "")
        chat_id = str(message.get("chat_id") or "")
        chat_type = str(message.get("chat_type") or "")
        sender_id = sender.get("sender_id") or {}
        open_id = str(sender_id.get("open_id") or "")
        if not message_id or not chat_id or not chat_type or not open_id:
            log.warning("忽略字段不完整的飞书消息事件")
            return
        if chat_type not in {"p2p", "group"}:
            return
        log.info(
            "收到飞书消息：open_id=%s chat_id=%s chat_type=%s",
            open_id,
            chat_id,
            chat_type,
        )

        now = time.monotonic()
        self._cleanup_dedupe(now)
        if message_id in self._processed_messages:
            return

        is_admin = open_id in self.admin_open_ids
        if chat_type == "p2p" and not is_admin:
            log.warning("单聊用户未授权：open_id=%s chat_id=%s", open_id, chat_id)
            self._processed_messages[message_id] = now
            await self.reply_text(message_id, "无操作权限。")
            return
        if chat_type != "p2p" and not (message.get("mentions") or []):
            return

        try:
            content = json.loads(message.get("content") or "{}")
            text = str(content.get("text") or "")
        except (TypeError, json.JSONDecodeError):
            return
        for mention in message.get("mentions") or []:
            key = str(mention.get("key") or "")
            if key:
                text = text.replace(key, "")
        text = text.strip()
        if not text:
            return

        command = IncomingCommand(
            message_id=message_id,
            chat_id=chat_id,
            sender_open_id=open_id,
            text=text,
            is_admin=is_admin,
        )
        queue = self.command_queue
        if queue is None:
            log.error("收到飞书指令，但指令队列未初始化")
            return
        try:
            queue.put_nowait(command)
        except asyncio.QueueFull:
            log.error("飞书指令队列已满，丢弃消息 %s", message_id)
            self._processed_messages[message_id] = now
            await self.reply_text(message_id, "当前指令过多，请稍后重试。")
            return
        self._processed_messages[message_id] = now

    def _cleanup_dedupe(self, now: float) -> None:
        expired = [
            message_id
            for message_id, received_at in self._processed_messages.items()
            if now - received_at >= MESSAGE_DEDUPE_TTL
        ]
        for message_id in expired:
            self._processed_messages.pop(message_id, None)
