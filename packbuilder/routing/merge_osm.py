"""Merge sorted OSM PBF extracts into one, dropping objects present in more than one input.

Geofabrik state extracts overlap along borders: a way that crosses the
PA/OH line, its nodes, and relations touching both states are in both files.
valhalla_build_tiles does not de-duplicate across input files (see the
report), so we merge first. Both inputs come from the same planet snapshot,
so a duplicate is byte-for-byte the same object; we keep the first one and
count any version mismatch.

This is a plain two-way (k-way) merge on (type, id) using pyosmium iterators,
streaming, so memory stays small. Uses pyosmium (BSD-2-Clause).

usage: python merge_osm.py OUT.osm.pbf IN1.osm.pbf IN2.osm.pbf [...]
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import osmium

ORDER = {"n": 0, "w": 1, "r": 2}


def _stream(path: Path, pool):
    last = (-1, -1)
    for o in osmium.FileProcessor(str(path), thread_pool=pool):
        key = (ORDER[o.type_str()], o.id)
        if key < last:
            raise RuntimeError(f"{path} is not sorted at {key}")
        last = key
        yield key, o


def merge(out: Path, inputs: list[Path], threads: int = 4) -> dict:
    t0 = time.time()
    tmp = out.with_suffix(".part")
    if tmp.exists():
        tmp.unlink()
    out.parent.mkdir(parents=True, exist_ok=True)
    # one small shared decoder pool: other heavy jobs share this machine
    pool = osmium.io.ThreadPool(threads)
    streams = [_stream(p, pool) for p in inputs]
    heads = []
    for s in streams:
        heads.append(next(s, None))
    written = {"n": 0, "w": 0, "r": 0}
    dups = {"n": 0, "w": 0, "r": 0}
    version_mismatch = 0
    writer = osmium.SimpleWriter(osmium.io.File(str(tmp), "pbf"), thread_pool=pool)
    try:
        while True:
            live = [i for i, h in enumerate(heads) if h is not None]
            if not live:
                break
            kmin = min(heads[i][0] for i in live)
            winners = [i for i in live if heads[i][0] == kmin]
            first = heads[winners[0]][1]
            t = first.type_str()
            if len(winners) > 1:
                dups[t] += len(winners) - 1
                v0 = first.version
                for i in winners[1:]:
                    if heads[i][1].version != v0:
                        version_mismatch += 1
            writer.add(first)
            written[t] += 1
            for i in winners:
                heads[i] = next(streams[i], None)
    finally:
        writer.close()
    tmp.replace(out)
    return {
        "inputs": [p.name for p in inputs],
        "written": written,
        "duplicates_dropped": dups,
        "version_mismatches": version_mismatch,
        "wall_s": round(time.time() - t0, 1),
        "size_bytes": out.stat().st_size,
    }


if __name__ == "__main__":
    res = merge(Path(sys.argv[1]), [Path(p) for p in sys.argv[2:]])
    print(json.dumps(res, indent=2))
