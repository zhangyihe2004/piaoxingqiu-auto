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
    purchase_unit,
    required_audience_count,
)
from piaoxingqiu_auto.domain.seating import Candidate, SeatSelection
from piaoxingqiu_auto.platform.auth import AuthGuard
from piaoxingqiu_auto.platform.inventory import GeneralAdmissionSelection, Inventory


PRE_ORDER_PATH = "/cyy_gatewayapi/trade/buyer/order/cart/v1/pre_order"
CREATE_ORDER_PATH = "/cyy_gatewayapi/trade/buyer/order/cart/v1/create_order"
POSITIONING_PATH = "/cyy_gatewayapi/mcommon/pub/v1/positioning"
PROMOTIONS_PATH = "/cyy_gatewayapi/show/pub/v3/promotions/list"


class CartRejected(RuntimeError):
    def __init__(
        self,
        code: str | None,
        sub_code: str | None,
        message: str,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.sub_code = sub_code
        self.message = message


@dataclass(frozen=True)
class OrderSummary:
    quantity: int
    unit: str
    plans: tuple[str, ...]
    total: str
    discount: bool

    def describe(self) -> str:
        details = [
            f"目标 {self.quantity} {self.unit}",
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


@dataclass(frozen=True)
class _ComboPriceGroup:
    ticket_ids: tuple[str, ...]
    discount: float


class CartClient:
    def __init__(
        self,
        site,
        auth: AuthGuard,
        cache: dict[str, Any] | None = None,
    ) -> None:
        self.site = site
        self.auth = auth
        self.cache = cache if cache is not None else {}

    async def warm(self, audiences: tuple[AudienceConfig, ...]) -> None:
        await self.site.prepare_booking()
        operations = [self._load_client(), self._location_id()]
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
        )
        pre_request, price_groups = _seat_preorder(
            scheme,
            inventory.plan_prices,
            show_id,
            session_id,
            self._version,
        )
        return await self._prepare(
            pre_request,
            audiences,
            len(selection.candidates),
            tuple(dict.fromkeys(item.plan for item in selection.candidates)),
            price_groups,
            inventory.has_activity,
        )

    async def prepare_general(
        self,
        selection: GeneralAdmissionSelection,
        audiences: tuple[AudienceConfig, ...],
    ) -> CartOrder:
        show_id, session_id = self.site.booking_ids
        return await self._prepare(
            _general_preorder(
                selection,
                show_id,
                session_id,
                self._version,
            ),
            audiences,
            selection.quantity,
            (selection.plan,),
            (),
            selection.has_activity,
        )

    async def create(self, payload: dict) -> None:
        await self._request(CREATE_ORDER_PATH, payload, tolerate_error=True)

    @property
    def _version(self) -> str:
        return self.auth.headers.get("ver", "4.63.6")

    async def _prepare(
        self,
        pre_request: dict,
        configured: tuple[AudienceConfig, ...],
        quantity: int,
        plans: tuple[str, ...],
        price_groups: tuple[_ComboPriceGroup, ...],
        has_activity: bool,
    ) -> CartOrder:
        required = required_audience_count(
            self.site.config.purchase.real_name_mode, quantity
        )
        selected = configured[:required]
        if len(selected) != required:
            raise RuntimeError(
                f"订单需要 {required} 个实名证件，当前仅配置 {len(selected)} 个"
            )
        pre_started = asyncio.get_running_loop().time()
        pre_task = asyncio.create_task(self._request(PRE_ORDER_PATH, pre_request))
        location_task = asyncio.create_task(self._location_id())
        audience_task = (
            asyncio.create_task(self._audience_ids(selected)) if selected else None
        )
        promotion_task = (
            asyncio.create_task(self._promotions()) if has_activity else None
        )
        tasks = tuple(
            task
            for task in (pre_task, location_task, audience_task, promotion_task)
            if task
        )
        try:
            pre_response, location_id = await asyncio.gather(pre_task, location_task)
            audience_ids = await audience_task if audience_task else []
            promotions = await promotion_task if promotion_task else None
            if promotions is None and any(
                item.get("priceItemType") == "ACTIVITY_DISCOUNT_FEE"
                for item in pre_response["data"]["orders"][0]["priceItems"]
            ):
                promotions = await self._promotions()
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
        payload = _create_payload(
            pre_request,
            pre_response,
            location_id,
            audience_ids,
            self.site.config.purchase.real_name_mode,
            price_groups,
            promotions,
        )
        return CartOrder(
            payload,
            selected,
            OrderSummary(
                quantity,
                purchase_unit(self.site.config.project.support_seat_picking),
                plans,
                _pay_amount(pre_response),
                any(
                    float(item["priceItemVal"]) < 0
                    for item in pre_response["data"]["orders"][0]["priceItems"]
                ),
            ),
        )

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

    async def _promotions(self) -> dict:
        show_id, _ = self.site.booking_ids
        payload = await self._request(
            f"{PROMOTIONS_PATH}?showId={show_id}&verControl=true",
            {},
            method="GET",
        )
        return payload["data"]

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
                return { body: response?.data?.statusCode ? response.data : response };
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
                str((body or {}).get("comments") or "响应格式错误"),
            )
        return body


