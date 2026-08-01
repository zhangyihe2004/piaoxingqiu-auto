from __future__ import annotations

import asyncio
import base64
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypeVar
from urllib.parse import urlencode

import geobuf

from piaoxingqiu_auto.platform.auth import AuthGuard, request_context
from piaoxingqiu_auto.platform.api import BASE_URL, PxqClient, PxqError, is_success_payload
from piaoxingqiu_auto.domain.seating import (
    Candidate,
    Seat,
    SeatSelection,
    select_groups,
)
from piaoxingqiu_auto.domain.position import (
    PositionScorer,
    Venue,
    index_seats,
    venue_from_features,
)

if TYPE_CHECKING:
    from piaoxingqiu_auto.platform.booking import PurchasePage


BATCH_SIZE = 25
# 单账号库存请求和全局静态资源下载的独立上限。
DYNAMIC_CONCURRENCY = 4
DOWNLOAD_CONCURRENCY = 8
FAST_STOCK_POLL_SECONDS = 0.25
FAST_STOCK_WINDOW_SECONDS = 5.0
STOCK_POLL_SECONDS = 1.0
STOCK_WAIT_SECONDS = 60.0
STATIC_UNAVAILABLE_CODE = "22024036"
STATIC_LAYOUT_CACHE_SIZE = 16
DECODED_RESOURCE_CACHE_SIZE = 64
SELECTION_QUEUE_SIZE = 10
T = TypeVar("T")


class InventoryUnavailable(RuntimeError):
    pass


class StaticInventoryUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class StaticLayout:
    resources: dict[str, str]
    plan_zones: dict[str, frozenset[str]]
    zone_aliases: dict[str, tuple[str, str]]
    venue_url: str | None


_STATIC_LAYOUTS: OrderedDict[str, StaticLayout] = OrderedDict()
_STATIC_LOADS: dict[str, asyncio.Task[StaticLayout]] = {}
_STATIC_PREWARMS: set[tuple[str, tuple[str, ...]]] = set()
_DECODED_RESOURCES: OrderedDict[str, tuple[Seat, ...]] = OrderedDict()
_DECODE_LOADS: dict[str, asyncio.Task[tuple[Seat, ...]]] = {}
_VENUES: OrderedDict[str, Venue] = OrderedDict()
_VENUE_LOADS: dict[str, asyncio.Task[Venue]] = {}
_DOWNLOAD_SEMAPHORE = asyncio.Semaphore(DOWNLOAD_CONCURRENCY)


async def prewarm_static(
    client: PxqClient,
    show_id: str,
    session_id: str,
    plan_ids: tuple[str, ...],
) -> None:
    """任务级加载 static；元数据就绪后继续预解码相关看台。"""
    key = (
        f"{BASE_URL}/show/pub/v5/show/{show_id}/session/{session_id}/seating/static"
    )

    async def fetch() -> StaticLayout:
        try:
            return _static_layout(await client.seating_static(show_id, session_id))
        except PxqError as exc:
            if exc.status_code == int(STATIC_UNAVAILABLE_CODE):
                raise StaticInventoryUnavailable("静态座位资源尚未下发") from exc
            raise

    layout = await _load_static_layout(key, fetch)
    preload_key = key, tuple(sorted(plan_ids))
    if preload_key in _STATIC_PREWARMS:
        return
    _STATIC_PREWARMS.add(preload_key)
    preload = asyncio.create_task(
        _decode_zones(
            layout.resources,
            _configured_zones(layout, plan_ids),
            client.download,
        )
    )
    preload.add_done_callback(
        lambda completed: _complete_preload(preload_key, completed)
    )


def _complete_preload(
    key: tuple[str, tuple[str, ...]],
    task: asyncio.Task,
) -> None:
    with suppress(asyncio.CancelledError):
        if task.exception() is None:
            return
    _STATIC_PREWARMS.discard(key)


def _cache_get(cache: OrderedDict[str, T], key: str) -> T | None:
    value = cache.pop(key, None)
    if value is not None:
        cache[key] = value
    return value


def _cache_put(
    cache: OrderedDict[str, T],
    key: str,
    value: T,
    limit: int,
) -> None:
    cache.pop(key, None)
    cache[key] = value
    while len(cache) > limit:
        cache.popitem(last=False)


