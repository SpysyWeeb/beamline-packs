"""The front door of a large building that is a point of interest (docs/next-round.md item 28).

A search entry of an area uses its centroid. For a large store that point is the middle of the roof,
60 m or more from every lane, and the lane nearest to it is as often the service road behind the
building as the lane along its front. So the search index also carries, for a large building, the
point of its outline where customers go in:

1. a mapped main entrance (a node of the outline tagged entrance=main);
2. else a mapped customer entrance (entrance=yes, shop, entrance, secondary): the only one, or with
   several the one nearest to the customer parking around the building;
3. else, for a shop only, the point of the outline nearest to the middle of that parking (within
   PARKING_WITHIN_M of the building: each parking area counted by its area there, each parking
   aisle by its length there times AISLE_WIDTH_M, since many lots are mapped by their aisles alone);
4. else none: the entry keeps its centroid alone.

Rule 3 was measured on the 209 large buildings of Pennsylvania (extract 261114) that have exactly one
mapped main entrance, the entrance as the answer key. Shops (85, 74 with parking mapped within 60 m):
the point is on the opposite side of the building from the entrance for 4 %, median 19 m from the
entrance (areas alone: 62 answered, 3 %, 18 m; aisles alone: 67, 7 %, 19 m). Schools, churches,
libraries and offices: on the opposite side for 25-40 %, so rule 3 is not used for them (their
parking is as often behind or beside the building).

Never a service, emergency, exit, garage, staircase or home entrance, and never one tagged
access=private or no. Only for a building at least 2 x MIN_HALF_WIDTH_M across (its centroid at
least that far inside its outline, or outside it): a small building's centroid is near its lanes
already and keeps the behaviour its routes were tuned with.

Pure functions on shapely geometries in degrees; lengths in metres on a local flat grid.
"""
from __future__ import annotations

import math

import numpy as np
import shapely
import shapely.ops

MIN_HALF_WIDTH_M = 15.0     # the centroid at least this far inside the outline
PARKING_WITHIN_M = 60.0     # customer parking counted within this distance of the building
PARKING_MIN_M2 = 300.0      # less parking than this around the building says nothing about its front
AISLE_WIDTH_M = 16.0        # a parking aisle counts as a strip this wide (the lane and a row each side)
ENTRANCE_MAIN = ("main",)
ENTRANCE_CUSTOMER = ("yes", "shop", "entrance", "secondary")
ENTRANCE_NO_ACCESS = ("private", "no")
PARKING_SKIP = ("underground", "multi-storey", "rooftop")   # not a lot in front of a door
PARKING_NO_ACCESS = ("private", "no")


def entrance_kind(tags) -> str | None:
    """"main", "customer" or None for a node's tags (the entrance value and its access)."""
    v = tags.get("entrance")
    if v is None or tags.get("access") in ENTRANCE_NO_ACCESS:
        return None
    if v in ENTRANCE_MAIN:
        return "main"
    if v in ENTRANCE_CUSTOMER:
        return "customer"
    return None


def aisle_counts(tags) -> bool:
    """True for a parking aisle customers drive on (highway=service, service=parking_aisle)."""
    return (tags.get("highway") == "service" and tags.get("service") == "parking_aisle"
            and tags.get("access") not in PARKING_NO_ACCESS)


def parking_counts(tags) -> bool:
    """True for an amenity=parking area customers park on at ground level."""
    return (tags.get("amenity") == "parking" and tags.get("parking") not in PARKING_SKIP
            and tags.get("access") not in PARKING_NO_ACCESS)


def _local(geom, lon0: float, lat0: float):
    """[geom] in metres east/north of (lon0, lat0)."""
    k = np.array([111320.0 * math.cos(math.radians(lat0)), 111320.0])
    o = np.array([lon0, lat0])
    return shapely.transform(geom, lambda c: (c - o) * k)


