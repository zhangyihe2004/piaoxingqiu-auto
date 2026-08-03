"""票星球官方页面订单模型桥接。"""

from __future__ import annotations

from typing import Any


_LOAD = r"""
async ({showId, sessionId}) => {
  window.__pxqOfficialOrderModel ||= (async () => {
    const resources = performance.getEntriesByType("resource")
      .map(item => item.name);
    const mainUrl = resources.find(
      url => /\/index-[^/]+\.js(?:\?|$)/.test(url)
    );
    if (!mainUrl) throw new Error("未找到 booking 主模块");
    const mainText = await fetch(mainUrl).then(response => response.text());
    const chunkUrls = name => {
      const escaped = name.replaceAll("-", "\\-");
      const pattern = new RegExp(
        `${escaped}\\.[A-Za-z0-9_-]+\\.js`, "g"
      );
      return [...new Set([
        ...resources.filter(url => url.includes(`/${name}.`)),
        ...(mainText.match(pattern) || [])
          .map(file => new URL(file, mainUrl).href),
      ])];
    };
    const [shoppingModules, confirmationModules] = await Promise.all([
      Promise.all(chunkUrls("shopping").map(url => import(url))),
      Promise.all(chunkUrls("audience-popup").map(url => import(url))),
    ]);
    const shopping = shoppingModules.flatMap(Object.values);
    const confirmation = confirmationModules.flatMap(Object.values);
    const SaleAssistant = shopping.find(value =>
      typeof value === "function"
      && typeof value.prototype?.selectShow === "function"
      && typeof value.prototype?.takeTickets === "function"
      && typeof value.prototype?.updatePreorder === "function"
    );
    const PromotionDiscount = shopping.find(
      value => value?.PromotionDiscountType === "promotionDiscount"
    );
    const ComboDiscount = shopping.find(
      value => value?.ComboDiscountType === "comboDiscount"
    );
    const ConfirmOrder = confirmation.find(value =>
      typeof value === "function"
      && typeof value.prototype?.getCreateOrderParam === "function"
      && typeof value.prototype?.checkCreateOrderParam === "function"
    );
    if (!SaleAssistant || !PromotionDiscount || !ComboDiscount || !ConfirmOrder) {
      throw new Error("未找到官方订单模型");
    }
    return {
      SaleAssistant,
      PromotionDiscount,
      ComboDiscount,
      ConfirmOrder,
      data: new Map(),
    };
  })();
  const model = await window.__pxqOfficialOrderModel;
  const key = `${showId}:${sessionId}`;
  if (!model.data.has(key)) {
    model.data.set(key, Promise.all([
      fetch(
        `/cyy_gatewayapi/show/pub/v5/show/${showId}/sessions`
        + "?source=FROM_QUICK_ORDER&src=WEB"
      ).then(response => response.json()),
      fetch(
        `/cyy_gatewayapi/show/pub/v5/show/${showId}/session/`
        + `${sessionId}/seat_plans?source=FROM_QUICK_ORDER&src=WEB`
      ).then(response => response.json()),
      fetch(
        `/cyy_gatewayapi/show/pub/v3/promotions/list?showId=${showId}`
        + "&verControl=true"
      ).then(response => response.json()),
    ]).catch(error => {
      model.data.delete(key);
      throw error;
    }));
  }
  await model.data.get(key);
  return true;
}
"""


