"""官方场次状态、开售时间和轮询节奏。"""

from __future__ import annotations

import time
from typing import Any


MIN_INTERVAL = 10
PREWARM_SECONDS = 60
POST_SALE_WAIT_SECONDS = 60
INTENSIVE_SECONDS = 5
INTENSIVE_POLL_SECONDS = 0.25
WARM_POLL_SECONDS = 1.0
MISSING_SESSION_STATUS = "MISSING"
OPEN_SESSION_STATUSES = {"ON_SALE", "PRE_SALE"}
ACTIVE_SESSION_STATUSES = OPEN_SESSION_STATUSES | {
    "LACK_OF_TICKET",
    "PENDING",
}


def sale_phase(task, plans, now_ms: int | None = None) -> str:
    """Return the internal phase derived from the official sessionStatus."""
    status = str(task["session_status"] or "").upper()
    if status in OPEN_SESSION_STATUSES:
        return (
            "AVAILABLE"
            if any(plan["sale_started"] and plan["can_buy_count"] > 0 for plan in plans)
            else "RESTOCK"
        )
    if status == "LACK_OF_TICKET":
        return "RESTOCK"
    if status == "PENDING" and task["sale_time_ms"] is not None:
        current_ms = int(time.time() * 1000) if now_ms is None else now_ms
        remaining = (task["sale_time_ms"] - current_ms) / 1000
        if remaining > PREWARM_SECONDS:
            return "SCHEDULED"
        return "PREWARM" if remaining >= -POST_SALE_WAIT_SECONDS else "RESTOCK"
    return "WAITING"


def presale_poll_interval(remaining_seconds: float) -> float:
    return (
        INTENSIVE_POLL_SECONDS
        if abs(remaining_seconds) <= INTENSIVE_SECONDS
        else WARM_POLL_SECONDS
    )


def sale_time(value: Any) -> int | None:
    candidates = {
        int(item)
        for item in _walk_sale_times(value)
        if isinstance(item, (int, float)) and item > 1_000_000_000_000
    }
    return candidates.pop() if len(candidates) == 1 else None


def session_sale_time(value: Any, session_id: str) -> int | None:
    matches = [
        item
        for item in _walk_dicts(value)
        if str(item.get("bizShowSessionId") or "") == session_id
    ]
    return sale_time(matches)


def find_session(value: Any, session_id: str) -> dict[str, Any] | None:
    return next(
        (
            item
            for item in _walk_dicts(value)
            if str(item.get("bizShowSessionId") or "") == session_id
        ),
        None,
    )


def _walk_sale_times(value: Any):
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {
                "sessionSaleTime",
                "saleTime",
                "saleStartTime",
                "startSaleTime",
            }:
                yield item
            yield from _walk_sale_times(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_sale_times(item)


def _walk_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _walk_dicts(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_dicts(item)
