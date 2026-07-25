"""基于官方 booking 客户端的快速预下单与创建订单。"""

from __future__ import annotations

import asyncio
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from piaoxingqiu_auto.domain.models import AudienceConfig, required_audience_count
from piaoxingqiu_auto.domain.seating import Candidate, SeatSelection
from piaoxingqiu_auto.platform.auth import (
    AuthGuard,
    request_context,
)
from piaoxingqiu_auto.platform.inventory import GeneralAdmissionSelection, Inventory


PRE_ORDER_PATH = "/cyy_gatewayapi/trade/buyer/order/cart/v1/pre_order"
CREATE_ORDER_PATH = "/cyy_gatewayapi/trade/buyer/order/cart/v1/create_order"


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
    plans: tuple[str, ...]
    total: str
    combo: bool

    def describe(self) -> str:
        details = [f"目标 {self.quantity} 张", f"票档：{'、'.join(self.plans)}"]
        if self.combo:
            details.append("套票优惠：票星球已自动计算")
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
        combo = (
            _combo_plan(selection, inventory.combo_plans)
            if self.site.config.purchase.real_name_mode == "NONE"
            else None
        )
        pre_request = (
            _combo_seat_preorder(
                selection,
                combo,
                show_id,
                session_id,
                self._version,
            )
            if combo is not None
            else _seat_preorder(
                selection,
                inventory.plan_prices,
                show_id,
                session_id,
                self._version,
                self.site.config.purchase.plan_ids,
            )
        )
        return await self._prepare(
            pre_request,
            audiences,
            len(selection.candidates),
            tuple(dict.fromkeys(item.plan for item in selection.candidates)),
            combo is not None,
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
            False,
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
        combo: bool,
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
        try:
            pre_response, location_id = await asyncio.gather(pre_task, location_task)
            audience_ids = await audience_task if audience_task else []
        except BaseException:
            for task in (pre_task, location_task, audience_task):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(
                *(task for task in (pre_task, location_task, audience_task) if task),
                return_exceptions=True,
            )
            raise
        finally:
            self.site.record_timing(
                "pre_order", asyncio.get_running_loop().time() - pre_started
            )
        payload = (
            _combo_create(pre_request, pre_response, location_id)
            if combo
            else _standard_create(
                pre_request,
                pre_response,
                location_id,
                audience_ids,
                self.site.config.purchase.real_name_mode,
            )
        )
        return CartOrder(
            payload,
            selected,
            OrderSummary(
                quantity,
                plans,
                _pay_amount(pre_response),
                combo,
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
        query = urlencode(request_context(self.auth.headers))
        response = await self.site.page.context.request.get(
            f"{self.site.origin}/cyy_gatewayapi/mcommon/pub/v1/positioning?{query}",
            headers=self.auth.headers,
        )
        if not response.ok:
            raise RuntimeError(f"定位接口返回 HTTP {response.status}")
        payload = await response.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        location_id = str((data or {}).get("locationId") or "")
        if str(payload.get("statusCode")) != "200" or not location_id:
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
        tolerate_error: bool = False,
    ) -> dict:
        result = await self.site.page.evaluate(
            """
            async ({ endpoint, payload, tolerateError }) => {
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
                  method: "POST",
                  data: payload
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
    selection: SeatSelection,
    prices: dict[str, float],
    show_id: str,
    session_id: str,
    ver: str,
    plan_order: tuple[str, ...],
) -> dict:
    grouped: OrderedDict[str, list[Candidate]] = OrderedDict(
        (plan_id, [])
        for plan_id in plan_order
        if any(item.plan_id == plan_id for item in selection.candidates)
    )
    for candidate in selection.candidates:
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
    return _base_request(items, ver)


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


def _combo_plan(selection: SeatSelection, plans: tuple[dict, ...]) -> dict | None:
    base_ids = {item.plan_id for item in selection.candidates}
    if len(base_ids) != 1:
        return None
    matches = [
        plan
        for plan in plans
        if int(plan.get("unitQty") or 1) == 1
        and {
            str(item.get("bizSeatPlanId") or "")
            for item in plan.get("items", [])
            if isinstance(item, dict)
        }
        == base_ids
    ]
    if len(matches) > 1:
        raise RuntimeError("基础票档对应多个单张套票方案")
    return matches[0] if matches else None


def _combo_seat_preorder(
    selection: SeatSelection,
    combo: dict,
    show_id: str,
    session_id: str,
    ver: str,
) -> dict:
    base_id = selection.candidates[0].plan_id
    ticket_ids = iter(_ticket_ids(len(selection.candidates)))
    group_ids = iter(_ticket_ids(len(selection.candidates)))
    return _base_request(
        [
            {
                "sku": {
                    "qty": len(selection.candidates),
                    "skuId": str(combo["seatPlanId"]),
                    "skuType": "COMBO",
                    "ticketItems": [
                        {
                            "comboItemId": base_id,
                            "seatConcreteId": candidate.seat.seat_id,
                            "seatGroupId": next(group_ids),
                            "zoneConcreteId": candidate.seat.zone_id,
                            "id": next(ticket_ids),
                            "groupId": "default",
                        }
                        for candidate in selection.candidates
                    ],
                    "ticketPrice": _number(float(combo.get("originalPrice") or 0)),
                },
                "spu": {"sessionId": session_id, "showId": show_id},
            }
        ],
        ver,
    )


def _standard_create(
    pre_request: dict,
    pre_response: dict,
    location_id: str,
    audience_ids: list[str],
    real_name_mode: str,
) -> dict:
    response_order = pre_response["data"]["orders"][0]
    order = pre_request["orders"][0]
    audience_iter = iter(audience_ids)
    items = []
    for item in order["items"]:
        sku = item["sku"]
        tickets = [
            {
                **ticket,
                "audienceId": (
                    next(audience_iter) if real_name_mode == "PER_TICKET" else ""
                ),
            }
            for ticket in sku["ticketItems"]
        ]
        items.append(
            _create_item(
                item,
                tickets,
                response_order["supportDeliveries"][0]["name"],
            )
        )
    if real_name_mode == "PER_TICKET" and next(audience_iter, None) is not None:
        raise RuntimeError("观演人数量多于票数")
    price = response_order["priceItems"][0]
    amount = price["priceItemVal"]
    result = _create_base(
        pre_request,
        location_id,
        items,
        _money(amount),
        _money(amount),
        [
            {
                "applyTickets": [],
                "priceItemName": "票款总额",
                "priceItemVal": _money(amount),
                "priceItemType": price["priceItemType"],
                "priceItemSpecies": price["priceItemSpecies"],
                "direction": price["direction"],
                "priceDisplay": _price_display(amount),
            }
        ],
    )
    if real_name_mode == "PER_ORDER":
        result["orders"][0]["many2OneAudience"] = {
            "audienceId": audience_ids[0],
            "sessionIds": list(
                dict.fromkeys(item["spu"]["sessionId"] for item in order["items"])
            ),
        }
    return result


def _combo_create(
    pre_request: dict,
    pre_response: dict,
    location_id: str,
) -> dict:
    response_order = pre_response["data"]["orders"][0]
    prices = sorted(
        response_order["priceItems"],
        key=lambda item: item["priceItemType"] != "TICKET_FEE",
    )
    delivery = response_order["supportDeliveries"][0]["name"]
    order = pre_request["orders"][0]
    items = [
        _create_item(
            item,
            [{**ticket, "audienceId": ""} for ticket in item["sku"]["ticketItems"]],
            delivery,
        )
        for item in order["items"]
    ]
    tickets = [
        (ticket, item["spu"], item["sku"]["skuId"])
        for item in order["items"]
        for ticket in item["sku"]["ticketItems"]
    ]
    params = []
    for price in prices:
        value = float(price["priceItemVal"])
        param = {
            "applyTickets": [],
            "priceItemName": (
                "票款总额"
                if price["priceItemType"] == "TICKET_FEE"
                else price["priceItemName"]
            ),
            "priceItemVal": _money(value),
            "priceItemType": price["priceItemType"],
            "priceItemSpecies": price["priceItemSpecies"],
            "direction": price["direction"],
            "priceDisplay": _price_display(value),
        }
        if price["priceItemType"] == "COMBO_DISCOUNT_FEE":
            discount = abs(value) / len(tickets)
            param.update(
                {
                    "applyTickets": [
                        [
                            {
                                "id": ticket["id"],
                                "discountAmount": _display_money(discount),
                                "seatPlanId": sku_id,
                                "seatConcreteId": ticket["seatConcreteId"],
                                "zoneConcreteId": ticket["zoneConcreteId"],
                                "seatGroupId": ticket["seatGroupId"],
                                "showId": spu["showId"],
                                "sessionId": spu["sessionId"],
                                "groupId": ticket["groupId"],
                            }
                            for ticket, spu, sku_id in tickets
                        ]
                    ],
                    "tag": "COMBO",
                    "discountId": "comboDiscount",
                    "priceItemTitle": price["priceItemName"],
                }
            )
        params.append(param)
    ticket_total = next(
        item for item in prices if item["priceItemType"] == "TICKET_FEE"
    )
    return _create_base(
        pre_request,
        location_id,
        items,
        _money(ticket_total["priceItemVal"]),
        _money(sum(float(item["priceItemVal"]) for item in prices)),
        params,
    )


def _create_item(item: dict, tickets: list[dict], delivery: str) -> dict:
    return {
        "sku": {
            **item["sku"],
            "ticketPrice": _money(item["sku"]["ticketPrice"]),
            "ticketItems": tickets,
        },
        "spu": {
            **item["spu"],
            "promotionVersionHash": "EMPTY_PROMOTION_HASH",
            "addPromoVersionHash": "EMPTY_PROMOTION_HASH",
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
