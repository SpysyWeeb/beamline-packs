"""roads.tar: a state's roads as StarPilot's own road-line tiles (docs/comma-pack-format.md section 4).

    python packbuilder/comma/roads.py --map <pack>/map.pmtiles --out roads.tar
                                      [--zooms 8,11,14] [--mtime SECONDS] [--workers N] [--bbox W,S,E,N]
                                      [--report roads.json]
    python packbuilder/comma/roads.py --verify roads.tar [--bounds W,S,E,N]

Reads Beamline's map tiles (PMTiles v3, schema protomaps-basemap 4, layer `roads`) and writes, for
zooms 8, 11 and 14, one `<z>/<x>/<y>.rt` member per map tile of that zoom that has at least one road,
into an uncompressed USTAR tar (sorted by z, x, y; owner 0, mode 0644, one fixed time). Each tile is
taken from the map's tile of the same zoom.

The `.rt` bytes are StarPilot's format `SPRT` version 1 (openpilot/starpilot/navigation/road_tiles.py at
StarPilot commit 008b0cb: `encode_road_tile` / `decode_road_tile`), written here from reading that
file, not by running it: a header `<4sBB` (b"SPRT", 1, number of classes), then zlib (level 6) of,
per class in ascending order, `<BII` (class, lines, points), the line lengths as uint16 and the
points as int32 pairs, each the difference from the point before it (the first one absolute), in
tile units 0..4096 (clipped to +-32767). Only line features are taken; a class without a line of two
points is left out; a tile without one is not written.

The classes (the contract's table): a link first (`is_link`, or a kind_detail ending in `_link`),
then motorway (motorway, trunk), major (primary, secondary), street (tertiary, residential,
unclassified, living_street), minor (service, track). A road feature (kind highway, major_road or
minor_road) with a kind_detail the table does not name takes its kind's class: highway -> motorway,
major_road -> major, minor_road -> minor (raceway, alley, ...). The kinds path, rail, ferry, aeroway,
pier and other are left out, except the two kind_details the table names that protomaps-basemap 4
files under them: `track` (kind path) and `living_street` (kind other).

The map reader is the `pmtiles` package (packbuilder/map/requirements.txt: run with .venv/map);
the vector tiles are read with packbuilder/map/mvt.py. --verify needs only the standard library.
"""
from __future__ import annotations

import argparse
import array
import gzip
import json
import math
import multiprocessing
import os
import struct
import sys
import time
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "map"))
import common  # noqa: E402
from common import ROAD_ZOOMS, Refused  # noqa: E402

# StarPilot's road_tiles.py (the format; its numbers, not its code)
EXTENT = 4096
MAX_STORED_BYTES = 2 * 1024 * 1024
MAX_BODY_BYTES = 32 * MAX_STORED_BYTES
MAGIC, VERSION = b"SPRT", 1
HEADER = struct.Struct("<4sBB")
CLASS_HEADER = struct.Struct("<BII")
COORD_LIMIT = 32767
MAX_LINE_POINTS = 65535
MOTORWAY, MAJOR, STREET, LINK, MINOR = range(5)
CLASS_NAMES = ("motorway", "major", "street", "link", "minor")

ROAD_KINDS = {"highway": MOTORWAY, "major_road": MAJOR, "minor_road": MINOR}
BY_DETAIL = {"motorway": MOTORWAY, "trunk": MOTORWAY, "primary": MAJOR, "secondary": MAJOR,
             "tertiary": STREET, "residential": STREET, "unclassified": STREET, "living_street": STREET,
             "service": MINOR, "track": MINOR}
NAMED_OUTSIDE_ROAD_KINDS = {"track", "living_street"}


def road_class(kind, detail, is_link) -> int | None:
    """StarPilot's class of one feature of Beamline's `roads` layer; None = not a road."""
    detail = detail if isinstance(detail, str) else ""
    if kind not in ROAD_KINDS and detail not in NAMED_OUTSIDE_ROAD_KINDS:
        return None
    if is_link is True or detail.endswith("_link"):
        return LINK
    if detail in BY_DETAIL:
        return BY_DETAIL[detail]
    return ROAD_KINDS[kind]