def _scalar(value: object, *keys: str) -> str | None:
    if not isinstance(value, dict):
        return None
    for key in keys:
        item = value.get(key)
        if isinstance(item, (str, int, float)):
            return str(item)
    return None


def _ticket_ids(count: int) -> list[str]:
    prefix = str(int(time.time() * 1000))
    values: set[str] = set()
    while len(values) < count:
        values.add(f"{prefix}{secrets.randbelow(1_000_000_000):09d}")
    return list(values)


def _base_request(items: list[dict], ver: str) -> dict:
    return {
        "clientCurrency": "CNY",
        "couponQueryParam": {"src": "H5", "onlySearchCanUse": False},
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
        "src": "H5",
        "ver": ver,
    }


def _seat_preorder(
    scheme: SeatOrderScheme,
    prices: dict[str, float],
    show_id: str,
    session_id: str,
    ver: str,
) -> tuple[dict, tuple[_ComboPriceGroup, ...]]:
    grouped: OrderedDict[str, list[Candidate]] = OrderedDict()
    for candidate in scheme.singles:
        grouped.setdefault(candidate.plan_id, []).append(candidate)
    items = []
    for plan_id, candidates in grouped.items():
        if plan_id not in prices:
            raise RuntimeError(f"缺少票档 {plan_id} 的公开价格")
        ticket_ids = iter(_ticket_ids(len(candidates)))
        items.append(
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

    price_groups = []
    by_variant: OrderedDict[str, list[ComboInstance]] = OrderedDict()
    for instance in scheme.combos:
        by_variant.setdefault(instance.variant.sku_id, []).append(instance)
    for instances in by_variant.values():
        variant = instances[0].variant
        tickets = []
        for instance in instances:
            ticket_ids = _ticket_ids(len(instance.candidates))
            group_id = _ticket_ids(1)[0]
            price_groups.append(
                _ComboPriceGroup(
                    tuple(ticket_ids),
                    prices[variant.base_id] * len(instance.candidates)
                    - variant.price,
                )
            )
            tickets.extend(
                {
                    "comboItemId": candidate.plan_id,
                    "seatConcreteId": candidate.seat.seat_id,
                    "seatGroupId": group_id,
                    "zoneConcreteId": candidate.seat.zone_id,
                    "id": ticket_id,
                    "groupId": "default",
                }
                for candidate, ticket_id in zip(instance.candidates, ticket_ids)
            )
        items.append(
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
    return _base_request(items, ver), tuple(price_groups)


def _general_preorder(
    selection: GeneralAdmissionSelection,
    show_id: str,
    session_id: str,
    ver: str,
) -> dict:
    return _base_request(
        [
            {
                "sku": {
                    "qty": selection.units,
                    "skuId": selection.plan_id,
                    "skuType": "SINGLE",
                    "ticketItems": [
                        {"groupId": "default", "id": ticket_id}
                        for ticket_id in _ticket_ids(selection.quantity)
                    ],
                    "ticketPrice": _number(selection.price),
                },
                "spu": {"sessionId": session_id, "showId": show_id},
            }
        ],
        ver,
    )


def _create_payload(
    pre_request: dict,
    pre_response: dict,
    location_id: str,
    audience_ids: list[str],
    real_name_mode: str,
    groups: tuple[_ComboPriceGroup, ...],
    promotions: dict | None,
) -> dict:
    response_order = pre_response["data"]["orders"][0]
    prices = sorted(
        response_order["priceItems"],
        key=lambda item: item["priceItemType"] != "TICKET_FEE",
    )
    order = pre_request["orders"][0]
    items, ticket_index = _create_items(
        order,
        response_order["supportDeliveries"][0]["name"],
        audience_ids,
        real_name_mode,
        promotions,
    )
    params = _price_params(prices)
    for price, param in zip(prices, params):
        value = float(price["priceItemVal"])
        if price["priceItemType"] == "COMBO_DISCOUNT_FEE":
            applications = _combo_applications(
                groups,
                ticket_index,
                abs(value),
            )
            param.update(
                {
                    "applyTickets": applications,
                    "tag": "COMBO",
                    "discountId": "comboDiscount",
                    "priceItemTitle": price["priceItemName"],
                }
            )
        elif price["priceItemType"] == "ACTIVITY_DISCOUNT_FEE":
            promotion = _promotion_for_price(price, promotions, ticket_index)
            param.update(
                {
                    "applyTickets": _activity_applications(
                        ticket_index,
                        promotion,
                        abs(value),
                    ),
                    "tag": price.get("tag") or "惠",
                    "discountId": "promotionDiscount",
                    "priceItemName": (
                        promotion.get("promotionName") or price["priceItemName"]
                    ),
                    "priceItemTitle": "满减满折",
                    "priceItemId": promotion["promotionId"],
                    "priceItemIdType": promotion["promotionMethod"],
                }
            )
    ticket_total = next(
        item for item in prices if item["priceItemType"] == "TICKET_FEE"
    )
    result = _create_base(
        pre_request,
        location_id,
        items,
        _money(ticket_total["priceItemVal"]),
        _money(sum(float(item["priceItemVal"]) for item in prices)),
        params,
    )
    if real_name_mode == "PER_ORDER":
        result["orders"][0]["many2OneAudience"] = {
            "audienceId": audience_ids[0],
            "sessionIds": list(
                dict.fromkeys(item["spu"]["sessionId"] for item in order["items"])
            ),
        }
    return result


def _create_items(
    order: dict,
    delivery: str,
    audience_ids: list[str],
    real_name_mode: str,
    promotions: dict | None,
) -> tuple[list[dict], dict[str, tuple[dict, dict, str, float]]]:
    ticket_count = sum(len(item["sku"]["ticketItems"]) for item in order["items"])
    if real_name_mode == "PER_TICKET" and len(audience_ids) != ticket_count:
        raise RuntimeError("观演人数量与票数不一致")
    audience_iter = iter(audience_ids)
    items = []
    ticket_index = {}
    for item in order["items"]:
        tickets = []
        for ticket in item["sku"]["ticketItems"]:
            created = {
                **ticket,
                "audienceId": (
                    next(audience_iter) if real_name_mode == "PER_TICKET" else ""
                ),
            }
            tickets.append(created)
            ticket_index[ticket["id"]] = (
                ticket,
                item["spu"],
                item["sku"]["skuId"],
                float(item["sku"]["ticketPrice"]),
            )
        items.append(
            _create_item(
                item,
                tickets,
                delivery,
                promotions,
            )
        )
    return items, ticket_index


def _price_params(prices: list[dict]) -> list[dict]:
    return [
        {
            "applyTickets": [],
            "priceItemName": (
                "票款总额"
                if price["priceItemType"] == "TICKET_FEE"
                else price["priceItemName"]
            ),
            "priceItemVal": _money(price["priceItemVal"]),
            "priceItemType": price["priceItemType"],
            "priceItemSpecies": price["priceItemSpecies"],
            "direction": price["direction"],
            "priceDisplay": _price_display(price["priceItemVal"]),
        }
        for price in prices
    ]


def _activity_applications(
    ticket_index: dict[str, tuple[dict, dict, str, float]],
    promotion: dict,
    discount: float,
) -> list[list[dict]]:
    eligible_plan_ids = _promotion_plan_ids(promotion)
    tickets = tuple(
        indexed for indexed in ticket_index.values() if indexed[2] in eligible_plan_ids
    )
    if not tickets:
        raise RuntimeError("预下单返回优惠，但未能定位参加活动的票档")
    discounts = _split_discount(discount, [ticket[3] for ticket in tickets])
    return [
        [
            _discount_ticket(ticket, amount)
            for ticket, amount in zip(tickets, discounts)
        ]
    ]


def _combo_applications(
    groups: tuple[_ComboPriceGroup, ...],
    ticket_index: dict[str, tuple[dict, dict, str, float]],
    actual_discount: float,
) -> list[list[dict]]:
    expected = sum(group.discount for group in groups)
    if not groups or abs(expected - actual_discount) > 0.02:
        raise RuntimeError("FREE_COMBO 优惠与预下单结果不一致")
    applications = []
    for group in groups:
        ticket_discounts = _split_discount(
            group.discount,
            [1.0] * len(group.ticket_ids),
        )
        applications.append(
            [
                _discount_ticket(ticket_index[ticket_id], discount)
                for ticket_id, discount in zip(group.ticket_ids, ticket_discounts)
            ]
        )
    return applications


def _promotion_for_price(
    price: dict,
    promotions: dict | None,
    ticket_index: dict[str, tuple[dict, dict, str, float]],
) -> dict:
    if not promotions:
        raise RuntimeError("预下单返回活动优惠，但未取得官方促销规则")
    selected = {indexed[2] for indexed in ticket_index.values()}
    available = (
        *(promotions.get("sameSessionPromotions") or []),
        *(promotions.get("crossSessionPromotions") or []),
    )
    candidates = [
        promotion
        for promotion in available
        if isinstance(promotion, dict)
        and promotion.get("isValid") is not False
        and selected & _promotion_plan_ids(promotion)
    ]
    name = str(price.get("priceItemName") or "")
    named = [
        promotion
        for promotion in candidates
        if name in str(promotion.get("promotionName") or "")
        or str(promotion.get("promotionName") or "") in name
    ]
    matches = named if len(named) == 1 else candidates
    if len(matches) != 1:
        raise RuntimeError("无法唯一匹配预下单优惠与官方促销规则")
    promotion = matches[0]
    if not promotion.get("promotionId") or not promotion.get("promotionMethod"):
        raise RuntimeError("官方促销规则缺少必要标识")
    return promotion


def _promotion_plan_ids(promotion: dict) -> set[str]:
    return {
        str(item.get("seatPlanId") or "")
        for item in promotion.get("seatPlans", [])
        if isinstance(item, dict)
    }


def _discount_ticket(
    indexed: tuple[dict, dict, str, float],
    discount: float,
) -> dict:
    ticket, spu, sku_id, _ = indexed
    result = {
        "id": ticket["id"],
        "discountAmount": _money(discount),
        "seatPlanId": sku_id,
        "showId": spu["showId"],
        "sessionId": spu["sessionId"],
        "groupId": ticket["groupId"],
    }
    result.update(
        {
            key: ticket[key]
            for key in ("seatConcreteId", "zoneConcreteId", "seatGroupId")
            if key in ticket
        }
    )
    return result


def _split_discount(total: float, weights: list[float]) -> list[float]:
    if not weights or any(weight <= 0 for weight in weights):
        raise RuntimeError("优惠票档价格无效")
    cents = round(total * 100)
    weight_total = sum(weights)
    result = []
    allocated = 0
    cumulative = 0.0
    for weight in weights:
        cumulative += cents * weight / weight_total
        current = round(cumulative) - allocated
        result.append(current / 100)
        allocated += current
    return result


def _create_item(
    item: dict,
    tickets: list[dict],
    delivery: str,
    promotions: dict | None,
) -> dict:
    promotions = promotions or {}
    return {
        "sku": {
            **item["sku"],
            "ticketPrice": _money(item["sku"]["ticketPrice"]),
            "ticketItems": tickets,
        },
        "spu": {
            **item["spu"],
            "promotionVersionHash": (
                promotions.get("promotionVersionHash") or "EMPTY_PROMOTION_HASH"
            ),
            "addPromoVersionHash": (
                promotions.get("addPromoVersionHash") or "EMPTY_PROMOTION_HASH"
            ),
        },
        "deliverMethod": delivery,
    }


def _create_base(
    pre_request: dict,
    location_id: str,
    items: list[dict],
    total: str,
    payable: str,
    prices: list[dict],
) -> dict:
    order = pre_request["orders"][0]
    return {
        "src": pre_request["src"],
        "ver": pre_request["ver"],
        "addressParam": {},
        "locationParam": {
            "locationCityId": location_id,
            "bsCityId": location_id,
        },
        "paymentParam": {"totalAmount": total, "payAmount": payable},
        "priceItemParams": prices,
        "orders": [
            {
                "items": items,
                "priorityId": "",
                "sourceOrderId": "",
                "addPurchasePromotionId": "",
                "scene": pre_request["scene"],
                "exchangeRate": 1,
                "clientCurrency": order["clientCurrency"],
                "many2OneAudience": {},
                "orderSource": order["orderSource"],
                "groupId": order["groupId"],
            }
        ],
        "scene": pre_request["scene"],
        "clientCurrency": pre_request["clientCurrency"],
        "orderFrom": "DEFAULT",
    }


def _pay_amount(pre_response: dict) -> str:
    values = pre_response["data"]["orders"][0]["priceItems"]
    return _display_money(sum(float(item["priceItemVal"]) for item in values))


def _number(value: float) -> int | float:
    return int(value) if value.is_integer() else value


def _money(value: Any) -> str:
    return f"{float(value):.2f}"


def _display_money(value: Any) -> str:
    number = float(value)
    return str(int(number)) if number.is_integer() else f"{number:.2f}"


def _price_display(value: Any) -> str:
    number = float(value)
    return f"{'-￥' if number < 0 else '￥'}{_display_money(abs(number))}"
