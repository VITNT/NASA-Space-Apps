"""Areas of interest that are not plain bounding boxes.

Every query already carries a bounding box. A shape narrows it further:

* a circle (point and radius) is tested exactly, by great-circle distance;
* a drawn polygon is tested exactly, by ray casting in SQL;
* a country is tested on the calendar's own 0.1° grid: a detection belongs to
  the country when the centre of its cell lies inside the Natural Earth border.

Shapes are compiled to SQL with numeric literals only, so nothing a client
sends is ever interpolated as text.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import pyarrow as pa
from fastapi import HTTPException

from .sensitivity import CELL_X_SQL, CELL_Y_SQL

EARTH_RADIUS_KM = 6371.0088
MAX_RADIUS_KM = 2000.0
MAX_POLYGON_VERTICES = 120
COUNTRIES = Path(__file__).resolve().parents[1] / "frontend" / "assets" / "geography" / "countries-50m.json"
# One integer per 0.1° cell, the same cell the calendar counts.
CELL_CODE_SQL = f"(({CELL_Y_SQL} + 900) * 3600 + ({CELL_X_SQL}) + 1800)::INTEGER"


@dataclass(frozen=True)
class Shape:
    """Extra SQL conditions for an area, and the tables they refer to."""

    conditions: tuple[str, ...] = ()
    tables: dict[str, Any] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.conditions)


def _numbers(text: str, name: str) -> list[float]:
    try:
        values = [float(part) for part in text.split(",")]
    except ValueError:
        raise HTTPException(status_code=422, detail=f"{name} must be a list of numbers") from None
    if not all(math.isfinite(value) for value in values):
        raise HTTPException(status_code=422, detail=f"{name} must be a list of finite numbers")
    return values


def circle_sql(text: str) -> str:
    """`lat,lon,radius_km` as a great-circle distance test."""
    values = _numbers(text, "circle")
    if len(values) != 3:
        raise HTTPException(status_code=422, detail="circle must be lat,lon,radius_km")
    lat, lon, radius = values
    if not (-90 <= lat <= 90 and -180 <= lon <= 180 and 0 < radius <= MAX_RADIUS_KM):
        raise HTTPException(status_code=422, detail=f"circle needs a valid point and a radius up to {MAX_RADIUS_KM:g} km")
    # Haversine, written so it stays accurate for short distances.
    return (
        f"2 * {EARTH_RADIUS_KM!r} * asin(least(1, sqrt("
        f"pow(sin(radians(latitude - {lat!r}) / 2), 2) + "
        f"cos(radians({lat!r})) * cos(radians(latitude)) * pow(sin(radians(longitude - {lon!r}) / 2), 2)"
        f"))) <= {radius!r}"
    )


def polygon_sql(text: str) -> str:
    """`lat,lon,lat,lon,…` as an even-odd ray-casting test."""
    values = _numbers(text, "polygon")
    if len(values) % 2 or not 6 <= len(values) <= 2 * MAX_POLYGON_VERTICES:
        raise HTTPException(status_code=422, detail=f"polygon needs 3 to {MAX_POLYGON_VERTICES} lat,lon pairs")
    ring = list(zip(values[0::2], values[1::2], strict=True))
    if not all(-90 <= lat <= 90 and -180 <= lon <= 180 for lat, lon in ring):
        raise HTTPException(status_code=422, detail="polygon vertices must be valid coordinates")
    crossings = []
    for (lat1, lon1), (lat2, lon2) in zip(ring, ring[1:] + ring[:1], strict=True):
        if lat1 == lat2:
            continue
        slope = (lon2 - lon1) / (lat2 - lat1)
        crossings.append(
            f"(({lat1!r} > latitude) <> ({lat2!r} > latitude) "
            f"AND longitude < {slope!r} * (latitude - {lat1!r}) + {lon1!r})::INTEGER"
        )
    if not crossings:
        raise HTTPException(status_code=422, detail="polygon has no area")
    return f"({' + '.join(crossings)}) % 2 = 1"


def _rings(topology: dict[str, Any], geometry: dict[str, Any]) -> list[list[tuple[float, float]]]:
    """A country's rings as (lon, lat) lists, decoded from TopoJSON arcs."""
    scale_x, scale_y = topology["transform"]["scale"]
    shift_x, shift_y = topology["transform"]["translate"]

    def arc(index: int) -> list[tuple[float, float]]:
        x = y = 0
        points = []
        for dx, dy in topology["arcs"][index if index >= 0 else ~index]:
            x += dx
            y += dy
            points.append((x * scale_x + shift_x, y * scale_y + shift_y))
        return points if index >= 0 else points[::-1]

    def ring(indexes: list[int]) -> list[tuple[float, float]]:
        points: list[tuple[float, float]] = []
        for index in indexes:
            points.extend(arc(index)[1 if points else 0:])
        return points

    polygons = geometry["arcs"] if geometry["type"] == "MultiPolygon" else [geometry["arcs"]]
    return [ring(indexes) for polygon in polygons for indexes in polygon]


