"""创建订单响应监听、解析和失败分类。"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from playwright.async_api import Response

from piaoxingqiu_auto.platform.order_guard import CART_CREATE_PATH, is_create_url


CREATE_FAILURE_ACTIONS = {
    "22035010": "RESELECT",
    "23502000": "RESELECT",
    "22039998": "REBUILD",
    "22031901": "REBUILD",
    "27902319": "REMOVE_AUDIENCE",
    "28217767": "STOP_BINDING",
    "33000000": "STOP_BINDING",
}
SUCCESS_CODES = {"0", "200", "200000"}


@dataclass(frozen=True)
class CreateResult:
    success: bool
    order_id: str | None
    order_number: str | None
    payment_deadline_ms: int | None
    unpaid_transaction_count: int
    http_status: int
    code: str | None
    sub_code: str | None
    message: str | None


class CreateResponseWatcher:
    def __init__(self) -> None:
        self._responses: asyncio.Queue[Response] = asyncio.Queue()

    def handle(self, response: Response) -> None:
        if response.request.method.upper() != "POST":
            return
        if not is_create_url(response.url):
            return
        self._responses.put_nowait(response)

    async def wait(self, timeout_seconds: float) -> CreateResult:
        response = await asyncio.wait_for(
            self._responses.get(), timeout=timeout_seconds
        )
        try:
            payload = await response.json()
        except Exception:
            payload = None
        order_id = order_number = None
        payment_deadline_ms = None
        unpaid_transaction_count = 0
        if urlsplit(response.url).path.lower() == CART_CREATE_PATH:
            (
                order_id,
                order_number,
                payment_deadline_ms,
                unpaid_transaction_count,
            ) = _cart_create_details(payload)
        code = _find_scalar(payload, ("code", "statusCode", "errorCode"))
        message = (
            redact_preview(
                _find_scalar(
                    payload,
                    ("message", "msg", "errorMessage", "errorMsg", "comments", "desc"),
                )
                or "",
                limit=300,
            )
            or None
        )
        return CreateResult(
            success=response.ok
            and _response_success(payload, order_id, order_number, message),
            order_id=order_id,
            order_number=order_number,
            payment_deadline_ms=payment_deadline_ms,
            unpaid_transaction_count=unpaid_transaction_count,
            http_status=response.status,
            code=code,
            sub_code=_find_scalar(payload, ("subCode", "sub_code", "bizCode")),
            message=message,
        )


def redact_preview(value: str, limit: int = 1200) -> str:
    preview = re.sub(r"\s+", " ", value).strip()[:limit]
    preview = re.sub(r"(?<!\d)1\d{10}(?!\d)", "1**********", preview)
    return re.sub(
        r"(?<![0-9Xx])\d{6}(?:19|20)\d{2}\d{2}\d{2}\d{3}[0-9Xx](?![0-9Xx])",
        "******************",
        preview,
    )


def find_already_purchased_ids(value: str) -> tuple[str, ...]:
    normalized = re.sub(r"\s+", "", value or "")
    if "已购买过" not in normalized or "请更换其他实名信息" not in normalized:
        return ()
    return tuple(
        dict.fromkeys(re.findall(r"(?<!\d)\d{3,6}\*+[0-9Xx]{4}(?![0-9Xx])", normalized))
    )


def create_failure_action(result: CreateResult) -> str:
    codes = tuple(filter(None, (result.code, result.sub_code)))
    for code in codes:
        if action := CREATE_FAILURE_ACTIONS.get(code):
            return action
    if not 200 <= result.http_status < 300 or any(
        code not in SUCCESS_CODES for code in codes
    ):
        return "FAILED"
    return "UNKNOWN"


def match_configured_ids(
    reported_ids: tuple[str, ...], configured_ids: tuple[str, ...]
) -> tuple[str, ...]:
    matched = []
    for configured in configured_ids:
        prefix = configured[:3]
        suffix = configured[-4:]
        if any(
            item.startswith(prefix) and item.endswith(suffix) for item in reported_ids
        ):
            matched.append(configured)
    return tuple(matched)


def _response_success(
    payload: object,
    order_id: str | None,
    order_number: str | None,
    message: str | None,
) -> bool:
    if not isinstance(payload, dict):
        return False
    code = payload.get("code", payload.get("statusCode"))
    if code is not None and str(code) not in SUCCESS_CODES:
        return False
    return (
        payload.get("success") is True
        or order_id is not None
        or order_number is not None
        or message == "成功"
    )


def _find_scalar(payload: object, keys: tuple[str, ...]) -> str | None:
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, (str, int, float, bool)):
                return str(value)
        for value in payload.values():
            found = _find_scalar(value, keys)
            if found is not None:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = _find_scalar(value, keys)
            if found is not None:
                return found
    return None


def _cart_create_details(
    payload: object,
) -> tuple[str | None, str | None, int | None, int]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        return None, None, None, 0
    data = payload["data"]
    orders = data.get("orders")
    order = (
        next((item for item in orders if isinstance(item, dict)), {})
        if isinstance(orders, list)
        else {}
    )
    transactions = data.get("unPaidTransactionIds")
    return (
        _string_value(order.get("orderId")),
        _string_value(order.get("orderNumber")),
        _positive_int(data.get("paidDeadLineTime")),
        len(transactions) if isinstance(transactions, list) else 0,
    )


def _string_value(value: object) -> str | None:
    if not isinstance(value, (str, int)):
        return None
    result = str(value)
    return result or None


def _positive_int(value: object) -> int | None:
    if not isinstance(value, (str, int)):
        return None
    try:
        result = int(value)
    except (ValueError, OverflowError):
        return None
    return result if result > 0 else None
