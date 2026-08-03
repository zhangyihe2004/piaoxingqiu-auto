"""基于官方 booking 客户端的快速预下单与创建订单。"""

from __future__ import annotations

import asyncio
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from piaoxingqiu_auto.domain.combo import (
    ComboInstance,
    SeatOrderScheme,
    seat_order_scheme,
)
from piaoxingqiu_auto.domain.models import (
    AudienceConfig,
    required_audience_count,
)
from piaoxingqiu_auto.domain.seating import Candidate, SeatSelection
from piaoxingqiu_auto.platform.auth import AuthGuard
from piaoxingqiu_auto.platform.inventory import GeneralAdmissionSelection, Inventory
from piaoxingqiu_auto.platform.order_model import OfficialOrderModel, has_discount
from piaoxingqiu_auto.platform.submission import (
    LIMITING_INTERVAL_SECONDS,
    LIMITING_MODE,
    MAX_LIMITING_RETRIES,
    QUEUE_CODE,
    RiskEvent,
    RiskNotice,
    VERIFY_CODE,
    normalize_mode,
)


PRE_ORDER_PATH = "/cyy_gatewayapi/trade/buyer/order/cart/v1/pre_order"
CREATE_ORDER_PATH = "/cyy_gatewayapi/trade/buyer/order/cart/v1/create_order"
POSITIONING_PATH = "/cyy_gatewayapi/mcommon/pub/v1/positioning"
_LAST_TICKET_TIMESTAMP = 0


