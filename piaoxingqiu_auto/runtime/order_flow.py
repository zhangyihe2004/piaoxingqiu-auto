"""单账号库存选择与 cart/v1 下单工作流。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from typing import TypeVar

from playwright.async_api import Page

from piaoxingqiu_auto.domain.models import AccountRunConfig
from piaoxingqiu_auto.domain.outcomes import RunResult
from piaoxingqiu_auto.domain.seating import SeatSelection
from piaoxingqiu_auto.platform.auth import AuthGuard, AuthenticationRequired
from piaoxingqiu_auto.platform.cart import CartClient, CartOrder, CartRejected
from piaoxingqiu_auto.platform.booking import PurchasePage
from piaoxingqiu_auto.platform.inventory import (
    GeneralAdmissionSelection,
    GeneralAdmissionInventory,
    Inventory,
    InventoryBootstrap,
    InventoryUnavailable,
    StaticInventoryUnavailable,
)
from piaoxingqiu_auto.platform.order_guard import OrderFirewall, PersistentOrderGuard
from piaoxingqiu_auto.platform.order_response import (
    CreateResponseWatcher,
    CreateResult,
    create_failure_action,
    find_already_purchased_ids,
    match_configured_ids,
)
from piaoxingqiu_auto.platform.sale_gate import SaleGate, SaleUnavailable
from piaoxingqiu_auto.runtime.browser import blank_page, save_screenshot
from piaoxingqiu_auto.runtime.timing import RunTimings


log = logging.getLogger("piaoxingqiu.auto")
MAX_CREATE_ATTEMPTS = 10
MAX_PREORDER_ATTEMPTS = 3
CREATE_ROUTE = "**/trade/buyer/order/cart/v1/create_order*"
T = TypeVar("T")


async def run_account(
    config: AccountRunConfig,
    context,
    *,
    prewarm: bool,
    trace_label: str | None = None,
    page: Page | None = None,
    auth_headers: dict[str, str] | None = None,
    runtime_cache: dict[str, object] | None = None,
    execution_gate: asyncio.Semaphore | None = None,
) -> RunResult:
    """执行一个账号的一次自动抢票生命周期，最多创建一个订单。"""
    if not config.create_order:
        return RunResult("DISABLED", "全局 create_order_enabled 尚未开启")
    page = page or await blank_page(context)
    firewall = OrderFirewall()
    watcher = CreateResponseWatcher()
    route_handler = firewall.route
    response_handler = watcher.handle
    await page.route(CREATE_ROUTE, route_handler)
    page.on("response", response_handler)
    slot_acquired = False

    async def acquire_execution() -> None:
        nonlocal slot_acquired
        if execution_gate is not None and not slot_acquired:
            await execution_gate.acquire()
            slot_acquired = True
            log.info("%s 获得创建执行权", trace_label or config.project.name)

    try:
        return await _run_account(
            config,
            page,
            firewall,
            watcher,
            prewarm=prewarm,
            trace_label=trace_label,
            auth_headers=auth_headers,
            runtime_cache=runtime_cache,
            acquire_execution=acquire_execution,
        )
    finally:
        if slot_acquired:
            execution_gate.release()
        with suppress(Exception):
            page.remove_listener("response", response_handler)
        with suppress(Exception):
            await page.unroute(CREATE_ROUTE, route_handler)


async def _run_account(
    config: AccountRunConfig,
    page: Page,
    firewall: OrderFirewall,
    watcher: CreateResponseWatcher,
    *,
    prewarm: bool,
    trace_label: str | None,
    auth_headers: dict[str, str] | None,
    runtime_cache: dict[str, object] | None,
    acquire_execution: Callable[[], Awaitable[None]],
) -> RunResult:
    timings = RunTimings(trace_label or config.project.name)
    site = PurchasePage(page, config, timings.record)
    guard = PersistentOrderGuard(config.state_path, config.plan_key)
    guard.require_ready()
    auth = AuthGuard(site, auth_headers)
    try:
        await auth.ensure()
    except AuthenticationRequired:
        return RunResult("NEEDS_LOGIN", "登录状态已失效")

    cart = CartClient(site, auth, runtime_cache)
    seat_source: Inventory | None = None
    general_source: GeneralAdmissionInventory | None = None
    people = config.purchase.audiences
    ticket_quantity = config.purchase.quantity
    removed: list[str] = []
    fulfilled_quantity = 0

    try:
        if config.project.support_seat_picking:
            seat_source, selection = await _prepare_seat_selection(
                site,
                auth,
                cart,
                timings,
                ticket_quantity,
                prewarm,
                acquire_execution,
            )
            selection, prepared = await _prepare_seats(
                cart,
                seat_source,
                selection,
                people,
                ticket_quantity,
                site,
                auth,
            )
            selected_ticket_count = len(selection.candidates)
        else:
            if prewarm:
                await cart.warm(people)
                await _wait_sale(site, auth)
            await acquire_execution()
            general_source = GeneralAdmissionInventory.open(site, auth)
            timings.begin(1)
            load = (
                general_source.wait_available(ticket_quantity)
                if prewarm
                else general_source.refresh(ticket_quantity)
            )
            selection, _ = await asyncio.gather(
                _measure(timings, "general_inventory", load),
                site.prepare_booking(),
            )
            selection, prepared = await _prepare_general(
                cart,
                general_source,
                selection,
                people,
                ticket_quantity,
                site,
                auth,
            )
            selected_ticket_count = selection.quantity
        selected_people = prepared.audiences
    except AuthenticationRequired:
        timings.finish("NEEDS_LOGIN")
        return RunResult("NEEDS_LOGIN", "登录状态已失效")
    except InventoryUnavailable:
        timings.finish("RESTOCK")
        return RunResult(
            "RESTOCK",
            "配置票档当前没有可售库存",
            reuse_page=await site.reusable_task_page(),
        )
    except SaleUnavailable:
        timings.finish("RESTOCK")
        return RunResult("RESTOCK", "当前场次尚不可执行")
    except StaticInventoryUnavailable:
        timings.finish("STATIC_UNAVAILABLE")
        return RunResult(
            "RESTOCK",
            "静态座位资源尚未下发",
            reuse_page=await site.reusable_task_page(),
        )
    except CartRejected as exc:
        result = _rejected_result(exc)
        action = create_failure_action(result)
        timings.finish(f"PRE_ORDER_{result.code or 'FAILED'}")
        return RunResult(
            "NEEDS_LOGIN" if action == "NEEDS_LOGIN" else "FAILED",
            f"预下单被明确拒绝：{_create_diagnostic(result)}",
        )
    except Exception:
        timings.finish("PREPARE_FAILED")
        await _save_failure(site, config, "prepare-failed")
        raise

    if firewall.blocked_requests:
        timings.finish("UNEXPECTED_CREATE")
        raise RuntimeError("准备阶段出现意外创建请求，已拦截并停止")

    attempt = 0
    risk_rebuilds = 0
    while True:
        attempt += 1

        try:
            result = await _create_once(
                cart,
                prepared,
                firewall,
                watcher,
                guard,
                timings,
                config.browser.timeout_ms / 1000,
            )
        except TimeoutError:
            return RunResult(
                "UNKNOWN",
                "创建请求已经发出，但没有观察到确定响应，请人工核对待支付订单",
                removed_audiences=tuple(removed),
                fulfilled_quantity=fulfilled_quantity,
            )
        if result is None:
            return RunResult(
                "FAILED",
                "创建请求未发出",
                removed_audiences=tuple(removed),
                fulfilled_quantity=fulfilled_quantity,
            )

        timings.finish(result.code or ("SUCCESS" if result.success else "NO_CODE"))
        if result.success:
            guard.created(result.order_id)
            used_ids = tuple(person.masked_id for person in selected_people)
            removed.extend(item for item in used_ids if item not in removed)
            fulfilled_quantity += selected_ticket_count
            return _created_result(
                result,
                prepared,
                attempt,
                removed,
                fulfilled_quantity,
                config.purchase.quantity,
            )

        action = create_failure_action(result)
        diagnostic = _create_diagnostic(result)
        log.info("创建请求第 %s 次失败：%s", attempt, diagnostic)
        if action == "UNKNOWN":
            guard.unknown()
            await _save_failure(site, config, "create-failed")
            return RunResult(
                "UNKNOWN",
                f"创建结果无法确定：{diagnostic}",
                removed_audiences=tuple(removed),
                fulfilled_quantity=fulfilled_quantity,
            )

        guard.ready()
        if action == "NEEDS_LOGIN":
            if risk_rebuilds:
                return RunResult(
                    "NEEDS_LOGIN",
                    f"重建风控环境后仍被拒绝：{diagnostic}",
                    removed_audiences=tuple(removed),
                    fulfilled_quantity=fulfilled_quantity,
                )
            risk_rebuilds += 1
            recovery = "REBUILD"
        else:
            recovery = action
        if action == "FAILED":
            await _save_failure(site, config, "create-failed")
            return RunResult(
                "FAILED",
                f"创建请求被明确拒绝：{diagnostic}",
                removed_audiences=tuple(removed),
                fulfilled_quantity=fulfilled_quantity,
            )

        if action == "RESELECT" and seat_source is not None:
            seat_source.reject(selection)

        if action == "REMOVE_AUDIENCE":
            purchased = match_configured_ids(
                find_already_purchased_ids(result.message or ""),
                tuple(person.masked_id for person in selected_people),
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
            people = tuple(
                person for person in people if person.masked_id not in purchased
            )
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
                f"连续 {MAX_CREATE_ATTEMPTS} 次创建请求被明确拒绝；"
                f"最后一次：{diagnostic}",
                removed_audiences=tuple(removed),
                fulfilled_quantity=fulfilled_quantity,
            )

        timings.begin(attempt + 1)
        try:
            if config.project.support_seat_picking:
                if seat_source is None:
                    raise RuntimeError("选座库存状态无效")
                if recovery == "REBUILD":
                    await site.open_purchase()
                selection = await seat_source.refresh(ticket_quantity)
                selection, prepared = await _prepare_seats(
                    cart,
                    seat_source,
                    selection,
                    people,
                    ticket_quantity,
                    site,
                    auth,
                )
                selected_ticket_count = len(selection.candidates)
            else:
                if general_source is None:
                    raise RuntimeError("票档库存状态无效")
                if recovery == "REBUILD":
                    await site.open_purchase()
                selection = await general_source.refresh(ticket_quantity)
                selection, prepared = await _prepare_general(
                    cart,
                    general_source,
                    selection,
                    people,
                    ticket_quantity,
                    site,
                    auth,
                )
                selected_ticket_count = selection.quantity
            selected_people = prepared.audiences
        except CartRejected as exc:
            rejected = _rejected_result(exc)
            action = create_failure_action(rejected)
            timings.finish(f"RECOVER_PRE_ORDER_{rejected.code or 'FAILED'}")
            return RunResult(
                "NEEDS_LOGIN" if action == "NEEDS_LOGIN" else "FAILED",
                f"冲突恢复时预下单被拒绝：{_create_diagnostic(rejected)}",
                removed_audiences=tuple(removed),
                fulfilled_quantity=fulfilled_quantity,
            )
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


async def _prepare_seat_selection(
    site: PurchasePage,
    auth: AuthGuard,
    cart: CartClient,
    timings: RunTimings,
    quantity: int,
    prewarm: bool,
    acquire_execution: Callable[[], Awaitable[None]],
) -> tuple[Inventory, SeatSelection]:
    source: Inventory | None = None
    bootstrap: InventoryBootstrap | None = None
    if prewarm:
        await cart.warm(site.config.purchase.audiences)
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
                    source = (
                        await static_task
                        if static_task.done()
                        else await bootstrap.wait_static(
                            remaining_seconds=sale.remaining_seconds
                        )
                    )
                finally:
                    if not static_task.done():
                        static_task.cancel()
                        await asyncio.gather(static_task, return_exceptions=True)
        bootstrap = bootstrap or InventoryBootstrap.open(site, auth)

    await acquire_execution()
    timings.begin(1)

    async def load():
        current = source or (
            await bootstrap.wait_static()
            if bootstrap is not None
            else await Inventory.open(site, auth)
        )
        selection = (
            await current.wait_available(quantity)
            if prewarm
            else await current.refresh(quantity)
        )
        return current, selection

    (source, selection), _ = await asyncio.gather(load(), site.prepare_booking())
    return source, selection


async def _prepare_seats(
    cart: CartClient,
    source: Inventory,
    selection: SeatSelection,
    people,
    quantity: int,
    site: PurchasePage,
    auth: AuthGuard,
) -> tuple[SeatSelection, CartOrder]:
    return await _prepare_cart(
        selection,
        lambda current: cart.prepare_seats(current, source, people),
        lambda: source.refresh(quantity),
        source.reject,
        site,
        auth,
    )


async def _prepare_general(
    cart: CartClient,
    source: GeneralAdmissionInventory,
    selection: GeneralAdmissionSelection,
    people,
    quantity: int,
    site: PurchasePage,
    auth: AuthGuard,
) -> tuple[GeneralAdmissionSelection, CartOrder]:
    return await _prepare_cart(
        selection,
        lambda current: cart.prepare_general(current, people),
        lambda: source.refresh(quantity),
        None,
        site,
        auth,
    )


async def _prepare_cart(
    selection: T,
    prepare: Callable[[T], Awaitable[CartOrder]],
    refresh: Callable[[], Awaitable[T]],
    reject: Callable[[T], None] | None,
    site: PurchasePage,
    auth: AuthGuard,
) -> tuple[T, CartOrder]:
    risk_rebuilt = False
    for attempt in range(MAX_PREORDER_ATTEMPTS):
        await auth.require_recent()
        try:
            return selection, await prepare(selection)
        except CartRejected as exc:
            if attempt + 1 == MAX_PREORDER_ATTEMPTS:
                raise
            action = create_failure_action(_rejected_result(exc))
            if action == "RESELECT":
                if reject is not None:
                    reject(selection)
            elif action == "REBUILD":
                await site.open_purchase()
            elif action == "NEEDS_LOGIN" and not risk_rebuilt:
                risk_rebuilt = True
                await site.open_purchase()
            else:
                raise
            selection = await refresh()
    raise AssertionError("unreachable")


async def _wait_sale(site: PurchasePage, auth: AuthGuard) -> None:
    gate = SaleGate(site)
    sale = await gate.fetch()
    if not sale.on_sale:
        sale = await gate.wait_until_prewarm(sale, auth)
        if not sale.on_sale:
            await site.prepare_booking()
            await gate.wait_until_sale(sale, auth)


async def _create_once(
    cart: CartClient,
    prepared: CartOrder,
    firewall: OrderFirewall,
    watcher: CreateResponseWatcher,
    guard: PersistentOrderGuard,
    timings: RunTimings,
    timeout: float,
) -> CreateResult | None:
    firewall.arm_once()
    guard.submitting()
    started = asyncio.get_running_loop().time()
    try:
        await cart.create(prepared.payload)
    except Exception:
        if not firewall.attempt_allowed:
            firewall.disarm()
            guard.ready()
            raise
    if not firewall.attempt_allowed:
        firewall.disarm()
        guard.ready()
        timings.record("create_total", asyncio.get_running_loop().time() - started)
        return None
    try:
        result = await watcher.wait(timeout)
    except TimeoutError:
        guard.unknown()
        timings.record("create_total", asyncio.get_running_loop().time() - started)
        timings.finish("TIMEOUT")
        raise
    finally:
        firewall.disarm()
    timings.record("create_total", asyncio.get_running_loop().time() - started)
    return result


def _created_result(
    result: CreateResult,
    prepared: CartOrder,
    attempt: int,
    removed: list[str],
    fulfilled: int,
    target: int,
) -> RunResult:
    details = [
        f"订单已创建（尝试 {attempt} 次）；{prepared.summary.describe()}；"
        f"使用 {len(prepared.audiences)} 个证件"
    ]
    details.append(
        f"订单号：{result.order_number}"
        if result.order_number
        else "订单号：官方未返回，请在票星球待支付订单中核对"
    )
    if result.payment_deadline_ms:
        deadline = datetime.fromtimestamp(
            result.payment_deadline_ms / 1000,
            timezone(timedelta(hours=8)),
        )
        details.append(f"支付截止：{deadline:%Y-%m-%d %H:%M:%S}（北京时间）")
    if result.unpaid_transaction_count > 1:
        details.append(
            f"官方返回 {result.unpaid_transaction_count} 个待支付交易，"
            "请在票星球逐一核对"
        )
    if remaining := target - fulfilled:
        details.append(f"处理本单后再次启动绑定，可继续等待剩余 {remaining} 张")
    return RunResult(
        "CREATED",
        "\n".join(details),
        order_id=result.order_id,
        removed_audiences=tuple(removed),
        fulfilled_quantity=fulfilled,
    )


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
    site: PurchasePage,
    config: AccountRunConfig,
    name: str,
) -> None:
    with suppress(Exception):
        directory = (
            config.browser.profile_dir.parent / "artifacts" / config.state_path.stem
        )
        await save_screenshot(site.page, directory, name)


def _create_diagnostic(result: CreateResult) -> str:
    return (
        f"HTTP={result.http_status} code={result.code or '无'} "
        f"subCode={result.sub_code or '无'} message={result.message or '无'}"
    )


def _rejected_result(error: CartRejected) -> CreateResult:
    return CreateResult(
        False,
        None,
        None,
        None,
        0,
        200,
        error.code,
        error.sub_code,
        error.message,
    )