# ------------------------------------------------------------------ the .rt format
def encode_tile(roads: dict[int, list[list[tuple[int, int]]]]) -> bytes | None:
    """StarPilot's SPRT v1 bytes of {class: [line of (x, y) tile units]}; None when no class has a line."""
    body = bytearray()
    classes = 0
    for road_class_ in sorted(roads):
        lines = [line for line in roads[road_class_] if len(line) >= 2]
        if not lines:
            continue
        lengths = array.array("H", (min(len(line), MAX_LINE_POINTS) for line in lines))
        deltas = array.array("i")
        px = py = 0
        total = 0
        for line in lines:
            for x, y in line[:MAX_LINE_POINTS]:
                x = -COORD_LIMIT if x < -COORD_LIMIT else COORD_LIMIT if x > COORD_LIMIT else x
                y = -COORD_LIMIT if y < -COORD_LIMIT else COORD_LIMIT if y > COORD_LIMIT else y
                deltas.append(x - px)
                deltas.append(y - py)
                px, py = x, y
                total += 1
        if sys.byteorder != "little":
            lengths.byteswap()
            deltas.byteswap()
        body += CLASS_HEADER.pack(road_class_, len(lines), total)
        body += lengths.tobytes()
        body += deltas.tobytes()
        classes += 1
    if not classes:
        return None
    return HEADER.pack(MAGIC, VERSION, classes) + zlib.compress(bytes(body), 6)


def check_tile(raw: bytes) -> dict[int, tuple[int, int]]:
    """The checks of StarPilot's decode_road_tile, without numpy: {class: (lines, points)}; ValueError
    when its decoder would refuse the bytes. A written tile also has at least one class."""
    if len(raw) < HEADER.size or len(raw) > MAX_STORED_BYTES:
        raise ValueError("bad road tile size")
    magic, version, classes = HEADER.unpack_from(raw)
    if magic != MAGIC or version != VERSION or classes > len(CLASS_NAMES):
        raise ValueError("not a road tile")
    inflater = zlib.decompressobj()
    try:
        body = inflater.decompress(raw[HEADER.size:], MAX_BODY_BYTES)
    except zlib.error as e:
        raise ValueError("corrupt road tile") from e
    if not inflater.eof or inflater.unused_data or inflater.unconsumed_tail:
        raise ValueError("truncated, oversized or padded road tile")
    pos, out = 0, {}
    for _ in range(classes):
        if pos + CLASS_HEADER.size > len(body):
            raise ValueError("truncated road tile")
        c, count, total = CLASS_HEADER.unpack_from(body, pos)
        pos += CLASS_HEADER.size
        need = count * 2 + total * 8
        if c >= len(CLASS_NAMES) or c in out or pos + need > len(body):
            raise ValueError("bad road tile class")
        lengths = struct.unpack_from(f"<{count}H", body, pos)
        pos += need
        if sum(lengths) != total or (count and min(lengths) < 2):
            raise ValueError("bad road tile lengths")
        out[c] = (count, total)
    if pos != len(body):
        raise ValueError("trailing road tile data")
    if not out:
        raise ValueError("a road tile without roads")
    return out


# ------------------------------------------------------------------ the map's tiles
def tile_roads(mvt_bytes: bytes) -> dict[int, list[list[tuple[int, int]]]]:
    """{class: lines} of one uncompressed vector tile's `roads` layer, in 0..4096 tile units."""
    import mvt
    out: dict[int, list] = {}
    for fn, _wt, layer_bytes in mvt.fields(mvt_bytes):
        if fn != 3:
            continue
        name = None
        for lf, _lw, v in mvt.fields(bytes(layer_bytes)):
            if lf == 1:
                name = bytes(v).decode("utf-8", "replace")
                break
        if name != "roads":
            continue
        layer = mvt.Layer.parse(layer_bytes)
        if layer.extent <= 0:
            continue
        scale = EXTENT / layer.extent
        kind_i = layer.keys.index("kind") if "kind" in layer.keys else -1
        detail_i = layer.keys.index("kind_detail") if "kind_detail" in layer.keys else -1
        link_i = layer.keys.index("is_link") if "is_link" in layer.keys else -1
        values: dict[int, object] = {}

        def value(i):
            if i not in values:
                values[i] = mvt.decode_value(layer.values[i])
            return values[i]
        for fb in layer.features:
            _fid, ftype, tags, geom = layer.feature_parts(fb)
            if ftype != 2:
                continue
            kind = detail = link = None
            for k in range(0, len(tags) - 1, 2):
                if tags[k] == kind_i:
                    kind = value(tags[k + 1])
                elif tags[k] == detail_i:
                    detail = value(tags[k + 1])
                elif tags[k] == link_i:
                    link = value(tags[k + 1])
            c = road_class(kind, detail, link)
            if c is None:
                continue
            for part in mvt.decode_geometry(geom):
                if len(part) < 2:
                    continue
                if scale != 1.0:
                    part = [(round(x * scale), round(y * scale)) for x, y in part]
                out.setdefault(c, []).append(part)
    return out


