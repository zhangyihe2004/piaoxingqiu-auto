"""选座套票分组与 FREE_COMBO 方案求解；不依赖页面与网络。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from piaoxingqiu_auto.domain.seating import Candidate, SeatSelection


@dataclass(frozen=True)
class ComboVariant:
    sku_id: str
    base_id: str
    quantity: int
    price: float
    components: tuple[tuple[str, float], ...]
    capacity: int
    display_tag: str
    order: int
    required: bool = False


@dataclass(frozen=True)
class ComboInstance:
    variant: ComboVariant
    candidates: tuple[Candidate, ...]


@dataclass(frozen=True)
class SeatOrderScheme:
    singles: tuple[Candidate, ...]
    combos: tuple[ComboInstance, ...]


def seat_order_scheme(
    selection: SeatSelection,
    combo_plans: tuple[dict[str, Any], ...],
    plan_order: tuple[str, ...],
    plan_prices: dict[str, float],
) -> SeatOrderScheme:
    grouped: dict[str, list[Candidate]] = {plan_id: [] for plan_id in plan_order}
    for candidate in selection.candidates:
        grouped.setdefault(candidate.plan_id, []).append(candidate)
    counts = {plan_id: len(items) for plan_id, items in grouped.items() if items}
    variants = _variants(combo_plans, set(counts), plan_prices)
    by_base: dict[str, tuple[ComboVariant, ...]] = {
        plan_id: tuple(
            sorted(
                (item for item in variants if item.base_id == plan_id),
                key=lambda item: (-item.quantity, item.sku_id),
            )
        )
        for plan_id in counts
    }
    combo_units = []
    for plan_id, quantity in counts.items():
        variant = next(
            (item for item in by_base[plan_id] if item.required),
            None,
        )
        if variant:
            units, remainder = divmod(quantity, variant.quantity)
            if remainder:
                raise RuntimeError(
                    f"独立套票 {variant.sku_id} 的座位数必须是 "
                    f"{variant.quantity} 的倍数"
                )
            combo_units.append((variant, units))
        else:
            combo_units.extend(
                _best_units(quantity, by_base[plan_id], plan_prices[plan_id])
            )
    pools = {
        plan_id: iter(
            sorted(
                candidates,
                key=lambda item: (item.seat.zone_id, item.seat.seat_no),
            )
        )
        for plan_id, candidates in grouped.items()
    }
    instances = tuple(
        ComboInstance(
            variant,
            tuple(next(pools[variant.base_id]) for _ in range(variant.quantity)),
        )
        for variant, unit_count in combo_units
        for _ in range(unit_count)
    )
    singles = tuple(
        candidate
        for plan_id in plan_order
        for candidate in pools[plan_id]
    )
    return SeatOrderScheme(singles, instances)


def _variants(
    plans: tuple[dict[str, Any], ...],
    wanted: set[str],
    plan_prices: dict[str, float],
) -> tuple[ComboVariant, ...]:
    result = []
    for order, plan in enumerate(plans):
        category = str(plan.get("seatPlanCategory") or "")
        if (
            category not in {"COMBO", "FREE_COMBO"}
            or plan.get("saleStarted") is False
            or plan.get("isStopSale") is True
        ):
            continue
        components: list[tuple[str, float]] = []
        component_bases: set[str] = set()
        for item in plan.get("items", ()):
            if isinstance(item, dict) and item.get("bizSeatPlanId"):
                base_id = str(item["bizSeatPlanId"])
                combo_item_id = (
                    str(item.get("stdSeatPlanId") or base_id)
                    if category == "COMBO"
                    else base_id
                )
                count = max(1, int(item.get("unitQty") or 1))
                price = float(item.get("originalPrice") or plan_prices.get(base_id, 0))
                components.extend((combo_item_id, price) for _ in range(count))
                component_bases.add(base_id)
        if not components or any(price <= 0 for _, price in components):
            continue
        independent = category == "COMBO" and str(plan.get("seatPlanId") or "") in wanted
        # FREE_COMBO 目前只采用已验证的同基础票档组合。
        if independent:
            base_id = str(plan["seatPlanId"])
        elif len(component_bases) == 1:
            base_id = next(iter(component_bases))
        else:
            continue
        if base_id not in wanted:
            continue
        price = float(plan.get("originalPrice") or 0)
        capacity = int(plan.get("canBuyCount") or 0)
        display_tag = str(
            plan.get("comboDisplayTag") or ("COMBO" if independent else "")
        )
        if (
            not plan.get("seatPlanId")
            or price <= 0
            or capacity <= 0
            or display_tag not in {"COMBO", "DISCOUNT"}
        ):
            continue
        result.append(
            ComboVariant(
                sku_id=str(plan["seatPlanId"]),
                base_id=base_id,
                quantity=len(components),
                price=price,
                components=tuple(components),
                capacity=capacity,
                display_tag=display_tag,
                order=order,
                required=independent,
            )
        )
    return tuple(result)


def _best_units(
    quantity: int,
    variants: tuple[ComboVariant, ...],
    base_price: float,
) -> tuple[tuple[ComboVariant, int], ...]:
    best_score: tuple | None = None
    best: tuple[tuple[ComboVariant, int], ...] = ()

    def visit(
        index: int,
        remaining: int,
        price: float,
        chosen: list[tuple[ComboVariant, int]],
    ) -> None:
        nonlocal best_score, best
        if index == len(variants):
            score = (
                round(price + remaining * base_price, 2),
                len(chosen) + bool(remaining),
                -sum(units for _, units in chosen) - remaining,
                tuple(sorted((item.order, units) for item, units in chosen)),
            )
            if best_score is None or score < best_score:
                best_score = score
                best = tuple(chosen)
            return
        variant = variants[index]
        max_units = min(variant.capacity, remaining // variant.quantity)
        for units in range(max_units + 1):
            if units:
                chosen.append((variant, units))
            visit(
                index + 1,
                remaining - units * variant.quantity,
                price + units * variant.price,
                chosen,
            )
            if units:
                chosen.pop()

    visit(0, quantity, 0, [])
    return best
