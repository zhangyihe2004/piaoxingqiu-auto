"""单账号选票、确认订单和创建订单工作流。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from typing import TypeVar

from playwright.async_api import Page

from piaoxingqiu_auto.platform.auth import AuthGuard, AuthenticationRequired
from piaoxingqiu_auto.runtime.browser import blank_page, save_screenshot
from piaoxingqiu_auto.domain.models import AccountRunConfig, AudienceConfig
from piaoxingqiu_auto.domain.outcomes import RunResult
from piaoxingqiu_auto.platform.order_guard import OrderFirewall, PersistentOrderGuard
from piaoxingqiu_auto.platform.inventory import (
    GeneralAdmissionInventory,
    Inventory,
    InventoryBootstrap,
    InventoryUnavailable,
    StaticInventoryUnavailable,
)
from piaoxingqiu_auto.platform.order_response import (
    CreateResult,
    CreateResponseWatcher,
    create_failure_action,
    find_already_purchased_ids,
    match_configured_ids,
)
from piaoxingqiu_auto.platform.sale_gate import SaleGate, SaleUnavailable
from piaoxingqiu_auto.platform.seat_map import reselect_seats, select_ready_seats
from piaoxingqiu_auto.domain.seating import SeatSelection
from piaoxingqiu_auto.platform.checkout_page import PreparedOrder, PurchasePage
from piaoxingqiu_auto.runtime.timing import RunTimings

log = logging.getLogger("piaoxingqiu.auto")
MAX_CREATE_ATTEMPTS = 10
T = TypeVar("T")
async def run_account(
    config: AccountRunConfig,
    context,
    *,
    prewarm: bool,
    trace_label: str | None = None,
    page: Page | None = None,
    auth_headers: dict[str, str] | None = None,
    page_changed: Callable[[Page], None] | None = None,
) -> RunResult:
    """执行一个账号的一次自动抢票生命周期，最多成功创建一个订单。"""
    if not config.create_order:
        return RunResult("DISABLED", "全局 create_order_enabled 尚未开启")
    if page is None:
        page = await blank_page(context)
    firewall = OrderFirewall()
    route_handler = firewall.route
    watcher = CreateResponseWatcher()
    response_handler = watcher.handle
    instrumented: set[Page] = set()

    async def instrument(target: Page) -> None:
        if target in instrumented:
            return
        await target.route("**/*", route_handler)
        target.on("response", response_handler)
        instrumented.add(target)
        if page_changed is not None:
            page_changed(target)

    await instrument(page)
    try:
        return await _run_account(
            config,
            page,
            firewall,
            watcher,
            prewarm=prewarm,
            trace_label=trace_label,
            auth_headers=auth_headers,
            adopt_page=instrument,
        )
    finally:
        for target in tuple(instrumented):
            with suppress(Exception):
                target.remove_listener("response", response_handler)
            with suppress(Exception):
                await target.unroute("**/*", route_handler)


async def _run_account(
    config: AccountRunConfig,
    page: Page,
    firewall: OrderFirewall,
    watcher: CreateResponseWatcher,
    *,
    prewarm: bool,
    trace_label: str | None,
    auth_headers: dict[str, str] | None,
    adopt_page: Callable[[Page], Awaitable[None]],
) -> RunResult:
    timings = RunTimings(trace_label or config.project.name)
    site = PurchasePage(page, config, timings.record, adopt_page)
    guard = PersistentOrderGuard(config.state_path, config.plan_key)
    guard.require_ready()
    auth = AuthGuard(site, auth_headers)
    try:
        await auth.ensure()
    except AuthenticationRequired:
        return RunResult("NEEDS_LOGIN", "登录状态已失效")

    seat_source: Inventory | None = None
    general_source: GeneralAdmissionInventory | None = None
    bootstrap: InventoryBootstrap | None = None
    people = config.purchase.audiences
    ticket_quantity = config.purchase.quantity
    removed: list[str] = []
    fulfilled_quantity = 0
    page_ready = False
    try:
        if not config.project.support_seat_picking:
            if prewarm:
                gate = SaleGate(site)
                sale = await gate.fetch()
                if not sale.on_sale:
                    sale = await gate.wait_until_prewarm(sale, auth)
                    if not sale.on_sale:
                        await site.prepare_booking()
                        await gate.wait_until_sale(sale, auth)
            general_source = GeneralAdmissionInventory.open(site, auth)
            timings.begin(1)
            if prewarm:
                general_selection = await _measure(
                    timings,
                    "general_inventory",
                    general_source.wait_available(ticket_quantity),
                )
            else:
                general_selection = await _measure(
                    timings,
                    "general_inventory",
                    general_source.refresh(ticket_quantity),
                )
            prepared = await site.prepare_general_order(
                general_selection, people
            )
            selected_people = prepared.audiences
            selected_ticket_count = general_selection.quantity
        elif prewarm:
            gate = SaleGate(site)
            sale = await gate.fetch()
            if not sale.on_sale:
                sale = await gate.wait_until_prewarm(sale, auth)
                bootstrap = InventoryBootstrap.open(site, auth)
                if not sale.on_sale:
                    await site.prepare_booking()
                    static_task = asyncio.create_task(
                        bootstrap.wait_static(
                            remaining_seconds=sale.remaining_seconds,
                            preload=True,
                        )
                    )
                    try:
                        sale = await gate.wait_until_sale(sale, auth)
                        if static_task.done():
                            seat_source = await static_task
                        else:
                            seat_source = await bootstrap.wait_static(
                                remaining_seconds=sale.remaining_seconds,
                            )
                    finally:
                        if not static_task.done():
                            static_task.cancel()
                            await asyncio.gather(
                                static_task, return_exceptions=True
                            )
            if bootstrap is None:
                bootstrap = InventoryBootstrap.open(site, auth)

            async def wait_selection():
                current = seat_source or await bootstrap.wait_static()
                return current, await current.wait_available(ticket_quantity)

            async def open_ready_seat_map() -> None:
                nonlocal page_ready
                await site.open_seat_map()
                page_ready = True

            timings.begin(1)
            seat_source, seat_selection = await _parallel_result(
                wait_selection(),
                _measure(timings, "seat_page", open_ready_seat_map()),
                finish_side_on_unavailable=True,
            )
            prepared = await _prepare_seat_order(
                site, seat_selection, people, timings=timings
            )
            selected_people = prepared.audiences
            selected_ticket_count = len(seat_selection.candidates)
        else:
            timings.begin(1)

            async def load_selection():
                current = await Inventory.open(site, auth)
                return current, await current.refresh(ticket_quantity)

            async def open_ready_seat_map() -> None:
                nonlocal page_ready
                await site.open_seat_map()
                page_ready = True

            seat_source, seat_selection = await _parallel_result(
                load_selection(),
                _measure(timings, "seat_page", open_ready_seat_map()),
                finish_side_on_unavailable=True,
            )
            prepared = await _prepare_seat_order(
                site, seat_selection, people, timings=timings
            )
            selected_people = prepared.audiences
            selected_ticket_count = len(seat_selection.candidates)
    except AuthenticationRequired:
        timings.finish("NEEDS_LOGIN")
        return RunResult("NEEDS_LOGIN", "登录状态已失效")
    except InventoryUnavailable:
        timings.finish("RESTOCK")
        return RunResult(
            "RESTOCK",
            "配置票档当前没有可售库存",
            reuse_page=(
                (not config.project.support_seat_picking or page_ready)
                and await site.reusable_task_page()
            ),
        )
    except SaleUnavailable:
        timings.finish("RESTOCK")
        return RunResult("RESTOCK", "当前场次尚不可执行")
    except StaticInventoryUnavailable:
        timings.finish("STATIC_UNAVAILABLE")
        return RunResult(
            "RESTOCK",
            "静态座位资源尚未下发",
            reuse_page=page_ready and await site.reusable_task_page(),
        )
    except Exception:
        timings.finish("PREPARE_FAILED")
        await _save_failure(site, config, "prepare-failed")
        raise

    if firewall.blocked_requests:
        timings.finish("UNEXPECTED_CREATE")
        raise RuntimeError("准备阶段出现意外创建请求，已拦截并停止")

    attempt = 0
    while True:
        attempt += 1
        try:
            await auth.require_recent()
        except AuthenticationRequired:
            timings.finish("NEEDS_LOGIN")
            return RunResult("NEEDS_LOGIN", "提交前登录状态已失效")
        firewall.arm_once()
        guard.submitting()
        create_started = asyncio.get_running_loop().time()
        click_started = create_started
        try:
            await prepared.submit.evaluate("element => element.click()")
        except Exception as exc:
            now = asyncio.get_running_loop().time()
            timings.record("submit_click", now - click_started)
            timings.record("create_total", now - create_started)
            timings.finish("CLICK_FAILED")
            firewall.disarm()
            if firewall.attempt_allowed:
                guard.unknown()
                return RunResult(
                    "UNKNOWN",
                    f"创建请求可能已经发出，但点击流程异常：{exc}",
                    removed_audiences=tuple(removed),
                    fulfilled_quantity=fulfilled_quantity,
                )
            guard.ready()
            raise
        timings.record(
            "submit_click", asyncio.get_running_loop().time() - click_started
        )
        response_started = asyncio.get_running_loop().time()
        try:
            result = await watcher.wait(config.browser.timeout_ms / 1000)
        except TimeoutError:
            now = asyncio.get_running_loop().time()
            timings.record("create_response", now - response_started)
            timings.record("create_total", now - create_started)
            timings.finish("TIMEOUT")
            firewall.disarm()
            blocked = _unexpected_posts(firewall)
            if firewall.attempt_allowed:
                guard.unknown()
                return RunResult(
                    "UNKNOWN",
                    "创建请求已经发出，但没有观察到确定结果" + blocked,
                    removed_audiences=tuple(removed),
                    fulfilled_quantity=fulfilled_quantity,
                )
            guard.ready()
            return RunResult("FAILED", "没有创建请求被放行" + blocked)
        now = asyncio.get_running_loop().time()
        timings.record("create_response", now - response_started)
        timings.record("create_total", now - create_started)
        timings.finish(result.code or ("SUCCESS" if result.success else "NO_CODE"))
        firewall.disarm()

        if result.success:
            order_id = result.order_id
            guard.created(order_id)
            used_ids = tuple(person.masked_id for person in selected_people)
            removed.extend(item for item in used_ids if item not in removed)
            fulfilled_quantity += selected_ticket_count
            remaining = config.purchase.quantity - fulfilled_quantity
            details = [
                (
                    f"订单已创建（尝试 {attempt} 次）；"
                    f"{prepared.summary.describe()}；使用 {len(selected_people)} 个证件"
                )
            ]
            if result.order_number:
                details.append(f"订单号：{result.order_number}")
            else:
                details.append("订单号：官方未返回，请在票星球待支付订单中核对")
            if result.payment_deadline_ms:
                deadline = datetime.fromtimestamp(
                    result.payment_deadline_ms / 1000,
                    timezone(timedelta(hours=8)),
                )
                details.append(
                    f"支付截止：{deadline:%Y-%m-%d %H:%M:%S}（北京时间）"
                )
            if result.unpaid_transaction_count > 1:
                details.append(
                    f"官方返回 {result.unpaid_transaction_count} 个待支付交易，"
                    "请在票星球逐一核对"
                )
            if remaining:
                details.append(
                    f"处理本单后再次启动绑定，可继续等待剩余 {remaining} 张"
                )
            return RunResult(
                "CREATED",
                "\n".join(details),
                order_id=order_id,
                removed_audiences=tuple(removed),
                fulfilled_quantity=fulfilled_quantity,
            )

        action = create_failure_action(result)
        diagnostic = _create_diagnostic(result)
        log.info("创建请求第 %s 次失败：%s", attempt, diagnostic)
        if action == "UNKNOWN":
            guard.unknown()
            await _save_failure(site, config, "create-failed")
            return RunResult(
                "UNKNOWN",
                f"创建结果无法确定：{diagnostic}{_unexpected_posts(firewall)}",
                removed_audiences=tuple(removed),
                fulfilled_quantity=fulfilled_quantity,
            )

        guard.ready()
        if action == "NEEDS_LOGIN":
            return RunResult(
                "NEEDS_LOGIN",
                f"风控状态已失效，订单明确未创建：{diagnostic}",
                removed_audiences=tuple(removed),
                fulfilled_quantity=fulfilled_quantity,
            )
        if action == "FAILED":
            await _save_failure(site, config, "create-failed")
            return RunResult(
                "FAILED",
                f"创建请求被明确拒绝：{diagnostic}",
                removed_audiences=tuple(removed),
                fulfilled_quantity=fulfilled_quantity,
            )

        recovery = action
        if action == "REMOVE_AUDIENCE":
            reported = find_already_purchased_ids(result.message or "")
            purchased = match_configured_ids(
                reported, tuple(person.masked_id for person in selected_people)
            )
            if not purchased:
                await _save_failure(site, config, "create-failed")
                return RunResult(
                    "FAILED",
                    f"官方返回证件已购，但无法匹配配置观演人：{diagnostic}",
                    removed_audiences=tuple(removed),
                    fulfilled_quantity=fulfilled_quantity,
                )
            removed.extend(item for item in purchased if item not in removed)
            per_order = len(selected_people) == 1 and selected_ticket_count > 1
            completed = ticket_quantity if per_order else len(purchased)
            fulfilled_quantity += completed
            ticket_quantity -= completed
            people = tuple(person for person in people if person.masked_id not in purchased)
            if ticket_quantity <= 0:
                return RunResult(
                    "COMPLETE",
                    "配置目标对应的证件已购买，本次不再创建订单",
                    removed_audiences=tuple(removed),
                    fulfilled_quantity=fulfilled_quantity,
                )
            recovery = "RESELECT"

        if attempt >= MAX_CREATE_ATTEMPTS:
            await _save_failure(site, config, "create-failed")
            return RunResult(
                "FAILED",
                (
                    f"连续 {MAX_CREATE_ATTEMPTS} 次创建请求被明确拒绝；"
                    f"最后一次：{diagnostic}"
                ),
                removed_audiences=tuple(removed),
                fulfilled_quantity=fulfilled_quantity,
            )

        timings.begin(attempt + 1)
        try:
            if config.project.support_seat_picking:
                if seat_source is None:
                    raise RuntimeError("选座库存状态无效")
                if recovery == "REBUILD":

                    async def rebuild_map() -> None:
                        await site.open_purchase()
                        await site.open_seat_map()

                    seat_selection = await _parallel_result(
                        seat_source.refresh(ticket_quantity),
                        _measure(timings, "seat_page", rebuild_map()),
                    )
                    prepared = await _prepare_seat_order(
                        site, seat_selection, people, timings=timings
                    )
                else:
                    seat_selection = await _parallel_result(
                        seat_source.refresh(ticket_quantity),
                        _measure(timings, "seat_page", site.reopen_seat_map()),
                    )
                    prepared = await _prepare_seat_order(
                        site,
                        seat_selection,
                        people,
                        timings=timings,
                        reselect=True,
                    )
                selected_people = prepared.audiences
                selected_ticket_count = len(seat_selection.candidates)
            else:
                if general_source is None:
                    raise RuntimeError("票档库存状态无效")
                general_selection = await _parallel_result(
                    _measure(
                        timings,
                        "general_inventory",
                        general_source.refresh(ticket_quantity),
                    ),
                    site.open_purchase(),
                )
                prepared = await site.prepare_general_order(
                    general_selection, people
                )
                selected_people = prepared.audiences
                selected_ticket_count = general_selection.quantity
        except AuthenticationRequired:
            timings.finish("RECOVER_NEEDS_LOGIN")
            return RunResult(
                "NEEDS_LOGIN",
                "冲突恢复时登录状态已失效",
                removed_audiences=tuple(removed),
                fulfilled_quantity=fulfilled_quantity,
            )
        except InventoryUnavailable:
            timings.finish("RECOVER_RESTOCK")
            return RunResult(
                "RESTOCK",
                "冲突后刷新实时库存，当前已无可售票",
                removed_audiences=tuple(removed),
                fulfilled_quantity=fulfilled_quantity,
            )
        except Exception as exc:
            timings.finish("RECOVER_FAILED")
            await _save_failure(site, config, "recover-failed")
            return RunResult(
                "FAILED",
                f"冲突恢复失败：{exc}",
                removed_audiences=tuple(removed),
                fulfilled_quantity=fulfilled_quantity,
            )


async def _prepare_seat_order(
    site: PurchasePage,
    selection: SeatSelection,
    audiences: tuple[AudienceConfig, ...],
    *,
    timings: RunTimings,
    reselect: bool = False,
) -> PreparedOrder:
    started = asyncio.get_running_loop().time()
    try:
        select = reselect_seats if reselect else select_ready_seats
        confirm = await select(site, selection)
    finally:
        timings.record("seat_map", asyncio.get_running_loop().time() - started)
    return await site.prepare_order(selection, audiences, confirm)


async def _measure(
    timings: RunTimings,
    stage: str,
    operation: Awaitable[T],
) -> T:
    started = asyncio.get_running_loop().time()
    try:
        return await operation
    finally:
        timings.record(stage, asyncio.get_running_loop().time() - started)


async def _parallel_result(
    result_coro,
    side_coro,
    *,
    finish_side_on_unavailable: bool = False,
):
    result = asyncio.create_task(result_coro)
    side = asyncio.create_task(side_coro)
    try:
        value, _ = await asyncio.gather(result, side)
        return value
    except InventoryUnavailable:
        result.cancel()
        if not finish_side_on_unavailable:
            side.cancel()
        await asyncio.gather(side, return_exceptions=True)
        raise
    except BaseException:
        result.cancel()
        side.cancel()
        await asyncio.gather(result, side, return_exceptions=True)
        raise


async def check_login(config: AccountRunConfig, context) -> bool:
    page = await context.new_page()
    site = PurchasePage(page, config)
    try:
        await AuthGuard(site).ensure()
        return True
    except AuthenticationRequired:
        return False
    finally:
        with suppress(Exception):
            await page.close()


async def _save_failure(
    site: PurchasePage, config: AccountRunConfig, name: str
) -> None:
    with suppress(Exception):
        directory = (
            config.browser.profile_dir.parent / "artifacts" / config.state_path.stem
        )
        await save_screenshot(site.page, directory, name)


def _unexpected_posts(firewall: OrderFirewall) -> str:
    if not firewall.unexpected_posts:
        return ""
    return "；已拦截未识别 POST：" + "、".join(sorted(firewall.unexpected_posts))


def _create_diagnostic(result: CreateResult) -> str:
    return (
        f"HTTP={result.http_status} code={result.code or '无'} "
        f"subCode={result.sub_code or '无'} message={result.message or '无'}"
    )
