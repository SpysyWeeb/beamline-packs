"""The lines of the streets in the search pack (table street_line; docs/next-round.md item 31).

A street entry has one point (the middle of its longest way). That is not enough to say how far the
street is from somebody standing on it, where two streets cross, or which street a dropped pin is on.
So each street entry also carries its line: the ways it was made from, simplified, as one small blob.

The blob (integers only, so the Python reference and the app read exactly the same points):

    varint  number of lines
    per line:
        varint  number of points (2 or more)
        per point: zigzag varint of the latitude step, zigzag varint of the longitude step

Steps are in units of 1e-5 degree (about 1.1 m) from the point before; the very first point of the blob
steps from 0, 0, and a line's first point steps from the last point of the line before it. A varint is
7 bits a byte, low bits first, the high bit set on every byte but the last (as in Protocol Buffers).

`encode` and `decode` are the reference. The geometry helpers (`nearest`, `crossings`) work on a flat
plane around the point asked for, with the same formulas as the Kotlin side (StreetLines.kt).
"""
from __future__ import annotations

import math

SCALE = 100_000          # points are whole steps of 1e-5 degree
SIMPLIFY_DEG = 0.00004   # Douglas-Peucker tolerance when the pack is built: about 4 m
M_PER_DEG = 111_320.0    # metres in a degree of latitude (the plane the helpers measure on)


def _varint(n: int, out: bytearray) -> None:
    while n >= 0x80:
        out.append((n & 0x7F) | 0x80)
        n >>= 7
    out.append(n)


def _zigzag(n: int) -> int:
    return (n << 1) if n >= 0 else ((-n << 1) - 1)


def encode(lines: list[list[tuple[int, int]]]) -> bytes:
    """[lines]: each a list of (lat, lon) in whole 1e-5 degrees, 2 points or more."""
    out = bytearray()
    _varint(len(lines), out)
    plat = plon = 0
    for line in lines:
        _varint(len(line), out)
        for lat, lon in line:
            _varint(_zigzag(lat - plat), out)
            _varint(_zigzag(lon - plon), out)
            plat, plon = lat, lon
    return bytes(out)


def decode(blob: bytes) -> list[list[tuple[int, int]]]:
    """The lines of a blob, each point (lat, lon) in whole 1e-5 degrees. Raises ValueError on a broken blob."""
    pos = 0
    n = len(blob)

    def varint() -> int:
        nonlocal pos
        v = shift = 0
        while True:
            if pos >= n or shift > 63:
                raise ValueError("broken street line")
            b = blob[pos]
            pos += 1
            v |= (b & 0x7F) << shift
            if b < 0x80:
                return v
            shift += 7

    def signed() -> int:
        v = varint()
        return (v >> 1) if v & 1 == 0 else -((v + 1) >> 1)

    lines = []
    plat = plon = 0
    for _ in range(varint()):
        count = varint()
        if count < 2 or count > n:
            raise ValueError("broken street line")
        line = []
        for _ in range(count):
            plat += signed()
            plon += signed()
            if not (-9_000_000 <= plat <= 9_000_000 and -18_000_000 <= plon <= 18_000_000):
                raise ValueError("broken street line")
            line.append((plat, plon))
        lines.append(line)
    if pos != n:
        raise ValueError("broken street line")
    return lines


# ---- geometry on a flat plane around a point (metres) ----------------------------------------------

def _kx(lat_deg: float) -> float:
    return M_PER_DEG * math.cos(math.radians(lat_deg))


