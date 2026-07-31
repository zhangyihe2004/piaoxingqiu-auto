"""一次性人工验证入口；只远程操作原 Playwright 风控区域。"""

from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from aiohttp import web

if TYPE_CHECKING:
    from playwright.async_api import Page
else:
    Page = Any


VERIFY_TTL_SECONDS = 300
SESSION_COOKIE = "pxq_verify"
MAX_TEXT_LENGTH = 64


@dataclass
class _Session:
    task_id: int
    account_id: int
    page: Page
    session_id: str
    token_hash: str
    expires_at: float
    result: asyncio.Future[bool]
    clip: dict[str, float] | None = None


@dataclass(frozen=True)
class VerificationAccess:
    url: str
    _result: asyncio.Future[bool]
    _expires_at: float

    async def wait(self) -> None:
        completed = await asyncio.wait_for(
            asyncio.shield(self._result),
            max(0.0, self._expires_at - time.monotonic()),
        )
        if not completed:
            raise TimeoutError


class OperatorGateway:
    def __init__(self) -> None:
        self.public_url = os.environ.get(
            "PIAOXINGQIU_OPERATOR_PUBLIC_URL", ""
        ).rstrip("/")
        self.listen = os.environ.get(
            "PIAOXINGQIU_OPERATOR_LISTEN", "127.0.0.1:8765"
        )
        self._sessions: dict[str, _Session] = {}
        self._tokens: dict[str, str] = {}
        self._runner: web.AppRunner | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.public_url)

    async def start(self) -> None:
        if not self.enabled:
            return
        host, separator, raw_port = self.listen.rpartition(":")
        if not separator or not host or not raw_port.isdigit():
            raise ValueError(
                "PIAOXINGQIU_OPERATOR_LISTEN 必须为 host:port"
            )
        app = web.Application(client_max_size=64 * 1024)
        app.add_routes(
            (
                web.get("/verify/{token}", self._redeem),
                web.get("/view", self._view),
                web.get("/frame", self._frame),
                web.post("/event", self._event),
                web.post("/done", self._done),
            )
        )
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        await web.TCPSite(self._runner, host, int(raw_port)).start()

    async def close(self) -> None:
        for session_id in tuple(self._sessions):
            self._discard(session_id)
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    def issue(
        self,
        task_id: int,
        account_id: int,
        page: Page,
    ) -> VerificationAccess | None:
        if not self.enabled or page.is_closed():
            return None
        self._cleanup()
        self.revoke(task_id, account_id)
        token = secrets.token_urlsafe(32)
        token_hash = _digest(token)
        session_id = secrets.token_urlsafe(32)
        expires_at = time.monotonic() + VERIFY_TTL_SECONDS
        result = asyncio.get_running_loop().create_future()
        session = _Session(
            task_id,
            account_id,
            page,
            session_id,
            token_hash,
            expires_at,
            result,
        )
        self._sessions[session_id] = session
        self._tokens[token_hash] = session_id
        return VerificationAccess(
            f"{self.public_url}/verify/{token}",
            result,
            expires_at,
        )

    def revoke(self, task_id: int, account_id: int) -> None:
        for session_id, session in tuple(self._sessions.items()):
            if (session.task_id, session.account_id) != (task_id, account_id):
                continue
            self._discard(session_id)

    async def _redeem(self, request: web.Request) -> web.StreamResponse:
        self._cleanup()
        token_hash = _digest(request.match_info["token"])
        session_id = self._tokens.pop(token_hash, None)
        session = self._sessions.get(session_id or "")
        if session is None:
            raise web.HTTPGone(text="链接无效或已过期")
        response = web.HTTPFound(f"{self.public_url}/view")
        response.set_cookie(
            SESSION_COOKIE,
            session.session_id,
            httponly=True,
            secure=urlsplit(self.public_url).scheme == "https",
            samesite="Strict",
            max_age=VERIFY_TTL_SECONDS,
            path=urlsplit(self.public_url).path or "/",
        )
        return response

    async def _view(self, request: web.Request) -> web.Response:
        self._session(request)
        return web.Response(
            text=_VIEW_HTML,
            content_type="text/html",
            headers={"Cache-Control": "no-store"},
        )

    async def _frame(self, request: web.Request) -> web.Response:
        session = self._session(request)
        clip = await _verification_clip(session.page)
        if clip is None:
            raise web.HTTPConflict(
                text="暂时无法安全定位官方验证区域，请稍后刷新"
            )
        session.clip = clip
        content = await session.page.screenshot(
            type="jpeg",
            quality=75,
            clip=clip,
        )
        return web.Response(
            body=content,
            content_type="image/jpeg",
            headers={"Cache-Control": "no-store"},
        )

    async def _event(self, request: web.Request) -> web.Response:
        session = self._session(request)
        clip = session.clip
        if clip is None:
            raise web.HTTPConflict(text="验证画面尚未准备完成")
        data = await request.json()
        action = str(data.get("action") or "")
        if action in {"down", "move", "up", "click"}:
            x = clip["x"] + _ratio(data.get("x")) * clip["width"]
            y = clip["y"] + _ratio(data.get("y")) * clip["height"]
            if action == "down":
                await session.page.mouse.move(x, y)
                await session.page.mouse.down()
            elif action == "move":
                await session.page.mouse.move(x, y)
            elif action == "up":
                await session.page.mouse.move(x, y)
                await session.page.mouse.up()
            else:
                await session.page.mouse.click(x, y)
        elif action == "type":
            text = str(data.get("text") or "")
            if not text or len(text) > MAX_TEXT_LENGTH:
                raise web.HTTPBadRequest(text="输入长度无效")
            await session.page.keyboard.type(text)
        elif action == "enter":
            await session.page.keyboard.press("Enter")
        else:
            raise web.HTTPBadRequest(text="未知操作")
        return web.json_response({"ok": True})

    async def _done(self, request: web.Request) -> web.Response:
        session = self._session(request)
        if not session.result.done():
            session.result.set_result(True)
        return web.json_response({"ok": True})

    def _session(self, request: web.Request) -> _Session:
        self._cleanup()
        session = self._sessions.get(request.cookies.get(SESSION_COOKIE, ""))
        if session is None or session.page.is_closed():
            raise web.HTTPUnauthorized(text="验证会话无效或已结束")
        return session

    def _cleanup(self) -> None:
        now = time.monotonic()
        for session_id, session in tuple(self._sessions.items()):
            if now < session.expires_at and not session.page.is_closed():
                continue
            self._discard(session_id)

    def _discard(self, session_id: str) -> None:
        session = self._sessions.pop(session_id, None)
        if session is None:
            return
        self._tokens.pop(session.token_hash, None)
        if not session.result.done():
            session.result.set_result(False)