@dataclass(frozen=True)
class GeneralAdmissionSelection:
    plan: str
    plan_id: str
    units: int
    price: float
    has_activity: bool
    combo_items: tuple[tuple[str, int, float], ...]

    @property
    def ticket_count(self) -> int:
        return self.units * (sum(item[1] for item in self.combo_items) or 1)


@dataclass(frozen=True)
class PlanInventory:
    caps: dict[str, int]
    prices: dict[str, float]
    combos: tuple[dict[str, Any], ...]
    has_activity: bool


@dataclass
class GeneralAdmissionInventory:
    site: PurchasePage
    endpoint: str
    common: dict[str, str]
    headers: dict[str, str]
    plan_names: tuple[str, ...]
    plan_ids: tuple[str, ...]

    @classmethod
    def open(
        cls,
        site: PurchasePage,
        auth: AuthGuard,
    ) -> GeneralAdmissionInventory:
        show_id, session_id = site.booking_ids
        origin = site.origin
        if not auth.headers:
            raise RuntimeError("库存查询缺少已验证的登录状态")
        return cls(
            site=site,
            endpoint=(
                f"{origin}/cyy_gatewayapi/show/pub/v5/show/{show_id}/session/"
                f"{session_id}/seat_plans"
            ),
            common=request_context(auth.headers),
            headers=auth.headers,
            plan_names=site.config.purchase.plans,
            plan_ids=site.config.purchase.plan_ids,
        )

    async def refresh(self, quantity: int) -> GeneralAdmissionSelection:
        query = dict(self.common, source="FROM_QUICK_ORDER", src="WEB")
        response = await self.site.page.context.request.get(
            _url(self.endpoint, query), headers=self.headers
        )
        if not response.ok:
            raise RuntimeError(f"票档库存接口返回 HTTP {response.status}")
        data = _response_data(await response.json(), "票档库存接口")
        if not isinstance(data, dict) or not isinstance(data.get("seatPlans"), list):
            raise RuntimeError("票档库存接口缺少 seatPlans 数组")
        live = {
            str(item.get("seatPlanId") or ""): item
            for item in data["seatPlans"]
            if isinstance(item, dict)
        }
        options: list[GeneralAdmissionSelection] = []
        for name, plan_id in zip(self.plan_names, self.plan_ids):
            item = live.get(plan_id)
            if not item or not item.get("saleStarted"):
                continue
            can_buy = int(item.get("canBuyCount") or 0)
            combo = (
                bool(item.get("isCombo"))
                or item.get("seatPlanCategory") == "COMBO"
            ) and item.get("seatPlanCategory") != "FREE_COMBO"
            combo_items = _fixed_combo_items(item) if combo else ()
            units = min(can_buy, quantity)
            if units > 0:
                options.append(
                    GeneralAdmissionSelection(
                        plan=name,
                        plan_id=plan_id,
                        units=units,
                        price=float(item.get("originalPrice") or 0),
                        has_activity=bool(item.get("hasActivity")),
                        combo_items=combo_items,
                    )
                )
        if not options:
            raise InventoryUnavailable("配置票档当前均没有可售票")
        full = next((option for option in options if option.units == quantity), None)
        return full or max(options, key=lambda option: option.units)

    async def wait_available(self, quantity: int) -> GeneralAdmissionSelection:
        return await _wait_inventory(lambda: self.refresh(quantity))


