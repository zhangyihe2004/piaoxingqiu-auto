"""FREE_COMBO 方案求解；不依赖页面与网络。"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any

from piaoxingqiu_auto.domain.seating import Candidate, SeatSelection


@dataclass(frozen=True)
class ComboVariant:
    sku_id: str
    base_id: str
    quantity: int
    price: float
    capacity: int


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
    variants = _variants(combo_plans, set(counts))
    by_base: dict[str, tuple[ComboVariant, ...]] = {
        plan_id: tuple(
            sorted(
                (item for item in variants if item.base_id == plan_id),
                key=lambda item: item.sku_id,
            )
        )
        for plan_id in counts
    }
    combo_units = tuple(
        unit
        for plan_id, quantity in counts.items()
        for unit in _best_units(quantity, by_base[plan_id], plan_prices[plan_id])
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
) -> tuple[ComboVariant, ...]:
    result = []
    for plan in plans:
        if (
            plan.get("seatPlanCategory") != "FREE_COMBO"
            or plan.get("saleStarted") is False
            or plan.get("isStopSale") is True
        ):
            continue
        components: Counter[str] = Counter()
        for item in plan.get("items", ()):
            if isinstance(item, dict) and item.get("bizSeatPlanId"):
                components[str(item["bizSeatPlanId"])] += max(
                    1, int(item.get("unitQty") or 1)
                )
        # 已验证的官方 FREE_COMBO 都在同一基础票档内组合；跨基础票档
        # 的折扣分摊规则未知，宁可不提交也不猜 create_order。
        if len(components) != 1:
            continue
        base_id, item_quantity = next(iter(components.items()))
        if base_id not in wanted:
            continue
        price = float(plan.get("originalPrice") or 0)
        capacity = int(plan.get("canBuyCount") or 0)
        if not plan.get("seatPlanId") or price <= 0 or capacity <= 0:
            continue
        result.append(
            ComboVariant(
                sku_id=str(plan["seatPlanId"]),
                base_id=base_id,
                quantity=item_quantity,
                price=price,
                capacity=capacity,
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
            sizes = sorted(
                (
                    item.quantity
                    for item, units in chosen
                    for _ in range(units)
                ),
                reverse=True,
            )
            exact = len(chosen) == 1 and chosen[0][1] == 1 and remaining == 0
            score = (
                not exact,
                round(price + remaining * base_price, 2),
                tuple(-size for size in sizes)
                + (0,) * (quantity - len(sizes)),
                tuple((item.sku_id, units) for item, units in chosen),
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