async def _verification_clip(page: Page) -> dict[str, float] | None:
    return await page.evaluate(
        """
        () => {
          const visible = element => {
            const style = getComputedStyle(element);
            const rect = element.getBoundingClientRect();
            return style.visibility !== "hidden" && style.display !== "none" &&
              rect.width >= 160 && rect.height >= 120 &&
              rect.bottom > 0 && rect.right > 0 &&
              rect.top < innerHeight && rect.left < innerWidth;
          };
          const selectors = [
            "iframe[src*=captcha i]",
            "iframe[src*=verify i]",
            "iframe[title*=验证 i]",
            "[role=dialog]",
            "[aria-modal=true]",
            "[class*=captcha i]",
            "[class*=verify i]",
            "[class*=risk i]"
          ];
          const candidates = [...document.querySelectorAll(selectors.join(","))]
            .filter(visible);
          for (const element of document.querySelectorAll("body *")) {
            if (!visible(element)) continue;
            const text = (element.innerText || "").replace(/\\s+/g, "");
            if (!/(人机验证|安全验证|依次点击|拖动滑块|请输入计算结果)/.test(text)) {
              continue;
            }
            let current = element;
            for (let depth = 0; depth < 4 && current.parentElement; depth++) {
              if (visible(current)) candidates.push(current);
              current = current.parentElement;
            }
          }
          if (!candidates.length) return null;
          const unique = [...new Set(candidates)];
          unique.sort((left, right) => {
            const a = left.getBoundingClientRect();
            const b = right.getBoundingClientRect();
            return a.width * a.height - b.width * b.height;
          });
          const rect = unique[0].getBoundingClientRect();
          const x = Math.max(0, rect.left);
          const y = Math.max(0, rect.top);
          return {
            x,
            y,
            width: Math.min(innerWidth - x, rect.width),
            height: Math.min(innerHeight - y, rect.height)
          };
        }
        """
    )


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _ratio(value: object) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise web.HTTPBadRequest(text="坐标无效") from exc
    if not 0 <= result <= 1:
        raise web.HTTPBadRequest(text="坐标越界")
    return result


_VIEW_HTML = """<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>票星球人工验证</title>
<style>
body{font:16px system-ui;margin:0;background:#111;color:#fff;text-align:center}
main{max-width:520px;margin:auto;padding:16px}
img{width:100%;touch-action:none;background:#222;border-radius:8px}
input,button{font:inherit;margin:8px 4px;padding:10px}
#status{min-height:24px;color:#ccc}
</style>
<main>
<h3>票星球官方验证</h3>
<p id="status">正在读取原浏览器验证区域…</p>
<img id="frame" draggable="false">
<div><input id="text" maxlength="64" placeholder="需要输入时填写">
<button id="send">输入</button><button id="enter">回车</button></div>
<button id="done">验证已完成</button>
</main>
<script>
const image=document.querySelector("#frame"),status=document.querySelector("#status");
let dragging=false,moved=false,lastMove=0;
async function call(path,data){const response=await fetch(path,{method:"POST",
headers:{"Content-Type":"application/json"},body:JSON.stringify(data||{})});
if(!response.ok)throw Error(await response.text());return response}
function point(event){const box=image.getBoundingClientRect();return{
x:(event.clientX-box.left)/box.width,y:(event.clientY-box.top)/box.height}}
async function event(action,p){try{await call("event",{action,...p})}
catch(error){status.textContent=error.message}}
image.onpointerdown=e=>{dragging=true;moved=false;image.setPointerCapture(e.pointerId);
event("down",point(e))};
image.onpointermove=e=>{if(!dragging||Date.now()-lastMove<40)return;
lastMove=Date.now();moved=true;event("move",point(e))};
image.onpointerup=e=>{dragging=false;event("up",point(e))};
image.onclick=e=>{if(!moved)event("click",point(e))};
document.querySelector("#send").onclick=()=>event("type",{
text:document.querySelector("#text").value});
document.querySelector("#enter").onclick=()=>event("enter",{});
document.querySelector("#done").onclick=async()=>{await call("done");
status.textContent="已通知程序继续验证流程";};
async function refresh(){try{const response=await fetch("frame",{cache:"no-store"});
if(!response.ok)throw Error(await response.text());image.src=URL.createObjectURL(
await response.blob());status.textContent="请在上方官方验证区域完成操作";}
catch(error){status.textContent=error.message}setTimeout(refresh,600)}
refresh();
</script>"""