@dataclass
class InventoryBootstrap:
    site: PurchasePage
    endpoint: str
    plan_endpoint: str
    static_url: str
    common: dict[str, str]
    headers: dict[str, str]
    plan_names: tuple[str, ...]
    plan_ids: tuple[str, ...]

    @classmethod
    def open(
        cls,
        site: PurchasePage,
        auth: AuthGuard,
    ) -> InventoryBootstrap:
        show_id, session_id = site.booking_ids
        origin = site.origin
        headers = auth.headers
        if not headers:
            raise RuntimeError("库存预热缺少已验证的登录状态")
        common = request_context(headers)
        root = f"{origin}/cyy_gatewayapi/show"
        plan_names = site.config.purchase.plans
        plan_ids = site.config.purchase.plan_ids
        static_url = _url(
            f"{root}/pub/v5/show/{show_id}/session/{session_id}/seating/static",
            common,
        )
        return cls(
            site=site,
            endpoint=f"{root}/buyer/v5/show/{show_id}/session/{session_id}/seating/dynamic",
            plan_endpoint=f"{root}/pub/v5/show/{show_id}/session/{session_id}/seat_plans",
            static_url=static_url,
            common=common,
            headers=headers,
            plan_names=plan_names,
            plan_ids=plan_ids,
        )

    async def activate(self, *, preload: bool = False) -> Inventory:
        layout = await _load_static_layout(
            self.static_url.partition("?")[0],
            lambda: _fetch_static_layout(self.site, self.static_url),
        )
        return await self._inventory(layout, preload)

    async def _inventory(
        self,
        layout: StaticLayout,
        preload: bool,
    ) -> Inventory:
        zone_ids = _configured_zones(layout, self.plan_ids)
        if self.site.config.purchase.stand_names:
            matched = _match_stands(
                layout,
                zone_ids,
                self.site.config.purchase.stand_names,
            )
            if matched is not None:
                zone_ids = matched
        position_priority = self.site.config.purchase.position_priority
        venue = (
            await _load_venue(self.site, layout.venue_url)
            if position_priority
            else None
        )
        preload_zones = set(zone_ids) if preload else set()
        if position_priority and venue.center is None:
            preload_zones.update(layout.resources)
        zones = (
            await _decode_zones(
                layout.resources,
                preload_zones,
                lambda url: _download_resource(self.site, url),
            )
            if preload_zones
            else {}
        )
        return Inventory(
            site=self.site,
            endpoint=self.endpoint,
            plan_endpoint=self.plan_endpoint,
            common=self.common,
            headers=self.headers,
            plan_names=self.plan_names,
            plan_ids=self.plan_ids,
            resources=layout.resources,
            zone_ids=frozenset(zone_ids),
            zones=zones,
            venue=venue,
        )

@dataclass
class Inventory:
    site: PurchasePage
    endpoint: str
    plan_endpoint: str
    common: dict[str, str]
    headers: dict[str, str]
    plan_names: tuple[str, ...]
    plan_ids: tuple[str, ...]
    resources: dict[str, str]
    zone_ids: frozenset[str]
    zones: dict[str, tuple[Seat, ...]]
    venue: Venue | None
    position: PositionScorer | None = None
    selection_queue: tuple[SeatSelection, ...] = ()
    rejected_seat_ids: set[str] = field(default_factory=set)
    plan_prices: dict[str, float] = field(default_factory=dict)
    combo_plans: tuple[dict[str, Any], ...] = ()
    has_activity: bool = False

    async def refresh(
        self,
        quantity: int,
        blocked_seat_ids: frozenset[str] = frozenset(),
    ) -> SeatSelection:
        configured_zones = self.zone_ids - self.zones.keys()
        records, plan_inventory, decoded = await asyncio.gather(
            _timed(
                self.site,
                "dynamic",
                _fetch_all_dynamic(
                    self.site,
                    self.endpoint,
                    self.common,
                    self.headers,
                    tuple(self.zone_ids),
                    self.plan_ids,
                ),
            ),
            _timed(
                self.site,
                "plan_inventory",
                _fetch_plan_inventory(
                    self.site,
                    self.plan_endpoint,
                    self.common,
                    self.headers,
                    self.plan_ids,
                ),
            ),
            _timed(
                self.site,
                "seat_decode",
                _decode_zones(
                    self.resources,
                    configured_zones,
                    lambda url: _download_resource(self.site, url),
                ),
            ),
        )
        self.zones.update(decoded)
        plan_caps = plan_inventory.caps
        self.plan_prices = plan_inventory.prices
        self.combo_plans = plan_inventory.combos
        self.has_activity = plan_inventory.has_activity
        plan_units = {
            str(plan["seatPlanId"]): max(1, int(plan.get("unitQty") or 1))
            for plan in self.combo_plans
            if plan.get("seatPlanCategory") == "COMBO"
        }
        inventories = {
            (rank, plan_name, plan_id): {
                str(record["zoneConcreteId"]): bits
                for record in records
                if str(record.get("zoneConcreteId") or "") in self.zone_ids
                and (bits := _plan_bits(record, plan_id))
                and any(bits)
            }
            for rank, (plan_name, plan_id) in enumerate(
                zip(self.plan_names, self.plan_ids)
            )
            if plan_caps.get(plan_id, 0) > 0
        }
        live_zone_ids = {
            zone_id for bitsets in inventories.values() for zone_id in bitsets
        }
        if not live_zone_ids:
            raise InventoryUnavailable("配置票档当前均没有可售座位")
        missing = live_zone_ids - self.zones.keys()
        if missing:
            started = asyncio.get_running_loop().time()
            self.zones.update(
                await _decode_zones(
                    self.resources,
                    missing,
                    lambda url: _download_resource(self.site, url),
                )
            )
            self.site.record_timing(
                "seat_decode", asyncio.get_running_loop().time() - started
            )

        started = asyncio.get_running_loop().time()
        available: list[Candidate] = []
        for (rank, plan_name, plan_id), bitsets in inventories.items():
            for zone_id, bits in bitsets.items():
                for seat in self.zones[zone_id]:
                    if _bit_is_set(bits, seat.seat_no):
                        available.append(Candidate(seat, plan_name, plan_id, rank))
        if not available:
            raise RuntimeError("动态库存存在，但未能映射到静态座位")
        if len({candidate.seat.seat_id for candidate in available}) != len(available):
            raise RuntimeError("物理座位与基础票档映射不唯一")
        available = [
            candidate
            for candidate in available
            if candidate.seat.seat_id
            not in self.rejected_seat_ids | blocked_seat_ids
        ]
        if not available:
            raise InventoryUnavailable("刷新后没有未尝试的可售座位")
        candidates = tuple(available)
        if self.position is None and self.venue is not None:
            self.position = PositionScorer(self.venue, self.zones)
        counts = {
            plan_id: sum(candidate.plan_id == plan_id for candidate in candidates)
            for plan_id in self.plan_ids
        }
        selected_quantity = min(
            quantity,
            sum(
                min(plan_caps.get(plan_id, 0), count)
                for plan_id, count in counts.items()
            ),
        )
        queue = next(
            (
                queue
                for current_quantity in range(selected_quantity, 0, -1)
                if (
                    queue := _selection_queue(
                        candidates,
                        current_quantity,
                        plan_caps,
                        plan_units,
                        self.position,
                    )
                )
            ),
            (),
        )
        if not queue:
            raise InventoryUnavailable("当前可售座位不足以组成完整套票")
        self.selection_queue = queue
        self.site.record_timing(
            "seat_score", asyncio.get_running_loop().time() - started
        )
        return queue[0]

    def reject(self, selection: SeatSelection) -> None:
        rejected = {candidate.seat.seat_id for candidate in selection.candidates}
        self.rejected_seat_ids.update(rejected)
        self.selection_queue = tuple(
            queued
            for queued in self.selection_queue
            if rejected.isdisjoint(
                candidate.seat.seat_id for candidate in queued.candidates
            )
        )

    async def wait_available(self, quantity: int) -> SeatSelection:
        return await _wait_inventory(lambda: self.refresh(quantity))


