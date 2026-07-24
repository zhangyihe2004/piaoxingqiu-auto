"""票星球购票页和确认订单页的 Playwright 适配。"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypeVar
from urllib.parse import parse_qs, urlsplit

from playwright.async_api import Locator, Page, Request

from piaoxingqiu_auto.domain.models import AccountRunConfig, AudienceConfig
from piaoxingqiu_auto.platform.order_response import redact_preview

if TYPE_CHECKING:
    from piaoxingqiu_auto.platform.inventory import GeneralAdmissionSelection
    from piaoxingqiu_auto.domain.seating import SeatSelection


SUBMIT_LABEL = "去支付"
BOOKING_ACTION_SELECTOR = ".bottom-btn-item"
CONFIRM_SEAT_SELECTOR = ".capsule-right"
AUDIENCE_SELECTOR = ".audiences-label"
SUBMIT_SELECTOR = ".btn-pay"
POLL_INTERVAL_MS = 250
T = TypeVar("T")


class PurchasePage:
    def __init__(
        self,
        page: Page,
        config: AccountRunConfig,
        timing: Callable[[str, float], None] | None = None,
        adopt_page: Callable[[Page], Awaitable[None]] | None = None,
    ) -> None:
        self.page = page
        self.config = config
        self._timing = timing
        self._adopt_page = adopt_page
        self._pick_seat_url: str | None = None
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
        expected_path = (
            f"/cyy_gatewayapi/show/pub/v5/show/{show_id}/session/"
            f"{session_id}/seat_plans"
        )
        async with self.page.context.expect_event(
            "requestfinished",
            predicate=lambda request: (
                request.method.upper() == "GET"
                and urlsplit(request.url).path == expected_path
            ),
            timeout=self.config.browser.timeout_ms,
        ) as request_info:
            await self._goto(self.config.project.booking_url)
        await self._check_finished_request(await request_info.value, "票档")

    async def prepare_booking(self) -> None:
        current = urlsplit(self.page.url)
        current_session = (parse_qs(current.query).get("saleShowSessionId") or [""])[0]
        if (
            current.path.rstrip("/") != self._booking_path
            or current_session != self._session_id
        ):
            await self.open_purchase()
        if await self._poll(
            lambda: self._find_plan(self.config.purchase.plan_ids[0])
        ) is None:
            raise RuntimeError("booking 页未加载配置场次的票档")

    async def reopen_seat_map(self) -> None:
        if self._pick_seat_url is None:
            raise RuntimeError("尚未记录可复用的选座地址")
        await self._goto(self._pick_seat_url)
        if urlsplit(self.page.url).path != "/pick-seat":
            raise RuntimeError("原选座地址已失效")

    async def _goto(self, url: str) -> None:
        await self.page.goto(url, wait_until="domcontentloaded")

    async def prepare_order(
        self,
        selection: SeatSelection,
        audiences: tuple[AudienceConfig, ...],
        confirm: Locator,
    ) -> PreparedOrder:
        started = asyncio.get_running_loop().time()
        try:
            await confirm.evaluate("element => element.click()")
        finally:
            self.record_timing(
                "confirm_page", asyncio.get_running_loop().time() - started
            )
        counts: dict[str, int] = {}
        for candidate in selection.candidates:
            counts[candidate.plan] = counts.get(candidate.plan, 0) + 1
        return await self._prepare_submit(audiences, len(selection.candidates), counts)

    async def prepare_general_order(
        self,
        selection: GeneralAdmissionSelection,
        audiences: tuple[AudienceConfig, ...],
    ) -> PreparedOrder:
        started = asyncio.get_running_loop().time()
        try:
            await self._select_general_ticket(selection)
        finally:
            self.record_timing(
                "general_page", asyncio.get_running_loop().time() - started
            )
        return await self._prepare_submit(
            audiences,
            selection.quantity,
            {selection.plan: selection.quantity},
        )

    async def _prepare_submit(
        self,
        audiences: tuple[AudienceConfig, ...],
        quantity: int,
        expected_plans: dict[str, int],
    ) -> PreparedOrder:
        required = await self._poll(self._required_audience_count)
        if required is None:
            body = redact_preview(
                await self.page.locator("body").inner_text(),
                limit=300,
            )
            raise RuntimeError(
                f"订单页未返回实名观演人数要求：{self.page.url}；页面：{body}"
            )
        if len(audiences) < required:
            raise RuntimeError(
                f"订单需要 {required} 个实名证件，当前仅配置 {len(audiences)} 个"
        )
        selected = audiences[:required]
        if selected:
            started = asyncio.get_running_loop().time()
            try:
                await self._select_audiences(selected)
            finally:
                self.record_timing(
                    "audience", asyncio.get_running_loop().time() - started
                )

        submit = await self._poll(
            lambda: self._enabled_action(SUBMIT_SELECTOR, SUBMIT_LABEL)
        )
        if submit is None:
            body = redact_preview(
                await self.page.locator("body").inner_text(),
                limit=500,
            )
            raise RuntimeError(
                f"选择门票后未到达提交订单页：{self.page.url}；页面：{body}"
            )
        body = _normalize(await self.page.locator("body").inner_text())
        summary = _order_summary(body, quantity, expected_plans)
        return PreparedOrder(submit, selected, summary)

    async def _required_audience_count(self) -> int | None:
        body = _normalize(await self.page.locator("body").inner_text())
        matches = {int(value) for value in re.findall(r"已选\d+/(\d+)位", body)}
        if len(matches) == 1:
            return matches.pop()
        if (
            "订单总额" in body
            and "实名观演人" not in body
            and await self._find_action(SUBMIT_SELECTOR, SUBMIT_LABEL)
        ):
            return 0
        return None

    async def _select_general_ticket(
        self,
        selection: GeneralAdmissionSelection,
    ) -> None:
        await self._select_plan(selection.plan_id, selection.plan)
        await self._set_quantity(selection.units)

        next_step = await self._poll(
            lambda: self._enabled_action(BOOKING_ACTION_SELECTOR, "下一步")
        )
        if next_step is None:
            raise RuntimeError("booking 页等待可用的“下一步”按钮超时")
        await next_step.evaluate("element => element.click()")

    async def _select_audiences(self, audiences: tuple[AudienceConfig, ...]) -> None:
        async def find_audience(name: str, masked_id: str) -> Locator | None:
            cards = self.page.locator(AUDIENCE_SELECTOR)
            expected_name = _normalize(name)
            expected_id = _normalize(masked_id)
            for index in range(await cards.count()):
                audience = cards.nth(index)
                content = _normalize(await audience.inner_text())
                if (
                    await audience.is_visible()
                    and expected_name in content
                    and expected_id in content
                ):
                    return audience
            return None

        targets = {(item.name, item.masked_id) for item in audiences}
        located: dict[tuple[str, str], Locator] = {}
        for configured in self.config.purchase.audiences:

            async def find_configured() -> Locator | None:
                return await find_audience(configured.name, configured.masked_id)

            audience = await self._poll(find_configured)
            if audience is None:
                raise RuntimeError(f"订单页等待观演人“{configured.name}”超时")
            key = (configured.name, configured.masked_id)
            located[key] = audience
            if await _is_audience_selected(audience) and key not in targets:
                await audience.evaluate("element => element.click()")

        for configured in audiences:
            audience = located[(configured.name, configured.masked_id)]
            if not await _is_audience_selected(audience):
                await audience.evaluate("element => element.click()")

        async def audience_selected() -> bool | None:
            for key, audience in located.items():
                if await _is_audience_selected(audience) != (key in targets):
                    return None
            body = _normalize(await self.page.locator("body").inner_text())
            quantity = len(audiences)
            return True if f"已选{quantity}/{quantity}" in body else None

        if await self._poll(audience_selected) is None:
            raise RuntimeError("订单页实际选中的观演人与配置不完全一致")

    async def open_seat_map(self) -> None:
        current = urlsplit(self.page.url)
        query = parse_qs(current.query)
        if (
            current.path == "/pick-seat"
            and (query.get("showId") or [""])[0] == self._show_id
            and (query.get("bizShowSessionId") or [""])[0] == self._session_id
        ):
            self._pick_seat_url = self.page.url
            return
        go_pick = await self._poll(
            lambda: self._enabled_action(BOOKING_ACTION_SELECTOR, "去选座")
        )
        if go_pick is None:
            raise RuntimeError("booking 页等待可用的“去选座”按钮超时")
        pages_before = set(self.page.context.pages)
        url_before = self.page.url
        async with self._expect_dynamic_request() as request_info:
            await go_pick.evaluate("element => element.click()")
            if not await self._wait_for_transition(pages_before, url_before):
                raise RuntimeError("点击“去选座”后等待页面变化超时")
        await self._check_finished_request(await request_info.value, "动态座位")
        if urlsplit(self.page.url).path != "/pick-seat":
            raise RuntimeError("进入选座流程后未到达官方选座页")
        self._pick_seat_url = self.page.url

    async def reusable_task_page(self) -> bool:
        if self.page.is_closed():
            return False
        current = urlsplit(self.page.url)
        query = parse_qs(current.query)
        session_id = (query.get("saleShowSessionId") or [""])[0]
        if current.path == "/pick-seat":
            session_id = (query.get("bizShowSessionId") or [""])[0]
            valid = (
                (query.get("showId") or [""])[0] == self._show_id
                and session_id == self._session_id
                and not await self.page.locator(".seat-item").count()
            )
        else:
            valid = (
                current.path.rstrip("/") == self._booking_path
                and session_id == self._session_id
            )
        return valid and not await self.page.locator(
            "#global-loding .loading-wrapper:visible"
        ).count()

    async def _set_quantity(self, target: int) -> None:
        current = await self._poll(self._current_quantity)
        if current is None:
            raise RuntimeError("booking 页未显示数量控件")
        selector = f".qty-changer .{'plus' if current < target else 'minus'}"
        control = self.page.locator(selector).first
        for _ in range(abs(target - current)):
            await control.evaluate("element => element.click()")

    async def _current_quantity(self) -> int | None:
        count = self.page.locator(".section-count").first
        if not await count.count() or not await count.is_visible():
            return None
        value = (await count.inner_text()).strip()
        return int(value) if value.isdigit() else None

    async def _select_plan(self, plan_id: str, plan_name: str) -> None:
        target_plan = await self._poll(lambda: self._find_plan(plan_id))
        if target_plan is None or await _is_disabled(target_plan):
            raise RuntimeError(f"booking 页无法选择目标票档“{plan_name}”")
        await target_plan.evaluate("element => element.click()")
        if await self._poll(lambda: _selected_plan(target_plan)) is None:
            raise RuntimeError(f"点击票档后未进入选中状态：“{plan_name}”")

    async def _find_plan(self, plan_id: str) -> Locator | None:
        plan = self.page.locator(f'[data-seat-plan-id="{plan_id}"]').first
        return plan if await plan.count() and await plan.is_visible() else None

    def _expect_dynamic_request(self):
        show_id, session_id = self.booking_ids
        expected_path = (
            f"/cyy_gatewayapi/show/buyer/v5/show/{show_id}/session/"
            f"{session_id}/seating/dynamic"
        )

        def matches(request: Request) -> bool:
            return (
                request.method.upper() == "GET"
                and urlsplit(request.url).path == expected_path
            )

        return self.page.context.expect_event(
            "requestfinished",
            predicate=matches,
            timeout=self.config.browser.timeout_ms,
        )

    async def _check_finished_request(self, request: Request, label: str) -> None:
        response = await request.response()
        if response is None:
            raise RuntimeError(f"{label}请求未返回响应")
        if not response.ok:
            raise RuntimeError(f"{label}接口返回 HTTP {response.status}")

    async def _poll(
        self,
        finder: Callable[[], Awaitable[T | None]],
    ) -> T | None:
        deadline = (
            asyncio.get_running_loop().time() + self.config.browser.timeout_ms / 1000
        )
        while asyncio.get_running_loop().time() < deadline:
            result = await finder()
            if result is not None:
                return result
            await self.page.wait_for_timeout(POLL_INTERVAL_MS)
        return None

    async def _wait_for_transition(
        self,
        pages_before: set[Page],
        url_before: str,
    ) -> bool:
        deadline = (
            asyncio.get_running_loop().time() + self.config.browser.timeout_ms / 1000
        )
        while asyncio.get_running_loop().time() < deadline:
            new_pages = [
                page for page in self.page.context.pages if page not in pages_before
            ]
            if new_pages:
                self.page = new_pages[-1]
                if self._adopt_page is not None:
                    await self._adopt_page(self.page)
                await self.page.wait_for_load_state("domcontentloaded")
                pages_before.add(self.page)
            if self.page.url not in {url_before, "about:blank"}:
                return True
            await self.page.wait_for_timeout(POLL_INTERVAL_MS)
        return False

    def record_timing(self, stage: str, seconds: float) -> None:
        if self._timing is not None:
            self._timing(stage, seconds)

    async def wait_confirm_seat(self) -> Locator | None:
        return await self._poll(
            lambda: self._enabled_action(CONFIRM_SEAT_SELECTOR, "确认选座")
        )

    async def _enabled_action(self, selector: str, text: str) -> Locator | None:
        candidate = await self._find_action(selector, text)
        return (
            candidate
            if candidate is not None and not await _is_disabled(candidate)
            else None
        )

    async def _find_action(self, selector: str, text: str) -> Locator | None:
        locator = self.page.locator(selector)
        expected = _normalize(text)
        for index in range(await locator.count()):
            candidate = locator.nth(index)
            if (
                await candidate.is_visible()
                and _normalize(await candidate.inner_text()) == expected
            ):
                return candidate
        return None


@dataclass(frozen=True)
class PreparedOrder:
    submit: Locator
    audiences: tuple[AudienceConfig, ...]
    summary: OrderSummary


@dataclass(frozen=True)
class OrderSummary:
    quantity: int
    plans: tuple[str, ...]
    total: str | None
    combo: bool

    def describe(self) -> str:
        details = [f"目标 {self.quantity} 张", f"票档：{'、'.join(self.plans)}"]
        if self.combo:
            details.append("套票优惠：票星球已自动计算")
        if self.total:
            details.append(f"应付：¥{self.total}")
        return "；".join(details)


async def _is_disabled(locator: Locator) -> bool:
    return bool(
        await locator.evaluate(
            """element => {
                for (let node = element, depth = 0;
                     node && depth < 6; node = node.parentElement, depth++) {
                    const cls = String(node.className || '').toLowerCase();
                    if (node.disabled || node.getAttribute('aria-disabled') === 'true' ||
                        /(disabled|inactive)/.test(cls)) return true;
                }
                return false;
            }"""
        )
    )


async def _selected_plan(locator: Locator) -> bool | None:
    selected = await locator.evaluate(
        """element =>
            element.matches('.active, .selected') ||
            Boolean(element.querySelector('.active, .selected'))"""
    )
    return True if selected else None


async def _is_audience_selected(locator: Locator) -> bool:
    return bool(await locator.locator(".icon-xuanzhong").count())


def _order_summary(
    body: str,
    expected_quantity: int,
    expected_plans: dict[str, int],
) -> OrderSummary:
    body = _normalize(body)
    missing = [plan for plan in expected_plans if _normalize(plan) not in body]
    if missing:
        raise RuntimeError(f"订单页未显示目标票档：{'、'.join(missing)}")
    totals = re.findall(r"应付金额[：:]?¥?([0-9,]+(?:\.\d+)?)", body)
    return OrderSummary(
        expected_quantity,
        tuple(expected_plans),
        totals[-1] if totals else None,
        "套票" in body and "优惠" in body,
    )


def _normalize(value: str) -> str:
    return re.sub(r"\s+", "", value or "")