_BUILD = r"""
async ({preRequest, preResponse, audienceIds, realNameMode, locationId}) => {
  const model = await window.__pxqOfficialOrderModel;
  const order = preRequest.orders[0];
  const firstItem = order.items[0];
  const showId = firstItem.spu.showId;
  const sessionId = firstItem.spu.sessionId;
  const key = `${showId}:${sessionId}`;
  const [sessionResponse, planResponse, promotionResponse] =
    await model.data.get(key);
  const session = sessionResponse.data?.find(
    item => item.bizShowSessionId === sessionId
  );
  const plans = planResponse.data?.seatPlans || [];
  if (!session || !plans.length) {
    throw new Error("官方订单模型缺少场次或票档");
  }

  const tickets = [];
  const freeComboInstances = [];
  for (const item of order.items) {
    const plan = plans.find(value => value.seatPlanId === item.sku.skuId);
    if (!plan) throw new Error(`未找到票档 ${item.sku.skuId}`);
    const isCombo = item.sku.skuType === "COMBO";
    const isFreeCombo = isCombo && plan.seatPlanCategory === "FREE_COMBO";
    if (isFreeCombo) {
      const instances = {};
      for (const ticket of item.sku.ticketItems) {
        const id = ticket.seatGroupId || ticket.id;
        (instances[id] ||= []).push(ticket.id);
      }
      for (const ticketGenerateIds of Object.values(instances)) {
        freeComboInstances.push({comboId: plan.seatPlanId, ticketGenerateIds});
      }
    }
    for (const ticket of item.sku.ticketItems) {
      const ticketPlan = isCombo
        ? plan.items?.find(value => [
            value.stdSeatPlanId,
            value.bizSeatPlanId,
            value.seatPlanId,
          ].includes(ticket.comboItemId))
        : plan;
      if (!ticketPlan) throw new Error("官方订单模型未找到套票子票档");
      tickets.push({
        generateId: ticket.id,
        stdSeatPlanId: ticketPlan.stdSeatPlanId,
        seatPlanId: ticketPlan.bizSeatPlanId || ticketPlan.seatPlanId,
        seatPlanName: ticketPlan.itemSeatPlanName || ticketPlan.seatPlanName,
        originalPrice: ticketPlan.originalPrice,
        ticketPrice: ticketPlan.originalPrice,
        seatId: ticket.seatConcreteId,
        zoneConcreteId: ticket.zoneConcreteId,
        groupId: ticket.groupId,
        ...(isCombo && !isFreeCombo ? {
          combo: {
            ...plan,
            id: ticket.seatGroupId || ticket.id,
            comboId: plan.seatPlanId,
            comboPrice: plan.originalPrice,
          },
        } : {}),
      });
    }
  }

  let assistant = new model.SaleAssistant();
  assistant.selectShow({showId, stdShowId: plans[0].stdShowId});
  assistant.selectSession(session);
  assistant.takeTickets(tickets);
  assistant.useDiscount(new model.ComboDiscount());
  if (freeComboInstances.length) {
    assistant = assistant.applyFreeComboPlan({
      seatPlans: plans,
      plans: [{groupId: order.groupId, comboInstances: freeComboInstances}],
    });
  }
  const promotions = promotionResponse.data || {};
  assistant.useDiscount(new model.PromotionDiscount([
    ...(promotions.sameSessionPromotions || []),
    ...(promotions.crossSessionPromotions || []),
  ]));
  assistant.updatePreorder(preResponse.data);

  const confirm = new model.ConfirmOrder(preResponse.data);
  for (const group of confirm.orders || []) {
    group.audiences = audienceIds.map(id => ({id}));
    const delivery = group.supportDeliveries?.[0];
    if (delivery) {
      confirm.selectDeliver(delivery, {groupId: group.groupId, showId});
    }
  }
  const mappedTickets = assistant.tickets.map((ticket, index) => ({
    generateId: ticket.generateId,
    audienceId: realNameMode === "PER_TICKET"
      ? audienceIds[index] || ""
      : "",
  }));
  const groupAudienceInfoMap = new Map();
  if (realNameMode === "PER_ORDER" && audienceIds.length) {
    groupAudienceInfoMap.set(order.groupId, {
      displayedAudienceIds: audienceIds,
    });
  }
  const payload = confirm.getCreateOrderParam(
    assistant,
    null,
    () => [],
    value => value,
    {
      localSite: locationId,
      bsCityId: locationId,
      tickets: mappedTickets,
      promotionVersionHash:
        promotions.promotionVersionHash || "EMPTY_PROMOTION_HASH",
      addPromoVersionHash:
        promotions.addPromoVersionHash || "EMPTY_PROMOTION_HASH",
      orderCurrency: preRequest.clientCurrency,
      exchangeRate: 1,
      scene: preRequest.scene,
      orderFrom: "DEFAULT",
    },
    order.orderSource,
    "",
    "",
    {groupAudienceInfoMap},
  );
  payload.src = preRequest.src;
  payload.ver = preRequest.ver;
  for (const resultOrder of payload.orders || []) {
    resultOrder.priorityId ??= order.priorityId || "";
    for (const item of resultOrder.items || []) {
      for (const ticket of item.sku?.ticketItems || []) {
        for (const key of ["comboItemId", "seatGroupId", "ticketSeatId"]) {
          if (!ticket[key]) delete ticket[key];
        }
      }
    }
  }
  for (const item of payload.priceItemParams || []) {
    if (!item.priceItemId) delete item.priceItemId;
  }
  return payload;
}
"""


