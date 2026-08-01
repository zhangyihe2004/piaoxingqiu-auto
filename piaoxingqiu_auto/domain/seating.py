"""与网络和页面无关的座位模型与组合算法。"""

from __future__ import annotations

import asyncio
import heapq
import math
import secrets
from collections import Counter
from collections.abc import Callable
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
        return plan_priority + compactness


@dataclass(frozen=True)
class SeatSelection:
    candidates: tuple[Candidate, ...]


GroupScore = Callable[[SeatGroup], tuple]


class SessionSeatClaims:
    """同一进程内原子隔离同场次各绑定正在提交的座位。"""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._owners: dict[tuple[str, str], str] = {}

    async def claim_first(
        self,
        session_id: str,
        owner: str,
        selections: tuple[SeatSelection, ...],
    ) -> SeatSelection | None:
        async with self._lock:
            for selection in selections:
                keys = tuple(
                    (session_id, candidate.seat.seat_id)
                    for candidate in selection.candidates
                )
                if all(
                    self._owners.get(key) in {None, owner}
                    for key in keys
                ):
                    self._release(session_id, owner)
                    self._owners.update(dict.fromkeys(keys, owner))
                    return selection
        return None

    async def release(self, session_id: str, owner: str) -> None:
        async with self._lock:
            self._release(session_id, owner)

    async def blocked(self, session_id: str, owner: str) -> frozenset[str]:
        async with self._lock:
            return frozenset(
                seat_id
                for (current_session, seat_id), current_owner in self._owners.items()
                if current_session == session_id and current_owner != owner
            )

    def _release(self, session_id: str, owner: str) -> None:
        for key in tuple(self._owners):
            if key[0] == session_id and self._owners[key] == owner:
                del self._owners[key]


@dataclass(frozen=True)
class SeatClaim:
    registry: SessionSeatClaims
    session_id: str
    owner: str

    async def claim_first(
        self, selections: tuple[SeatSelection, ...]
    ) -> SeatSelection | None:
        return await self.registry.claim_first(
            self.session_id,
            self.owner,
            selections,
        )

    async def blocked(self) -> frozenset[str]:
        return await self.registry.blocked(self.session_id, self.owner)

    async def release(self) -> None:
        await self.registry.release(self.session_id, self.owner)


def select_groups(
    candidates: tuple[Candidate, ...],
    quantity: int,
    plan_caps: dict[str, int],
    plan_units: dict[str, int],
    *,
    limit: int = 10,
    score: GroupScore | None = None,
) -> tuple[SeatGroup, ...]:
    if quantity < 1 or limit < 1:
        return ()
    ranked: list[SeatGroup] = []
    seen: set[tuple[str, ...]] = set()

    def append(groups: list[SeatGroup]) -> None:
        for group in _rank_groups(groups, limit - len(ranked), score):
            key = tuple(
                sorted(candidate.seat.seat_id for candidate in group.candidates)
            )
            if key not in seen:
                seen.add(key)
                ranked.append(group)
                if len(ranked) == limit:
                    return

    append(_continuous_groups(candidates, quantity, plan_caps, plan_units))
    if len(ranked) == limit:
        return tuple(ranked)
    by_zone: dict[str, list[Candidate]] = {}
    for candidate in candidates:
        by_zone.setdefault(candidate.seat.zone_id, []).append(candidate)
    same_zone = [
        group
        for zone_candidates in by_zone.values()
        if len(zone_candidates) >= quantity
        for group in _compact_groups(
            tuple(zone_candidates),
            quantity,
            cohesion=1,
            plan_caps=plan_caps,
            plan_units=plan_units,
        )
    ]
    append(same_zone)
    if len(ranked) < limit:
        append(
            _compact_groups(
                candidates,
                quantity,
                cohesion=2,
                plan_caps=plan_caps,
                plan_units=plan_units,
            )
        )
    return tuple(ranked)


def _continuous_groups(
    candidates: tuple[Candidate, ...],
    quantity: int,
    plan_caps: dict[str, int],
    plan_units: dict[str, int],
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
                _append_windows(groups, run, quantity, plan_caps, plan_units)
                run = []
            run.append(candidate)
        _append_windows(groups, run, quantity, plan_caps, plan_units)
    return groups


def _append_windows(
    groups: list[SeatGroup],
    run: list[Candidate],
    quantity: int,
    plan_caps: dict[str, int],
    plan_units: dict[str, int],
) -> None:
    for start in range(len(run) - quantity + 1):
        candidates = tuple(run[start : start + quantity])
        if valid_selection(candidates, plan_caps, plan_units):
            groups.append(SeatGroup(cohesion=0, candidates=candidates))


def _compact_groups(
    candidates: tuple[Candidate, ...],
    quantity: int,
    *,
    cohesion: int,
    plan_caps: dict[str, int],
    plan_units: dict[str, int],
) -> list[SeatGroup]:
    if len(candidates) < quantity:
        return []
    by_plan: dict[str, list[Candidate]] = {}
    for candidate in candidates:
        by_plan.setdefault(candidate.plan_id, []).append(candidate)
    groups: dict[tuple[str, ...], SeatGroup] = {}
    for anchor in candidates:
        choices: dict[int, tuple[Candidate, ...]] = {0: ()}
        for plan_id, plan_candidates in by_plan.items():
            ordered = heapq.nsmallest(
                min(len(plan_candidates), quantity),
                plan_candidates,
                key=lambda candidate: _distance(anchor.seat, candidate.seat),
            )
            unit = plan_units.get(plan_id, 1)
            cap = min(len(ordered), plan_caps.get(plan_id, 0), quantity)
            updated: dict[int, tuple[Candidate, ...]] = {}
            for total, selected in choices.items():
                for count in range(0, min(cap, quantity - total) + 1, unit):
                    combined = selected + tuple(ordered[:count])
                    size = total + count
                    previous = updated.get(size)
                    if previous is None or SeatGroup(
                        cohesion, combined
                    ).score < SeatGroup(cohesion, previous).score:
                        updated[size] = combined
            choices = updated
        nearest = choices.get(quantity)
        if nearest is None:
            continue
        key = tuple(sorted(candidate.seat.seat_id for candidate in nearest))
        groups[key] = SeatGroup(cohesion=cohesion, candidates=nearest)
    return list(groups.values())


def valid_selection(
    candidates: tuple[Candidate, ...],
    plan_caps: dict[str, int],
    plan_units: dict[str, int],
) -> bool:
    counts = Counter(candidate.plan_id for candidate in candidates)
    return counts <= Counter(plan_caps) and all(
        count % plan_units.get(plan_id, 1) == 0
        for plan_id, count in counts.items()
    )


def _rank_groups(
    groups: list[SeatGroup],
    limit: int,
    score: GroupScore | None,
) -> list[SeatGroup]:
    if score is not None:
        return sorted(groups, key=score)[:limit]
    by_score: dict[tuple, list[SeatGroup]] = {}
    for group in groups:
        by_score.setdefault(group.score, []).append(group)
    ranked: list[SeatGroup] = []
    random = secrets.SystemRandom()
    for score in sorted(by_score):
        pool = by_score[score]
        remaining = limit - len(ranked)
        if remaining <= 0:
            break
        if len(pool) > remaining:
            pool = random.sample(pool, remaining)
        else:
            random.shuffle(pool)
        ranked.extend(pool)
    return ranked


def _distance(left: Seat, right: Seat) -> float:
    return math.hypot(left.x - right.x, left.y - right.y)