def nearest(lines, lat: float, lon: float):
    """The point of [lines] nearest to (lat, lon): (metres, lat, lon), or None without a line.
    Plane: x east and y north in metres from (lat, lon), longitudes scaled by the cosine of lat."""
    kx = _kx(lat)
    best = None
    for line in lines:
        ax = (line[0][1] / SCALE - lon) * kx
        ay = (line[0][0] / SCALE - lat) * M_PER_DEG
        for i in range(1, len(line)):
            bx = (line[i][1] / SCALE - lon) * kx
            by = (line[i][0] / SCALE - lat) * M_PER_DEG
            dx, dy = bx - ax, by - ay
            den = dx * dx + dy * dy
            t = 0.0 if den == 0.0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / den))
            px, py = ax + t * dx, ay + t * dy
            d2 = px * px + py * py
            if best is None or d2 < best[0]:
                best = (d2, px, py)
            ax, ay = bx, by
    if best is None:
        return None
    return (math.sqrt(best[0]), lat + best[2] / M_PER_DEG, lon + best[1] / kx)


def _seg_seg(ax, ay, bx, by, cx, cy, dx, dy):
    """The nearest points of segment AB and segment CD: (squared metres, x, y of the middle of the two)."""
    ux, uy = bx - ax, by - ay
    vx, vy = dx - cx, dy - cy
    wx, wy = ax - cx, ay - cy
    a = ux * ux + uy * uy
    b = ux * vx + uy * vy
    c = vx * vx + vy * vy
    d = ux * wx + uy * wy
    e = vx * wx + vy * wy
    den = a * c - b * b
    s = 0.0 if den <= 1e-9 else max(0.0, min(1.0, (b * e - c * d) / den))
    # the point of CD nearest to A + s*u, then the point of AB nearest to that (one round is exact for
    # crossing segments and good to a few centimetres for the rest, which is far below the tolerance)
    t = 0.0 if c == 0.0 else max(0.0, min(1.0, (b * s + e) / c))
    s = 0.0 if a == 0.0 else max(0.0, min(1.0, (b * t - d) / a))
    px, py = ax + s * ux, ay + s * uy
    qx, qy = cx + t * vx, cy + t * vy
    ex, ey = px - qx, py - qy
    return (ex * ex + ey * ey, (px + qx) / 2.0, (py + qy) / 2.0)


def crossings(lines_a, lines_b, within_m: float, apart_m: float) -> list[tuple[float, float]]:
    """Where the lines of two streets meet: (lat, lon) points, in the order found. Two segments meet when
    they come within [within_m] of each other (they cross, or one ends on the other); meetings nearer
    than [apart_m] to one already found are the same crossing (the two carriageways of a divided road,
    the pieces of one junction) and are folded into it (the first one found stays)."""
    if not lines_a or not lines_b:
        return []
    lat0 = lines_a[0][0][0] / SCALE
    lon0 = lines_a[0][0][1] / SCALE
    kx = _kx(lat0)

    def plane(lines):
        return [[((p[1] / SCALE - lon0) * kx, (p[0] / SCALE - lat0) * M_PER_DEG) for p in line] for line in lines]

    pa, pb = plane(lines_a), plane(lines_b)
    lim = within_m * within_m
    found: list[tuple[float, float]] = []
    for la in pa:
        for i in range(1, len(la)):
            ax, ay = la[i - 1]
            bx, by = la[i]
            lox, hix = (ax, bx) if ax <= bx else (bx, ax)
            loy, hiy = (ay, by) if ay <= by else (by, ay)
            for lb in pb:
                for j in range(1, len(lb)):
                    cx, cy = lb[j - 1]
                    dx, dy = lb[j]
                    if (cx < lox - within_m and dx < lox - within_m) or (cx > hix + within_m and dx > hix + within_m) \
                            or (cy < loy - within_m and dy < loy - within_m) or (cy > hiy + within_m and dy > hiy + within_m):
                        continue
                    d2, x, y = _seg_seg(ax, ay, bx, by, cx, cy, dx, dy)
                    if d2 > lim:
                        continue
                    if any((x - fx) * (x - fx) + (y - fy) * (y - fy) < apart_m * apart_m for fx, fy in found):
                        continue
                    found.append((x, y))
    return [(lat0 + y / M_PER_DEG, lon0 + x / kx) for x, y in found]