class OfficialOrderModel:
    def __init__(self, page, show_id: str, session_id: str) -> None:
        self.page = page
        self.show_id = show_id
        self.session_id = session_id

    async def warm(self) -> None:
        await self.page.evaluate(
            _LOAD,
            {"showId": self.show_id, "sessionId": self.session_id},
        )

    async def build(
        self,
        pre_request: dict,
        pre_response: dict,
        audience_ids: list[str],
        real_name_mode: str,
        location_id: str,
    ) -> dict:
        payload = await self.page.evaluate(
            _BUILD,
            {
                "preRequest": pre_request,
                "preResponse": pre_response,
                "audienceIds": audience_ids,
                "realNameMode": real_name_mode,
                "locationId": location_id,
            },
        )
        if not isinstance(payload, dict):
            raise RuntimeError("官方订单模型未返回创建参数")
        _validate(payload, pre_request, audience_ids, real_name_mode)
        return payload


def _validate(
    payload: dict,
    pre_request: dict,
    audience_ids: list[str],
    real_name_mode: str,
) -> None:
    expected_items = pre_request["orders"][0]["items"]
    expected_tickets = [
        ticket
        for item in expected_items
        for ticket in item["sku"]["ticketItems"]
    ]
    orders = payload.get("orders")
    if not isinstance(orders, list) or not orders:
        raise RuntimeError("官方订单模型未生成订单")
    items = [item for order in orders for item in order.get("items", [])]
    tickets = [
        ticket
        for item in items
        for ticket in item.get("sku", {}).get("ticketItems", [])
    ]
    if len(tickets) != len(expected_tickets):
        raise RuntimeError("官方订单模型生成的票数不一致")
    expected_seats = sorted(
        str(ticket.get("seatConcreteId") or "")
        for ticket in expected_tickets
        if ticket.get("seatConcreteId")
    )
    actual_seats = sorted(
        str(ticket.get("seatConcreteId") or "")
        for ticket in tickets
        if ticket.get("seatConcreteId")
    )
    if actual_seats != expected_seats:
        raise RuntimeError("官方订单模型生成的座位不一致")
    expected_sessions = {
        (str(item["spu"]["showId"]), str(item["spu"]["sessionId"]))
        for item in expected_items
    }
    actual_sessions = {
        (str(item["spu"]["showId"]), str(item["spu"]["sessionId"]))
        for item in items
    }
    if actual_sessions != expected_sessions:
        raise RuntimeError("官方订单模型生成的场次不一致")
    if real_name_mode == "PER_TICKET" and [
        str(ticket.get("audienceId") or "") for ticket in tickets
    ] != audience_ids:
        raise RuntimeError("官方订单模型生成的观演人不一致")
    if real_name_mode == "PER_ORDER" and not any(
        str(order.get("many2OneAudience", {}).get("audienceId") or "")
        == audience_ids[0]
        for order in orders
    ):
        raise RuntimeError("官方订单模型未生成一单一证信息")
    payment = payload.get("paymentParam")
    try:
        payable = float(payment["payAmount"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("官方订单模型未生成应付金额") from exc
    if payable < 0:
        raise RuntimeError("官方订单模型生成的应付金额无效")


def has_discount(payload: dict[str, Any]) -> bool:
    return any(
        float(item.get("priceItemVal") or 0) < 0
        for item in payload.get("priceItemParams", [])
    )