def _longitude_span(rings: list[list[tuple[float, float]]]) -> tuple[float, float]:
    """The narrowest west-to-east span holding every ring; west > east when it crosses 180°."""
    spans = sorted((min(x for x, _ in ring), max(x for x, _ in ring)) for ring in rings)
    merged = [list(spans[0])]
    for west, east in spans[1:]:
        if west <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], east)
        else:
            merged.append([west, east])
    # The widest gap between spans, including the one across the antimeridian, is left out.
    gaps = [(merged[i + 1][0] - merged[i][1], i) for i in range(len(merged) - 1)]
    gaps.append((merged[0][0] + 360 - merged[-1][1], len(merged) - 1))
    _, index = max(gaps)
    if index == len(merged) - 1:
        return merged[0][0], merged[-1][1]
    return merged[index + 1][0], merged[index][1]


def _cells(rings: list[list[tuple[float, float]]]) -> list[int]:
    """Codes of the 0.1° cells whose centres lie inside the rings (even-odd)."""
    rows: dict[int, list[float]] = {}
    for ring in rings:
        for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1], strict=True):
            if y1 == y2:
                continue
            if y1 > y2:
                x1, y1, x2, y2 = x2, y2, x1, y1
            # Rows whose centre latitude (row + 0.5) / 10 falls in [y1, y2).
            first = math.ceil(y1 * 10 - .5)
            last = math.ceil(y2 * 10 - .5) - 1
            for row in range(max(first, -900), min(last, 899) + 1):
                lat = (row + .5) / 10
                rows.setdefault(row, []).append(x1 + (x2 - x1) * (lat - y1) / (y2 - y1))
    cells = []
    for row, crossings in rows.items():
        crossings.sort()
        for west, east in zip(crossings[0::2], crossings[1::2], strict=False):
            first = max(-1800, math.ceil(west * 10 - .5))
            last = min(1799, math.ceil(east * 10 - .5) - 1)
            cells.extend((row + 900) * 3600 + column + 1800 for column in range(first, last + 1))
    if not cells:
        # Smaller than a cell: fall back to the cells its outline touches.
        cells = list({
            (min(899, math.floor(y * 10)) + 900) * 3600 + (math.floor(x * 10) if x < 180 else -1800) + 1800
            for ring in rings for x, y in ring
        })
    return cells


@lru_cache(maxsize=1)
def _topology() -> dict[str, Any]:
    return json.loads(COUNTRIES.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _country_geometries() -> dict[str, dict[str, Any]]:
    geometries = _topology()["objects"]["countries"]["geometries"]
    return {geometry["properties"]["name"]: geometry for geometry in geometries if geometry.get("arcs")}


@lru_cache(maxsize=1)
def countries() -> list[dict[str, Any]]:
    """Every country with the bounding box the client should query."""
    topology = _topology()
    result = []
    for name, geometry in sorted(_country_geometries().items()):
        rings = _rings(topology, geometry)
        west, east = _longitude_span(rings)
        result.append({
            "name": name,
            "min_lat": round(max(-90.0, min(y for ring in rings for _, y in ring)), 4),
            "max_lat": round(min(90.0, max(y for ring in rings for _, y in ring)), 4),
            "min_lon": round(max(-180.0, west), 4),
            "max_lon": round(min(180.0, east), 4),
        })
    return result


@lru_cache(maxsize=24)
def _country_cells(name: str) -> pa.Table:
    geometry = _country_geometries().get(name)
    if geometry is None:
        raise HTTPException(status_code=422, detail="Unknown country")
    return pa.table({"cell": pa.array(_cells(_rings(_topology(), geometry)), type=pa.int32())})


def shape(circle: str | None = None, polygon: str | None = None, country: str | None = None) -> Shape:
    """Compile at most one shape into SQL conditions."""
    if sum(value is not None for value in (circle, polygon, country)) > 1:
        raise HTTPException(status_code=422, detail="Supply only one of circle, polygon and country")
    if circle is not None:
        return Shape((circle_sql(circle),))
    if polygon is not None:
        return Shape((polygon_sql(polygon),))
    if country is not None:
        return Shape(
            (f"{CELL_CODE_SQL} IN (SELECT cell FROM area_cells)",),
            {"area_cells": _country_cells(country)},
        )
    return Shape()
