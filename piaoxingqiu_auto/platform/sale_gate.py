"""官方销售状态、服务端时间与开售放行。"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlencode

from playwright.async_api import APIResponse

from piaoxingqiu_auto.platform.auth import AuthGuard, request_context
from piaoxingqiu_auto.platform.api import is_success_payload
from piaoxingqiu_auto.domain.sale import (
    INTENSIVE_SECONDS,
    OPEN_SESSION_STATUSES,
    POST_SALE_WAIT_SECONDS,
    PREWARM_SECONDS,
    find_session,
    presale_poll_interval,
    sale_time,
    session_sale_time,
)
from piaoxingqiu_auto.platform.checkout_page import PurchasePage


FAR_POLL_SECONDS = 30.0


class SaleUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class SaleState:
    session_status: str
    sale_time_ms: int | None
    server_time_ms: int

    @property
    def on_sale(self) -> bool:
        return self.session_status in OPEN_SESSION_STATUSES

    @property
    def remaining_seconds(self) -> float | None:
        if self.sale_time_ms is None:
            return None
        return (self.sale_time_ms - self.server_time_ms) / 1000


class SaleGate:
    def __init__(self, site: PurchasePage) -> None:
        self.site = site
        self.show_id = site.show_id
        _, self.session_id = site.booking_ids
        root = f"{site.origin}/cyy_gatewayapi/show/pub"
        common = urlencode(request_context())
        self.show_url = f"{root}/v5/show/{self.show_id}/dynamic?{common}"
        self.session_url = (
            f"{root}/v5/show/{self.show_id}/sessions?"
            f"source=FROM_QUICK_ORDER&src=WEB&{common}"
        )

    async def fetch(self) -> SaleState:
        payloads, now_ms = await self._fetch_payloads((self.show_url, self.session_url))
        session = find_session(payloads[1], self.session_id)
        if session is None:
            raise RuntimeError("开售状态接口未找到 booking_url 对应的目标场次")
        session_status = str(session.get("sessionStatus") or "").upper()
        if not session_status:
            raise RuntimeError("目标场次缺少 sessionStatus")
        return SaleState(
            session_status=session_status,
            sale_time_ms=sale_time(session)
            or session_sale_time(payloads[0], self.session_id),
            server_time_ms=now_ms,
        )

    async def _refresh_session(self, previous: SaleState) -> SaleState:
        payloads, now_ms = await self._fetch_payloads((self.session_url,))
        session = find_session(payloads[0], self.session_id)
        if session is None:
            raise RuntimeError("场次状态轮询未找到目标场次")
        session_status = str(session.get("sessionStatus") or "").upper()
        if not session_status:
            raise RuntimeError("目标场次缺少 sessionStatus")
        return SaleState(
            session_status=session_status,
            sale_time_ms=sale_time(session) or previous.sale_time_ms,
            server_time_ms=now_ms,
        )

    async def _fetch_payloads(
        self, urls: tuple[str, ...]
    ) -> tuple[list[dict[str, Any]], int]:
        responses = await asyncio.gather(
            *(self.site.page.context.request.get(url) for url in urls)
        )
        payloads = []
        server_times = []
        for response in responses:
            self._check_response(response)
            payload = await response.json()
            if (
                isinstance(payload, dict)
                and str(payload.get("statusCode")) == "22024033"
            ):
                raise RuntimeError("节目暂不可售（22024033）")
            if not is_success_payload(payload):
                raise RuntimeError("开售状态接口返回异常业务状态")
            payloads.append(payload)
            if server_time := _server_time_ms(response):
                server_times.append(server_time)
        return payloads, max(server_times, default=int(time.time() * 1000))

    async def wait_until_prewarm(self, state: SaleState, auth: AuthGuard) -> SaleState:
        if state.on_sale:
            return state
        self._check_waitable(state)
        remaining = state.remaining_seconds
        if remaining is None:
            raise RuntimeError("官方接口未返回开售时间，无法进入预抢票模式")
        loop = asyncio.get_running_loop()
        next_auth = loop.time() + auth.interval(remaining)
        while remaining > PREWARM_SECONDS:
            await asyncio.sleep(
                min(
                    FAR_POLL_SECONDS,
                    remaining - PREWARM_SECONDS,
                    max(0, next_auth - loop.time()),
                )
            )
            state = await self._refresh_session(state)
            if state.on_sale:
                await auth.require_valid(allow_refresh=True)
                return state
            self._check_waitable(state)
            remaining = state.remaining_seconds
            if remaining is None:
                raise RuntimeError("等待期间官方接口不再返回开售时间")
            if loop.time() >= next_auth:
                await auth.ensure()
                next_auth = loop.time() + auth.interval(remaining)
        return state

    async def wait_until_sale(self, state: SaleState, auth: AuthGuard) -> SaleState:
        loop = asyncio.get_running_loop()
        next_auth = loop.time() + 10
        final_auth = False
        while True:
            state = await self._refresh_session(state)
            remaining = state.remaining_seconds
            if state.on_sale:
                if not final_auth:
                    await auth.require_valid(
                        allow_refresh=remaining is None or remaining > INTENSIVE_SECONDS
                    )
                return state
            self._check_waitable(state)
            if remaining is None:
                raise RuntimeError("等待开售时目标场次缺少开售时间")
            if remaining < -POST_SALE_WAIT_SECONDS:
                raise SaleUnavailable("开售状态未在等待窗口内更新")
            now = loop.time()
            if remaining <= INTENSIVE_SECONDS:
                if not final_auth:
                    await auth.require_valid(allow_refresh=False)
                    final_auth = True
            elif not final_auth and now >= next_auth:
                await auth.require_valid(allow_refresh=True)
                next_auth = now + 10
            await asyncio.sleep(presale_poll_interval(remaining))

    @staticmethod
    def _check_response(response: APIResponse) -> None:
        if response.status in {401, 429, 469}:
            raise RuntimeError(
                f"开售状态接口触发限制（HTTP {response.status}），已停止"
            )
        if not response.ok:
            raise RuntimeError(f"开售状态接口返回 HTTP {response.status}")

    @staticmethod
    def _check_waitable(state: SaleState) -> None:
        if state.session_status == "PENDING":
            return
        if state.session_status == "LACK_OF_TICKET":
            raise SaleUnavailable("场次当前无票")
        if state.session_status == "DELAY":
            raise RuntimeError("目标场次延期（DELAY）")
        raise RuntimeError(f"未知场次状态：{state.session_status}")


def _server_time_ms(response: APIResponse) -> int | None:
    value = response.headers.get("date")
    if not value:
        return None
    try:
        return int(parsedate_to_datetime(value).timestamp() * 1000)
    except (TypeError, ValueError, OverflowError):
        return None