def tile_entries(map_path: Path, zooms, bbox=None) -> tuple[list[tuple[int, int, int, int, int]], int]:
    """[(z, x, y, data offset, length)] of the map's tiles at the given zooms, sorted, and the map's
    tile compression (pmtiles: 1 none, 2 gzip). Refused unless the map's directories are gzip (its
    header's internal_compression)."""
    import mmap
    from pmtiles.tile import deserialize_directory, deserialize_header, tileid_to_zxy
    zooms = set(zooms)
    # an mmap of our own, closed at the end (pmtiles' MmapSource keeps its mapping open, and Windows
    # does not delete a mapped file)
    with open(map_path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
        def get(offset, length):
            return mm[offset:offset + length]
        try:
            h = deserialize_header(get(0, 127))
        except Exception as e:  # noqa: BLE001 - pmtiles' own errors (no magic, other version, unknown enum value)
            raise Refused(f"{map_path.name}: not a PMTiles version 3 map ({type(e).__name__}: {e})") from None
        if getattr(h["tile_type"], "value", h["tile_type"]) != 1:
            raise Refused(f"{map_path.name}: tiles are not vector tiles (MVT)")
        # pmtiles' deserialize_directory always gunzips: a map whose directories are stored otherwise
        # (internal_compression none, brotli, zstd) is refused here rather than failing with a gzip error
        internal = getattr(h["internal_compression"], "value", h["internal_compression"])
        if internal != 2:
            label = getattr(h["internal_compression"], "name", str(internal)).lower()
            raise Refused(f"{map_path.name}: its directories are stored with compression '{label}'; only gzip "
                          f"directories are read here (Beamline's map builder writes gzip)")
        compression = getattr(h["tile_compression"], "value", h["tile_compression"])
        if compression not in (1, 2):
            raise Refused(f"{map_path.name}: tile compression {h['tile_compression']} is not read here")
        lo = {z: (4 ** z - 1) // 3 for z in range(0, 16)}
        wanted = [(lo[z], lo[z + 1]) for z in sorted(zooms)]
        out = []

        def walk(offset, length):
            for e in deserialize_directory(get(offset, length)):
                if e.run_length == 0:
                    walk(h["leaf_directory_offset"] + e.offset, e.length)
                    continue
                first, last = e.tile_id, e.tile_id + e.run_length
                for a, b in wanted:
                    for tid in range(max(first, a), min(last, b)):
                        z, x, y = tileid_to_zxy(tid)
                        if bbox is not None and not in_bbox(z, x, y, bbox):
                            continue
                        out.append((z, x, y, h["tile_data_offset"] + e.offset, e.length))
        walk(h["root_offset"], h["root_length"])
    out.sort()
    return out, compression


def tile_lon_lat(z: int, x: float, y: float) -> tuple[float, float]:
    n = 1 << z
    return x / n * 360.0 - 180.0, math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))


def in_bbox(z: int, x: int, y: int, bbox) -> bool:
    """True when tile z/x/y touches [west, south, east, north]."""
    west, north = tile_lon_lat(z, x, y)
    east, south = tile_lon_lat(z, x + 1, y + 1)
    return east >= bbox[0] and west <= bbox[2] and north >= bbox[1] and south <= bbox[3]


# ------------------------------------------------------------------ building (one worker per chunk)
_worker_file = None
_worker_map = None


def _open_map(path: str):
    global _worker_file, _worker_map
    import mmap
    _worker_file = open(path, "rb")
    _worker_map = mmap.mmap(_worker_file.fileno(), 0, access=mmap.ACCESS_READ)


def _close_map():
    """Closes the map a single-process build opened (Windows keeps an open file from being deleted)."""
    global _worker_file, _worker_map
    for c in (_worker_map, _worker_file):
        if c is not None:
            c.close()
    _worker_file = _worker_map = None


def _convert(job):
    """[(z, x, y, .rt bytes or None, {class: lines})] of one chunk of tile entries."""
    entries, compression = job
    out, cache = [], {}
    for z, x, y, off, length in entries:
        key = (off, length)
        if key not in cache:
            data = _worker_map[off:off + length]
            if compression == 2:
                data = gzip.decompress(data)
            roads = tile_roads(data)
            cache = {key: (encode_tile(roads), {c: len(v) for c, v in roads.items()})}
        rt, counts = cache[key]
        out.append((z, x, y, rt, counts))
    return out


