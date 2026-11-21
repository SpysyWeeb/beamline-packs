"""Small geometry helpers (build time): point-in-polygon on many points,
distances, .poly files."""
from __future__ import annotations

import math

import numpy as np
import shapely

EARTH_R_M = 6371008.8


def haversine_m(lat1, lon1, lat2, lon2):
    lat1, lon1, lat2, lon2 = map(np.radians, (lat1, lon1, lat2, lon2))
    a = np.sin((lat2 - lat1) / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2
    return 2 * EARTH_R_M * np.arcsin(np.sqrt(np.minimum(1.0, a)))


def haversine1(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * EARTH_R_M * math.asin(math.sqrt(min(1.0, a)))


def assign_points(xs: np.ndarray, ys: np.ndarray, polys, cell: float = 0.05) -> np.ndarray:
    """Index of the first polygon (in list order) that contains each point,
    -1 if none. Points are binned into a grid so each polygon only tests the
    points inside its bounding box; polygons are prepared (fast tests)."""
    n = len(xs)
    res = np.full(n, -1, dtype=np.int32)
    if n == 0 or not len(polys):
        return res
    x0, y0 = float(xs.min()), float(ys.min())
    cx = ((xs - x0) / cell).astype(np.int64)
    cy = ((ys - y0) / cell).astype(np.int64)
    ncx = int(cx.max()) + 1
    ncy = int(cy.max()) + 1
    cid = cy * ncx + cx
    order = np.argsort(cid, kind="stable")
    scid = cid[order]
    for pi, poly in enumerate(polys):
        if poly is None or poly.is_empty:
            continue
        minx, miny, maxx, maxy = poly.bounds
        ix0 = max(0, int((minx - x0) / cell))
        ix1 = min(ncx - 1, int((maxx - x0) / cell))
        iy0 = max(0, int((miny - y0) / cell))
        iy1 = min(ncy - 1, int((maxy - y0) / cell))
        if ix1 < ix0 or iy1 < iy0:
            continue
        parts = []
        for iy in range(iy0, iy1 + 1):
            lo = np.searchsorted(scid, iy * ncx + ix0, "left")
            hi = np.searchsorted(scid, iy * ncx + ix1, "right")
            if hi > lo:
                parts.append(order[lo:hi])
        if not parts:
            continue
        cand = np.concatenate(parts)
        cand = cand[res[cand] == -1]
        if len(cand) == 0:
            continue
        shapely.prepare(poly)
        inside = shapely.contains_xy(poly, xs[cand], ys[cand])
        res[cand[inside]] = pi
    return res


def read_poly(path: str):
    """Osmosis .poly file -> shapely (Multi)Polygon."""
    rings_outer, rings_holes = [], []
    with open(path, encoding="utf-8") as f:
        lines = [ln.strip() for ln in f]
    i = 1  # first line is the name
    while i < len(lines):
        head = lines[i]
        i += 1
        if head == "END" or head == "":
            continue
        pts = []
        while i < len(lines) and lines[i] != "END":
            parts = lines[i].split()
            if len(parts) >= 2:
                pts.append((float(parts[0]), float(parts[1])))
            i += 1
        i += 1
        (rings_holes if head.startswith("!") else rings_outer).append(pts)
    polys = [shapely.Polygon(r) for r in rings_outer]
    geom = shapely.union_all(polys)
    for h in rings_holes:
        geom = geom.difference(shapely.Polygon(h))
    return geom
