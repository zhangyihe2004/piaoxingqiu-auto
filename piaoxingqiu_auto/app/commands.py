"""飞书指令消费、路由与回复。"""

from __future__ import annotations

import asyncio
import shlex
import time

from piaoxingqiu_auto.app.run_config import build_login_config
from piaoxingqiu_auto.config import mask_phone, remove_account_home
from piaoxingqiu_auto.app.binding_flow import BindingSetupFlow
from piaoxingqiu_auto.app.database import Database
from piaoxingqiu_auto.app.scheduler import TaskScheduler
from piaoxingqiu_auto.adapters.feishu_gateway import FeishuGateway, IncomingCommand
from piaoxingqiu_auto.app.login_flow import FeishuLoginManager
from piaoxingqiu_auto.platform.order_guard import PersistentOrderGuard
from piaoxingqiu_auto.domain.models import purchase_unit
from piaoxingqiu_auto.domain.sale import MIN_INTERVAL, sale_phase
from piaoxingqiu_auto.app.tasks import TaskService, real_name_label


SEARCH_TTL = 900
REPLY_RETRY_SECONDS = 10
MAX_PENDING_REPLIES = 100
STATUS_LABELS = {
    "STOPPED": "已停止",
    "READY": "等待抢票",
    "RUNNING": "正在抢票",
    "NEEDS_LOGIN": "登录已失效",
    "CREATED": "订单已创建",
    "UNKNOWN": "订单结果未知",
    "COMPLETE": "目标数量已完成",
}
HELP = """票星球自动抢票

【创建】
搜索 <关键词>
抢票 <搜索序号> [间隔 <秒>]

【查看】
列表
详情 <任务ID>

【账号】
登录
账号
删除账号 <账号ID>

【绑定】
绑定 <任务ID> <账号ID>
启动 <任务ID> <账号ID>
停止 <任务ID> <账号ID>
解绑 <任务ID> <账号ID>

【任务】
间隔 <任务ID> <秒>
暂停 <任务ID>
恢复 <任务ID>
删除 <任务ID>"""


