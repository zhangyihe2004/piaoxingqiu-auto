"""目标 booking 页面初始化与复用。"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from typing import TypeVar
from urllib.parse import parse_qs, urlsplit

from playwright.async_api import Page, Request

from piaoxingqiu_auto.domain.models import AccountRunConfig


POLL_INTERVAL_MS = 250
T = TypeVar("T")


class PurchasePage:
    def __init__(
        self,
        page: Page,
        config: AccountRunConfig,
        timing: Callable[[str, float], None] | None = None,
    ) -> None:
        self.page = page
        self.config = config
        self._timing = timing
        booking = urlsplit(config.project.booking_url)
        self._booking_path = booking.path.rstrip("/")
        self._origin = f"{booking.scheme}://{booking.netloc}"
        query = parse_qs(booking.query)
        self._show_id = (
            query.get("showId") or [booking.path.rstrip("/").rsplit("/", 1)[-1]]
        )[0]
        self._session_id = (query.get("saleShowSessionId") or [""])[0]
        if not re.fullmatch(r"[0-9a-fA-F]{24}", self._show_id) or (
            self._session_id and not re.fullmatch(r"[0-9a-fA-F]{24}", self._session_id)
        ):
            raise RuntimeError("booking_url 缺少有效 showId 或 saleShowSessionId")

    @property
    def show_id(self) -> str:
        return self._show_id

    @property
    def origin(self) -> str:
        return self._origin

    @property
    def booking_ids(self) -> tuple[str, str]:
        if not self._session_id:
            raise RuntimeError("booking_url 缺少 saleShowSessionId")
        return self._show_id, self._session_id

    async def open_purchase(self) -> None:
        show_id, session_id = self.booking_ids
        path = (
            f"/cyy_gatewayapi/show/pub/v5/show/{show_id}/session/"
            f"{session_id}/seat_plans"
        )
        async with self.page.context.expect_event(
            "requestfinished",
            predicate=lambda request: (
                request.method == "GET" and urlsplit(request.url).path == path
            ),
            timeout=self.config.browser.timeout_ms,
        ) as request_info:
            await self.page.goto(
                self.config.project.booking_url,
                wait_until="domcontentloaded",
            )
        await _check_request(await request_info.value, "票档")

    async def prepare_booking(self) -> None:
        current = urlsplit(self.page.url)
        session_id = (parse_qs(current.query).get("saleShowSessionId") or [""])[0]
        if (
            current.path.rstrip("/") != self._booking_path
            or session_id != self._session_id
        ):
            await self.open_purchase()
        if await self._poll(self._client_loaded) is None:
            raise RuntimeError("booking 主模块未加载")

    async def reusable_task_page(self) -> bool:
        if self.page.is_closed():
            return False
        current = urlsplit(self.page.url)
        session_id = (parse_qs(current.query).get("saleShowSessionId") or [""])[0]
        return (
            current.path.rstrip("/") == self._booking_path
            and session_id == self._session_id
            and not await self.page.locator(
                "#global-loding .loading-wrapper:visible"
            ).count()
        )

    def record_timing(self, stage: str, seconds: float) -> None:
        if self._timing is not None:
            self._timing(stage, seconds)

    async def _client_loaded(self) -> bool | None:
        loaded = await self.page.evaluate(
            """
            () => performance.getEntriesByType("resource").some(
              entry => /\\/index-[^/]+\\.js(?:\\?|$)/.test(entry.name)
            )
            """
        )
        return True if loaded else None

    async def _poll(
        self,
        finder: Callable[[], Awaitable[T | None]],
    ) -> T | None:
        deadline = (
            asyncio.get_running_loop().time() + self.config.browser.timeout_ms / 1000
        )
        while asyncio.get_running_loop().time() < deadline:
            if result := await finder():
                return result
            await self.page.wait_for_timeout(POLL_INTERVAL_MS)
        return None


async def _check_request(request: Request, label: str) -> None:
    response = await request.response()
    if response is None:
        raise RuntimeError(f"{label}请求未返回响应")
    if not response.ok:
        raise RuntimeError(f"{label}接口返回 HTTP {response.status}")