def _selection_queue(
    candidates: tuple[Candidate, ...],
    quantity: int,
    plan_caps: dict[str, int],
    plan_units: dict[str, int],
    position: PositionScorer | None,
) -> tuple[SeatSelection, ...]:
    queue: list[SeatSelection] = []
    seen: set[tuple[tuple[str, str], ...]] = set()

    def append(selection: SeatSelection) -> None:
        key = _selection_key(selection)
        if key not in seen:
            seen.add(key)
            queue.append(selection)

    for group in select_groups(
        candidates,
        quantity,
        plan_caps,
        plan_units,
        limit=SELECTION_QUEUE_SIZE * 10,
        score=position,
    ):
        append(_selection(group.candidates))
        if len(queue) == SELECTION_QUEUE_SIZE:
            break
    return tuple(queue)


def _selection(candidates: tuple[Candidate, ...]) -> SeatSelection:
    return SeatSelection(
        candidates=tuple(
            sorted(
                candidates,
                key=lambda item: (item.seat.zone_id, item.seat.seat_no),
            )
        )
    )


def _selection_key(selection: SeatSelection) -> tuple[tuple[str, str], ...]:
    return tuple(
        sorted(
            (candidate.seat.seat_id, candidate.plan_id)
            for candidate in selection.candidates
        )
    )


async def _wait_inventory(load: Callable[[], Awaitable[T]]) -> T:
    started = asyncio.get_running_loop().time()
    while True:
        try:
            return await load()
        except (InventoryUnavailable, StaticInventoryUnavailable):
            elapsed = asyncio.get_running_loop().time() - started
            if elapsed >= STOCK_WAIT_SECONDS:
                raise
            await asyncio.sleep(_stock_poll_delay(elapsed))


