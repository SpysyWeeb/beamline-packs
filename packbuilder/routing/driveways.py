"""Entrance pieces tagged as driveways, made plain service roads in the graph's input.

Valhalla marks every `service=driveway` way "destination only": a route may end on one, but may not
drive through one while any other way exists. That is right for the driveway of a house. It is wrong
for the short pieces of a store's entrance road that mappers tag as driveways too: a signalised left
turn into a supermarket was closed to every route by ten metres of `service=driveway` between two
plain service roads, and the route went round the back of the building (the owner, 2026-11-21;
docs/next-round.md item 30).

The rule (the owner chose "lot entrances only"): a driveway is opened when
  - it is open to cars (no access, motor_vehicle, vehicle or motorcar tag that closes or limits it),
  - both of its ends are on another drivable way that is not a driveway, and those are not one and
    the same way at both ends (a horseshoe off one street gains nothing),
  - at least one of the ends is on a lane of a lot: `highway=service` with no `service` tag, or
    `service=parking_aisle`, itself open to cars.
A driveway that joins two streets (an alley counts as a street) stays as it is: many of those are
real private driveways and forecourts, and a route must not cut through them. Chains of several
driveway ways in a row are not looked at (each has an end on another driveway).

Opened means: the way goes into the graph's input without its `service` tag, so Valhalla reads a plain
service road. Nothing else of the extract changes, and only the routing graph is built from the result
(the map and the search data keep OpenStreetMap's tags).

usage: python driveways.py IN.osm.pbf OUT.osm.pbf [--list ways.json]
Uses pyosmium (BSD-2-Clause).
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

import osmium
import osmium.filter
import osmium.osm.mutable

# highway values no car drives on (Valhalla does not route cars over them either)
NOT_DRIVABLE = frozenset({"footway", "path", "cycleway", "steps", "pedestrian", "bridleway", "corridor", "platform", "proposed",
                          "construction", "elevator", "bus_guideway", "raceway", "abandoned", "razed", "track", "busway", "via_ferrata"})
ACCESS_KEYS = ("access", "motor_vehicle", "vehicle", "motorcar")
# any of these on a driveway: it is limited on purpose and stays destination only
LIMITED = frozenset({"private", "no", "customers", "permit", "delivery", "destination", "agricultural", "forestry", "military", "residents", "emergency"})
CLOSED = frozenset({"private", "no"})
LOT_SERVICE = frozenset({None, "parking_aisle"})
# The number of this rule: part of what the graph's input was built from (build.py's stamp), so a
# change of the rule builds the input again.
RULE = 1


def is_driveway(tags) -> bool:
    return tags.get("highway") == "service" and tags.get("service") == "driveway"


def is_open_driveway(tags) -> bool:
    return is_driveway(tags) and not any(tags.get(k) in LIMITED for k in ACCESS_KEYS)


def other_kind(tags) -> str | None:
    """What a way that is not a driveway is to a driveway's end: "lot" (a lane of a lot), "road" (any other
    way cars drive on), or None (not drivable, closed to cars, or itself a driveway)."""
    hw = tags.get("highway")
    if hw is None or hw in NOT_DRIVABLE or is_driveway(tags):
        return None
    if any(tags.get(k) in CLOSED for k in ACCESS_KEYS):
        return None
    if hw == "service" and tags.get("service") in LOT_SERVICE:
        return "lot"
    return "road"


def chosen(a_ways: dict, b_ways: dict) -> bool:
    """The rule for one driveway, from what its two end nodes are on ({way id: kind} each)."""
    if not a_ways or not b_ways:
        return False
    if len(a_ways) == 1 and a_ways.keys() == b_ways.keys():
        return False
    return "lot" in a_ways.values() or "lot" in b_ways.values()


def find(pbf: Path) -> dict[int, dict]:
    """The driveways of [pbf] to open: {way id: {"version", "nodes", "tags"}} (the way as it is in the file)."""
    highways = osmium.filter.KeyFilter("highway")
    cand: dict[int, dict] = {}
    for o in osmium.FileProcessor(str(pbf), osmium.osm.WAY).with_filter(highways):
        if len(o.nodes) >= 2 and is_open_driveway(o.tags):
            cand[o.id] = {"version": o.version, "nodes": [n.ref for n in o.nodes], "tags": dict(o.tags)}
    ends: dict[int, dict] = {}
    for c in cand.values():
        ends.setdefault(c["nodes"][0], {})
        ends.setdefault(c["nodes"][-1], {})
    for o in osmium.FileProcessor(str(pbf), osmium.osm.WAY).with_filter(highways):
        kind = other_kind(o.tags)
        if kind is None:
            continue
        for n in o.nodes:
            at = ends.get(n.ref)
            if at is not None:
                at[o.id] = kind
    return {i: c for i, c in cand.items()
            if c["nodes"][0] != c["nodes"][-1] and chosen(ends[c["nodes"][0]], ends[c["nodes"][-1]])}


def rewrite(pbf: Path, out: Path, ways: dict[int, dict]) -> None:
    """[pbf] with the [ways] written without their `service` tag, everything else as it is."""
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".part")
    tmp.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory() as d:
        changed = Path(d) / "ways.opl"
        w = osmium.SimpleWriter(str(changed))
        try:
            for i in sorted(ways):
                c = ways[i]
                # one version on, so the changed way is the newer of the two when the files are merged
                w.add_way(osmium.osm.mutable.Way(id=i, version=c["version"] + 1, visible=True, nodes=c["nodes"],
                                                 tags={k: v for k, v in c["tags"].items() if k != "service"}))
        finally:
            w.close()
        merge = osmium.MergeInputReader()
        merge.add_file(str(changed))
        reader = osmium.io.Reader(str(pbf))
        writer = osmium.io.Writer(osmium.io.File(str(tmp), "pbf"))
        try:
            merge.apply_to_reader(reader, writer)
        finally:
            writer.close()
            reader.close()
    tmp.replace(out)


def open_entrances(pbf: Path, out: Path) -> dict:
    """find + rewrite; what was done, for the build's stamp and the pack's report."""
    t0 = time.time()
    ways = find(pbf)
    rewrite(pbf, out, ways)
    return {"driveways_opened": len(ways), "wall_s": round(time.time() - t0, 1), "size_bytes": out.stat().st_size}


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Entrance pieces tagged as driveways, made plain service roads.")
    ap.add_argument("pbf", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--list", type=Path, help="also write the ids of the opened ways here (JSON)")
    a = ap.parse_args()
    t0 = time.time()
    found = find(a.pbf)
    rewrite(a.pbf, a.out, found)
    if a.list:
        a.list.write_text(json.dumps(sorted(found)), encoding="utf-8")
    print(json.dumps({"driveways_opened": len(found), "wall_s": round(time.time() - t0, 1), "size_bytes": a.out.stat().st_size}, indent=2))
