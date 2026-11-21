"""Fetch complete administrative boundary relations for the admin database.

Why: Geofabrik state extracts cut boundary relations at the extract edge. With
the PA and OH extracts, valhalla_build_admins found only Ohio complete;
Pennsylvania (relation 162109) and the United States (148838) were "missing
way member ... degenerate and will be skipped", so tiles got no country code
and no PA state code.

What: one query per relation (with all member ways and nodes) for the country
plus PA, OH and their neighbouring states. First choice is an Overpass API
"attic" query ([date:...]) *as of the extract's own replication timestamp*, so
the boundaries match the snapshot the rest of the pack is built from. If
Overpass is down (it returned 502/504 for a whole afternoon on 2026-11-15),
the main OSM API's relation/{id}/full call is used instead; that is current
data, so each part records the newest element timestamp and whether anything
changed after the snapshot. Each XML result is converted to PBF with pyosmium
and the pieces are merged (merge_osm.py drops the shared border ways/nodes).
Data: OpenStreetMap, ODbL 1.0. For a whole-US build, build admins from the US
extract itself plus these relations, or from a boundary-filtered planet.

usage: python fetch_boundaries.py OUT.osm.pbf SNAPSHOT_PBF [auto|overpass|osmapi]
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import osmium

sys.path.insert(0, str(Path(__file__).resolve().parent))
import merge_osm  # noqa: E402

OVERPASS = "https://overpass-api.de/api/interpreter"
RELATIONS = {
    162109: "Pennsylvania", 162061: "Ohio",
    61320: "New York", 224951: "New Jersey", 162110: "Delaware", 162112: "Maryland",
    162068: "West Virginia", 161655: "Kentucky", 161816: "Indiana", 165789: "Michigan",
    148838: "United States",
}


def snapshot_timestamp(pbf: Path) -> str:
    r = osmium.io.Reader(str(pbf), osmium.osm.osm_entity_bits.NOTHING)
    ts = r.header().get("osmosis_replication_timestamp")
    r.close()
    if not ts:
        raise RuntimeError(f"{pbf} has no replication timestamp in its header")
    return ts


def _query(q: str, dest: Path, tries: int = 4) -> float:
    for i in range(tries):
        t0 = time.time()
        try:
            req = urllib.request.Request(OVERPASS, data=urllib.parse.urlencode({"data": q}).encode(),
                                         headers={"User-Agent": "beamline-packbuilder"})
            with urllib.request.urlopen(req, timeout=1200) as resp, open(dest, "wb") as f:
                while chunk := resp.read(1 << 20):
                    f.write(chunk)
            if b"runtime error" in dest.read_bytes()[-2000:]:
                raise RuntimeError(dest.read_bytes()[-2000:].decode(errors="replace"))
            return time.time() - t0
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, RuntimeError) as e:
            print(f"[boundaries] attempt {i + 1} failed: {str(e)[:200]}", flush=True)
            time.sleep(60 * (i + 1))
    raise RuntimeError(f"Overpass query failed {tries} times")


OSM_API = "https://api.openstreetmap.org/api/0.6/relation/{rid}/full"


def _osmapi(rid: int, dest: Path) -> float:
    """Fallback: the main OSM API's relation/full call (current data, not the snapshot)."""
    t0 = time.time()
    req = urllib.request.Request(OSM_API.format(rid=rid), headers={"User-Agent": "beamline-packbuilder"})
    with urllib.request.urlopen(req, timeout=600) as resp, open(dest, "wb") as f:
        while chunk := resp.read(1 << 20):
            f.write(chunk)
    return time.time() - t0


def _sorted_pbf(xml: Path, pbf: Path) -> dict:
    """XML -> PBF. relation/full output is not sorted by id, so sort in memory (small files)."""
    buf = osmium.MergeInputReader()
    buf.add_file(str(xml))
    tmp = pbf.with_suffix(".part")
    w = osmium.SimpleWriter(osmium.io.File(str(tmp), "pbf"))
    newest = ""
    counts = {"n": 0, "w": 0, "r": 0}

    class H:
        def node(self, o):
            self._add(o)

        def way(self, o):
            self._add(o)

        def relation(self, o):
            self._add(o)

        def _add(self, o):
            nonlocal newest
            w.add(o)
            counts[o.type_str()] += 1
            ts = o.timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")
            newest = max(newest, ts)

    buf.apply(H(), simplify=True)  # sorts by type and id, keeps the newest version
    w.close()
    tmp.replace(pbf)
    return {"objects": counts, "newest_element": newest}


def fetch(out: Path, snapshot_pbf: Path, source: str = "auto") -> dict:
    ts = snapshot_timestamp(snapshot_pbf)
    parts_dir = out.parent / (out.name.split(".")[0] + "-parts")
    parts_dir.mkdir(parents=True, exist_ok=True)
    pbfs, info = [], {}
    overpass_ok = source in ("auto", "overpass")
    for rid, name in RELATIONS.items():
        pbf = parts_dir / f"{rid}.osm.pbf"
        meta = parts_dir / f"{rid}.json"
        if not pbf.exists():
            xml = parts_dir / f"{rid}.osm"
            used = None
            if overpass_ok:
                q = f'[out:xml][timeout:900][maxsize:2000000000][date:"{ts}"];relation({rid});(._;>;);out meta;'
                try:
                    secs = _query(q, xml, tries=1 if source == "auto" else 4)
                    used = "overpass-attic"
                except RuntimeError:
                    if source == "overpass":
                        raise
                    overpass_ok = False  # auto: Overpass is down, use the OSM API for the rest
            if used is None:
                secs = _osmapi(rid, xml)
                used = "osm-api-current"
            m = _sorted_pbf(xml, pbf)
            m.update(source=used, fetch_s=round(secs, 1), xml_bytes=xml.stat().st_size,
                     changed_after_snapshot=m["newest_element"] > ts)
            C_save(meta, m)
            xml.unlink()
            print(f"[boundaries] {name}: {m}", flush=True)
        info[name] = json.loads(meta.read_text())
        pbfs.append(pbf)
    merged = merge_osm.merge(out, pbfs)
    rel_ids = {o.id for o in osmium.FileProcessor(str(out), osmium.osm.RELATION)}
    missing = [RELATIONS[r] for r in RELATIONS if r not in rel_ids]
    if missing:
        raise RuntimeError(f"boundary result lacks relations: {missing}")
    return {"snapshot_timestamp": ts, "relations": list(RELATIONS.values()),
            "per_relation": info, "merged": merged}


def C_save(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, indent=1))


if __name__ == "__main__":
    src = sys.argv[3] if len(sys.argv) > 3 else "auto"
    print(json.dumps(fetch(Path(sys.argv[1]), Path(sys.argv[2]), src), indent=1))