async def _timed(
    site: PurchasePage,
    stage: str,
    operation: Awaitable[T],
) -> T:
    started = asyncio.get_running_loop().time()
    try:
        return await operation
    finally:
        site.record_timing(stage, asyncio.get_running_loop().time() - started)


def _stock_poll_delay(elapsed: float) -> float:
    interval = (
        FAST_STOCK_POLL_SECONDS
        if elapsed < FAST_STOCK_WINDOW_SECONDS
        else STOCK_POLL_SECONDS
    )
    return min(interval, STOCK_WAIT_SECONDS - elapsed)


async def _fetch_all_dynamic(
    site: PurchasePage,
    endpoint: str,
    common: dict[str, str],
    headers: dict[str, str],
    zone_ids: tuple[str, ...],
    plan_ids: tuple[str, ...],
) -> list[dict[str, Any]]:
    semaphore = asyncio.Semaphore(DYNAMIC_CONCURRENCY)

    async def fetch(batch: tuple[str, ...]) -> list[dict[str, Any]]:
        query = dict(common)
        query.update(
            zoneConcreteIds=",".join(batch),
            bizSeatPlanIds=",".join(plan_ids),
        )
        async with semaphore:
            response = await site.page.context.request.get(
                _url(endpoint, query), headers=headers
            )
        if not response.ok:
            raise RuntimeError(f"批量动态座位接口返回 HTTP {response.status}")
        data = _response_data(await response.json(), "批量动态座位接口")
        if not isinstance(data, list):
            raise RuntimeError("批量动态座位接口缺少 data 数组")
        return [item for item in data if isinstance(item, dict)]

    batches = await asyncio.gather(
        *(
            fetch(zone_ids[start : start + BATCH_SIZE])
            for start in range(0, len(zone_ids), BATCH_SIZE)
        )
    )
    return [record for batch in batches for record in batch]


async def _fetch_plan_inventory(
    site: PurchasePage,
    endpoint: str,
    common: dict[str, str],
    headers: dict[str, str],
    plan_ids: tuple[str, ...],
) -> PlanInventory:
    query = dict(common, source="FROM_QUICK_ORDER", src="WEB")
    response = await site.page.context.request.get(
        _url(endpoint, query), headers=headers
    )
    if not response.ok:
        raise RuntimeError(f"票档库存接口返回 HTTP {response.status}")
    data = _response_data(await response.json(), "票档库存接口")
    if not isinstance(data, dict) or not isinstance(data.get("seatPlans"), list):
        raise RuntimeError("票档库存接口缺少 seatPlans 数组")
    plans = tuple(item for item in data["seatPlans"] if isinstance(item, dict))
    wanted = set(plan_ids)
    base = tuple(item for item in plans if str(item.get("seatPlanId") or "") in wanted)
    return PlanInventory(
        caps={
            str(item["seatPlanId"]): int(item.get("canBuyCount") or 0) for item in base
        },
        prices={
            str(item["seatPlanId"]): float(item.get("originalPrice") or 0)
            for item in base
        },
        combos=tuple(
            item
            for item in plans
            if item.get("seatPlanCategory") == "FREE_COMBO"
            or (
                str(item.get("seatPlanId") or "") in wanted
                and item.get("seatPlanCategory") == "COMBO"
            )
        ),
        has_activity=any(item.get("hasActivity") for item in base),
    )


def _plan_bits(record: dict[str, Any], plan_id: str) -> bytes:
    for item in record.get("seatPlanSeatBits", []):
        if isinstance(item, dict) and str(item.get("bizSeatPlanId")) == plan_id:
            value = str(item.get("bitstr") or "")
            return base64.b64decode(value + "=" * (-len(value) % 4))
    return b""


def _fixed_combo_items(plan: dict[str, Any]) -> tuple[tuple[str, int, float], ...]:
    items = tuple(
        (
            str(item.get("stdSeatPlanId") or ""),
            max(1, int(item.get("unitQty") or 1)),
            float(item.get("originalPrice") or 0),
        )
        for item in plan.get("items", [])
        if isinstance(item, dict) and item.get("stdSeatPlanId")
    )
    unit_qty = max(1, int(plan.get("unitQty") or 1))
    if (
        not items
        or sum(count for _, count, _ in items) != unit_qty
        or sum(count * price for _, count, price in items) <= 0
    ):
        raise RuntimeError("固定套票组成与官方 unitQty 不一致")
    return items


