"""纯几何座位排序；不包含库存、页面或下单逻辑。"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, replace
from itertools import combinations
from statistics import median
from typing import Any

from piaoxingqiu_auto.domain.seating import Seat, SeatGroup


Point = tuple[float, float]


@dataclass(frozen=True)
class Venue:
    kind: str
    center: Point | None
    long_axis: Point | None
    stands: dict[str, Point]


class PositionScorer:
    def __init__(
        self,
        venue: Venue,
        zones: dict[str, tuple[Seat, ...]],
    ) -> None:
        self.venue = venue
        self.zones = zones
        self.spacing = seat_spacing(zones)
        if venue.kind == "theater":
            self.theater_center = venue.center or _bounds_center(
                tuple((seat.x, seat.y) for seats in zones.values() for seat in seats)
            )
            self.row_axis = _row_axis(zones)

    def __call__(self, group: SeatGroup) -> tuple:
        plans = tuple(sorted(candidate.plan_rank for candidate in group.candidates))
        compactness = (
            0
            if group.cohesion == 0
            else _distance_level(_diameter(group), self.spacing)
        )
        quality = (
            self._stadium(group)
            if self.venue.kind == "stadium"
            else self._theater(group)
        )
        return (
            plans,
            compactness,
            quality,
            tuple(sorted(item.seat.seat_id for item in group.candidates)),
        )

    def _stadium(self, group: SeatGroup) -> tuple[float, float]:
        center = self.venue.center
        if center is None:
            return math.inf, math.inf
        stand_centers: dict[str, Point] = {}
        for candidate in group.candidates:
            seat = candidate.seat
            stand_center = self.venue.stands.get(seat.zone_id) or self.venue.stands.get(
                seat.zone_name
            )
            stand_centers[seat.zone_id] = (
                stand_center
                if stand_center is not None
                else _seat_center(self.zones.get(seat.zone_id, (seat,)))
            )
        centers = tuple(stand_centers.values())
        axis = self.venue.long_axis
        stand_distance = max(
            (
                abs(_dot(_subtract(point, center), axis))
                if axis is not None
                else _distance(point, center)
                for point in centers
            ),
            default=math.inf,
        )
        seat_distance = max(
            _distance((item.seat.x, item.seat.y), center)
            for item in group.candidates
        )
        return stand_distance, seat_distance

    def _theater(self, group: SeatGroup) -> tuple[int, float]:
        distance = max(
            abs(
                _dot(
                    _subtract((item.seat.x, item.seat.y), self.theater_center),
                    self.row_axis,
                )
            )
            for item in group.candidates
        )
        return (
            _distance_level(distance, self.spacing),
            max(_row_number(item.seat.row) for item in group.candidates),
        )


def venue_from_features(features: Any) -> Venue:
    """有 innerTemplate 即球场，否则即剧场。"""
    stands: dict[str, Point] = {}
    stand_points: list[Point] = []
    fields: list[tuple[Point, ...]] = []
    for feature in features if isinstance(features, list) else ():
        if not isinstance(feature, dict):
            continue
        properties = feature.get("properties") or {}
        geometry = feature.get("geometry") or {}
        if not isinstance(properties, dict) or not isinstance(geometry, dict):
            continue
        polygons = _polygons(geometry)
        if properties.get("level") == "zone":
            for points in polygons:
                stand_points.extend(points)
                stand_center = _polygon_centroid(points)
                for key in ("zoneConcreteId", "id", "code", "text", "standText"):
                    if value := str(properties.get(key) or "").strip():
                        stands[value] = stand_center
        elif properties.get("level") == "innerTemplate":
            fields.extend(polygons)
    field = max(fields, key=lambda item: abs(_polygon_area(item)), default=None)
    if field is None:
        return Venue(
            "theater",
            _bounds_center(tuple(stand_points)) if stand_points else None,
            None,
            stands,
        )
    box = _minimum_box(field)
    center, axis = box if box is not None else (_bounds_center(field), None)
    return Venue("stadium", center, axis, stands)


def index_seats(seats: tuple[Seat, ...]) -> tuple[Seat, ...]:
    """按真实坐标重建排内位置，并用座位间距保留物理空位。"""
    zones = {seats[0].zone_id: seats} if seats else {}
    spacing = seat_spacing(zones)
    indexes: dict[str, int] = {}
    for row in _rows(zones).values():
        previous: Seat | None = None
        index = 0
        for seat in _ordered_row(row):
            if previous is not None:
                index += max(
                    1,
                    _distance_level(
                        _distance((previous.x, previous.y), (seat.x, seat.y)),
                        spacing,
                    ),
                )
            indexes[seat.seat_id] = index
            previous = seat
    return tuple(
        replace(seat, row_index=indexes[seat.seat_id])
        for seat in seats
    )


def seat_spacing(zones: dict[str, tuple[Seat, ...]]) -> float:
    gaps: list[float] = []
    for seats in _rows(zones).values():
        ordered = _ordered_row(seats)
        gaps.extend(
            gap
            for left, right in zip(ordered, ordered[1:])
            if (gap := _distance((left.x, left.y), (right.x, right.y))) > 0
        )
    return median(gaps) if gaps else 1.0


def _rows(zones: dict[str, tuple[Seat, ...]]) -> dict[tuple[str, str], list[Seat]]:
    rows: dict[tuple[str, str], list[Seat]] = {}
    for seats in zones.values():
        for seat in seats:
            rows.setdefault((seat.zone_id, seat.row), []).append(seat)
    return rows


def _ordered_row(seats: list[Seat]) -> list[Seat]:
    axis = _row_direction(seats) or (1.0, 0.0)
    return sorted(seats, key=lambda seat: (_dot((seat.x, seat.y), axis), seat.seat_id))


def _row_axis(zones: dict[str, tuple[Seat, ...]]) -> Point:
    axes = [axis for seats in _rows(zones).values() if (axis := _row_direction(seats))]
    if not axes:
        return 1.0, 0.0
    dx, dy = sum(axis[0] for axis in axes), sum(axis[1] for axis in axes)
    length = math.hypot(dx, dy)
    return (dx / length, dy / length) if length else (1.0, 0.0)


def _row_direction(seats: list[Seat]) -> Point | None:
    pair = max(
        combinations(seats, 2),
        key=lambda pair: _distance(
            (pair[0].x, pair[0].y), (pair[1].x, pair[1].y)
        ),
        default=None,
    )
    if pair is None:
        return None
    dx, dy = pair[1].x - pair[0].x, pair[1].y - pair[0].y
    length = math.hypot(dx, dy)
    if not length:
        return None
    dx, dy = dx / length, dy / length
    return (-dx, -dy) if dx < 0 or (dx == 0 and dy < 0) else (dx, dy)


def _diameter(group: SeatGroup) -> float:
    return max(
        (
            _distance((left.seat.x, left.seat.y), (right.seat.x, right.seat.y))
            for left, right in combinations(group.candidates, 2)
        ),
        default=0.0,
    )


def _row_number(value: str) -> float:
    match = re.search(r"(\d+)\s*排", value)
    return float(match.group(1)) if match else math.inf


def _distance_level(distance: float, spacing: float) -> int:
    return math.floor(distance / spacing + 0.5)


def _seat_center(seats: tuple[Seat, ...]) -> Point:
    return (
        sum(seat.x for seat in seats) / len(seats),
        sum(seat.y for seat in seats) / len(seats),
    )


def _polygons(geometry: dict[str, Any]) -> list[tuple[Point, ...]]:
    coordinates = geometry.get("coordinates")
    if geometry.get("type") == "Polygon" and isinstance(coordinates, list):
        rings = coordinates[:1]
    elif geometry.get("type") == "MultiPolygon" and isinstance(coordinates, list):
        rings = [item[0] for item in coordinates if isinstance(item, list) and item]
    else:
        return []
    return [points for ring in rings if len(points := _points(ring)) >= 3]


def _points(values: Any) -> tuple[Point, ...]:
    if not isinstance(values, list):
        return ()
    return tuple(
        (float(point[0]), float(point[1]))
        for point in values
        if isinstance(point, list)
        and len(point) >= 2
        and isinstance(point[0], int | float)
        and isinstance(point[1], int | float)
    )


def _polygon_area(points: tuple[Point, ...]) -> float:
    return sum(
        left[0] * right[1] - right[0] * left[1]
        for left, right in zip(points, points[1:] + points[:1])
    ) / 2


def _polygon_centroid(points: tuple[Point, ...]) -> Point:
    area = _polygon_area(points)
    if abs(area) < 1e-15:
        return _bounds_center(points)
    pairs = tuple(zip(points, points[1:] + points[:1]))
    return (
        sum((a[0] + b[0]) * (a[0] * b[1] - b[0] * a[1]) for a, b in pairs)
        / (6 * area),
        sum((a[1] + b[1]) * (a[0] * b[1] - b[0] * a[1]) for a, b in pairs)
        / (6 * area),
    )


def _bounds_center(points: tuple[Point, ...]) -> Point:
    xs, ys = zip(*points)
    return (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2


def _minimum_box(points: tuple[Point, ...]) -> tuple[Point, Point] | None:
    hull = _convex_hull(points)
    best: tuple[float, Point, Point, float, float, float, float] | None = None
    for left, right in zip(hull, hull[1:] + hull[:1]):
        dx, dy = right[0] - left[0], right[1] - left[1]
        length = math.hypot(dx, dy)
        if not length:
            continue
        axis = dx / length, dy / length
        normal = -axis[1], axis[0]
        along = tuple(_dot(point, axis) for point in hull)
        across = tuple(_dot(point, normal) for point in hull)
        width, height = max(along) - min(along), max(across) - min(across)
        candidate = (
            width * height,
            axis,
            normal,
            (min(along) + max(along)) / 2,
            (min(across) + max(across)) / 2,
            width,
            height,
        )
        if best is None or candidate[0] < best[0]:
            best = candidate
    if best is None:
        return None
    _, axis, normal, along_center, across_center, width, height = best
    if min(width, height) <= 0 or max(width, height) / min(width, height) < 1.1:
        return None
    center = (
        along_center * axis[0] + across_center * normal[0],
        along_center * axis[1] + across_center * normal[1],
    )
    return center, axis if width >= height else normal


def _convex_hull(points: tuple[Point, ...]) -> tuple[Point, ...]:
    ordered = sorted(set(points))
    if len(ordered) <= 1:
        return tuple(ordered)

    def cross(origin: Point, left: Point, right: Point) -> float:
        return (left[0] - origin[0]) * (right[1] - origin[1]) - (
            left[1] - origin[1]
        ) * (right[0] - origin[0])

    lower: list[Point] = []
    upper: list[Point] = []
    for point in ordered:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], point) <= 0:
            lower.pop()
        lower.append(point)
    for point in reversed(ordered):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], point) <= 0:
            upper.pop()
        upper.append(point)
    return tuple(lower[:-1] + upper[:-1])


def _subtract(left: Point, right: Point) -> Point:
    return left[0] - right[0], left[1] - right[1]


def _dot(left: Point, right: Point) -> float:
    return left[0] * right[0] + left[1] * right[1]


def _distance(left: Point, right: Point) -> float:
    return math.hypot(left[0] - right[0], left[1] - right[1])