class CartRejected(RuntimeError):
    def __init__(
        self,
        code: str | None,
        sub_code: str | None,
        message: str,
        *,
        mode: str | None = None,
        rc_code: str | None = None,
        rc_id: str | None = None,
        rc_show_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.sub_code = sub_code
        self.message = message
        self.mode = normalize_mode(mode)
        self.rc_code = rc_code
        self.rc_id = rc_id
        self.rc_show_id = rc_show_id


@dataclass(frozen=True)
class OrderSummary:
    quantity: int
    plans: tuple[str, ...]
    total: str
    discount: bool

    def describe(self) -> str:
        details = [
            f"目标 {self.quantity} 张",
            f"票档：{'、'.join(self.plans)}",
        ]
        if self.discount:
            details.append("优惠：票星球已自动计算")
        details.append(f"应付：¥{self.total}")
        return "；".join(details)


@dataclass(frozen=True)
class CartOrder:
    payload: dict
    audiences: tuple[AudienceConfig, ...]
    summary: OrderSummary


class CartClient:
    def __init__(
        self,
        site,
        auth: AuthGuard,
        cache: dict[str, Any] | None = None,
        risk_notice: RiskNotice | None = None,
    ) -> None:
        self.site = site
        self.auth = auth
        self.cache = cache if cache is not None else {}
        self.risk_notice = risk_notice
        self.order_model = OfficialOrderModel(site.page, *site.booking_ids)

    async def warm(self, audiences: tuple[AudienceConfig, ...]) -> None:
        await self.site.prepare_booking()
        operations = [
            self._load_client(),
            self.order_model.warm(),
            self._location_id(),
        ]
        if audiences:
            operations.append(self._audience_ids(audiences))
        await asyncio.gather(*operations)

    async def prepare_seats(
        self,
        selection: SeatSelection,
        inventory: Inventory,
        audiences: tuple[AudienceConfig, ...],
    ) -> CartOrder:
        show_id, session_id = self.site.booking_ids
        scheme = seat_order_scheme(
            selection,
            inventory.combo_plans,
            self.site.config.purchase.plan_ids,
            inventory.plan_prices,
        )
        pre_request = _seat_preorder(
            scheme,
            inventory.plan_prices,
            show_id,
            session_id,
            self._version,
            self._source,
        )
        return await self._prepare(
            pre_request,
            audiences,
            len(selection.candidates),
            len(selection.candidates),
            tuple(dict.fromkeys(item.plan for item in selection.candidates)),
        )

    async def prepare_general(
        self,
        selection: GeneralAdmissionSelection,
        audiences: tuple[AudienceConfig, ...],
    ) -> CartOrder:
        show_id, session_id = self.site.booking_ids
        pre_request = _general_preorder(
            selection,
            show_id,
            session_id,
            self._version,
            self._source,
        )
        return await self._prepare(
            pre_request,
            audiences,
            selection.units,
            selection.ticket_count,
            (selection.plan,),
        )

    async def create(self, payload: dict) -> None:
        await self._request(CREATE_ORDER_PATH, payload, tolerate_error=True)

    @property
    def _version(self) -> str:
        return self.auth.headers.get("ver", "4.63.6")

    @property
    def _source(self) -> str:
        source = self.auth.headers.get("src") or self.auth.headers.get("terminal-src")
        if not source:
            raise RuntimeError("认证请求缺少平台标识")
        return source.upper()

    async def _prepare(
        self,
        pre_request: dict,
        configured: tuple[AudienceConfig, ...],
        quantity: int,
        ticket_count: int,
        plans: tuple[str, ...],
    ) -> CartOrder:
        required = required_audience_count(
            self.site.config.purchase.real_name_mode, ticket_count
        )
        selected = configured[:required]
        if len(selected) != required:
            raise RuntimeError(
                f"订单需要 {required} 个实名证件，当前仅配置 {len(selected)} 个"
            )
        pre_started = asyncio.get_running_loop().time()
        pre_task = asyncio.create_task(self._preorder(pre_request))
        location_task = asyncio.create_task(self._location_id())
        model_task = asyncio.create_task(self.order_model.warm())
        audience_task = (
            asyncio.create_task(self._audience_ids(selected)) if selected else None
        )
        tasks = tuple(
            task
            for task in (pre_task, location_task, model_task, audience_task)
            if task
        )
        try:
            pre_response, location_id, _ = await asyncio.gather(
                pre_task,
                location_task,
                model_task,
            )
            audience_ids = await audience_task if audience_task else []
            payload = await self.order_model.build(
                pre_request,
                pre_response,
                audience_ids,
                self.site.config.purchase.real_name_mode,
                location_id,
            )
        except BaseException:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        finally:
            self.site.record_timing(
                "pre_order", asyncio.get_running_loop().time() - pre_started
            )
        return CartOrder(
            payload,
            selected,
            OrderSummary(
                quantity,
                plans,
                _display_money(payload["paymentParam"]["payAmount"]),
                has_discount(payload),
            ),
        )

    async def _preorder(self, payload: dict) -> dict:
        verification_retried = False
        for attempt in range(MAX_LIMITING_RETRIES + 1):
            try:
                return await self._request(PRE_ORDER_PATH, payload)
            except CartRejected as exc:
                risk = _risk_event(exc)
                if risk is not None and risk.state == "VERIFY_REQUIRED":
                    if verification_retried or self.risk_notice is None:
                        raise
                    notice = self.risk_notice(risk)
                    if notice is None:
                        raise
                    verification_retried = True
                    try:
                        await notice
                    except TimeoutError:
                        raise exc from None
                    continue
                if risk is not None and self.risk_notice is not None:
                    self.risk_notice(risk)
                if (
                    risk is None
                    or risk.state != "RATE_LIMITED"
                    or attempt == MAX_LIMITING_RETRIES
                ):
                    raise
                await asyncio.sleep(LIMITING_INTERVAL_SECONDS)
        raise AssertionError("unreachable")

    async def _audience_ids(self, configured: tuple[AudienceConfig, ...]) -> list[str]:
        official = self.cache.get("audiences")
        if not isinstance(official, tuple):
            official = await self.auth.audiences()
            self.cache["audiences"] = official
        result = []
        for audience in configured:
            matches = [
                item
                for item in official
                if item.name == audience.name and item.masked_id == audience.masked_id
            ]
            if len(matches) != 1:
                raise RuntimeError(f"无法唯一定位观演人“{audience.name}”")
            result.append(matches[0].id)
        return result

    async def _location_id(self) -> str:
        cached = self.cache.get("location_id")
        if isinstance(cached, str):
            return cached
        payload = await self._request(POSITIONING_PATH, {})
        data = payload["data"]
        location_id = str((data or {}).get("locationId") or "")
        if not location_id:
            raise RuntimeError("定位接口未返回 locationId")
        self.cache["location_id"] = location_id
        return location_id

    async def _load_client(self) -> None:
        await self.site.page.evaluate(
            """
            async () => {
              window.__pxqCartClient ||= (async () => {
                const url = performance.getEntriesByType("resource")
                  .map(entry => entry.name)
                  .find(value => /\\/index-[^/]+\\.js(?:\\?|$)/.test(value));
                if (!url) throw new Error("未找到 booking 主模块");
                const loaded = await import(url);
                const key = Object.keys(loaded).find(
                  name => typeof loaded[name]?.request === "function"
                );
                if (!key) throw new Error("booking 主模块未公开 request 客户端");
                return loaded[key];
              })();
              await window.__pxqCartClient;
            }
            """
        )

    async def _request(
        self,
        endpoint: str,
        payload: dict,
        *,
        method: str = "POST",
        tolerate_error: bool = False,
    ) -> dict:
        result = await self.site.page.evaluate(
            """
            async ({ endpoint, method, payload, tolerateError }) => {
              window.__pxqCartClient ||= (async () => {
                const url = performance.getEntriesByType("resource")
                  .map(entry => entry.name)
                  .find(value => /\\/index-[^/]+\\.js(?:\\?|$)/.test(value));
                if (!url) throw new Error("未找到 booking 主模块");
                const loaded = await import(url);
                const key = Object.keys(loaded).find(
                  name => typeof loaded[name]?.request === "function"
                );
                if (!key) throw new Error("booking 主模块未公开 request 客户端");
                return loaded[key];
              })();
              const client = await window.__pxqCartClient;
              try {
                const response = await client.request({
                  url: endpoint,
                  method,
                  ...(method === "POST" ? { data: payload } : {})
                });
                return {
                  body: response?.data?.statusCode ? response.data : response,
                  headers: response?.header || response?.headers || {}
                };
              } catch (error) {
                if (tolerateError) return { body: null };
                throw error;
              }
            }
            """,
            {
                "endpoint": endpoint,
                "method": method,
                "payload": payload,
                "tolerateError": tolerate_error,
            },
        )
        body = result.get("body") if isinstance(result, dict) else None
        headers = result.get("headers") if isinstance(result, dict) else None
        if tolerate_error:
            return body or {}
        if (
            not isinstance(body, dict)
            or str(body.get("statusCode")) != "200"
            or not isinstance(body.get("data"), dict)
        ):
            raise CartRejected(
                _scalar(body, "statusCode", "code", "errorCode"),
                _scalar(body, "subCode", "sub_code", "bizCode"),
                _scalar(body, "comments", "message", "msg", "desc")
                or "响应格式错误",
                mode=_scalar(body, "mode"),
                rc_code=_header(headers, "rc-code"),
                rc_id=_header(headers, "rc-id"),
                rc_show_id=_header(headers, "rc-show-id"),
            )
        return body


def _scalar(value: object, *keys: str) -> str | None:
    if not isinstance(value, dict):
        return None
    for key in keys:
        item = value.get(key)
        if isinstance(item, str | int | float):
            return str(item)
    return None


def _header(value: object, name: str) -> str | None:
    if not isinstance(value, dict):
        return None
    target = name.lower()
    for key, item in value.items():
        if str(key).lower() == target and isinstance(item, str | int | float):
            return str(item)
    return None


def _risk_event(error: CartRejected) -> RiskEvent | None:
    state = {
        QUEUE_CODE: "QUEUEING",
        VERIFY_CODE: "VERIFY_REQUIRED",
    }.get(error.rc_code)
    if state is None and error.mode == LIMITING_MODE:
        state = "RATE_LIMITED"
    return (
        RiskEvent(state, "PRE_ORDER", error.rc_id, error.rc_show_id)
        if state is not None
        else None
    )


def _ticket_ids(count: int) -> list[str]:
    global _LAST_TICKET_TIMESTAMP
    values = []
    for _ in range(count):
        _LAST_TICKET_TIMESTAMP = max(
            int(time.time() * 1000),
            _LAST_TICKET_TIMESTAMP + 1,
        )
        values.append(
            f"{_LAST_TICKET_TIMESTAMP}10000000{secrets.randbelow(9) + 1}"
        )
    return values


def _base_request(items: list[dict], ver: str, source: str) -> dict:
    return {
        "clientCurrency": "CNY",
        "couponQueryParam": {"src": source, "onlySearchCanUse": False},
        "orderSource": "COMMON",
        "orders": [
            {
                "clientCurrency": "CNY",
                "groupId": "default",
                "items": items,
                "orderSource": "COMMON",
                "priorityId": "",
            }
        ],
        "scene": "NORMAL",
        "src": source,
        "ver": ver,
    }


def _seat_preorder(
    scheme: SeatOrderScheme,
    prices: dict[str, float],
    show_id: str,
    session_id: str,
    ver: str,
    source: str,
) -> dict:
    grouped: OrderedDict[str, list[Candidate]] = OrderedDict()
    for candidate in scheme.singles:
        grouped.setdefault(candidate.plan_id, []).append(candidate)
    single_items = []
    for plan_id, candidates in grouped.items():
        if plan_id not in prices:
            raise RuntimeError(f"缺少票档 {plan_id} 的公开价格")
        ticket_ids = iter(_ticket_ids(len(candidates)))
        single_items.append(
            {
                "sku": {
                    "qty": len(candidates),
                    "skuId": plan_id,
                    "skuType": "SINGLE",
                    "ticketItems": [
                        {
                            "groupId": "default",
                            "id": next(ticket_ids),
                            "seatConcreteId": candidate.seat.seat_id,
                            "zoneConcreteId": candidate.seat.zone_id,
                        }
                        for candidate in candidates
                    ],
                    "ticketPrice": _number(prices[plan_id]),
                },
                "spu": {"sessionId": session_id, "showId": show_id},
            }
        )

    combo_items = []
    by_variant: OrderedDict[str, list[ComboInstance]] = OrderedDict()
    for instance in scheme.combos:
        by_variant.setdefault(instance.variant.sku_id, []).append(instance)
    for instances in by_variant.values():
        variant = instances[0].variant
        tickets = []
        for instance in instances:
            ticket_ids = _ticket_ids(len(instance.candidates))
            group_id = _ticket_ids(1)[0]
            components = variant.components
            if len(components) != len(instance.candidates):
                raise RuntimeError("套票组成数量与所选座位数不一致")
            tickets.extend(
                {
                    "comboItemId": component_id,
                    "seatConcreteId": candidate.seat.seat_id,
                    "seatGroupId": group_id,
                    "zoneConcreteId": candidate.seat.zone_id,
                    "id": ticket_id,
                    "groupId": "default",
                }
                for candidate, ticket_id, (component_id, _) in zip(
                    instance.candidates,
                    ticket_ids,
                    components,
                )
            )
        combo_items.append(
            {
                "sku": {
                    "qty": len(instances),
                    "skuId": variant.sku_id,
                    "skuType": "COMBO",
                    "ticketItems": tickets,
                    "ticketPrice": _number(variant.price),
                },
                "spu": {"sessionId": session_id, "showId": show_id},
            }
        )
    return _base_request(combo_items + single_items, ver, source)


def _general_preorder(
    selection: GeneralAdmissionSelection,
    show_id: str,
    session_id: str,
    ver: str,
    source: str,
) -> dict:
    ticket_ids = iter(_ticket_ids(selection.ticket_count))
    tickets = []
    if selection.combo_items:
        components = [
            (plan_id, price)
            for plan_id, count, price in selection.combo_items
            for _ in range(count)
        ]
        seat_group_ids = _ticket_ids(selection.units)
        for plan_id, _ in components:
            for seat_group_id in seat_group_ids:
                ticket_id = next(ticket_ids)
                tickets.append({
                    "comboItemId": plan_id,
                    "groupId": "default",
                    "id": ticket_id,
                    "seatGroupId": seat_group_id,
                })
    else:
        tickets.extend(
            {"groupId": "default", "id": ticket_id}
            for ticket_id in ticket_ids
        )
    return _base_request(
        [{
            "sku": {
                "qty": selection.units,
                "skuId": selection.plan_id,
                "skuType": "COMBO" if selection.combo_items else "SINGLE",
                "ticketItems": tickets,
                "ticketPrice": _number(selection.price),
            },
            "spu": {"sessionId": session_id, "showId": show_id},
        }],
        ver,
        source,
    )


def _number(value: float) -> int | float:
    return int(value) if value.is_integer() else value


def _display_money(value: Any) -> str:
    number = float(value)
    return str(int(number)) if number.is_integer() else f"{number:.2f}"