def _response_data(payload: Any, label: str) -> Any:
    if not is_success_payload(payload):
        status = payload.get("statusCode") if isinstance(payload, dict) else None
        raise RuntimeError(f"{label}返回异常业务状态（{status or '无状态码'}）")
    if "data" not in payload:
        raise RuntimeError(f"{label}缺少 data")
    return payload["data"]


def _static_data(payload: Any) -> dict[str, Any]:
    if (
        isinstance(payload, dict)
        and str(payload.get("statusCode")) == STATIC_UNAVAILABLE_CODE
    ):
        raise StaticInventoryUnavailable("静态座位资源尚未下发")
    data = _response_data(payload, "静态座位接口")
    if not isinstance(data, dict):
        raise RuntimeError("静态座位接口缺少 data 对象")
    return data


async def _load_static_layout(
    key: str,
    fetch: Callable[[], Awaitable[StaticLayout]],
) -> StaticLayout:
    if layout := _cache_get(_STATIC_LAYOUTS, key):
        return layout
    task = _STATIC_LOADS.get(key)
    if task is None:
        task = asyncio.create_task(fetch())
        _STATIC_LOADS[key] = task
    try:
        layout = await asyncio.shield(task)
    finally:
        if task.done() and _STATIC_LOADS.get(key) is task:
            _STATIC_LOADS.pop(key, None)
    _cache_put(_STATIC_LAYOUTS, key, layout, STATIC_LAYOUT_CACHE_SIZE)
    return layout


async def _fetch_static_layout(
    site: PurchasePage,
    url: str,
) -> StaticLayout:
    response = await site.page.context.request.get(url)
    if response.status in {401, 429, 469}:
        raise RuntimeError(f"静态座位接口触发限制（HTTP {response.status}），已停止")
    if not response.ok:
        raise RuntimeError(f"静态座位接口返回 HTTP {response.status}")
    return _static_layout(_static_data(await response.json()))


def _static_layout(data: dict[str, Any]) -> StaticLayout:
    resources = {
        str(item["zoneConcreteId"]): str(item["url"])
        for item in data.get("staticResList", [])
        if isinstance(item, dict)
        and item.get("dataType") == "ZONE_SEAT_DATA"
        and item.get("zoneConcreteId")
        and item.get("url")
    }
    if not resources:
        raise StaticInventoryUnavailable("静态座位资源尚未下发")
    plan_zones: dict[str, set[str]] = {}
    zone_aliases: dict[str, tuple[str, str]] = {}
    for item in data.get("planZoneList", []):
        if not isinstance(item, dict) or not item.get("seatPlanId"):
            continue
        zones = [
            zone
            for zone in item.get("zoneConcretes", [])
            if isinstance(zone, dict) and zone.get("zoneConcreteId")
        ]
        plan_zones.setdefault(str(item["seatPlanId"]), set()).update(
            str(zone["zoneConcreteId"]) for zone in zones
        )
        for zone in zones:
            zone_id = str(zone["zoneConcreteId"])
            name = str(zone.get("zoneName") or "").strip()
            full_name = str(zone.get("sectorName") or "").strip() + name
            if name:
                zone_aliases[zone_id] = full_name or name, name
    venue_url = next(
        (
            str(item["url"])
            for item in data.get("staticResList", [])
            if isinstance(item, dict)
            and item.get("dataType") == "VENUE_DATA"
            and item.get("url")
        ),
        None,
    )
    return StaticLayout(
        resources,
        {plan_id: frozenset(zones) for plan_id, zones in plan_zones.items()},
        zone_aliases,
        venue_url,
    )


def _match_stands(
    layout: StaticLayout,
    zone_ids: set[str],
    names: tuple[str, ...],
) -> set[str] | None:
    matched: set[str] = set()
    for name in names:
        exact = {
            zone_id
            for zone_id in zone_ids
            if layout.zone_aliases.get(zone_id, ("", ""))[0] == name
        }
        exact = exact or {
            zone_id
            for zone_id in zone_ids
            if layout.zone_aliases.get(zone_id, ("", ""))[1] == name
        }
        if not exact:
            return None
        matched.update(exact)
    return matched


def _configured_zones(
    layout: StaticLayout,
    plan_ids: tuple[str, ...],
) -> set[str]:
    return {
        zone_id
        for plan_id in plan_ids
        for zone_id in layout.plan_zones.get(plan_id, ())
        if zone_id in layout.resources
    }


