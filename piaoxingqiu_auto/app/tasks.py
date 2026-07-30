"""抢票任务创建、刷新与展示数据处理。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import Any

from piaoxingqiu_auto.platform.api import PxqClient, PxqError
from piaoxingqiu_auto.platform.inventory import (
    StaticInventoryUnavailable,
    available_stand_names,
)
from piaoxingqiu_auto.app.database import Database
from piaoxingqiu_auto.domain.sale import (
    MIN_INTERVAL,
    MISSING_SESSION_STATUS,
    OPEN_SESSION_STATUSES,
    sale_time,
)


REAL_NAME_KEY = "REAL_NAME_PURCHASE_TICKET_INTRODUCTION"
REAL_NAME_LABELS = {
    "NONE": "无需实名",
    "PER_ORDER": "一单一证",
    "PER_TICKET": "一票一证",
}


class SessionUnavailable(RuntimeError):
    pass


def parse_numbers(value: str, upper: int, label: str = "票档") -> list[int]:
    try:
        numbers = list(
            dict.fromkeys(
                int(item.strip())
                for item in value.replace("，", ",").split(",")
                if item.strip()
            )
        )
    except ValueError as exc:
        raise ValueError(f"{label}编号必须用逗号分隔，如 1,3") from exc
    if not numbers or any(number < 1 or number > upper for number in numbers):
        raise ValueError(f"{label}编号必须在 1~{upper} 之间")
    return numbers


def parse_real_name_mode(payload: dict[str, Any]) -> str:
    instructions = payload.get("descInfo", {}).get("ticketInstructions", [])
    item = next(
        (
            entry
            for entry in instructions
            if isinstance(entry, dict) and entry.get("key") == REAL_NAME_KEY
        ),
        None,
    )
    value = str(item.get("value") or "") if item else ""
    if "无需实名" in value:
        return "NONE"
    if "一单一证" in value:
        return "PER_ORDER"
    if "一票一证" in value:
        return "PER_TICKET"
    if any(
        isinstance(entry, dict) and entry.get("code") == "REAL_NAME_TICKET"
        for entry in payload.get("noteInfos", [])
    ):
        return "PER_TICKET"
    return "NONE"


def logical_plans(payload: dict[str, Any]) -> list[dict]:
    raw = payload.get("seatPlans")
    if not isinstance(raw, list):
        raise ValueError("票档接口未返回 seatPlans")
    combo_bases = {
        str(component.get("bizSeatPlanId") or "")
        for item in raw
        if isinstance(item, dict)
        and item.get("seatPlanCategory") == "FREE_COMBO"
        for component in item.get("items", [])
        if isinstance(component, dict) and component.get("bizSeatPlanId")
    }
    return [
        {
            "pid": item["seatPlanId"],
            "plan_name": str(item.get("seatPlanName") or item["seatPlanId"]),
            "price": float(item.get("originalPrice") or 0),
            "can_buy_count": int(item.get("canBuyCount") or 0),
            "limitation": int(item.get("limitation") or 0),
            "unit_qty": max(1, int(item.get("unitQty") or 1)),
            "has_combo": str(item["seatPlanId"]) in combo_bases,
            "sale_started": bool(item.get("saleStarted")),
        }
        for item in raw
        if isinstance(item, dict)
        and item.get("seatPlanId")
        and item.get("seatPlanCategory") != "FREE_COMBO"
    ]


class TaskService:
    def __init__(self, db: Database, client: PxqClient):
        self.db = db
        self.client = client

    async def search(self, keyword: str) -> list[dict]:
        shows = await self.client.search_shows(keyword)
        modes = await asyncio.gather(
            *(
                self.real_name_mode(str(show.get("showId") or ""))
                for show in shows
            ),
            return_exceptions=True,
        )
        for show, mode in zip(shows, modes):
            show["_real_name_mode"] = mode if isinstance(mode, str) else "NONE"
        return shows

    async def real_name_mode(self, show_id: str) -> str:
        if not show_id:
            return "NONE"
        return parse_real_name_mode(await self.client.show_static(show_id))

    async def show_sessions(self, show_id: str) -> tuple[str, list[dict]]:
        sessions = await self.client.quick_order_sessions(show_id)
        show_name = str(sessions[0].get("showName") or show_id) if sessions else show_id
        return show_name, sessions

    async def plans(self, show_id: str, session_id: str) -> list[dict]:
        return logical_plans(await self.client.quick_order_plans(show_id, session_id))

    async def stands(
        self, task_id: int, plan_ids: list[str]
    ) -> list[str] | None:
        task = self.db.get_task(task_id)
        if not task or not task["support_seat_picking"]:
            return []
        try:
            data = await self.client.seating_static(
                task["show_id"], task["session_id"]
            )
            return available_stand_names(data, tuple(plan_ids))
        except PxqError as exc:
            if exc.status_code == 22024036:
                return None
            raise
        except StaticInventoryUnavailable:
            return None

    def create_task(
        self,
        *,
        show_id: str,
        show_name: str,
        session: dict,
        plans: list[dict],
        interval: int,
        real_name_mode: str,
    ) -> tuple[int, bool]:
        session_id = str(session["bizShowSessionId"])
        if not plans:
            raise ValueError("该场次当前没有票档")
        session_limitation = int(session.get("limitation") or 0)
        if session_limitation < 1:
            raise ValueError("官方未返回有效的场次限购数量")
        task_id, created = self.db.create_task(
            show_id=show_id,
            show_name=show_name,
            session_id=session_id,
            session_name=str(session.get("sessionName") or session_id),
            support_seat_picking=bool(session.get("supportSeatPicking")),
            show_limit=int(session.get("showLimit") or 0),
            session_limitation=session_limitation,
            real_name_mode=real_name_mode,
            interval_sec=max(MIN_INTERVAL, interval),
            session_status=str(
                session.get("sessionStatus") or MISSING_SESSION_STATUS
            ).upper(),
            sale_time_ms=sale_time(session),
            plans=plans,
        )
        return task_id, created
    async def refresh_task(
        self,
        task,
        sessions_task: Awaitable[list[dict]] | None = None,
    ) -> tuple[str, int | None, list[tuple[str, int, bool]] | None]:
        show_id, session_id = task["show_id"], task["session_id"]
        if sessions_task is not None:
            sessions = await sessions_task
            session = next(
                (
                    item
                    for item in sessions
                    if item.get("bizShowSessionId") == session_id
                ),
                None,
            )
            if session is None:
                raise SessionUnavailable("目标场次已从公开接口移除")
            session_status = str(
                session.get("sessionStatus") or MISSING_SESSION_STATUS
            ).upper()
            sale_time_ms = sale_time(session)
        else:
            session_status = str(task["session_status"]).upper()
            sale_time_ms = task["sale_time_ms"]
        snapshot = None
        if session_status in OPEN_SESSION_STATUSES:
            plans = await self.client.quick_order_plans(show_id, session_id)
            snapshot = [
                (
                    str(item["seatPlanId"]),
                    int(item.get("canBuyCount") or 0),
                    bool(item.get("saleStarted")),
                )
                for item in plans["seatPlans"]
            ]
        return (
            session_status,
            sale_time_ms,
            snapshot,
        )


def real_name_label(mode: str) -> str:
    return REAL_NAME_LABELS.get(mode, REAL_NAME_LABELS["NONE"])


def quantity_label(support_seat_picking: bool) -> tuple[str, str]:
    return ("人数", "人") if support_seat_picking else ("数量", "张")