def build(map_path: Path, out: Path, zooms=ROAD_ZOOMS, mtime: int = 0, workers: int = 1, bbox=None) -> dict:
    """Writes roads.tar; returns the report {zoom: {...}}."""
    t0 = time.time()
    map_path, out = Path(map_path), Path(out)
    entries, compression = tile_entries(map_path, zooms, bbox)
    report = {str(z): {"map_tiles": 0, "files": 0, "bytes": 0, "largest": 0, "lines": {n: 0 for n in CLASS_NAMES}}
              for z in sorted(zooms)}
    chunks = [(entries[i:i + 400], compression) for i in range(0, len(entries), 400)]
    if workers > 1 and len(chunks) > 1:
        pool = multiprocessing.get_context("spawn").Pool(workers, initializer=_open_map, initargs=(str(map_path),))
        results = pool.imap(_convert, chunks)
    else:
        pool = None
        _open_map(str(map_path))
        results = map(_convert, chunks)
    failed = True
    try:
        with common.TarOut(out, mtime=mtime) as tar:
            for chunk in results:
                for z, x, y, rt, counts in chunk:
                    r = report[str(z)]
                    r["map_tiles"] += 1
                    if rt is None:
                        continue
                    if len(rt) > MAX_STORED_BYTES:
                        raise Refused(f"tile {z}/{x}/{y} is {len(rt)} bytes, over StarPilot's {MAX_STORED_BYTES}-byte limit")
                    check_tile(rt)
                    tar.add_bytes(f"{z}/{x}/{y}.rt", rt)
                    r["files"] += 1
                    r["bytes"] += len(rt)
                    r["largest"] = max(r["largest"], len(rt))
                    for c, n in counts.items():
                        r["lines"][CLASS_NAMES[c]] += n
        failed = False
    finally:
        if pool is not None:
            if failed:
                pool.terminate()
            else:
                pool.close()
            pool.join()
        else:
            _close_map()
    for z in sorted(zooms):
        if report[str(z)]["files"] == 0:
            out.unlink(missing_ok=True)
            raise Refused(f"{map_path.name}: no tile of zoom {z} has a road")
    return {"file": out.name, "bytes": out.stat().st_size, "sha256": common.sha256_file(out),
            "seconds": round(time.time() - t0, 1), "zooms": report}


def verify(tar_path: Path, bounds=None) -> dict:
    """The member rule, every tile against StarPilot decoder's checks, each zoom present and, with
    bounds, every tile touching them. Returns {zoom: {files, bytes, largest}}; Refused otherwise."""
    import tarfile
    members = common.checked_members(tar_path, "roads")
    report = {str(z): {"files": 0, "bytes": 0, "largest": 0} for z in ROAD_ZOOMS}
    with tarfile.open(tar_path, "r:") as t:
        for m in members:
            z, x, y = (int(v) for v in m.name[:-3].split("/"))
            if bounds is not None and not in_bbox(z, x, y, bounds):
                raise Refused(f"{Path(tar_path).name}: tile {m.name} lies outside the pack's bounds")
            raw = t.extractfile(m).read()
            try:
                check_tile(raw)
            except ValueError as e:
                raise Refused(f"{Path(tar_path).name}: {m.name}: {e}") from None
            r = report[str(z)]
            r["files"] += 1
            r["bytes"] += len(raw)
            r["largest"] = max(r["largest"], len(raw))
    missing = [z for z, r in report.items() if r["files"] == 0]
    if missing:
        raise Refused(f"{Path(tar_path).name}: no tiles of zoom {', '.join(missing)}")
    return report


def parse_box(text: str | None):
    if not text:
        return None
    v = [float(x) for x in text.split(",")]
    if len(v) != 4:
        raise SystemExit("a box is W,S,E,N")
    return v


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--map", type=Path, help="the pack's map.pmtiles")
    ap.add_argument("--out", type=Path, help="roads.tar to write")
    ap.add_argument("--zooms", default=",".join(map(str, ROAD_ZOOMS)))
    ap.add_argument("--mtime", type=int, default=0, help="the members' time (seconds; the pack's created time)")
    ap.add_argument("--workers", type=int, default=min(4, os.cpu_count() or 1))
    ap.add_argument("--bbox", help="only the tiles touching W,S,E,N (a quick local check)")
    ap.add_argument("--report", type=Path, help="write the report (JSON) here too")
    ap.add_argument("--verify", type=Path, metavar="TAR", help="check a roads.tar instead of building one")
    ap.add_argument("--bounds", help="with --verify: every tile must touch W,S,E,N")
    a = ap.parse_args(argv)
    try:
        if a.verify:
            rep = verify(a.verify, parse_box(a.bounds))
            print(json.dumps(rep, indent=1))
            print(f"{a.verify.name}: every tile passes StarPilot's decoder checks")
            return 0
        if not a.map or not a.out:
            ap.error("--map and --out are needed (or --verify)")
        zooms = tuple(int(z) for z in a.zooms.split(","))
        if not set(zooms) <= set(ROAD_ZOOMS):
            ap.error(f"zooms are {ROAD_ZOOMS}")
        rep = build(a.map, a.out, zooms, a.mtime, max(1, a.workers), parse_box(a.bbox))
    except Refused as e:
        print(f"refused: {e}", file=sys.stderr)
        return 1
    text = json.dumps(rep, indent=1)
    if a.report:
        a.report.parent.mkdir(parents=True, exist_ok=True)
        a.report.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