def available_stand_names(
    data: dict[str, Any],
    plan_ids: tuple[str, ...],
) -> list[str]:
    layout = _static_layout(data)
    return sorted(
        {
            layout.zone_aliases[zone_id][0]
            for zone_id in _configured_zones(layout, plan_ids)
            if zone_id in layout.zone_aliases
        }
    )


async def _decode_zones(
    resources: dict[str, str],
    zone_ids: set[str] | frozenset[str],
    download: Callable[[str], Awaitable[bytes]],
) -> dict[str, tuple[Seat, ...]]:
    async def decode(zone_id: str) -> tuple[str, tuple[Seat, ...]]:
        url = resources.get(zone_id)
        if not url:
            raise RuntimeError(f"静态座位资源缺少区域 {zone_id}")
        seats = _cache_get(_DECODED_RESOURCES, url)
        if seats is None:
            task = _DECODE_LOADS.get(url)
            if task is None:
                task = asyncio.create_task(_decode_resource(download, url, zone_id))
                _DECODE_LOADS[url] = task
            try:
                seats = await asyncio.shield(task)
            finally:
                if task.done() and _DECODE_LOADS.get(url) is task:
                    _DECODE_LOADS.pop(url, None)
            _cache_put(
                _DECODED_RESOURCES,
                url,
                seats,
                DECODED_RESOURCE_CACHE_SIZE,
            )
        return zone_id, seats

    return dict(await asyncio.gather(*(decode(zone_id) for zone_id in zone_ids)))


async def _decode_resource(
    download: Callable[[str], Awaitable[bytes]],
    url: str,
    zone_id: str,
) -> tuple[Seat, ...]:
    async with _DOWNLOAD_SEMAPHORE:
        content = await download(url)
    features = geobuf.decode(content).get("features", [])
    return index_seats(
        tuple(filter(None, (_seat(feature, zone_id) for feature in features)))
    )


async def _download_resource(site: PurchasePage, url: str) -> bytes:
    response = await site.page.context.request.get(url)
    if not response.ok:
        raise RuntimeError(f"看台布局接口返回 HTTP {response.status}")
    return await response.body()


def _seat(feature: Any, zone_id: str) -> Seat | None:
    if not isinstance(feature, dict):
        return None
    geometry = feature.get("geometry", {})
    properties = feature.get("properties", {})
    coordinates = geometry.get("coordinates") if isinstance(geometry, dict) else None
    if (
        not isinstance(properties, dict)
        or not isinstance(coordinates, list)
        or len(coordinates) < 2
    ):
        return None
    seat_id = str(properties.get("seatConcreteId") or "")
    if not seat_id:
        return None
    try:
        return Seat(
            zone_id=zone_id,
            zone_name=str(properties.get("zoneName") or zone_id),
            seat_id=seat_id,
            row=_row_name(properties),
            seat_no=int(properties["seatNo"]),
            x=float(coordinates[0]),
            y=float(coordinates[1]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _row_name(properties: dict[str, Any]) -> str:
    prefix, separator, _ = str(properties.get("seatName") or "").rpartition("排")
    return prefix + separator if separator else str(properties.get("row") or "")


async def _load_venue(site: PurchasePage, url: str | None) -> Venue:
    if url is None:
        return venue_from_features([])
    if venue := _cache_get(_VENUES, url):
        return venue
    task = _VENUE_LOADS.get(url)
    if task is None:
        async def load() -> Venue:
            content = await _download_resource(site, url)
            return venue_from_features(geobuf.decode(content).get("features", []))

        task = asyncio.create_task(load())
        _VENUE_LOADS[url] = task
    try:
        venue = await asyncio.shield(task)
    finally:
        if task.done() and _VENUE_LOADS.get(url) is task:
            _VENUE_LOADS.pop(url, None)
    _cache_put(_VENUES, url, venue, STATIC_LAYOUT_CACHE_SIZE)
    return venue


def _bit_is_set(bits: bytes, seat_no: int) -> bool:
    byte_index, bit_index = divmod(seat_no, 8)
    return byte_index < len(bits) and bool(bits[byte_index] & (128 >> bit_index))


def _url(endpoint: str, query: dict[str, str]) -> str:
    return f"{endpoint}?{urlencode(query)}"
