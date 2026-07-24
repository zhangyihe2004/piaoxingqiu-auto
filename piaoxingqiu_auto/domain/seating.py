"""与网络和页面无关的座位模型与组合算法。"""

from __future__ import annotations

import heapq
import math
import secrets
from dataclasses import dataclass


@dataclass(frozen=True)
class Seat:
    zone_id: str
    zone_name: str
    seat_id: str
    row: str
    seat_no: int
    x: float
    y: float
    row_index: int = 0


@dataclass(frozen=True)
class Candidate:
    seat: Seat
    plan: str
    plan_id: str
    plan_rank: int


@dataclass(frozen=True)
class SeatGroup:
    cohesion: int
    candidates: tuple[Candidate, ...]

    @property
    def score(self) -> tuple:
        seats = self.candidates
        plan_priority = tuple(sorted(item.plan_rank for item in seats))
        if self.cohesion == 0:
            return plan_priority
        compactness = min(
            (max(distances), sum(distances))
            for anchor in seats
            if (distances := tuple(_distance(anchor.seat, item.seat) for item in seats))
        )
        return compactness + plan_priority


@dataclass(frozen=True)
class SeatSelection:
    candidates: tuple[Candidate, ...]


def select_group(
    candidates: tuple[Candidate, ...],
    quantity: int,
    plan_caps: dict[str, int] | None = None,
) -> SeatGroup | None:
    return _select_spatial_group(candidates, quantity, plan_caps)


def _select_spatial_group(
    candidates: tuple[Candidate, ...],
    quantity: int,
    plan_caps: dict[str, int] | None = None,
) -> SeatGroup | None:
    continuous = _continuous_groups(candidates, quantity, plan_caps)
    if continuous:
        return _random_best(continuous)

    by_zone: dict[str, list[Candidate]] = {}
    for candidate in candidates:
        by_zone.setdefault(candidate.seat.zone_id, []).append(candidate)
    same_zone = [
        group
        for zone_candidates in by_zone.values()
        if len(zone_candidates) >= quantity
        if (
            group := _compact_group(
                tuple(zone_candidates), quantity, cohesion=1, plan_caps=plan_caps
            )
        )
    ]
    if same_zone:
        return _random_best(same_zone)
    return _compact_group(candidates, quantity, cohesion=2, plan_caps=plan_caps)


def _continuous_groups(
    candidates: tuple[Candidate, ...],
    quantity: int,
    plan_caps: dict[str, int] | None = None,
) -> list[SeatGroup]:
    rows: dict[tuple[str, str], list[Candidate]] = {}
    for candidate in candidates:
        rows.setdefault((candidate.seat.zone_id, candidate.seat.row), []).append(
            candidate
        )
    groups: list[SeatGroup] = []
    for row in rows.values():
        ordered = sorted(row, key=lambda candidate: candidate.seat.row_index)
        run: list[Candidate] = []
        for candidate in ordered:
            if run and candidate.seat.row_index != run[-1].seat.row_index + 1:
                _append_windows(groups, run, quantity, plan_caps)
                run = []
            run.append(candidate)
        _append_windows(groups, run, quantity, plan_caps)
    return groups


def _append_windows(
    groups: list[SeatGroup],
    run: list[Candidate],
    quantity: int,
    plan_caps: dict[str, int] | None,
) -> None:
    for start in range(len(run) - quantity + 1):
        candidates = tuple(run[start : start + quantity])
        if _within_caps(candidates, plan_caps):
            groups.append(SeatGroup(cohesion=0, candidates=candidates))


def _compact_group(
    candidates: tuple[Candidate, ...],
    quantity: int,
    *,
    cohesion: int,
    plan_caps: dict[str, int] | None = None,
) -> SeatGroup | None:
    if len(candidates) < quantity:
        return None
    groups: dict[tuple[str, ...], SeatGroup] = {}
    for anchor in candidates:
        ordered = heapq.nsmallest(
            len(candidates),
            candidates,
            key=lambda candidate: _distance(anchor.seat, candidate.seat),
        )
        counts: dict[str, int] = {}
        nearest_list: list[Candidate] = []
        for candidate in ordered:
            count = counts.get(candidate.plan_id, 0)
            if plan_caps is not None and count >= plan_caps.get(candidate.plan_id, 0):
                continue
            nearest_list.append(candidate)
            counts[candidate.plan_id] = count + 1
            if len(nearest_list) == quantity:
                break
        if len(nearest_list) < quantity:
            continue
        nearest = tuple(nearest_list)
        key = tuple(sorted(candidate.seat.seat_id for candidate in nearest))
        groups[key] = SeatGroup(cohesion=cohesion, candidates=nearest)
    return _random_best(list(groups.values())) if groups else None


def _within_caps(
    candidates: tuple[Candidate, ...],
    plan_caps: dict[str, int] | None,
) -> bool:
    if plan_caps is None:
        return True
    counts: dict[str, int] = {}
    for candidate in candidates:
        counts[candidate.plan_id] = counts.get(candidate.plan_id, 0) + 1
    return all(count <= plan_caps.get(plan_id, 0) for plan_id, count in counts.items())


def _random_best(groups: list[SeatGroup]) -> SeatGroup:
    best_score = min(group.score for group in groups)
    return secrets.choice([group for group in groups if group.score == best_score])


def _distance(left: Seat, right: Seat) -> float:
    return math.hypot(left.x - right.x, left.y - right.y)