def _lonlat(x: float, y: float, lon0: float, lat0: float) -> tuple[float, float]:
    return (lon0 + x / (111320.0 * math.cos(math.radians(lat0))), lat0 + y / 111320.0)


def is_retail(tags) -> bool:
    """True for a shop's building: the parking rule applies to it."""
    return tags.get("shop") not in (None, "no", "vacant") or tags.get("building") in ("retail", "supermarket")


def is_large(building) -> bool:
    """True when the building's centroid is at least MIN_HALF_WIDTH_M inside its outline, or outside it."""
    c = building.centroid
    b = _local(building, c.x, c.y)
    origin = shapely.Point(0.0, 0.0)
    return (not b.contains(origin)) or b.boundary.distance(origin) >= MIN_HALF_WIDTH_M


def parking_middle(building, parkings, aisles=()) -> tuple[float, float] | None:
    """The middle of the customer parking around [building]: the centroid of the [parkings] (polygons
    in degrees) within PARKING_WITHIN_M of it, weighted by area, and of the [aisles] (lines) there,
    weighted by length x AISLE_WIDTH_M, the building's own footprint taken out; in metres east/north of
    the building's centroid. None when there is less than PARKING_MIN_M2."""
    c = building.centroid
    b = _local(building, c.x, c.y)
    near = b.buffer(PARKING_WITHIN_M).difference(b)
    sx = sy = area = 0.0
    for p in parkings:
        try:
            part = _local(p, c.x, c.y).intersection(near)
        except shapely.errors.GEOSException:
            continue
        a = part.area
        if a <= 0:
            continue
        pc = part.centroid
        sx, sy, area = sx + pc.x * a, sy + pc.y * a, area + a
    for line in aisles:
        try:
            part = _local(line, c.x, c.y).intersection(near)
        except shapely.errors.GEOSException:
            continue
        a = part.length * AISLE_WIDTH_M
        if a <= 0:
            continue
        pc = part.centroid
        sx, sy, area = sx + pc.x * a, sy + pc.y * a, area + a
    if area < PARKING_MIN_M2:
        return None
    return (sx / area, sy / area)


def door(building, entrances, parkings, retail: bool, aisles=()) -> tuple[float, float, str] | None:
    """The front door of [building] (a polygon or multipolygon in degrees): (lon, lat, how), how one of
    "main", "entrance", "parking"; None when the building is not large or nothing says where its front
    is. [entrances]: (node id, lon, lat, kind) of the outline's nodes, kind from [entrance_kind];
    [parkings]: the parking polygons near the building that pass [parking_counts], [aisles]: the
    parking aisles near it that pass [aisle_counts]; [retail]: the building is a shop's ([is_retail]),
    so the parking rule may answer."""
    if building.is_empty or not is_large(building):
        return None
    c = building.centroid
    middle = parking_middle(building, parkings, aisles)

    def nearest_to_middle(cands):
        if len(cands) == 1 or middle is None:
            return cands[0] if len(cands) == 1 else None
        k = 111320.0 * math.cos(math.radians(c.y))
        return min(cands, key=lambda e: (math.hypot((e[1] - c.x) * k - middle[0], (e[2] - c.y) * 111320.0 - middle[1]), e[0]))

    mains = sorted(e for e in entrances if e[3] == "main")
    if mains:
        e = nearest_to_middle(mains) or mains[0]
        return (e[1], e[2], "main")
    others = sorted(e for e in entrances if e[3] == "customer")
    if others:
        e = nearest_to_middle(others)
        if e is not None:
            return (e[1], e[2], "entrance")
    if middle is None or not retail:
        return None
    b = _local(building, c.x, c.y)
    outline = b.exterior if b.geom_type == "Polygon" else shapely.unary_union([g.exterior for g in b.geoms])
    q = shapely.ops.nearest_points(outline, shapely.Point(middle))[0]
    lon, lat = _lonlat(q.x, q.y, c.x, c.y)
    return (lon, lat, "parking")
