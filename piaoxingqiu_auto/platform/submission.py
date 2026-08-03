"""订单提交租约、网络保护、响应解析与风险状态机。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from playwright.async_api import Page, Request, Response, Route
else:
    Page = Request = Response = Route = Any


GuardStatus = Literal["READY", "SUBMITTING", "CREATED", "UNKNOWN"]
RiskState = Literal["QUEUEING", "VERIFY_REQUIRED", "RATE_LIMITED"]
RiskStage = Literal["PRE_ORDER", "CREATE_ORDER"]
FailureAction = Literal[
    "RESELECT",
    "REBUILD",
    "REPRICE",
    "RETRY",
    "REMOVE_AUDIENCE",
    "NEEDS_LOGIN",
    "STOP_BINDING",
    "FAILED",
    "UNKNOWN",
]

CART_CREATE_PATH = "/cyy_gatewayapi/trade/buyer/order/cart/v1/create_order"
CREATE_ROUTE = "**/trade/buyer/order/cart/v1/create_order*"
QUEUE_CODE = "33007201"
VERIFY_CODE = "33007202"
TOKEN_EXPIRED_CODE = "15012010"
LIMITING_MODE = "limiting"
LIMITING_INTERVAL_SECONDS = 3
MAX_LIMITING_RETRIES = 3
MAX_LEASE_REQUESTS = 6
RISK_WAIT_SECONDS = 300
SUCCESS_CODES = {"0", "200", "200000"}

MODE_NAMES = {
    "0": "none",
    "1": "toast",
    "2": "alert",
    "3": "login",
    "4": "back",
    "5": "waiting",
    "8": "back_refresh",
    "9": LIMITING_MODE,
    "10": "retry",
    "11": "booking",
}

CREATE_FAILURE_ACTIONS: dict[str, FailureAction] = {
    "22035010": "RESELECT",
    "23502000": "RESELECT",
    "22039998": "REBUILD",
    "22031901": "REBUILD",
    "27902319": "REMOVE_AUDIENCE",
    "27902332": "STOP_BINDING",
    "28217767": "STOP_BINDING",
    "12501099": "REPRICE",
    "22037056": "REPRICE",
    "22037057": "REPRICE",
    "32713071": "REPRICE",
    "10002008": "NEEDS_LOGIN",
    "15012002": "NEEDS_LOGIN",
    "15012007": "NEEDS_LOGIN",
    "15012008": "NEEDS_LOGIN",
    "15012009": "NEEDS_LOGIN",
    "15012010": "NEEDS_LOGIN",
}

MODE_FAILURE_ACTIONS: dict[str, FailureAction] = {
    "toast": "FAILED",
    "alert": "FAILED",
    "login": "NEEDS_LOGIN",
    "back": "REBUILD",
    "waiting": "FAILED",
    "back_refresh": "REBUILD",
    "retry": "FAILED",
    "booking": "REBUILD",
}


def is_create_url(url: str) -> bool:
    return urlsplit(url).path.lower() == CART_CREATE_PATH


@dataclass(frozen=True)
class OrderState:
    plan_key: str
    status: GuardStatus
    updated_at: str
    order_id: str | None = None


class PersistentOrderGuard:
    def __init__(self, path: Path, plan_key: str) -> None:
        self.path = path
        self.plan_key = plan_key

    def current(self) -> OrderState:
        try:
            state = self.load(self.path)
            if state is None:
                return self._new("READY")
            if state.plan_key != self.plan_key:
                if state.status != "READY":
                    raise RuntimeError(
                        "状态文件属于其他绑定且仍有订单保护，请先人工核对"
                    )
                return self._new("READY")
            return state
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise RuntimeError(f"下单状态文件损坏，请人工检查：{self.path}") from exc

    @staticmethod
    def load(path: Path) -> OrderState | None:
        if not path.exists():
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
        status = raw["status"]
        if status not in {"READY", "SUBMITTING", "CREATED", "UNKNOWN"}:
            raise ValueError(f"未知状态：{status}")
        return OrderState(
            plan_key=str(raw["plan_key"]),
            status=status,
            updated_at=str(raw["updated_at"]),
            order_id=raw.get("order_id"),
        )

    def require_ready(self) -> None:
        state = self.current()
        if state.status != "READY":
            raise RuntimeError(
                f"当前计划状态为 {state.status}，为避免重复订单已禁止再次提交。"
                f"请先在票星球“待支付”订单中人工核对。状态文件：{self.path}"
            )

    def submitting(self) -> None:
        self._write(self._new("SUBMITTING"))

    def ready(self) -> None:
        self._write(self._new("READY"))

    def created(self, order_id: str | None = None) -> None:
        self._write(self._new("CREATED", order_id=order_id))

    def unknown(self) -> None:
        self._write(self._new("UNKNOWN"))

    @staticmethod
    def clear(path: Path) -> None:
        path.unlink(missing_ok=True)

    def _new(
        self,
        status: GuardStatus,
        *,
        order_id: str | None = None,
    ) -> OrderState:
        return OrderState(
            plan_key=self.plan_key,
            status=status,
            updated_at=datetime.now(UTC).isoformat(),
            order_id=order_id,
        )

    def _write(self, state: OrderState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(asdict(state), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.path)


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
    mode: str | None = None
    rc_code: str | None = None
    rc_id: str | None = None
    rc_show_id: str | None = None

    @property
    def risk_state(self) -> RiskState | None:
        if self.rc_code == QUEUE_CODE:
            return "QUEUEING"
        if self.rc_code == VERIFY_CODE:
            return "VERIFY_REQUIRED"
        if normalize_mode(self.mode) == LIMITING_MODE:
            return "RATE_LIMITED"
        return None


@dataclass(frozen=True)
class SubmissionOutcome:
    result: CreateResult
    request_count: int


@dataclass(frozen=True)
class RiskEvent:
    state: RiskState
    stage: RiskStage
    rc_id: str | None = None
    rc_show_id: str | None = None


RiskNotice = Callable[[RiskEvent], Awaitable[None] | None]


class SubmissionLease:
    """只放行同一逻辑订单在官方状态机中的受控重发。"""

    def __init__(self, max_requests: int = MAX_LEASE_REQUESTS) -> None:
        self.max_requests = max_requests
        self.digest: str | None = None
        self.active = False
        self.in_flight = False
        self.permits = 0
        self.request_count = 0
        self.blocked_requests = 0

    def open(self, payload: dict) -> None:
        if self.active:
            raise RuntimeError("订单提交租约已存在")
        self.digest = _payload_digest(payload)
        self.active = True
        self.in_flight = False
        self.permits = 1
        self.request_count = 0

    def allow_retry(self) -> None:
        if self.active and self.request_count < self.max_requests:
            self.permits = 1

    def authorize(self, request: Request) -> bool:
        if request.method.upper() != "POST" or not is_create_url(request.url):
            return True
        if (
            not self.active
            or self.in_flight
            or self.permits != 1
            or self.request_count >= self.max_requests
            or _request_digest(request) != self.digest
        ):
            self.blocked_requests += 1
            return False
        self.permits = 0
        self.in_flight = True
        self.request_count += 1
        return True

    def response_received(self) -> None:
        if self.active:
            self.in_flight = False

    def close(self) -> None:
        self.digest = None
        self.active = False
        self.in_flight = False
        self.permits = 0


class SubmissionSession:
    """监听一个页面上的创建订单请求，直到得到明确终态。"""

    def __init__(
        self,
        page: Page,
        guard: PersistentOrderGuard,
    ) -> None:
        self.page = page
        self.guard = guard
        self.lease = SubmissionLease()
        self._responses: asyncio.Queue[Response] = asyncio.Queue()
        self._attached = False

    @property
    def blocked_requests(self) -> int:
        return self.lease.blocked_requests

    async def attach(self) -> None:
        if self._attached:
            return
        await self.page.route(CREATE_ROUTE, self._route)
        self.page.on("response", self._handle_response)
        self._attached = True

    async def detach(self) -> None:
        if not self._attached:
            return
        with suppress(Exception):
            self.page.remove_listener("response", self._handle_response)
        with suppress(Exception):
            await self.page.unroute(CREATE_ROUTE, self._route)
        self._attached = False

    async def submit(
        self,
        payload: dict,
        send: Callable[[], Awaitable[None]],
        timeout_seconds: float,
        on_risk: RiskNotice | None = None,
    ) -> SubmissionOutcome:
        self._drain()
        self.lease.open(payload)
        self.guard.submitting()
        sender = asyncio.create_task(send())
        limiting_retries = 0
        retry_used = False
        token_fallback: tuple[CreateResult, int] | None = None
        waiting_for_risk = False
        reported_risks: set[tuple[RiskState, str | None]] = set()
        try:
            while True:
                timeout = (
                    max(timeout_seconds, RISK_WAIT_SECONDS)
                    if waiting_for_risk
                    else timeout_seconds
                )
                response = await self._next_response(sender, timeout)
                result = await parse_create_response(response)
                risk = result.risk_state
                risk_key = (risk, result.rc_id)
                if (
                    risk is not None
                    and risk_key not in reported_risks
                    and on_risk is not None
                ):
                    reported_risks.add(risk_key)
                    on_risk(
                        RiskEvent(
                            risk,
                            "CREATE_ORDER",
                            result.rc_id,
                            result.rc_show_id,
                        )
                    )
                if risk == "RATE_LIMITED":
                    if limiting_retries < MAX_LIMITING_RETRIES:
                        limiting_retries += 1
                        self.lease.allow_retry()
                        request_count = self.lease.request_count
                        await asyncio.sleep(LIMITING_INTERVAL_SECONDS)
                        if self.lease.request_count == request_count and sender.done():
                            await _finish_sender(sender)
                            sender = asyncio.create_task(send())
                        waiting_for_risk = True
                        continue
                if (
                    risk == "RATE_LIMITED" or result.mode == "retry"
                ) and not retry_used:
                    retry_used = True
                    self.lease.allow_retry()
                    await self._click_official_retry(timeout_seconds)
                    waiting_for_risk = True
                    continue
                if risk in {"QUEUEING", "VERIFY_REQUIRED"}:
                    waiting_for_risk = True
                    continue
                if (
                    TOKEN_EXPIRED_CODE in {result.code, result.sub_code}
                    and token_fallback is None
                ):
                    token_fallback = (result, self.lease.request_count)
                    self.lease.allow_retry()
                    continue

                await _finish_sender(sender)
                if result.success:
                    self.guard.created(result.order_id)
                else:
                    self.guard.ready()
                return SubmissionOutcome(result, self.lease.request_count)
        except TimeoutError:
            if token_fallback is not None:
                result, request_count = token_fallback
                if self.lease.request_count == request_count:
                    self.guard.ready()
                    return SubmissionOutcome(result, request_count)
            self.guard.unknown()
            raise
        finally:
            if not sender.done():
                sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)
            self.lease.close()

    async def _click_official_retry(self, timeout_seconds: float) -> None:
        retry = self.page.get_by_text("重试", exact=True).last
        try:
            await retry.wait_for(state="visible", timeout=timeout_seconds * 1000)
            await retry.evaluate("node => node.click()")
        except Exception as exc:
            self.guard.ready()
            raise RuntimeError("官方重试按钮未出现") from exc

    async def _route(self, route: Route, request: Request) -> None:
        if request.method.upper() != "POST" or not is_create_url(request.url):
            await route.continue_()
            return
        if self.lease.authorize(request):
            await route.continue_()
        else:
            await route.abort("blockedbyclient")

    def _handle_response(self, response: Response) -> None:
        if response.request.method.upper() != "POST" or not is_create_url(response.url):
            return
        self.lease.response_received()
        rc_code = _response_header(response, "rc-code")
        if rc_code in {QUEUE_CODE, VERIFY_CODE}:
            self.lease.allow_retry()
        self._responses.put_nowait(response)

    def _drain(self) -> None:
        while not self._responses.empty():
            with suppress(asyncio.QueueEmpty):
                self._responses.get_nowait()

    async def _next_response(
        self,
        sender: asyncio.Task[None],
        timeout: float,
    ) -> Response:
        waiter = asyncio.create_task(self._responses.get())
        if sender.done():
            try:
                self._raise_unsent(sender)
            except Exception:
                waiter.cancel()
                await asyncio.gather(waiter, return_exceptions=True)
                raise
            return await asyncio.wait_for(waiter, timeout)

        done, _ = await asyncio.wait(
            (waiter, sender),
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
            raise TimeoutError
        if waiter in done:
            return waiter.result()

        try:
            self._raise_unsent(sender)
        except Exception:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
            raise
        return await asyncio.wait_for(waiter, timeout)

    def _raise_unsent(self, sender: asyncio.Task[None]) -> None:
        if self.lease.request_count:
            return
        self.guard.ready()
        error = sender.exception()
        if error is not None:
            raise error
        raise RuntimeError("创建请求未发出")


async def parse_create_response(response: Response) -> CreateResult:
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
    code = _response_scalar(payload, ("statusCode", "code", "errorCode"))
    message = (
        redact_preview(
            _response_scalar(
                payload,
                ("comments", "message", "msg", "errorMessage", "errorMsg", "desc"),
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
        sub_code=_response_scalar(payload, ("subCode", "sub_code", "bizCode")),
        message=message,
        mode=normalize_mode(_response_scalar(payload, ("mode",))),
        rc_code=_response_header(response, "rc-code"),
        rc_id=_response_header(response, "rc-id"),
        rc_show_id=_response_header(response, "rc-show-id"),
    )


def create_failure_action(result: CreateResult) -> FailureAction:
    codes = tuple(filter(None, (result.code, result.sub_code)))
    for code in codes:
        if action := CREATE_FAILURE_ACTIONS.get(code):
            return action
    if action := MODE_FAILURE_ACTIONS.get(normalize_mode(result.mode) or ""):
        return action
    if not 200 <= result.http_status < 300:
        return "FAILED"
    if any(code not in SUCCESS_CODES for code in codes):
        return "RETRY"
    return "UNKNOWN"


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


async def _finish_sender(sender: asyncio.Task[None]) -> None:
    with suppress(Exception):
        await sender


def _payload_digest(payload: object) -> str:
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode()).hexdigest()


def _request_digest(request: Request) -> str | None:
    try:
        payload = request.post_data_json
    except Exception:
        return None
    return _payload_digest(payload) if payload is not None else None


def _response_header(response: Response, name: str) -> str | None:
    value = response.headers.get(name.lower())
    return str(value) if value not in {None, ""} else None


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


def normalize_mode(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    lowered = text.lower().replace("-", "_")
    return MODE_NAMES.get(text, "back_refresh" if lowered == "backrefresh" else lowered)


def _response_scalar(payload: object, keys: tuple[str, ...]) -> str | None:
    if not isinstance(payload, dict):
        return None
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str | int | float | bool):
            return str(value)
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
    if not isinstance(value, str | int):
        return None
    result = str(value)
    return result or None


def _positive_int(value: object) -> int | None:
    if not isinstance(value, str | int):
        return None
    try:
        result = int(value)
    except (ValueError, OverflowError):
        return None
    return result if result > 0 else None