class CommandWorker:
    def __init__(
        self,
        queue: asyncio.Queue[IncomingCommand],
        db: Database,
        service: TaskService,
        scheduler: TaskScheduler,
        login: FeishuLoginManager,
        feishu: FeishuGateway,
    ) -> None:
        self.queue = queue
        self.db = db
        self.service = service
        self.scheduler = scheduler
        self.login = login
        self.feishu = feishu
        self.pending_replies: dict[str, str] = {}
        login.reply = self._reply
        self.configurator = BindingSetupFlow(
            db,
            scheduler.cancel_binding,
            scheduler.official_audiences,
            self._clear_order_state,
            self._reply,
        )
        self.searches: dict[tuple[str, str], tuple[float, list[dict]]] = {}

    async def run_forever(self) -> None:
        maintenance = asyncio.create_task(self._maintain())
        try:
            while True:
                command = await self.queue.get()
                try:
                    response = await self.execute(command)
                except Exception as exc:
                    response = f"执行失败：{exc}"
                try:
                    await self._reply(command.message_id, response)
                finally:
                    self.queue.task_done()
        finally:
            maintenance.cancel()
            await asyncio.gather(maintenance, return_exceptions=True)

    async def _reply(self, message_id: str, text: str) -> None:
        if await self.feishu.reply_text(message_id, text):
            self.pending_replies.pop(message_id, None)
            return
        self._queue_reply(message_id, text)

    def _queue_reply(self, message_id: str, text: str) -> None:
        if len(self.pending_replies) >= MAX_PENDING_REPLIES:
            self.pending_replies.pop(next(iter(self.pending_replies)))
        self.pending_replies[message_id] = text

    async def _maintain(self) -> None:
        while True:
            await asyncio.sleep(REPLY_RETRY_SECONDS)
            await self.configurator.expire()
            cutoff = time.monotonic() - SEARCH_TTL
            self.searches = {
                key: value for key, value in self.searches.items() if value[0] > cutoff
            }
            await self._retry_replies()

    async def _retry_replies(self) -> None:
        for message_id, text in list(self.pending_replies.items()):
            if await self.feishu.reply_text(message_id, text):
                self.pending_replies.pop(message_id, None)

    async def execute(self, command: IncomingCommand) -> str:
        response = await self.login.consume(command)
        if response is not None:
            return response
        response = await self.configurator.consume(command)
        if response is not None:
            return response
        try:
            parts = shlex.split(command.text)
        except ValueError as exc:
            return f"指令格式错误：{exc}"
        if not parts or parts[0] == "帮助":
            return HELP
        name = parts[0]
        if name not in {"搜索", "列表", "详情"} and not command.is_admin:
            return "无操作权限。"
        if name == "搜索":
            return await self._search(command, parts)
        if name == "抢票":
            return await self._create(command, parts)
        if name == "列表":
            return self._tasks()
        if name == "详情":
            return self._detail(parts)
        if name in {"暂停", "恢复", "删除"}:
            return await self._task_action(name, parts)
        if name == "间隔":
            return self._interval(parts)
        if name == "登录":
            return self._login(command, parts)
        if name == "账号":
            return self._accounts()
        if name == "删除账号":
            return await self._delete_account(parts)
        if name == "绑定":
            return await self._bind(command, parts)
        if name == "启动":
            return self._start_binding(parts)
        if name == "停止":
            return await self._stop_binding(parts)
        if name == "解绑":
            return await self._detach(parts)
        return f"无法识别指令：{name}\n发送：帮助"

    @staticmethod
    def _key(command: IncomingCommand) -> tuple[str, str]:
        return command.chat_id, command.sender_open_id

    async def _search(self, command: IncomingCommand, parts: list[str]) -> str:
        if len(parts) < 2:
            return "发送：搜索 <关键词>"
        shows = await self.service.search(" ".join(parts[1:]))
        self.searches[self._key(command)] = (time.monotonic(), shows)
        if not shows:
            return "搜索结果（0）\n请缩短关键词或改用演出名称中的连续文字重试。"
        lines = [f"搜索结果（{len(shows)}）"]
        for index, show in enumerate(shows, 1):
            lines.extend(
                (
                    "",
                    f"{index}. {show.get('showName', '')}",
                    f"时间：{show.get('showDate') or '未知'}",
                    f"地点：{show.get('cityName', '')} {show.get('venueName', '')}".strip(),
                    f"实名：{real_name_label(show.get('_real_name_mode', 'UNKNOWN'))}",
                )
            )
        lines.extend(("", "发送：抢票 <搜索序号>"))
        return "\n".join(lines)

    def _search_show(self, command: IncomingCommand, value: str) -> dict | None:
        if not value.isdigit():
            return None
        cached = self.searches.get(self._key(command))
        if not cached:
            return None
        if time.monotonic() - cached[0] >= SEARCH_TTL:
            self.searches.pop(self._key(command), None)
            return None
        index = int(value)
        if 1 <= index <= len(cached[1]):
            return cached[1][index - 1]
        return None

    async def _create(self, command: IncomingCommand, parts: list[str]) -> str:
        usage = "发送：抢票 <搜索序号> [间隔 <秒>]"
        if len(parts) < 2:
            return usage
        show = self._search_show(command, parts[1])
        if not show:
            return "搜索序号无效或结果已过期，请重新搜索。"
        show_id = str(show.get("showId") or "")
        options = {"场次": "", "间隔": "60"}
        interval_given = False
        index = 2
        while index < len(parts):
            if parts[index] not in options or index + 1 >= len(parts):
                return usage
            if parts[index] == "间隔":
                interval_given = True
            options[parts[index]] = parts[index + 1]
            index += 2
        if not options["间隔"].isdigit():
            return "间隔必须是整数秒。"
        if options["场次"] and not options["场次"].isdigit():
            return "场次序号必须是数字。"
        requested_interval = int(options["间隔"])
        show_name, sessions = await self.service.show_sessions(show_id)
        if not sessions:
            return "该演出当前没有场次。"
        if len(sessions) > 1 and not options["场次"]:
            lines = [f"演出：{show_name}", f"场次（{len(sessions)}）："]
            lines.extend(
                f"{number}. {session.get('sessionName', '')}"
                for number, session in enumerate(sessions, 1)
            )
            followup = f"发送：抢票 {parts[1]} 场次 <序号>"
            if interval_given:
                followup += f" 间隔 {options['间隔']}"
            lines.extend(("", followup))
            return "\n".join(lines)
        session_number = int(options["场次"] or "1")
        if not 1 <= session_number <= len(sessions):
            return f"场次编号必须在 1~{len(sessions)} 之间。"
        interval = max(MIN_INTERVAL, requested_interval)
        session = sessions[session_number - 1]
        plans = await self.service.plans(show_id, session["bizShowSessionId"])
        task_id, created = self.service.create_task(
            show_id=show_id,
            show_name=show_name,
            session=session,
            plans=plans,
            interval=interval,
            real_name_mode=str(show.get("_real_name_mode") or "UNKNOWN"),
        )
        if not created:
            return f"该场次已经是任务 #{task_id}，不会重复创建。\n发送：详情 {task_id}"
        adjusted = (
            f"（输入 {requested_interval} 秒，已按最低值调整）"
            if requested_interval < MIN_INTERVAL
            else ""
        )
        unit = purchase_unit(bool(session.get("supportSeatPicking")))
        return (
            f"已创建任务 #{task_id}\n\n"
            f"演出：{show_name}\n"
            f"场次：{session.get('sessionName', '')}\n"
            f"方式：{'选座' if session.get('supportSeatPicking') else '不选座'}\n"
            f"实名：{real_name_label(show.get('_real_name_mode', 'UNKNOWN'))}\n"
            f"数量：每个账号 1~{int(session['limitation'])} {unit}\n"
            f"间隔：{interval} 秒{adjusted}\n"
            "账号：0\n状态：等待添加账号\n\n"
            f"{self._account_next_step(task_id)}"
        )

    def _tasks(self) -> str:
        tasks = self.db.list_tasks()
        if not tasks:
            return "抢票任务（0）\n下一步：搜索 <关键词>"
        blocks = []
        for task in tasks:
            bindings = self.db.list_bindings(task_id=task["id"])
            enabled = sum(bool(binding["enabled"]) for binding in bindings)
            runnable = sum(
                bool(binding["enabled"])
                and binding["status"] in {"READY", "RUNNING"}
                for binding in bindings
            )
            block = (
                f"任务 #{task['id']}｜{'运行中' if task['status'] == 'active' else '已暂停'}\n"
                f"演出：{task['show_name']}\n"
                f"场次：{task['session_name']}\n"
                f"账号：{len(bindings)}（已启用 {enabled}｜可运行 {runnable}）\n"
                f"间隔：{task['interval_sec']} 秒"
            )
            if not bindings:
                block += f"\n{self._account_next_step(task['id'])}"
            blocks.append(block)
        return f"抢票任务（{len(tasks)}）\n\n" + "\n\n".join(blocks)

    def _detail(self, parts: list[str]) -> str:
        if len(parts) != 2 or not parts[1].isdigit():
            return "发送：详情 <任务ID>"
        task_id = int(parts[1])
        task = self.db.get_task(task_id)
        if not task:
            return f"任务 #{task_id} 不存在。"
        plans = self.db.get_task_plans(task_id)
        bindings = self.db.list_bindings(task_id=task_id)
        phase = sale_phase(task, plans)
        unit = purchase_unit(bool(task["support_seat_picking"]))
        lines = [
            f"任务 #{task_id}",
            f"演出：{task['show_name']}",
            f"场次：{task['session_name']}",
            f"方式：{'选座' if task['support_seat_picking'] else '不选座'}",
            f"实名：{real_name_label(task['real_name_mode'])}",
            f"场次限购：{task['session_limitation']} {unit}",
            f"项目累计限购：{task['show_limit'] or '未提供'}",
            f"状态：{_mode(task, phase)}",
            f"间隔：{task['interval_sec']} 秒",
            "",
            f"票档目录（{len(plans)}）：",
        ]
        for number, plan in enumerate(plans, 1):
            stock = (
                "未开售"
                if phase in {"SCHEDULED", "PREWARM", "WAITING"}
                else (
                    f"最多可买 "
                    f"{plan['can_buy_count'] * plan['unit_qty'] if task['support_seat_picking'] else plan['can_buy_count']} "
                    f"{unit}"
                )
                if plan["sale_started"] and plan["can_buy_count"] > 0
                else "可售但无票"
                if plan["sale_started"]
                else "未开售"
            )
            lines.append(
                f"{number}. {plan['plan_name']}｜¥{plan['price']:g}"
                f"{'｜支持套票优惠' if plan['has_combo'] else ''}｜{stock}"
            )
        lines.extend(("", f"账号（{len(bindings)}）："))
        for binding in bindings:
            account = self.db.get_account(binding["account_id"])
            if not account:
                continue
            account_plans = self.db.get_binding_plans(task_id, account["id"])
            people = self.db.get_binding_audiences(task_id, account["id"])
            lines.extend(
                (
                    f"#{account['id']}｜{mask_phone(account['phone'])}｜"
                    f"{STATUS_LABELS.get(binding['status'], binding['status'])}",
                    "票档："
                    + (
                        " → ".join(plan["plan_name"] for plan in account_plans)
                        if account_plans
                        else "未配置"
                    ),
                    f"数量：{binding['quantity']} {unit}",
                    (
                        "观演人：无需配置"
                        if task["real_name_mode"] == "NONE"
                        else f"观演人（{len(people)}）："
                    ),
                )
            )
            if people:
                lines.extend(
                    f"  {index}. {person['name']}｜{person['masked_id']}"
                    for index, person in enumerate(people, 1)
                )
            if next_step := _binding_next_step(task_id, account["id"], binding):
                lines.append(next_step)
            lines.append("")
        if not bindings:
            lines.append(self._account_next_step(task_id))
        elif lines[-1] == "":
            lines.pop()
        return "\n".join(lines)

    async def _task_action(self, name: str, parts: list[str]) -> str:
        if len(parts) != 2 or not parts[1].isdigit():
            return f"发送：{name} <任务ID>"
        task_id = int(parts[1])
        if name == "删除":
            task = self.db.get_task(task_id)
            if not task:
                return f"任务 #{task_id} 不存在。"
            accounts = [
                account
                for binding in self.db.list_bindings(task_id=task_id)
                if (account := self.db.get_account(binding["account_id"]))
            ]
            state_paths = [
                build_login_config(task, account, self.scheduler.system).state_path
                for account in accounts
            ]
            self.configurator.cancel_task_sessions(task_id)
            await self.scheduler.cancel_task(task_id)
            if not self.db.delete_task(task_id):
                return f"任务 #{task_id} 不存在。"
            for path in state_paths:
                PersistentOrderGuard.clear(path)
            for account in accounts:
                await self.scheduler.release_account_if_idle(int(account["id"]))
            return f"已删除任务 #{task_id}；账号及登录资料已保留。"
        status = "paused" if name == "暂停" else "active"
        if not self.db.set_task_status(task_id, status):
            return f"任务 #{task_id} 不存在。"
        if status == "paused":
            await self.scheduler.cancel_task(task_id)
        else:
            self.scheduler.next_poll[task_id] = 0
        return f"已{name}任务 #{task_id}。"

    def _interval(self, parts: list[str]) -> str:
        if len(parts) != 3 or not parts[1].isdigit():
            return "发送：间隔 <任务ID> <秒>"
        task_id = int(parts[1])
        task = self.db.get_task(task_id)
        if not task:
            return f"任务 #{task_id} 不存在。"
        if not parts[2].isdigit():
            return "间隔必须是整数秒。"
        requested = int(parts[2])
        interval = max(MIN_INTERVAL, requested)
        self.db.set_task_interval(task_id, interval)
        self.scheduler.next_poll[task_id] = 0
        adjusted = "（已按最低值调整）" if requested < MIN_INTERVAL else ""
        return f"任务 #{task_id} 间隔已改为 {interval} 秒{adjusted}。"

    def _login(self, command: IncomingCommand, parts: list[str]) -> str:
        if len(parts) != 1:
            return "发送：登录"
        return self.login.start(command)

    async def _bind(self, command: IncomingCommand, parts: list[str]) -> str:
        ids = _binding_ids(parts, "绑定")
        if isinstance(ids, str):
            return ids
        return await self.configurator.start(command, *ids)

    def _start_binding(self, parts: list[str]) -> str:
        ids = _binding_ids(parts, "启动")
        if isinstance(ids, str):
            return ids
        task_id, account_id = ids
        task = self.db.get_task(task_id)
        account = self.db.get_account(account_id)
        binding = self.db.get_binding(task_id, account_id)
        if not task or not account or not binding:
            return "任务、账号或绑定不存在。"
        if self.configurator.is_configuring(task_id, account_id):
            return "绑定正在配置中；请先发送“完成”或“取消”。"
        if binding["enabled"] and binding["status"] in {"READY", "RUNNING"}:
            return f"任务 #{task_id}｜账号 #{account_id} 已经启动。"
        try:
            self.db.activate_binding(task_id, account_id)
        except ValueError as exc:
            return f"{exc}。"
        PersistentOrderGuard.clear(
            build_login_config(task, account, self.scheduler.system).state_path
        )
        self.scheduler.next_poll[task_id] = 0
        if not self.scheduler.system.create_order_enabled:
            result = "系统当前禁止创建订单，只会继续监控。"
        elif task["status"] == "paused":
            result = f"绑定已启动，但任务已暂停。发送：恢复 {task_id}"
        else:
            result = "正在等待开售或回流；发现目标库存后自动抢票。"
        return f"任务 #{task_id}｜账号 #{account_id} 已启动。\n{result}"

    async def _stop_binding(self, parts: list[str]) -> str:
        ids = _binding_ids(parts, "停止")
        if isinstance(ids, str):
            return ids
        task_id, account_id = ids
        self.configurator.cancel_binding_session(task_id, account_id)
        await self.scheduler.cancel_binding(task_id, account_id)
        if not self.db.deactivate_binding(task_id, account_id):
            return "绑定不存在。"
        await self.scheduler.release_account_if_idle(account_id)
        return f"任务 #{task_id}｜账号 #{account_id} 已停止；配置和登录资料已保留。"

    async def _detach(self, parts: list[str]) -> str:
        ids = _binding_ids(parts, "解绑")
        if isinstance(ids, str):
            return ids
        task_id, account_id = ids
        task = self.db.get_task(task_id)
        account = self.db.get_account(account_id)
        if not task or not account:
            return "任务或账号不存在。"
        self.configurator.cancel_binding_session(task_id, account_id)
        await self.scheduler.cancel_binding(task_id, account_id)
        if not self.db.delete_binding(task_id, account_id):
            return "绑定不存在。"
        PersistentOrderGuard.clear(
            build_login_config(task, account, self.scheduler.system).state_path
        )
        await self.scheduler.release_account_if_idle(account_id)
        return f"任务 #{task_id} 已解绑账号 #{account_id}；账号和登录资料已保留。"

    def _accounts(self) -> str:
        accounts = self.db.list_accounts()
        if not accounts:
            return "账号（0）\n下一步：登录"
        blocks: list[str] = []
        for account in accounts:
            bindings = self.db.list_bindings(account_id=account["id"])
            lines = [
                f"账号 #{account['id']}｜{mask_phone(account['phone'])}｜"
                f"{'登录有效' if account['status'] == 'READY' else '需要登录'}"
            ]
            if bindings:
                lines.extend(
                    f"任务 #{binding['task_id']}｜"
                    f"{STATUS_LABELS.get(binding['status'], binding['status'])}"
                    for binding in bindings
                )
            else:
                lines.append("尚未绑定任务")
            blocks.append("\n".join(lines))
        return f"账号（{len(accounts)}）\n\n" + "\n\n".join(blocks)

    async def _delete_account(self, parts: list[str]) -> str:
        if len(parts) != 2 or not parts[1].isdigit():
            return "发送：删除账号 <账号ID>"
        account_id = int(parts[1])
        account = self.db.get_account(account_id)
        if not account:
            return f"账号 #{account_id} 不存在。"
        self.db.set_account_status(account_id, "NEEDS_LOGIN")
        self.configurator.cancel_account_sessions(account_id)
        await self.scheduler.cancel_account(account_id)
        await self.login.cancel_account(account_id)
        await asyncio.to_thread(remove_account_home, str(account["profile_key"]))
        if not self.db.delete_account(account_id):
            raise RuntimeError(f"账号 #{account_id} 本地资料已删除，但数据库删除失败")
        return f"账号 #{account_id} 及其全部绑定和本地登录资料已删除。"

    def _clear_order_state(self, task_id: int, account_id: int) -> None:
        task = self.db.get_task(task_id)
        account = self.db.get_account(account_id)
        if task and account:
            PersistentOrderGuard.clear(
                build_login_config(task, account, self.scheduler.system).state_path
            )

    def _account_next_step(self, task_id: int) -> str:
        ready_accounts = [
            account
            for account in self.db.list_accounts()
            if account["status"] == "READY"
        ]
        if not ready_accounts:
            return "下一步：登录"
        return f"下一步：绑定 {task_id} <账号ID>；发送“账号”查看编号"


def _binding_ids(parts: list[str], command: str) -> tuple[int, int] | str:
    if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
        return f"发送：{command} <任务ID> <账号ID>"
    return int(parts[1]), int(parts[2])


def _binding_next_step(task_id: int, account_id: int, binding) -> str:
    status = binding["status"]
    if status == "CREATED":
        return (
            "下一步：在票星球处理待支付订单；"
            f"如已取消并需继续，发送：启动 {task_id} {account_id}"
        )
    if status == "UNKNOWN":
        return (
            "下一步：检查票星球待支付订单；确认无订单后发送："
            f"启动 {task_id} {account_id}"
        )
    if status == "COMPLETE":
        return f"下一步：重新配置观演人，发送：绑定 {task_id} {account_id}"
    if status == "NEEDS_LOGIN":
        return "下一步：登录"
    if not binding["enabled"]:
        return f"下一步：启动 {task_id} {account_id}"
    return ""


def _mode(task, phase: str) -> str:
    if task["status"] != "active":
        return "已暂停"
    return {
        "SCHEDULED": "等待开售",
        "PREWARM": "预抢票准备",
        "AVAILABLE": "当前有票",
        "RESTOCK": "等待回流",
        "WAITING": "等待开售",
    }[phase]
