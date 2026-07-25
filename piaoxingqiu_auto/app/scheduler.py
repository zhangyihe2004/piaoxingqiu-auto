"""任务轮询、账号调度和结果通知。"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass

from piaoxingqiu_auto.platform.api import PxqError
from piaoxingqiu_auto.platform.auth import AuthGuard, OfficialAudience
from piaoxingqiu_auto.runtime.browser import AccountBrowserPool
from piaoxingqiu_auto.app.run_config import build_login_config, build_order_config
from piaoxingqiu_auto.domain.models import AccountRunConfig, SystemConfig
from piaoxingqiu_auto.app.database import Database
from piaoxingqiu_auto.adapters.feishu_gateway import FeishuGateway
from piaoxingqiu_auto.platform.order_guard import PersistentOrderGuard
from piaoxingqiu_auto.platform.booking import PurchasePage
from piaoxingqiu_auto.domain.outcomes import RunResult
from piaoxingqiu_auto.runtime.order_flow import check_login, run_account
from piaoxingqiu_auto.domain.sale import (
    ACTIVE_SESSION_STATUSES,
    PREWARM_SECONDS,
    sale_phase,
)
from piaoxingqiu_auto.app.tasks import (
    SessionUnavailable,
    TaskService,
)


log = logging.getLogger("piaoxingqiu.auto")
ERROR_ALERT_COOLDOWN = 3600.0
MAX_BACKOFF_FACTOR = 16
# 状态轮询不应阻塞真正抢票，但也不让大量任务同时打接口。
MAX_CONCURRENT_POLLS = 6
NOTICE_RETRY_SECONDS = 10.0


@dataclass(frozen=True)
class PendingNotice:
    key: str
    title: str
    body: str
    template: str
    required_task_status: str = "active"
    available_after: frozenset[str] | None = None
    error_alert: bool = False


class TaskScheduler:
    def __init__(
        self,
        db: Database,
        service: TaskService,
        feishu: FeishuGateway,
        system: SystemConfig,
    ) -> None:
        self.db = db
        self.service = service
        self.feishu = feishu
        self.system = system
        self.semaphore = asyncio.Semaphore(system.max_concurrent_accounts)
        self.login_semaphore = asyncio.Semaphore(
            min(system.max_concurrent_accounts, 4)
        )
        self.jobs: dict[int, asyncio.Task] = {}
        self.job_bindings: dict[int, tuple[int, int]] = {}
        self.checks: dict[int, asyncio.Task] = {}
        self.browsers = AccountBrowserPool()
        self.next_poll: dict[int, float] = {}
        self.next_auth: dict[int, float] = {}
        self.failures: dict[int, int] = {}
        self.last_error_alert: dict[int, float] = {}
        self.pending_notices: dict[int, dict[str, PendingNotice]] = {}
        self.next_notice_retry = 0.0
        self.available_plans: dict[int, set[str]] = {}
        self.phases: dict[int, str] = {}

    async def run_forever(self) -> None:
        log.info("自动抢票引擎启动")
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("抢票调度循环异常，将在下一轮重试")
            await asyncio.sleep(1)

    async def tick(self) -> None:
        tasks = self.db.list_tasks()
        active_ids = {task["id"] for task in tasks if task["status"] == "active"}
        self._prune_runtime_state(active_ids)
        now = time.monotonic()
        due = [
            task
            for task in tasks
            if task["status"] == "active" and now >= self.next_poll.get(task["id"], 0)
        ]
        if due:
            semaphore = asyncio.Semaphore(MAX_CONCURRENT_POLLS)
            session_tasks: dict[str, asyncio.Task[list[dict]]] = {}

            async def poll_due(task) -> None:
                async with semaphore:
                    show_id = str(task["show_id"])
                    sessions_task = session_tasks.get(show_id)
                    if sessions_task is None:
                        sessions_task = asyncio.create_task(
                            self.service.client.quick_order_sessions(show_id)
                        )
                        session_tasks[show_id] = sessions_task
                    await self._poll_safely(task, sessions_task)

            await asyncio.gather(*(poll_due(task) for task in due))
        await self._retry_notices()

    async def _poll_safely(
        self,
        task,
        sessions_task: asyncio.Task[list[dict]],
    ) -> None:
        task_id = int(task["id"])
        try:
            await self._poll(task, sessions_task)
            if self.failures.get(task_id):
                log.info("抢票任务 #%s 轮询恢复正常", task_id)
                self.pending_notices.get(task_id, {}).pop("poll-error", None)
            self.failures[task_id] = 0
        except SessionUnavailable:
            self.failures[task_id] = 0
            await self._pause_task(
                task,
                "session-gone",
                "目标场次未出现在官方场次列表",
            )
        except PxqError as exc:
            if exc.status_code == 22024033:
                self.failures[task_id] = 0
                await self._pause_task(
                    task,
                    "show-unavailable",
                    f"票星球返回 22024033：{exc.comments or '节目暂不可售'}",
                )
            else:
                self._handle_poll_error(task, exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._handle_poll_error(task, exc)
        finally:
            self._schedule_next(task_id)

    async def _poll(
        self,
        task,
        sessions_task: asyncio.Task[list[dict]],
    ) -> None:
        task_id = int(task["id"])
        if task_id not in self.available_plans:
            previous = self.db.get_task_plans(task_id)
            self.available_plans[task_id] = self._available_plan_ids(
                task_id,
                previous,
                sale_phase(task, previous),
            )
        status, sale_time_ms, snapshot = await self.service.refresh_task(
            task, sessions_task
        )
        if not self.db.update_task_snapshot(task_id, status, sale_time_ms, snapshot):
            return
        task = self.db.get_task(task_id)
        assert task is not None
        plans = self.db.get_task_plans(task_id)
        now_ms = int(time.time() * 1000)
        remaining = (
            (task["sale_time_ms"] - now_ms) / 1000
            if status == "PENDING" and task["sale_time_ms"] is not None
            else None
        )
        if status == "DELAY":
            await self._pause_task(task, "session-delay", "目标场次延期（DELAY）")
            return
        if status not in ACTIVE_SESSION_STATUSES:
            await self._pause_task(
                task,
                "unknown-status",
                f"官方返回未知场次状态：{status}",
            )
            return

        phase = sale_phase(task, plans, now_ms)
        previous_phase = self.phases.get(task_id)
        if phase != previous_phase:
            log.info(
                "抢票任务 #%s 阶段切换：%s -> %s（距开售 %s）",
                task_id,
                previous_phase or "初始",
                phase,
                "未知" if remaining is None else f"{remaining:.1f} 秒",
            )
            self.phases[task_id] = phase
        prewarm = phase == "PREWARM"
        if self.system.create_order_enabled:
            for binding in self.db.list_bindings(task_id=task_id):
                account = self.db.get_account(binding["account_id"])
                if not account or account["status"] != "READY":
                    continue
                account_plans = self.db.get_binding_plans(
                    task_id, binding["account_id"]
                )
                available = any(
                    plan["sale_started"] and plan["can_buy_count"] > 0
                    for plan in account_plans
                )
                if (
                    binding["enabled"]
                    and binding["status"] == "READY"
                    and (prewarm or (phase == "AVAILABLE" and available))
                ):
                    self._start_binding(
                        task_id, int(binding["account_id"]), prewarm=prewarm
                    )

        self._update_stock_notice(task, plans, phase)
        self._schedule_login_checks(task, remaining)

    async def _pause_task(self, task, key: str, reason: str) -> None:
        task_id = int(task["id"])
        if not self.db.set_task_status(task_id, "paused", current_status="active"):
            return
        await self.cancel_task(task_id, reason=reason)
        self._clear_runtime_state(task_id)
        self._send_notice(
            task_id,
            PendingNotice(
                key,
                "抢票任务已暂停",
                (
                    f"任务 #{task_id}\n**{task['show_name']}**\n"
                    f"场次：{task['session_name']}\n{reason}\n"
                    f"核对后发送：恢复 {task_id}"
                ),
                "orange",
                required_task_status="paused",
            ),
        )

    def _handle_poll_error(self, task, exc: Exception) -> None:
        task_id = int(task["id"])
        failures = self.failures.get(task_id, 0) + 1
        self.failures[task_id] = failures
        log.warning(
            "抢票任务 #%s 轮询失败（连续 %s 次）：%s",
            task_id,
            failures,
            exc,
        )
        if failures >= 5:
            self._alert_poll_error(task, failures, exc)

    def _update_stock_notice(self, task, plans, phase: str) -> None:
        task_id = int(task["id"])
        current = self._available_plan_ids(task_id, plans, phase)
        added = current - self.available_plans[task_id]
        if not added:
            self.available_plans[task_id] = current
            return
        available = [plan for plan in plans if str(plan["seat_plan_id"]) in added]
        self._send_notice(
            task_id,
            PendingNotice(
                "stock",
                "余票提醒",
                "\n".join(
                    (
                        f"任务 #{task_id}\n**{task['show_name']}**",
                        f"场次：{task['session_name']}",
                        *(
                            f"· {plan['plan_name']}：最多可买 "
                            f"{plan['can_buy_count'] * plan['unit_qty']} 张"
                            for plan in available
                        ),
                    )
                ),
                "red",
                available_after=frozenset(current),
            ),
        )

    def _available_plan_ids(self, task_id: int, plans, phase: str) -> set[str]:
        watched = {
            str(plan["seat_plan_id"])
            for binding in self.db.list_bindings(task_id=task_id)
            if binding["enabled"]
            for plan in self.db.get_binding_plans(task_id, binding["account_id"])
        }
        return {
            str(plan["seat_plan_id"])
            for plan in plans
            if phase == "AVAILABLE"
            and str(plan["seat_plan_id"]) in watched
            and plan["sale_started"]
            and plan["can_buy_count"] > 0
        }

    def _alert_poll_error(
        self,
        task,
        failures: int,
        exc: Exception,
    ) -> None:
        task_id = int(task["id"])
        last = self.last_error_alert.get(task_id)
        if last is not None and time.monotonic() - last < ERROR_ALERT_COOLDOWN:
            return
        self._send_notice(
            task_id,
            PendingNotice(
                "poll-error",
                "抢票监控异常",
                (
                    f"任务 #{task_id}\n**{task['show_name']}**\n"
                    f"连续 {failures} 次抓取失败，已自动放慢频率。\n最近错误：{exc}"
                ),
                "orange",
                error_alert=True,
            ),
        )

    def _send_notice(self, task_id: int, notice: PendingNotice) -> None:
        pending = self.pending_notices.setdefault(task_id, {})
        if notice.key in pending:
            return
        pending[notice.key] = notice

    async def _retry_notices(self) -> None:
        if not self.pending_notices or time.monotonic() < self.next_notice_retry:
            return
        failed = False
        for task_id, pending in list(self.pending_notices.items()):
            task = self.db.get_task(task_id)
            if not task:
                self.pending_notices.pop(task_id, None)
                continue
            for notice in list(pending.values()):
                if task["status"] != notice.required_task_status:
                    pending.pop(notice.key, None)
                    continue
                if not await self.feishu.send_card(
                    notice.title,
                    notice.body,
                    notice.template,
                ):
                    failed = True
                    continue
                pending.pop(notice.key, None)
                self._complete_notice(task_id, notice)
            if not pending:
                self.pending_notices.pop(task_id, None)
        self.next_notice_retry = (
            time.monotonic() + NOTICE_RETRY_SECONDS if failed else 0.0
        )

    def _complete_notice(self, task_id: int, notice: PendingNotice) -> None:
        if notice.available_after is not None:
            self.available_plans[task_id] = set(notice.available_after)
        if notice.error_alert:
            self.last_error_alert[task_id] = time.monotonic()

    def _schedule_next(self, task_id: int) -> None:
        task = self.db.get_task(task_id)
        if not task or task["status"] != "active":
            self._clear_runtime_state(task_id)
            return
        delay = float(task["interval_sec"])
        delay *= min(2 ** self.failures.get(task_id, 0), MAX_BACKOFF_FACTOR)
        if task["session_status"] == "PENDING" and task["sale_time_ms"] is not None:
            remaining = (task["sale_time_ms"] - int(time.time() * 1000)) / 1000
            if remaining > PREWARM_SECONDS:
                delay = min(delay, max(1.0, remaining - PREWARM_SECONDS))
        self.next_poll[task_id] = time.monotonic() + delay

    def _prune_runtime_state(self, active_ids: set[int]) -> None:
        for mapping in (
            self.next_poll,
            self.failures,
            self.last_error_alert,
            self.available_plans,
            self.phases,
        ):
            for task_id in set(mapping) - active_ids:
                mapping.pop(task_id, None)

    def _clear_runtime_state(self, task_id: int) -> None:
        for mapping in (
            self.next_poll,
            self.failures,
            self.last_error_alert,
            self.available_plans,
            self.phases,
        ):
            mapping.pop(task_id, None)

    def _start_binding(self, task_id: int, account_id: int, *, prewarm: bool) -> None:
        if account_id in self.jobs:
            log.debug("账号 #%s 已有执行任务，忽略重复启动", account_id)
            return
        mode = "开售预热" if prewarm else "回流抢票"
        log.info("任务 #%s 账号 #%s 已加入执行队列（%s）", task_id, account_id, mode)
        job = asyncio.create_task(
            self._run_after_check(
                task_id, account_id, prewarm, self.checks.get(account_id)
            ),
            name=(
                f"piaoxingqiu-binding-{task_id}-{account_id}-"
                f"{'prewarm' if prewarm else 'available'}"
            ),
        )
        self.jobs[account_id] = job
        self.job_bindings[account_id] = (task_id, account_id)

        def forget_job(_job: asyncio.Task) -> None:
            if self.jobs.get(account_id) is _job:
                self.jobs.pop(account_id, None)
                self.job_bindings.pop(account_id, None)
            try:
                outcome = _job.result()
            except asyncio.CancelledError:
                log.warning(
                    "任务 #%s 账号 #%s 执行任务被取消（%s）",
                    task_id,
                    account_id,
                    mode,
                )
            except Exception as exc:
                log.error(
                    "任务 #%s 账号 #%s 执行任务异常退出（%s）：%s",
                    task_id,
                    account_id,
                    mode,
                    exc,
                    exc_info=(type(exc), exc, exc.__traceback__),
                )
            else:
                log.info(
                    "任务 #%s 账号 #%s 执行任务结束（%s）：%s",
                    task_id,
                    account_id,
                    mode,
                    outcome,
                )

        job.add_done_callback(forget_job)

    async def _run_after_check(
        self,
        task_id: int,
        account_id: int,
        prewarm: bool,
        check: asyncio.Task | None,
    ) -> str:
        if check:
            log.info("账号 #%s 启动抢票前取消正在进行的登录检查", account_id)
            check.cancel()
            await asyncio.gather(check, return_exceptions=True)
        return await self._run_binding(task_id, account_id, prewarm)

    async def _run_binding(self, task_id: int, account_id: int, prewarm: bool) -> str:
        async with _binding_slot(self.semaphore, prewarm):
            log.info(
                "账号 #%s 开始%s",
                account_id,
                "开售预热" if prewarm else "回流抢票",
            )
            account = self.db.get_account(account_id)
            if not account:
                return "账号已不存在"
            if account["status"] != "READY":
                return "账号登录状态无效"
            binding = self.db.get_binding(task_id, account_id)
            if not binding or not binding["enabled"]:
                return "绑定未启动"
            if binding["status"] != "READY":
                return f"绑定状态为 {binding['status']}"
            task = self.db.get_task(task_id)
            if not task:
                return "任务已不存在"
            if task["status"] != "active":
                return f"任务状态为 {task['status']}"
            plans = self.db.get_binding_plans(task_id, account_id)
            if prewarm:
                phase = sale_phase(task, plans)
                if phase == "AVAILABLE":
                    prewarm = False
                elif phase != "PREWARM":
                    return f"预热阶段已结束（{phase}）"
            people = self.db.get_binding_audiences(task_id, account_id)
            config = build_order_config(
                task, plans, people, account, binding, self.system
            )
            if not self.db.claim_binding(task_id, account_id):
                return "绑定状态竞争失败"
            try:
                async with self.browsers.use(account_id, config.browser) as context:
                    warm = await self.browsers.task_page(account_id, task_id, context)
                    result = await run_account(
                        config,
                        context,
                        prewarm=prewarm,
                        trace_label=f"任务 #{task['id']}｜账号 #{account_id}",
                        page=warm.page,
                        auth_headers=warm.auth_headers,
                        runtime_cache=self.browsers.account_cache(account_id),
                        execution_gate=self.semaphore if prewarm else None,
                    )
            except asyncio.CancelledError:
                current = self.db.get_binding(task_id, account_id)
                if current:
                    protected = _protected_order_state(config)
                    if protected:
                        self.db.set_binding_status(
                            task_id,
                            account_id,
                            protected[0],
                            order_id=protected[1],
                            error="执行中断，请人工核对待支付订单",
                        )
                    else:
                        self.db.set_binding_status(
                            task_id,
                            account_id,
                            "READY" if current["enabled"] else "STOPPED",
                        )
                raise
            except Exception as exc:
                with suppress(Exception):
                    await self.browsers.close_task_page(account_id, task_id)
                log.exception("账号 #%s 抢票执行失败", account_id)
                current = self.db.get_binding(task_id, account_id)
                result = RunResult("FAILED", str(exc))
                if current:
                    protected = _protected_order_state(config)
                    if protected:
                        status, order_id = protected
                        self.db.set_binding_status(
                            task_id,
                            account_id,
                            status,
                            order_id=order_id,
                            error=str(exc),
                        )
                        result = RunResult(status, str(exc), order_id=order_id)
                    else:
                        self.db.set_binding_status(
                            task_id,
                            account_id,
                            "READY" if current["enabled"] else "STOPPED",
                            error=str(exc),
                        )
                self._notify_result(task_id, account_id, task, result)
                return result.status
            if not result.reuse_page:
                with suppress(Exception):
                    await self.browsers.close_task_page(account_id, task_id)
            else:
                log.info(
                    "任务 #%s 账号 #%s 保留热页面，供下一轮复用",
                    task_id,
                    account_id,
                )
            self.db.apply_fulfillment(
                task_id,
                account_id,
                result.fulfilled_quantity,
                result.removed_audiences,
            )
            current = self.db.get_binding(task_id, account_id)
            if current is None:
                return "执行完成后账号已不存在"
            next_status = {
                "CREATED": "CREATED",
                "UNKNOWN": "UNKNOWN",
                "NEEDS_LOGIN": "NEEDS_LOGIN",
                "COMPLETE": "COMPLETE",
            }.get(result.status, "READY" if current["enabled"] else "STOPPED")
            self.db.set_binding_status(
                task_id,
                account_id,
                next_status,
                order_id=result.order_id,
                error=""
                if result.status in {"CREATED", "COMPLETE"}
                else result.message,
            )
            if result.status == "NEEDS_LOGIN":
                self.db.set_account_status(
                    account_id, "NEEDS_LOGIN", error=result.message
                )
            if result.status != "RESTOCK":
                self._notify_result(task_id, account_id, task, result)
            return result.status

    def _schedule_login_checks(self, task, remaining: float | None) -> None:
        interval = AuthGuard.interval(
            remaining if remaining is not None else float("inf")
        )
        now = time.monotonic()
        for binding in self.db.list_bindings(task_id=task["id"]):
            account_id = int(binding["account_id"])
            account = self.db.get_account(account_id)
            next_check = self.next_auth.get(account_id, 0)
            if (
                not account
                or account["status"] != "READY"
                or not binding["enabled"]
                or binding["status"] != "READY"
                or account_id in self.jobs
                or account_id in self.checks
                or now < next_check <= now + interval
            ):
                continue
            self.next_auth[account_id] = now + interval
            check = asyncio.create_task(
                self._check_account_login(account_id, task, binding)
            )
            self.checks[account_id] = check

            def forget_check(_check: asyncio.Task, aid: int = account_id) -> None:
                if self.checks.get(aid) is _check:
                    self.checks.pop(aid, None)

            check.add_done_callback(forget_check)

    async def _check_account_login(self, account_id: int, task, binding) -> None:
        async with self.login_semaphore:
            account = self.db.get_account(account_id)
            if (
                not account
                or account["status"] != "READY"
                or not binding["enabled"]
                or account_id in self.jobs
            ):
                return
            config = build_login_config(task, account, self.system)
            try:
                async with self.browsers.use(account_id, config.browser) as context:
                    valid = await check_login(config, context)
            except Exception as exc:
                log.warning("账号 #%s 登录检查失败：%s", account_id, exc)
                return
            if not valid and self.db.get_account(account_id):
                self.db.set_account_status(
                    account_id, "NEEDS_LOGIN", error="登录已失效"
                )
                self._send_notice(
                    int(task["id"]),
                    PendingNotice(
                        f"login:{account_id}",
                        "账号登录已失效",
                        f"账号 #{account_id}\n发送：登录",
                        "orange",
                    ),
                )

    def _notify_result(
        self, task_id: int, account_id: int, task, result: RunResult
    ) -> None:
        title = {
            "CREATED": "订单已创建",
            "UNKNOWN": "订单结果未知",
            "NEEDS_LOGIN": "账号需要重新登录",
            "COMPLETE": "目标数量已完成",
        }.get(result.status, "抢票执行异常")
        color = "green" if result.status in {"CREATED", "COMPLETE"} else "orange"
        action = {
            "CREATED": (
                "下一步：在票星球处理待支付订单；"
                f"如已取消并需继续，发送：启动 {task_id} {account_id}"
            ),
            "UNKNOWN": (
                f"下一步：检查票星球待支付订单；确认无订单后发送："
                f"启动 {task_id} {account_id}"
            ),
            "NEEDS_LOGIN": "下一步：登录",
            "COMPLETE": f"下一步：绑定 {task_id} {account_id}",
        }.get(result.status, "状态：该绑定保持启动，将自动继续等待。")
        body = (
            f"任务 #{task['id']}｜账号 #{account_id}\n"
            f"**{task['show_name']}**\n{result.message}\n{action}\n操作：未支付"
        )
        self._send_notice(
            int(task["id"]),
            PendingNotice(
                f"result:{account_id}:{result.status}",
                title,
                body,
                color,
            ),
        )

    async def cancel_binding(
        self,
        task_id: int,
        account_id: int,
        *,
        reason: str = "账号操作",
    ) -> None:
        pending = self.pending_notices.get(task_id)
        if pending:
            for key in tuple(pending):
                if key.startswith(f"result:{account_id}:"):
                    pending.pop(key, None)
        activities = [
            activity
            for activity in (self.jobs.get(account_id),)
            if self.job_bindings.get(account_id) == (task_id, account_id)
            if activity
        ]
        if activities:
            log.warning("取消账号 #%s 后台任务：%s", account_id, reason)
        for activity in activities:
            activity.cancel()
        if activities:
            await asyncio.gather(*activities, return_exceptions=True)
        await self.browsers.close_task_page(account_id, task_id)

    async def release_account_if_idle(self, account_id: int) -> None:
        active = any(
            binding["enabled"]
            and binding["status"] in {"READY", "RUNNING"}
            and (task := self.db.get_task(binding["task_id"]))
            and task["status"] == "active"
            for binding in self.db.list_bindings(account_id=account_id)
        )
        if active:
            return
        for pending in self.pending_notices.values():
            pending.pop(f"login:{account_id}", None)
        await self.browsers.close(account_id)

    async def cancel_account(
        self, account_id: int, *, reason: str = "账号操作"
    ) -> None:
        self.next_auth.pop(account_id, None)
        for pending in self.pending_notices.values():
            for key in tuple(pending):
                if key == f"login:{account_id}" or key.startswith(
                    f"result:{account_id}:"
                ):
                    pending.pop(key, None)
        activities = [
            activity
            for activity in (self.jobs.get(account_id), self.checks.get(account_id))
            if activity
        ]
        if activities:
            log.warning("取消账号 #%s 后台任务：%s", account_id, reason)
        for activity in activities:
            activity.cancel()
        if activities:
            await asyncio.gather(*activities, return_exceptions=True)
        await self.browsers.close(account_id)

    async def cancel_task(self, task_id: int, *, reason: str = "任务操作") -> None:
        account_ids = {
            int(binding["account_id"])
            for binding in self.db.list_bindings(task_id=task_id)
        }
        self._move_login_notices(task_id)
        self.pending_notices.pop(task_id, None)
        await asyncio.gather(
            *(
                self.cancel_binding(
                    task_id,
                    binding["account_id"],
                    reason=f"{reason}（任务 #{task_id}）",
                )
                for binding in self.db.list_bindings(task_id=task_id)
            )
        )
        self._clear_runtime_state(task_id)
        await asyncio.gather(
            *(self.release_account_if_idle(account_id) for account_id in account_ids)
        )

    def _move_login_notices(self, task_id: int) -> None:
        pending = self.pending_notices.get(task_id)
        if not pending:
            return
        for key, notice in tuple(pending.items()):
            if not key.startswith("login:"):
                continue
            account_id = int(key.partition(":")[2])
            target = next(
                (
                    int(binding["task_id"])
                    for binding in self.db.list_bindings(account_id=account_id)
                    if int(binding["task_id"]) != task_id
                    and (task := self.db.get_task(binding["task_id"]))
                    and task["status"] == "active"
                ),
                None,
            )
            if target is not None:
                self.pending_notices.setdefault(target, {})[key] = notice

    async def close(self) -> None:
        activities = [*self.jobs.values(), *self.checks.values()]
        for activity in activities:
            activity.cancel()
        if activities:
            await asyncio.gather(*activities, return_exceptions=True)
        await self.browsers.close_all()

    async def official_audiences(
        self, task_id: int, account_id: int
    ) -> tuple[OfficialAudience, ...]:
        task = self.db.get_task(task_id)
        account = self.db.get_account(account_id)
        if not task or not account:
            raise ValueError("任务或账号不存在")
        if account["status"] != "READY":
            raise ValueError("账号登录状态无效")
        if account_id in self.jobs:
            raise ValueError("账号正在执行其他任务，请稍后重试")
        config = build_login_config(task, account, self.system)
        async with self.browsers.use(account_id, config.browser) as context:
            page = await context.new_page()
            site = PurchasePage(page, config)
            try:
                audiences = await AuthGuard(site).audiences()
                self.browsers.account_cache(account_id)["audiences"] = audiences
                return audiences
            finally:
                with suppress(Exception):
                    await page.close()


def _protected_order_state(
    config: AccountRunConfig,
) -> tuple[str, str | None] | None:
    try:
        state = PersistentOrderGuard(config.state_path, config.plan_key).current()
    except RuntimeError:
        return "UNKNOWN", None
    if state.status == "SUBMITTING":
        return "UNKNOWN", state.order_id
    if state.status in {"CREATED", "UNKNOWN"}:
        return state.status, state.order_id
    return None


@asynccontextmanager
async def _binding_slot(
    semaphore: asyncio.Semaphore,
    defer: bool,
):
    if defer:
        yield
    else:
        async with semaphore:
            yield
