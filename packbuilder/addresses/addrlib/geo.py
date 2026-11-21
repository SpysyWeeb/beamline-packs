"""Small geometry helpers used at run time (standard library only).

Edge geometry is stored in SQLite as a BLOB of little-endian int32 pairs
(lon, lat) in micro-degrees (1e-6 deg), first vertex first. 8 bytes per vertex;
easy to read from Kotlin (ByteBuffer.order(LITTLE_ENDIAN).getInt()).
"""
from __future__ import annotations

import math
import struct

R_EARTH = 6_371_008.8


def encode_line(coords) -> bytes:
    """coords: iterable of (lon, lat) floats -> BLOB."""
    flat = []
    for lon, lat in coords:
        flat.append(int(round(lon * 1e6)))
        flat.append(int(round(lat * 1e6)))
    return struct.pack("<%di" % len(flat), *flat)


def decode_line(blob: bytes):
    n = len(blob) // 4
    v = struct.unpack("<%di" % n, blob)
    return [(v[i] / 1e6, v[i + 1] / 1e6) for i in range(0, n, 2)]


def haversine_m(lon1, lat1, lon2, lat2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R_EARTH * math.asin(min(1.0, math.sqrt(a)))


def _local(coords):
    lat0 = sum(c[1] for c in coords) / len(coords)
    kx = math.cos(math.radians(lat0)) * math.pi / 180 * R_EARTH
    ky = math.pi / 180 * R_EARTH
    return kx, ky


def interpolate(coords, frac: float, side: str | None = None, offset_m: float = 0.0):
    """Point at fraction frac (0..1) of the line's length; optionally offset to the
    left ('L') or right ('R') of the line direction by offset_m metres."""
    if len(coords) == 1:
        return coords[0]
    kx, ky = _local(coords)
    xy = [(lon * kx, lat * ky) for lon, lat in coords]
    seg = [math.hypot(x1 - x0, y1 - y0) for (x0, y0), (x1, y1) in zip(xy[:-1], xy[1:])]
    total = sum(seg)
    if total == 0:
        return coords[0]
    target = max(0.0, min(1.0, frac)) * total
    acc = 0.0
    for i, s in enumerate(seg):
        if acc + s >= target or i == len(seg) - 1:
            t = 0.0 if s == 0 else (target - acc) / s
            (x0, y0), (x1, y1) = xy[i], xy[i + 1]
            x, y = x0 + t * (x1 - x0), y0 + t * (y1 - y0)
            if side in ("L", "R") and offset_m and s > 0:
                nx, ny = -(y1 - y0) / s, (x1 - x0) / s  # left normal
                if side == "R":
                    nx, ny = -nx, -ny
                x, y = x + nx * offset_m, y + ny * offset_m
            return x / kx, y / ky
        acc += s
    return coords[-1]


def point_line_distance_m(lon, lat, coords) -> float:
    kx, ky = _local(coords)
    px, py = lon * kx, lat * ky
    best = float("inf")
    xy = [(a * kx, b * ky) for a, b in coords]
    if len(xy) == 1:
        return math.hypot(px - xy[0][0], py - xy[0][1])
    for (x0, y0), (x1, y1) in zip(xy[:-1], xy[1:]):
        dx, dy = x1 - x0, y1 - y0
        L = dx * dx + dy * dy
        t = 0.0 if L == 0 else max(0.0, min(1.0, ((px - x0) * dx + (py - y0) * dy) / L))
        best = min(best, math.hypot(px - (x0 + t * dx), py - (y0 + t * dy)))
    return best
