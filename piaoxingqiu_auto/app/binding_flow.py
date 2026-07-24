"""任务与账号绑定的分步配置流程。"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from piaoxingqiu_auto.platform.auth import OfficialAudience
from piaoxingqiu_auto.domain.models import required_audience_count
from piaoxingqiu_auto.app.database import Database
from piaoxingqiu_auto.adapters.feishu_gateway import IncomingCommand
from piaoxingqiu_auto.app.tasks import parse_numbers, real_name_label


SESSION_TTL = 1800
BindingKey = tuple[int, int]


@dataclass
class BindingSetupSession:
    owner: str
    task_id: int
    account_id: int
    plan_ids: list[str]
    quantity: int
    people: list[tuple[str, str]]
    last_message_id: str
    phase: str = "PLANS"
    official_people: tuple[OfficialAudience, ...] = field(default_factory=tuple)
    touched_at: float = 0.0

    @property
    def key(self) -> BindingKey:
        return self.task_id, self.account_id


class BindingSetupFlow:
    """在内存中编辑绑定；只有“完成”才一次性写入数据库。"""

    def __init__(
        self,
        db: Database,
        cancel_binding: Callable[[int, int], Awaitable[None]],
        load_audiences: Callable[
            [int, int], Awaitable[tuple[OfficialAudience, ...]]
        ],
        clear_order_state: Callable[[int, int], None],
        reply: Callable[[str, str], Awaitable[None]],
    ) -> None:
        self.db = db
        self.cancel_binding = cancel_binding
        self.load_audiences = load_audiences
        self.clear_order_state = clear_order_state
        self.reply = reply
        self.sessions: dict[str, BindingSetupSession] = {}
        self.binding_owners: dict[BindingKey, str] = {}

    async def start(
        self, command: IncomingCommand, task_id: int, account_id: int
    ) -> str:
        await self.expire()
        owner = command.sender_open_id
        key = (task_id, account_id)
        if owner in self.sessions:
            return "你已有绑定流程进行中；发送“取消”后再操作。"
        task = self.db.get_task(task_id)
        account = self.db.get_account(account_id)
        if not task:
            return f"任务 #{task_id} 不存在。"
        if not account:
            return f"账号 #{account_id} 不存在。"
        if account["status"] in {"RESERVED", "NEEDS_LOGIN"}:
            return f"账号 #{account_id} 尚未登录。\n下一步：登录"
        if key in self.binding_owners:
            return f"任务 #{task_id} 与账号 #{account_id} 正在另一处配置。"
        binding = self.db.get_binding(task_id, account_id)
        if binding and binding["status"] == "RUNNING":
            return "该绑定正在创建订单，请等待本次结果。"
        if not self.db.begin_binding_configuration(task_id, account_id):
            return "该绑定刚刚进入抢票流程，请等待本次结果。"
        await self.cancel_binding(task_id, account_id)
        session = BindingSetupSession(
            owner=owner,
            task_id=task_id,
            account_id=account_id,
            plan_ids=[
                row["seat_plan_id"]
                for row in self.db.get_binding_plans(task_id, account_id)
            ],
            quantity=int(binding["quantity"]) if binding else 1,
            people=[
                (row["name"], row["masked_id"])
                for row in self.db.get_binding_audiences(task_id, account_id)
            ],
            last_message_id=command.message_id,
            touched_at=time.monotonic(),
        )
        self.sessions[owner] = session
        self.binding_owners[key] = owner
        return self._plans_prompt(session)

    async def consume(self, command: IncomingCommand) -> str | None:
        owner = command.sender_open_id
        session = self.sessions.get(owner)
        if session and time.monotonic() - session.touched_at >= SESSION_TTL:
            self._drop(session)
            return "绑定流程 30 分钟未操作，已取消；原配置不变。"
        await self.expire()
        session = self.sessions.get(owner)
        if session is None:
            return None
        session.last_message_id = command.message_id
        session.touched_at = time.monotonic()
        text = command.text.strip()
        if text == "取消":
            self._drop(session)
            return "绑定配置已取消；原配置不变，绑定不会自动启动。"
        if session.phase == "PLANS":
            return self._consume_plans(session, text)
        if session.phase == "QUANTITY":
            return await self._consume_quantity(session, text)
        if session.phase == "PEOPLE":
            return self._consume_people(session, text)
        if text == "完成":
            return self._finish(session)
        return self._confirm_prompt(session, "发送“完成”保存，或发送“取消”退出。")

    def cancel_binding_session(self, task_id: int, account_id: int) -> None:
        owner = self.binding_owners.get((task_id, account_id))
        if owner and (session := self.sessions.get(owner)):
            self._drop(session)

    def cancel_task_sessions(self, task_id: int) -> None:
        for session in list(self.sessions.values()):
            if session.task_id == task_id:
                self._drop(session)

    def cancel_account_sessions(self, account_id: int) -> None:
        for session in list(self.sessions.values()):
            if session.account_id == account_id:
                self._drop(session)

    def is_configuring(self, task_id: int, account_id: int) -> bool:
        return (task_id, account_id) in self.binding_owners

    def _consume_plans(self, session: BindingSetupSession, text: str) -> str:
        plans = self._task_plans(session)
        if text == "保留":
            if not session.plan_ids:
                return self._plans_prompt(session, "当前没有已选票档。")
        else:
            try:
                numbers = parse_numbers(text, len(plans))
            except ValueError as exc:
                return self._plans_prompt(session, str(exc))
            session.plan_ids = [plans[number - 1]["seat_plan_id"] for number in numbers]
        session.phase = "QUANTITY"
        return self._quantity_prompt(session)

    async def _consume_quantity(
        self, session: BindingSetupSession, text: str
    ) -> str:
        task = self._task(session)
        maximum = max(1, int(task["session_limitation"]))
        if not text.isdigit() or not 1 <= int(text) <= maximum:
            return self._quantity_prompt(
                session, f"购票数量必须是 1~{maximum} 的整数。"
            )
        session.quantity = int(text)
        required = self._required_people(session)
        if required == 0:
            session.people.clear()
            session.phase = "CONFIRM"
            return self._confirm_prompt(session)
        try:
            session.official_people = await self.load_audiences(
                session.task_id, session.account_id
            )
        except Exception as exc:
            self._drop(session)
            return (
                f"读取账号 #{session.account_id} 的官方观演人失败：{exc}\n"
                "原配置未修改；绑定不会自动启动。"
            )
        if len(session.official_people) < required:
            self._drop(session)
            return (
                f"票星球账号只有 {len(session.official_people)} 位可用观演人，"
                f"当前需要 {required} 位。\n"
                "请先在票星球添加或修正观演人，再重新发送绑定指令。"
                "\n原配置未修改；绑定不会自动启动。"
            )
        if len(session.official_people) == required:
            session.people = [
                (person.name, person.masked_id) for person in session.official_people
            ]
            session.phase = "CONFIRM"
            return self._confirm_prompt(session)
        session.phase = "PEOPLE"
        return self._people_prompt(session)

    def _consume_people(self, session: BindingSetupSession, text: str) -> str:
        required = self._required_people(session)
        try:
            numbers = parse_numbers(text, len(session.official_people), "观演人")
        except ValueError as exc:
            return self._people_prompt(session, str(exc))
        if len(numbers) != required:
            return self._people_prompt(session, f"请选择恰好 {required} 位观演人。")
        session.people = [
            (
                session.official_people[number - 1].name,
                session.official_people[number - 1].masked_id,
            )
            for number in numbers
        ]
        session.phase = "CONFIRM"
        return self._confirm_prompt(session)

    def _finish(self, session: BindingSetupSession) -> str:
        try:
            self.db.save_binding_config(
                session.task_id,
                session.account_id,
                session.plan_ids,
                session.quantity,
                session.people,
            )
            self.clear_order_state(session.task_id, session.account_id)
        except ValueError as exc:
            return self._confirm_prompt(session, f"无法保存：{exc}")
        summary = self._summary(session)
        self._drop(session)
        return (
            f"任务 #{session.task_id} 已绑定账号 #{session.account_id}。\n\n"
            f"{summary}\n\n状态：已停止\n"
            f"下一步：启动 {session.task_id} {session.account_id}"
        )

    def _plans_prompt(
        self, session: BindingSetupSession, notice: str = ""
    ) -> str:
        plans = self._task_plans(session)
        selected = {plan_id: index for index, plan_id in enumerate(session.plan_ids, 1)}
        lines = [
            f"绑定任务 #{session.task_id}｜账号 #{session.account_id}（1/3）",
            "选择票档",
        ]
        if notice:
            lines.extend(("", notice))
        for number, plan in enumerate(plans, 1):
            priority = selected.get(plan["seat_plan_id"])
            lines.append(
                f"{number}. {plan['plan_name']}｜¥{plan['price']:g}"
                f"{'｜支持套票优惠' if plan['has_combo'] else ''}"
                f"{f'｜✓ {priority}' if priority else ''}"
            )
        lines.extend(("", "发送：1,3,2（顺序即优先级）"))
        if session.plan_ids:
            lines.append("保留原选择：保留")
        lines.append("退出：取消")
        return "\n".join(lines)

    def _quantity_prompt(
        self, session: BindingSetupSession, notice: str = ""
    ) -> str:
        task = self._task(session)
        maximum = max(1, int(task["session_limitation"]))
        lines = [
            f"绑定任务 #{session.task_id}｜账号 #{session.account_id}（2/3）",
            "设置数量",
        ]
        if notice:
            lines.extend(("", notice))
        lines.extend(
            (
                "",
                f"场次限购：最多 {maximum} 张",
                f"实名规则：{real_name_label(task['real_name_mode'])}",
                f"发送：1~{maximum}",
                "退出：取消",
            )
        )
        return "\n".join(lines)

    def _people_prompt(
        self, session: BindingSetupSession, notice: str = ""
    ) -> str:
        required = self._required_people(session)
        lines = [
            f"绑定任务 #{session.task_id}｜账号 #{session.account_id}（3/3）",
            f"选择官方观演人（需要 {required} 位）",
        ]
        if notice:
            lines.extend(("", notice))
        lines.extend(
            f"{index}. {person.name}｜{person.masked_id}"
            for index, person in enumerate(session.official_people, 1)
        )
        lines.extend(("", f"发送：{','.join(str(i) for i in range(1, required + 1))}", "退出：取消"))
        return "\n".join(lines)

    def _confirm_prompt(
        self, session: BindingSetupSession, notice: str = ""
    ) -> str:
        lines = [
            f"绑定任务 #{session.task_id}｜账号 #{session.account_id}：确认",
        ]
        if notice:
            lines.extend(("", notice))
        lines.extend(("", self._summary(session), "", "保存：完成", "退出：取消"))
        return "\n".join(lines)

    def _summary(self, session: BindingSetupSession) -> str:
        plans = {
            row["seat_plan_id"]: row["plan_name"] for row in self._task_plans(session)
        }
        task = self._task(session)
        lines = [
            f"票档：{' → '.join(plans[item] for item in session.plan_ids)}",
            f"数量：{session.quantity} 张",
            f"实名：{real_name_label(task['real_name_mode'])}",
        ]
        if session.people:
            lines.append(f"观演人（{len(session.people)}）：")
            lines.extend(
                f"{index}. {name}｜{masked_id}"
                for index, (name, masked_id) in enumerate(session.people, 1)
            )
        return "\n".join(lines)

    def _task_plans(self, session: BindingSetupSession):
        task = self.db.get_task(session.task_id)
        if not task:
            raise ValueError(f"任务 #{session.task_id} 已被删除")
        return self.db.get_task_plans(session.task_id)

    def _task(self, session: BindingSetupSession):
        task = self.db.get_task(session.task_id)
        if not task:
            raise ValueError(f"任务 #{session.task_id} 已被删除")
        return task

    def _required_people(self, session: BindingSetupSession) -> int:
        return required_audience_count(
            self._task(session)["real_name_mode"], session.quantity
        )

    def _drop(self, session: BindingSetupSession) -> None:
        self.sessions.pop(session.owner, None)
        self.binding_owners.pop(session.key, None)

    async def expire(self) -> None:
        now = time.monotonic()
        for session in list(self.sessions.values()):
            if now - session.touched_at >= SESSION_TTL:
                self._drop(session)
                await self.reply(
                    session.last_message_id,
                    "绑定流程 30 分钟未操作，已取消；原配置不变。",
                )
