"""Build the offline search index for one state.

    python packbuilder/search/build.py --state pa             # every step, skipping finished ones
    python packbuilder/search/build.py --state pa --force index
    python packbuilder/search/build.py --state pa --step extract
    python packbuilder/search/build.py --state oh --out-dir data/r3-packs --work-dir data/r3-p/search-work

Steps (each runs in its own process so its peak memory can be measured):
  check    verify the input PBF against its .md5 file
  extract  read the PBF with pyosmium -> data/search/work/<state>-extract.pkl
  index    build <out-dir>/<state>-search.sqlite (read-only SQLite, FTS5 + R*Tree)

--out-dir (default data/search) holds the index and <state>-build-stats.json (timings, peak memory);
--work-dir (default data/search/work) the extract and the input check. An extract already in the
work folder is only read.

The meta table records what the pack format 3 manifest copies (docs/architecture.md 2.11):
addr_key_table_version, addr_key_fingerprint (packbuilder/addresses/key_fingerprint.py) and
ranking_sha256; every file hash it records is over LF-normalised text (lf_sha256).
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import math
import os
import pickle
import re
import sqlite3
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))

STATES = {
    "pa": {"pbf": "data/osm/pennsylvania-261114.osm.pbf", "poly": "data/osm/pennsylvania.poly",
           "name": "Pennsylvania", "abbr": "PA", "iso": "US-PA"},
    "oh": {"pbf": "data/osm/ohio-261114.osm.pbf", "poly": "data/osm/ohio.poly",
           "name": "Ohio", "abbr": "OH", "iso": "US-OH"},
}
# The weekly build (the packs repository) sets BEAMLINE_SNAPSHOT (YYMMDD) to the date of its extracts.
_SNAP = os.environ.get("BEAMLINE_SNAPSHOT")
if _SNAP:
    if not (len(_SNAP) == 6 and _SNAP.isdigit()):
        sys.exit(f"BEAMLINE_SNAPSHOT={_SNAP!r}: expected YYMMDD")
    for _st in STATES.values():
        _st["pbf"] = _st["pbf"].replace("-261114.", f"-{_SNAP}.")
SCHEMA_VERSION = 1
KIND_PLACE, KIND_STREET, KIND_POI = 1, 2, 3
DIRECTIONAL_WORDS = {"north", "south", "east", "west", "northeast", "northwest", "southeast", "southwest"}
# output folders, set by main() from --out-dir and --work-dir (relative to the repository root)
OUT_DIR = os.path.join("data", "search")
WORK_DIR = os.path.join("data", "search", "work")
# rule files of this folder the index is built with: their LF-normalised SHA-256 goes into meta
RULE_FILES = ("ranking.json", "categories.json", "categories_idterms.json", "abbreviations.json")


def rel(p: str) -> str:
    return os.path.relpath(p, REPO).replace("\\", "/")


def path(*parts: str) -> str:
    return os.path.join(REPO, *parts)


def peak_rss_bytes() -> int:
    try:
        import psutil
        mi = psutil.Process().memory_info()
        return int(getattr(mi, "peak_wset", 0) or 0) or int(mi.rss)
    except Exception:
        import resource
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024


def stats_path(state: str) -> str:
    return path(OUT_DIR, f"{state}-build-stats.json")


def lf_sha256(p: str) -> str:
    """SHA-256 of a text file with every CRLF read as LF (docs/architecture.md 2.11)."""
    with open(p, "rb") as f:
        return hashlib.sha256(f.read().replace(b"\r\n", b"\n")).hexdigest()


def record_stats(state: str, step: str, info: dict) -> None:
    """data[step] holds the latest run of that step; data['history'] keeps every
    run (step, finish time, seconds, peak memory) so earlier numbers are not lost."""
    p = stats_path(state)
    data = {}
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
    info = dict(info, finished_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
    data[step] = info
    data.setdefault("history", []).append(
        {"step": step, "finished_at": info["finished_at"], "seconds": info.get("seconds"),
         "peak_rss_mb": info.get("peak_rss_mb"), "output_bytes": info.get("output_bytes")})
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)


# --------------------------------------------------------------------------
# step: check
# --------------------------------------------------------------------------
def step_check(state: str, force: bool) -> None:
    cfg = STATES[state]
    pbf = path(cfg["pbf"])
    md5file = pbf + ".md5"
    ok_file = path(WORK_DIR, f"{state}-input.ok")
    if not os.path.exists(pbf) or not os.path.exists(md5file):
        sys.exit(f"missing {rel(pbf)} or its .md5")
    st = os.stat(pbf)
    sig = f"{st.st_size} {int(st.st_mtime)}"
    if not force and os.path.exists(ok_file) and open(ok_file).read().strip() == sig:
        print("check: input already verified")
        return
    want = open(md5file).read().split()[0].lower()
    h = hashlib.md5()
    with open(pbf, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    got = h.hexdigest()
    if got != want:
        sys.exit(f"MD5 mismatch for {rel(pbf)}: {got} != {want} (download not finished?)")
    os.makedirs(os.path.dirname(ok_file), exist_ok=True)
    with open(ok_file, "w") as f:
        f.write(sig)
    print("check: MD5 ok", got)


# --------------------------------------------------------------------------
# step: extract
# --------------------------------------------------------------------------
def extract_path(state: str) -> str:
    return path(WORK_DIR, f"{state}-extract.pkl")


def step_extract(state: str, force: bool) -> None:
    out = extract_path(state)
    if os.path.exists(out) and not force:
        print("extract: done already ->", rel(out))
        return
    import osm_extract
    import rules
    t0 = time.time()
    res = osm_extract.run(path(STATES[state]["pbf"]), rules.RuleBook(), threads=4)
    tmp = out + ".part"
    with open(tmp, "wb") as f:
        pickle.dump(res, f, protocol=5)
    os.replace(tmp, out)
    info = {"seconds": round(time.time() - t0, 1), "peak_rss_mb": round(peak_rss_bytes() / 2**20),
            "counts": res["counts"], "output": rel(out), "output_bytes": os.path.getsize(out)}
    record_stats(state, "extract", info)
    print("extract:", json.dumps(info))


# --------------------------------------------------------------------------
# step: index
# --------------------------------------------------------------------------
def db_path(state: str) -> str:
    return path(OUT_DIR, f"{state}-search.sqlite")


SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT) WITHOUT ROWID;
CREATE TABLE entry(
  id INTEGER PRIMARY KEY,
  kind INTEGER NOT NULL,          -- 1 place, 2 street, 3 poi
  name TEXT NOT NULL,             -- display title
  name_key TEXT NOT NULL,         -- normalize.key(name)
  alt_keys TEXT,                  -- other keys separated by '|' (alt names, brand)
  type TEXT,                      -- place type, road class or primary category code
  lat REAL NOT NULL, lon REAL NOT NULL,   -- representative point (POI areas: centroid)
  importance REAL NOT NULL,       -- 0..1, from ranking.json at build time
  muni_id INTEGER,                -- entry id of the containing municipality
  county_id INTEGER,              -- entry id of the containing county
  brand_id INTEGER,
  subtitle TEXT
);
CREATE TABLE place(entry_id INTEGER PRIMARY KEY, osm TEXT, place_type TEXT, admin_level INTEGER, population INTEGER);
CREATE TABLE street(entry_id INTEGER PRIMARY KEY, way_count INTEGER, length_m INTEGER, osm_way INTEGER,
  addr_key TEXT);                 -- the addresses track's street_key(name): join key for house numbers
CREATE TABLE poi(entry_id INTEGER PRIMARY KEY, osm TEXT, cats TEXT, brand TEXT, brand_wikidata TEXT,
  operator TEXT, opening_hours TEXT, phone TEXT, website TEXT,
  addr_housenumber TEXT, addr_street TEXT, addr_city TEXT, addr_postcode TEXT, is_area INTEGER);
-- Where customers go into a large building (doors.py): a point of its outline; how = main (a mapped
-- main entrance), entrance (another mapped customer entrance), parking (a shop: the side its parking
-- is on). The entry's own point stays the centroid. An app that does not know the table ignores it.
CREATE TABLE poi_door(entry_id INTEGER PRIMARY KEY, lat REAL NOT NULL, lon REAL NOT NULL, how TEXT NOT NULL)
  WITHOUT ROWID;
-- The line of a street entry (lines.py: the ways it was made from, simplified to about 4 m, as steps of
-- 1e-5 degree in varints): how far the street is from a point, where two streets cross, which street a
-- point is on. A street whose ways have fewer than two points in its municipality has no row.
CREATE TABLE street_line(entry_id INTEGER PRIMARY KEY, line BLOB NOT NULL);
-- signals.py (docs/next-round.md item 36): every traffic light by its 0.01 degree cell.
CREATE TABLE signal(cell INTEGER NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL, PRIMARY KEY(cell, lat, lon)) WITHOUT ROWID;
CREATE TABLE poi_cat(cat_id INTEGER, entry_id INTEGER, PRIMARY KEY(cat_id, entry_id)) WITHOUT ROWID;
CREATE TABLE context(entry_id INTEGER, place_id INTEGER, source INTEGER, weight REAL,
  PRIMARY KEY(entry_id, place_id)) WITHOUT ROWID;
CREATE TABLE category(id INTEGER PRIMARY KEY, code TEXT UNIQUE, label TEXT, grp TEXT, umbrella INTEGER, pa INTEGER);
CREATE TABLE category_term(term TEXT, cat_id INTEGER, weight REAL, PRIMARY KEY(term, cat_id)) WITHOUT ROWID;
CREATE TABLE brand(id INTEGER PRIMARY KEY, wikidata TEXT, name TEXT, poi_count INTEGER);
CREATE TABLE brand_alias(alias TEXT, brand_id INTEGER, count INTEGER, PRIMARY KEY(alias, brand_id)) WITHOUT ROWID;
CREATE TABLE vocab(id INTEGER PRIMARY KEY, word TEXT NOT NULL, freq INTEGER NOT NULL);
CREATE TABLE spell_del(h INTEGER, word_id INTEGER, PRIMARY KEY(h, word_id)) WITHOUT ROWID;
CREATE VIRTUAL TABLE vocab_tri USING fts5(word, tokenize='trigram', detail=full, content='');
CREATE VIRTUAL TABLE entry_rtree USING rtree(id, minlat, maxlat, minlon, maxlon);
-- search as you type (1-3 characters): named POIs by grid cell and the first 3
-- characters of their key; cell = (floor(lat*10)+900)*4000 + floor(lon*10)+1800
CREATE TABLE poi_prefix(cell INTEGER, p3 TEXT, entry_id INTEGER, PRIMARY KEY(cell, p3, entry_id)) WITHOUT ROWID;
"""
POI_PREFIX_CELL_DEG = 0.1
FTS_SQL = ("CREATE VIRTUAL TABLE entry_fts USING fts5(names, content='', columnsize=0, "
           "detail={detail}, prefix='{prefix}', tokenize='unicode61 remove_diacritics 0')")
INDEX_SQL = """
CREATE INDEX entry_name_key ON entry(name_key);
CREATE INDEX entry_place_key ON entry(name_key) WHERE kind = 1;
CREATE INDEX entry_brand ON entry(brand_id) WHERE brand_id IS NOT NULL;
CREATE UNIQUE INDEX vocab_word ON vocab(word);
"""


def first_value(v):
    """First value of an OSM ';' list, for display."""
    return v.split(";")[0].strip() if v else v


RAW_TYPE_KEYS = ("amenity", "shop", "tourism", "leisure", "office", "craft", "healthcare", "historic",
                 "aeroway", "railway", "public_transport", "emergency", "landuse")


def raw_poi_type(tags: dict):
    """'key=value' of the main OSM tag of a POI that fits no category (shown as its label)."""
    for k in RAW_TYPE_KEYS:
        v = first_value(tags.get(k))
        if v and v not in ("yes", "no"):
            return f"{k}={v}"
    return None


def load_addr_street_key():
    """The addresses track's street_key(), used at BUILD time only: it fills
    street.addr_key, the join key for house numbers, so nothing on the device
    runs that track's code. Returns (function, info dict) or raises."""
    adir = path("packbuilder", "addresses")
    if adir not in sys.path:
        sys.path.append(adir)
    from addrlib import normalize as an   # noqa: E402  (packbuilder/addresses/addrlib)
    import key_fingerprint                 # noqa: E402  (packbuilder/addresses/key_fingerprint.py)
    files = {rel(an.__file__): lf_sha256(an.__file__), rel(str(an.DATA_FILE)): lf_sha256(str(an.DATA_FILE)),
             rel(str(key_fingerprint.SPELLINGS_FILE)): lf_sha256(str(key_fingerprint.SPELLINGS_FILE))}
    info = {"module": "packbuilder/addresses/addrlib/normalize.py street_key()",
            "table_version": str(an.tables().raw["version"]), "sha256": files, "sha256_of": "LF-normalised text",
            "fingerprint": key_fingerprint.key_fingerprint(an.street_key)}
    return an.street_key, info


def parse_population(v) -> int:
    if not v:
        return 0
    digits = "".join(ch for ch in str(v).split(";")[0] if ch.isdigit())
    try:
        return int(digits) if digits else 0
    except ValueError:
        return 0


def close_gaps(lines, max_gap_deg: float = 0.15):
    """Join open boundary chains end to start (nearest endpoint first) and close the
    ring. Returns a polygon, or None when a gap is wider than max_gap_deg."""
    import shapely
    merged = shapely.line_merge(shapely.union_all(lines))
    chains = [list(g.coords) for g in shapely.get_parts(merged) if not g.is_empty]
    if not chains:
        return None
    ring = chains.pop(0)
    while chains:
        ex_, ey = ring[-1]
        best, bd, rev = None, None, False
        for i, c in enumerate(chains):
            for r_, pt in ((False, c[0]), (True, c[-1])):
                d = (pt[0] - ex_) ** 2 + (pt[1] - ey) ** 2
                if bd is None or d < bd:
                    best, bd, rev = i, d, r_
        if bd ** 0.5 > max_gap_deg:
            return None
        c = chains.pop(best)
        ring += list(reversed(c)) if rev else c
    if len(ring) < 4:
        return None
    gap = ((ring[0][0] - ring[-1][0]) ** 2 + (ring[0][1] - ring[-1][1]) ** 2) ** 0.5
    if gap > max_gap_deg:
        return None
    poly = shapely.make_valid(shapely.Polygon(ring + [ring[0]]))
    poly = shapely.union_all([g for g in shapely.get_parts(poly) if g.geom_type in ("Polygon", "MultiPolygon")])
    return None if poly.is_empty else poly


ROUTE_KEY_RE = re.compile(r"^(us|pa) (\d+[a-z]?)$")
# derived street keys for refs, so 'route 30' finds US 30 and 'route 8' / 'state route 8' find PA 8
ROUTE_ALIASES = {"us": ["route"], "pa": ["route", "state route"]}
POSSESSIVE_RE = re.compile(r"['’]s$", re.I)
# 'Beach no.1' -> key 'beach no 1', also found as 'beach 1' (derived key) ('No. 6', 'no 6')
NUMBER_NO_RE = re.compile(r"\bno (\d+)\b")
# a named natural place inside a park (categories.json in_park) is also found by the park's name,
# as written and without one of these endings: 'Presque Isle State Park' -> 'presque isle'
PARK_SUFFIXES = ("state park", "county park", "city park", "national park", "state forest",
                 "national forest", "national recreation area", "recreation area", "nature reserve",
                 "nature preserve", "preserve", "park")
PARKS_PER_PLACE = 3


def step_index(state: str, force: bool, fts_detail: str = "none", fts_prefix: str = "2 3",
               use_addr_key: bool = True) -> None:
    out = db_path(state)
    if os.path.exists(out) and not force:
        print("index: done already ->", rel(out))
        return
    import numpy as np
    import shapely
    import geo
    import signals as traffic_signals
    import lines as street_lines
    import normalize
    import rules

    addr_street_key, addr_key_info = None, {"status": "disabled (--no-addr-key)"}
    if use_addr_key:
        try:
            addr_street_key, addr_key_info = load_addr_street_key()
        except Exception as exc:
            sys.exit(f"index: cannot load the addresses track's street_key() ({exc!r}); house numbers "
                     "could not be joined. Fix that, or build without the join key: --no-addr-key")
    t0 = time.time()
    timings = {}

    def lap(name):
        timings[name] = round(time.time() - t0, 1)
        print(f"  [{timings[name]:7.1f}s] {name}", flush=True)

    cfg = STATES[state]
    rb = rules.RuleBook()
    with open(os.path.join(HERE, "ranking.json"), encoding="utf-8") as f:
        rk = json.load(f)
    imp = rk["importance"]
    with open(extract_path(state), "rb") as f:
        ex = pickle.load(f)
    lap("load extract")

    # ---------------- boundaries ----------------
    B = list(ex["boundaries"])
    bgeoms = list(shapely.from_wkb([b["wkb"] for b in B]))
    # rebuild administrative relations libosmium did not assemble (incomplete in the
    # extract or with ring errors) by polygonizing their member ways
    assembled = {b["id"] for b in B if b["t"] == "r"}
    member_ways = ex.get("admin_member_ways", {})
    rebuilt, unrebuilt = [], []
    for arel in ex.get("admin_relations", []):
        if arel["id"] in assembled or not arel["tags"].get("name"):
            continue
        ids = [w for w, role in arel["ways"] if role in ("outer", "inner", "")]
        missing = [w for w in ids if w not in member_ways]
        if ids and missing and len(missing) <= max(3, 0.05 * len(ids)):
            # a few member ways lie outside the extract: join the open chains with
            # straight segments (approximate; marked as such)
            poly = close_gaps(shapely.from_wkb([member_ways[w] for w in ids if w in member_ways]))
            if poly is not None:
                B.append({"t": "r", "id": arel["id"], "tags": arel["tags"], "wkb": None, "rebuilt": True})
                bgeoms.append(poly)
                rebuilt.append(f"r{arel['id']} {arel['tags'].get('name')} (level {arel['tags'].get('admin_level')}, "
                               f"approximate: {len(missing)}/{len(ids)} member ways missing, gaps closed)")
                continue
        if not ids or missing:
            unrebuilt.append(f"r{arel['id']} {arel['tags'].get('name')} (level {arel['tags'].get('admin_level')}, "
                             f"{len(missing)}/{len(ids)} member ways missing)")
            continue
        lines = shapely.from_wkb([member_ways[w] for w in ids])
        faces = shapely.get_parts(shapely.polygonize(shapely.get_parts(shapely.union_all(lines))))
        poly = shapely.union_all(faces) if len(faces) else None
        if poly is None or poly.is_empty:
            unrebuilt.append(f"r{arel['id']} {arel['tags'].get('name')} (rings do not close)")
            continue
        B.append({"t": "r", "id": arel["id"], "tags": arel["tags"], "wkb": None, "rebuilt": True})
        bgeoms.append(poly)
        rebuilt.append(f"r{arel['id']} {arel['tags'].get('name')} (level {arel['tags'].get('admin_level')})")
    bgeoms = np.array(bgeoms, dtype=object)
    bad = ~shapely.is_valid(bgeoms)
    if bad.any():
        bgeoms[bad] = shapely.make_valid(bgeoms[bad])
    lap(f"boundaries rebuilt from member ways: {len(rebuilt)}; not rebuildable: {len(unrebuilt)}")
    for x in rebuilt[:20]:
        print("    rebuilt", x)
    for x in unrebuilt[:20]:
        print("    not rebuilt", x)
    state_poly = None
    for b, g in zip(B, bgeoms):
        t = b["tags"]
        if t.get("admin_level") == "4" and (t.get("ISO3166-2") == cfg["iso"] or t.get("name") == cfg["name"]):
            state_poly = g if state_poly is None else state_poly.union(g)
    state_src = "osm admin_level=4 boundary"
    if state_poly is None:
        state_poly = geo.read_poly(path(cfg["poly"]))
        state_src = "Geofabrik .poly (state boundary relation not assembled)"
    shapely.prepare(state_poly)

    def inside_state(g) -> bool:
        return state_poly.contains(g.representative_point())

    county_idx = [i for i, b in enumerate(B) if b["tags"].get("boundary") == "administrative"
                  and b["tags"].get("admin_level") == "6" and b["tags"].get("name") and inside_state(bgeoms[i])]
    muni_idx = [i for i, b in enumerate(B) if b["tags"].get("boundary") == "administrative"
                and b["tags"].get("admin_level") == "8" and b["tags"].get("name") and inside_state(bgeoms[i])]
    if not muni_idx:
        muni_idx = [i for i, b in enumerate(B) if b["tags"].get("admin_level") == "7"
                    and b["tags"].get("name") and inside_state(bgeoms[i])]
    county_polys = [bgeoms[i] for i in county_idx]
    muni_polys = [bgeoms[i] for i in muni_idx]
    lap(f"boundaries: {len(county_idx)} counties, {len(muni_idx)} municipalities ({state_src})")

    def locate(lons, lats):
        lons = np.asarray(lons, dtype=np.float64)
        lats = np.asarray(lats, dtype=np.float64)
        m = geo.assign_points(lons, lats, muni_polys)
        c = geo.assign_points(lons, lats, county_polys)
        return m, c

    # ---------------- entries: places ----------------
    entries = []      # dicts with keys of the entry table + extras
    place_rows, street_rows, poi_rows, poicat_rows = [], [], [], []
    door_rows: dict[int, tuple] = {}   # entry id -> (lat, lon, how), doors.py
    rtree_rows = []
    fts_text = {}     # entry id -> names text
    ctx = collections.defaultdict(dict)  # entry id -> {place_id: (source, weight)}

    def add_entry(**kw) -> int:
        eid = len(entries) + 1
        kw["id"] = eid
        entries.append(kw)
        return eid

    def place_importance(ptype: str, pop: int) -> float:
        base = imp["place_type"].get(ptype, 0.3)
        if pop > 0:
            p = min(1.0, math.log10(pop + 1) / imp["population_log10_max"])
            w = imp["population_weight"]
            return round((1 - w) * base + w * p, 4)
        return round(base, 4)

    county_eid = {}
    for k, i in enumerate(county_idx):
        g = bgeoms[i]
        t = B[i]["tags"]
        rp = g.representative_point()
        name = t["name"]
        pop = parse_population(t.get("population"))
        eid = add_entry(kind=KIND_PLACE, name=name, type="county", lat=rp.y, lon=rp.x,
                        importance=place_importance("county", pop), muni=None, county=None,
                        subtitle=f"County, {cfg['abbr']}", alt=[])
        county_eid[k] = eid
        place_rows.append((eid, f"{B[i]['t']}{B[i]['id']}", "county", 6, pop or None))
        minx, miny, maxx, maxy = g.bounds
        rtree_rows.append((eid, miny, maxy, minx, maxx))
    # county of each municipality
    mreps = [g.representative_point() for g in muni_polys]
    _, mcounty = locate([p.x for p in mreps], [p.y for p in mreps])

    # place nodes/areas inside the state
    P = ex["places"]
    plon = np.array([p["lon"] for p in P])
    plat = np.array([p["lat"] for p in P])
    pin_state = shapely.contains_xy(state_poly, plon, plat) if len(P) else np.array([], bool)
    pm, pc = locate(plon, plat)
    place_by_muni_key = collections.defaultdict(list)
    for j, p in enumerate(P):
        if pin_state[j] and pm[j] >= 0:
            place_by_muni_key[(int(pm[j]), normalize.key(p["tags"]["name"]))].append(j)
    used_place = set()
    PLACE_RANK = {"city": 6, "town": 5, "borough": 5, "village": 4, "municipality": 4, "suburb": 3,
                  "hamlet": 2, "quarter": 2, "neighbourhood": 1, "locality": 0, "island": 0}

    muni_eid = {}
    for k, i in enumerate(muni_idx):
        g = bgeoms[i]
        t = B[i]["tags"]
        name = t["name"]
        nkey = normalize.key(name)
        cands = place_by_muni_key.get((k, nkey), [])
        node = None
        if cands:
            node = max(cands, key=lambda j: PLACE_RANK.get(P[j]["tags"].get("place"), 0))
            used_place.update(cands)
        bt = (t.get("border_type") or "").lower()
        if name.lower().endswith(" township") or bt == "township":
            ptype = "township"
        elif bt in ("city", "borough", "town", "village"):
            ptype = bt
        elif node is not None:
            ptype = P[node]["tags"].get("place", "municipality")
        else:
            ptype = "municipality"
        pop = parse_population(t.get("population"))
        if node is not None:
            pop = pop or parse_population(P[node]["tags"].get("population"))
            lat, lon = P[node]["lat"], P[node]["lon"]
        else:
            rp = mreps[k]
            lat, lon = rp.y, rp.x
        ck = int(mcounty[k])
        cname = entries[county_eid[ck] - 1]["name"] if ck >= 0 else None
        label = {"township": "Township", "city": "City", "borough": "Borough", "town": "Town",
                 "village": "Village"}.get(ptype, ptype.capitalize())
        alt = []
        if node is not None:
            alt = [P[node]["tags"][a] for a in ("alt_name", "official_name", "short_name") if a in P[node]["tags"]]
        alt += [t[a] for a in ("alt_name", "official_name", "short_name") if a in t]
        eid = add_entry(kind=KIND_PLACE, name=name, type=ptype, lat=lat, lon=lon,
                        importance=place_importance(ptype, pop), muni=None,
                        county=county_eid.get(ck), alt=alt,
                        subtitle=f"{label}, {cname + ', ' if cname else ''}{cfg['abbr']}")
        muni_eid[k] = eid
        entries[-1]["muni"] = eid
        osm = f"{B[i]['t']}{B[i]['id']}" + (f";{P[node]['t']}{P[node]['id']}" if node is not None else "")
        place_rows.append((eid, osm, ptype, int(t.get("admin_level", 8)), pop or None))
        minx, miny, maxx, maxy = g.bounds
        rtree_rows.append((eid, miny, maxy, minx, maxx))

    for j, p in enumerate(P):
        if j in used_place or not pin_state[j]:
            continue
        t = p["tags"]
        ptype = t.get("place")
        pop = parse_population(t.get("population"))
        mk, ck = int(pm[j]), int(pc[j])
        meid, ceid = muni_eid.get(mk), county_eid.get(ck)
        where = ", ".join(x for x in (entries[meid - 1]["name"] if meid else None,
                                      entries[ceid - 1]["name"] if ceid else None) if x)
        label = {"neighbourhood": "Neighborhood"}.get(ptype, ptype.capitalize())   # US spelling (ranking.json us_spellings)
        eid = add_entry(kind=KIND_PLACE, name=t["name"], type=ptype, lat=p["lat"], lon=p["lon"],
                        importance=place_importance(ptype, pop), muni=meid, county=ceid,
                        alt=[t[a] for a in ("alt_name", "official_name", "short_name") if a in t],
                        subtitle=f"{label} in {where}" if where else label)
        place_rows.append((eid, f"{p['t']}{p['id']}", ptype, None, pop or None))
        if "bbox" in p:
            minx, miny, maxx, maxy = p["bbox"]
            rtree_rows.append((eid, miny, maxy, minx, maxx))
        else:
            rtree_rows.append((eid, p["lat"], p["lat"], p["lon"], p["lon"]))
    lap(f"places: {len(entries)} so far (merged {len(used_place)} place nodes into municipalities)")

    # ---------------- addresses: ZIPs and postal city per (street, muni) ----------------
    A = ex["addr"]
    strings = A["strings"]
    alon = np.frombuffer(A["lon"], dtype=np.float64) if len(A["lon"]) else np.zeros(0)
    alat = np.frombuffer(A["lat"], dtype=np.float64) if len(A["lat"]) else np.zeros(0)
    ast = np.frombuffer(A["street"], dtype=np.int32) if len(A["street"]) else np.zeros(0, np.int32)
    acity = np.frombuffer(A["city"], dtype=np.int32) if len(A["city"]) else np.zeros(0, np.int32)
    apc = np.frombuffer(A["pc"], dtype=np.int32) if len(A["pc"]) else np.zeros(0, np.int32)
    am, ac = locate(alon, alat)
    a_in = (am >= 0) | (ac >= 0)
    lap(f"addresses located: {len(alon)} points, {int(a_in.sum())} inside the state")

    # ZIP places
    zip_eid = {}
    sel = a_in & (apc > 0)
    if sel.any():
        order = np.argsort(apc[sel], kind="stable")
        pcs, lons, lats = apc[sel][order], alon[sel][order], alat[sel][order]
        cities, counties = acity[sel][order], ac[sel][order]
        bounds = np.flatnonzero(np.diff(pcs)) + 1
        starts = np.concatenate([[0], bounds])
        ends = np.concatenate([bounds, [len(pcs)]])
        for s, e in zip(starts, ends):
            if e - s < 3:
                continue
            code = strings[int(pcs[s])]
            la, lo = lats[s:e], lons[s:e]
            mlat, mlon = float(np.median(la)), float(np.median(lo))
            minlat, maxlat = np.percentile(la, [2, 98])
            minlon, maxlon = np.percentile(lo, [2, 98])
            cc = collections.Counter(int(x) for x in cities[s:e] if x > 0).most_common(1)
            city = strings[cc[0][0]].title() if cc else None
            ck = collections.Counter(int(x) for x in counties[s:e] if x >= 0).most_common(1)
            ceid = county_eid.get(ck[0][0]) if ck else None
            eid = add_entry(kind=KIND_PLACE, name=code, type="zip", lat=mlat, lon=mlon,
                            importance=place_importance("zip", 0), muni=None, county=ceid, alt=[],
                            subtitle=f"ZIP code{' - ' + city if city else ''}, {cfg['abbr']}")
            zip_eid[code] = eid
            place_rows.append((eid, None, "zip", None, None))
            rtree_rows.append((eid, float(minlat), float(maxlat), float(minlon), float(maxlon)))
    lap(f"zip places: {len(zip_eid)}")

    # place-name lookup for postal-city text -> place entry (nearest with that key)
    place_key_index = collections.defaultdict(list)
    for e in entries:
        if e["kind"] == KIND_PLACE and e["type"] not in ("zip", "county"):
            place_key_index[normalize.key(e["name"])].append(e["id"])

    def resolve_city(ckey: str, lat: float, lon: float, max_km: float = 40.0):
        best, bd = None, max_km * 1000
        for pid in place_key_index.get(ckey, ()):
            pe = entries[pid - 1]
            d = geo.haversine1(lat, lon, pe["lat"], pe["lon"])
            if d < bd:
                best, bd = pid, d
        return best

    # postal city / zip counts per (street key, muni)
    addr_ctx = collections.defaultdict(collections.Counter)
    for s_, m_, c_, p_ in zip(ast[a_in], am[a_in], acity[a_in], apc[a_in]):
        if s_ > 0:
            if c_ > 0:
                addr_ctx[(int(s_), int(m_))][("c", int(c_))] += 1
            if p_ > 0:
                addr_ctx[(int(s_), int(m_))][("z", int(p_))] += 1
    street_sid = {strings[i]: i for i in range(1, len(strings))}
    del am, ac
    lap(f"address context keys: {len(addr_ctx)}")

    # ---------------- streets ----------------
    R = ex["roads"]
    rg = shapely.from_wkb([r["wkb"] for r in R])
    coords, widx = shapely.get_coordinates(rg, return_index=True)
    vx, vy = coords[:, 0].copy(), coords[:, 1].copy()
    del coords
    vm, vc = locate(vx, vy)
    lap(f"road vertices located: {len(vx)}")
    nm = len(muni_polys) + 1
    # locality code per vertex: municipality, else county (offset), else -1 (outside)
    loc = np.where(vm >= 0, vm, np.where(vc >= 0, nm + vc, -1)).astype(np.int64)
    # segment lengths, attributed to the first vertex
    same = np.zeros(len(vx), bool)
    same[:-1] = widx[:-1] == widx[1:]
    seglen = np.zeros(len(vx))
    if len(vx) > 1:
        d = geo.haversine_m(vy[:-1], vx[:-1], vy[1:], vx[1:])
        seglen[:-1] = np.where(same[:-1], d, 0.0)
    keep = loc >= 0
    kidx = np.flatnonzero(keep)
    comp = widx[kidx].astype(np.int64) * (2 * nm + 2) + loc[kidx]
    order = np.argsort(comp, kind="stable")
    sidx = kidx[order]
    scomp = comp[order]
    bnd = np.flatnonzero(np.diff(scomp)) + 1
    st_ = np.concatenate([[0], bnd])
    en_ = np.concatenate([bnd, [len(scomp)]])
    portions = []  # (way index, loc, minx, miny, maxx, maxy, length, midx, midy, runs)
    nv = len(vx)
    for s, e in zip(st_, en_):
        ii = sidx[s:e]
        w = int(widx[ii[0]])
        lc = int(loc[ii[0]])
        mid = ii[len(ii) // 2]
        # the runs of vertices of this way in this locality (ii ascends), each one vertex longer at both
        # ends where the way goes on, so the lines of a street's two municipalities meet at the border
        runs = []
        brk = np.flatnonzero(np.diff(ii) != 1) + 1
        for a_, b_ in zip(np.concatenate([[0], brk]), np.concatenate([brk, [len(ii)]])):
            i0, i1 = int(ii[a_]), int(ii[b_ - 1])
            if i0 > 0 and widx[i0 - 1] == w:
                i0 -= 1
            if i1 + 1 < nv and widx[i1 + 1] == w:
                i1 += 1
            if i1 > i0:
                runs.append((i0, i1))
        portions.append((w, lc, float(vx[ii].min()), float(vy[ii].min()), float(vx[ii].max()),
                         float(vy[ii].max()), float(seglen[ii].sum()), float(vx[mid]), float(vy[mid]), runs))
    del vm, vc, loc, widx, seglen, sidx, scomp
    line_runs = []   # (street entry id, first vertex, last vertex) of vx/vy: the lines of the streets
    lap(f"road portions: {len(portions)}")

    def road_name(r):
        if r["name"]:
            return r["name"]
        ref = (r["ref"] or "").split(";")[0].strip()
        if not ref:
            return None
        if ref.startswith("I ") or ref.startswith("I-"):
            return "I " + ref[2:].strip()
        return ref

    groups = collections.defaultdict(list)
    for p in portions:
        r = R[p[0]]
        nm_ = road_name(r)
        if not nm_:
            continue
        groups[(normalize.key(nm_), p[1])].append(p)
    rc = imp["road_class"]
    ROAD_ORDER = ["track", "busway", "service", "pedestrian", "motorway_link", "trunk_link", "primary_link",
                  "secondary_link", "tertiary_link", "living_street", "road", "residential", "unclassified",
                  "tertiary", "secondary", "primary", "trunk", "motorway"]
    rorder = {h: i for i, h in enumerate(ROAD_ORDER)}
    ex_lat, ex_lon = 0.0055, 0.0075   # ~600 m
    n_streets = 0
    for (nkey, lc), plist in groups.items():
        if not nkey:
            continue
        # cluster portions whose expanded bboxes touch (union-find)
        n = len(plist)
        parent = list(range(n))

        def find(a):
            while parent[a] != a:
                parent[a] = parent[parent[a]]
                a = parent[a]
            return a
        if n > 1:
            idx = sorted(range(n), key=lambda k: plist[k][2])
            for a_i in range(n):
                a = idx[a_i]
                pa = plist[a]
                for b_i in range(a_i + 1, n):
                    b = idx[b_i]
                    pb = plist[b]
                    if pb[2] > pa[4] + 2 * ex_lon:
                        break
                    if pb[3] <= pa[5] + 2 * ex_lat and pa[3] <= pb[5] + 2 * ex_lat:
                        ra, rb_ = find(a), find(b)
                        if ra != rb_:
                            parent[ra] = rb_
        clusters = collections.defaultdict(list)
        for k in range(n):
            clusters[find(k)].append(plist[k])
        for cl in clusters.values():
            names = collections.Counter()
            alts = set()
            best_hw = "track"
            length = 0.0
            for p in cl:
                r = R[p[0]]
                nm_ = road_name(r)
                names[nm_] += 1
                for a in r["alt"]:
                    alts.add(a)
                if r["ref"] and r["name"]:
                    for rf in r["ref"].split(";"):
                        alts.add(rf.strip())
                if rorder.get(r["hw"], 0) > rorder.get(best_hw, 0):
                    best_hw = r["hw"]
                length += p[6]
            name = names.most_common(1)[0][0]
            longest = max(cl, key=lambda p: p[6])
            minx = min(p[2] for p in cl)
            miny = min(p[3] for p in cl)
            maxx = max(p[4] for p in cl)
            maxy = max(p[5] for p in cl)
            if lc < nm:
                meid = muni_eid.get(lc)
                ceid = entries[meid - 1]["county"] if meid else None
            else:
                meid = None
                ceid = county_eid.get(lc - nm)
            importance = rc.get(best_hw, 0.1) + imp["road_length_bonus_per_log10_km"] * max(
                0.0, math.log10(max(length, 1.0) / 1000.0) + 1)
            sub = ", ".join(x for x in (entries[meid - 1]["name"] if meid else None,
                                        entries[ceid - 1]["name"] if ceid else None) if x)
            alts.discard(name)
            eid = add_entry(kind=KIND_STREET, name=name, type=best_hw, lat=longest[8], lon=longest[7],
                            importance=round(min(1.0, importance), 4), muni=meid, county=ceid,
                            alt=sorted(alts), subtitle=sub)
            street_rows.append((eid, len(cl), int(length), R[longest[0]]["id"],
                                (addr_street_key(name) or None) if addr_street_key else None))
            rtree_rows.append((eid, miny, maxy, minx, maxx))
            n_streets += 1
            for p in cl:
                for i0, i1 in p[9]:
                    line_runs.append((eid, i0, i1))
            # postal city / ZIP context from addresses on this street key in this municipality
            skey_id = street_sid.get(normalize.street_key(name))
            if skey_id is not None and lc < nm:
                cnt = addr_ctx.get((skey_id, lc))
                if cnt:
                    tot_c = sum(v for (t_, _), v in cnt.items() if t_ == "c") or 1
                    for (t_, sid), v in cnt.most_common(6):
                        if t_ == "c":
                            pid = resolve_city(strings[sid], longest[8], longest[7])
                            if pid and pid != meid:
                                ctx[eid][pid] = (4, round(v / tot_c, 3))
                        else:
                            pid = zip_eid.get(strings[sid])
                            if pid:
                                ctx[eid][pid] = (5, 1.0)
    del portions, groups, rg
    lap(f"streets: {n_streets}")

    # ---------------- the traffic lights (signals.py) ----------------
    # (an extract made before them has none: the table stays empty)
    signal_rows = sorted({(traffic_signals.cell(lat, lon), round(lat, 7), round(lon, 7)) for lon, lat in ex.get("signals", [])})
    lap(f"signals: {len(signal_rows)} traffic lights")

    # ---------------- the lines of the streets (lines.py) ----------------
    street_line_rows = []
    line_points = 0
    if line_runs:
        n_runs = len(line_runs)
        lens = np.fromiter((r[2] - r[1] + 1 for r in line_runs), np.int64, n_runs)
        starts = np.fromiter((r[1] for r in line_runs), np.int64, n_runs)
        offs = np.concatenate([[0], np.cumsum(lens)])
        idx = np.repeat(starts - offs[:-1], lens) + np.arange(offs[-1])
        geoms = shapely.linestrings(np.column_stack([vx[idx], vy[idx]]), indices=np.repeat(np.arange(n_runs), lens))
        del idx
        simple = shapely.simplify(geoms, street_lines.SIMPLIFY_DEG, preserve_topology=False)
        del geoms
        sc, si = shapely.get_coordinates(simple, return_index=True)
        del simple
        qlat = np.rint(sc[:, 1] * street_lines.SCALE).astype(np.int64).tolist()
        qlon = np.rint(sc[:, 0] * street_lines.SCALE).astype(np.int64).tolist()
        bnd = (np.flatnonzero(np.diff(si)) + 1).tolist()
        run_of = si[[0] + bnd].tolist()
        del sc
        by_street = collections.defaultdict(list)
        for k, (s, e) in enumerate(zip([0] + bnd, bnd + [len(si)])):
            pts = [(qlat[s], qlon[s])]
            for i in range(s + 1, e):
                if qlat[i] != pts[-1][0] or qlon[i] != pts[-1][1]:
                    pts.append((qlat[i], qlon[i]))
            if len(pts) >= 2:
                by_street[line_runs[run_of[k]][0]].append(pts)
        del qlat, qlon, si
        for eid in sorted(by_street):
            street_line_rows.append((eid, street_lines.encode(by_street[eid])))
            line_points += sum(len(x) for x in by_street[eid])
        del by_street
    del vx, vy, line_runs
    lap(f"street lines: {len(street_line_rows)} streets, {line_points} points, "
        f"{sum(len(b) for _, b in street_line_rows)} bytes")

    # ---------------- parks (for in_park categories) ----------------
    PARKS = [pk for pk in ex.get("parks", []) if normalize.key(pk["name"])]
    park_geoms = shapely.from_wkb([pk["wkb"] for pk in PARKS]) if PARKS else np.array([])
    park_area = shapely.area(park_geoms) if PARKS else np.array([])
    park_tree = shapely.STRtree(park_geoms) if PARKS else None
    n_in_park = 0

    def parks_of(lon: float, lat: float) -> list[str]:
        """Names of the named parks, nature reserves and protected areas holding the point, the
        smallest first, at most PARKS_PER_PLACE ('Glens Natural Area' inside 'Ricketts Glen State
        Park': both)."""
        if park_tree is None:
            return []
        hits = park_tree.query(shapely.Point(lon, lat), predicate="within")
        names = []
        for i in sorted(hits, key=lambda i: park_area[i]):
            nm = PARKS[int(i)]["name"]
            if nm not in names:
                names.append(nm)
        return names[:PARKS_PER_PLACE]

    # ---------------- POIs ----------------
    POI = ex["pois"]
    olon = np.array([p["lon"] for p in POI])
    olat = np.array([p["lat"] for p in POI])
    om, oc = locate(olon, olat)
    # dedupe: same name key + primary category within ~150 m (node and area of one place)
    seen_cell = {}
    brand_rows = {}
    brand_alias_counts = collections.defaultdict(collections.Counter)
    brand_name_counts = collections.defaultdict(collections.Counter)
    cat_code_to_id = {c["id"]: i + 1 for i, c in enumerate(rb.cats)}
    pc_imp = imp["poi_category"]
    n_poi = n_dup = 0
    # brand key -> most common brand:wikidata, so POIs tagged brand=Sheetz without
    # brand:wikidata still join the Sheetz brand (the poi table keeps the raw tag)
    wd_by_key = collections.defaultdict(collections.Counter)
    for p in POI:
        b = p["tags"].get("brand")
        w = (p["tags"].get("brand:wikidata") or "").split(";")[0].strip()
        if b and w:
            wd_by_key[normalize.key(b)][w] += 1
    wd_by_key = {k: c.most_common(1)[0][0] for k, c in wd_by_key.items()}
    for j, p in enumerate(POI):
        if om[j] < 0 and oc[j] < 0:
            continue
        t = p["tags"]
        cats = p["cats"]
        prim = rb.primary(cats)
        brand = t.get("brand")
        name = t.get("name") or brand or (rb.by_id[prim]["label"] if prim else None)
        if not name:
            continue
        unnamed = not (t.get("name") or brand)
        nkey = normalize.key(name)
        if not (t.get("name") or brand):
            nkey = ""
        cell = (nkey, prim, round(p["lat"] / 0.0015), round(p["lon"] / 0.002))
        if nkey and cell in seen_cell:
            # same place mapped twice (node + area, or fuel + shop): keep the first,
            # but keep the categories only the second one carries (fuel:diesel=yes)
            n_dup += 1
            keid, kidx = seen_cell[cell]
            kcats = (poi_rows[kidx][2] or "").split()
            for c in cats:
                if c not in kcats:
                    kcats.append(c)
                    poicat_rows.append((cat_code_to_id[c], keid))
            poi_rows[kidx] = poi_rows[kidx][:2] + (" ".join(kcats) or None,) + poi_rows[kidx][3:]
            if p.get("door") and keid not in door_rows:  # the kept one is the node, this one its building
                door_rows[keid] = (round(p["door"][1], 7), round(p["door"][0], 7), p["door"][2])
            continue
        wd = (t.get("brand:wikidata") or "").split(";")[0].strip() or None
        bkey = None
        if wd or brand:
            wd_join = wd or wd_by_key.get(normalize.key(brand))
            bkey = ("wd", wd_join) if wd_join else ("name", normalize.key(brand))
            if bkey not in brand_rows:
                brand_rows[bkey] = len(brand_rows) + 1
            bid = brand_rows[bkey]
            if brand:
                brand_alias_counts[bid][("b", normalize.key(brand))] += 1
                brand_name_counts[bid][brand] += 1
            if t.get("name"):
                brand_alias_counts[bid][("n", normalize.key(t["name"]))] += 1
        else:
            bid = None
        mk, ck = int(om[j]), int(oc[j])
        meid, ceid = muni_eid.get(mk), county_eid.get(ck)
        importance = imp["poi_base"] + (imp["poi_named_bonus"] if t.get("name") else 0) \
            + (imp["poi_brand_bonus"] if bid else 0) + (imp["poi_wikidata_bonus"] if t.get("wikidata") else 0)
        if prim and prim in pc_imp:
            importance = max(importance, pc_imp[prim])
        if t.get("aeroway") == "aerodrome" and t.get("iata"):
            importance = max(importance, 0.9)
        hn, stt = t.get("addr:housenumber"), t.get("addr:street")
        city = first_value(t.get("addr:city"))
        where = entries[meid - 1]["name"] if meid else (entries[ceid - 1]["name"] if ceid else "")
        # display: first value of OSM ';' lists ('476;478' -> '476'); the poi table keeps the raw tags
        hn1, stt1 = first_value(hn), first_value(stt)
        if hn1 and stt1:
            sub = f"{hn1} {stt1}, {city or where}"
        elif stt1:
            sub = f"{stt1}, {city or where}"
        else:
            sub = city or where
        alt = [t[a] for a in ("alt_name", "short_name", "official_name") if a in t]
        if brand and brand != name:
            alt.append(brand)
        eid = add_entry(kind=KIND_POI, name=name, type=prim or raw_poi_type(t), lat=p["lat"], lon=p["lon"],
                        importance=round(min(1.0, importance), 4), muni=meid, county=ceid,
                        alt=alt, subtitle=sub, brand=bid, unnamed=unnamed)
        if rb.in_park(prim) and t.get("name"):
            pks = [pk for pk in parks_of(p["lon"], p["lat"]) if normalize.key(pk) != nkey]
            if pks:
                entries[eid - 1]["park"] = (pks, rb.by_id[prim]["label"])
                n_in_park += 1
        poi_rows.append((eid, f"{p['t']}{p['id']}", " ".join(cats) or None, brand, wd, t.get("operator"),
                         t.get("opening_hours"), t.get("phone") or t.get("contact:phone"),
                         t.get("website") or t.get("contact:website"), hn, stt, t.get("addr:city"),
                         t.get("addr:postcode"), 1 if p["area"] else 0))
        for c in cats:
            poicat_rows.append((cat_code_to_id[c], eid))
        seen_cell[cell] = (eid, len(poi_rows) - 1)
        if p.get("door"):
            door_rows[eid] = (round(p["door"][1], 7), round(p["door"][0], 7), p["door"][2])
        rtree_rows.append((eid, p["lat"], p["lat"], p["lon"], p["lon"]))
        # POI context: its own addr:city / addr:postcode
        if city:
            pid = resolve_city(normalize.key(city), p["lat"], p["lon"])
            if pid and pid != meid:
                ctx[eid][pid] = (4, 1.0)
        pcode = (t.get("addr:postcode") or "")[:5]
        if pcode in zip_eid:
            ctx[eid][zip_eid[pcode]] = (5, 1.0)
        n_poi += 1
    lap(f"pois: {n_poi} (dropped {n_dup} node/area duplicates; {n_in_park} natural places in "
        f"{len(PARKS)} named parks)")

    # ---------------- nearby populated places as extra context ----------------
    pop_types = {"city", "town", "village", "hamlet", "suburb", "neighbourhood", "quarter", "borough"}
    near_pl = [e for e in entries if e["kind"] == KIND_PLACE and e["type"] in pop_types]
    if near_pl:
        tree = shapely.STRtree(shapely.points([e["lon"] for e in near_pl], [e["lat"] for e in near_pl]))
        targets = [e for e in entries if e["kind"] in (KIND_STREET, KIND_POI)]
        R_KM = 6.0
        chunk = 200000
        for c0 in range(0, len(targets), chunk):
            tg = targets[c0:c0 + chunk]
            tla = np.array([e["lat"] for e in tg])
            tlo = np.array([e["lon"] for e in tg])
            ti, pi = tree.query(shapely.points(tlo, tla), predicate="dwithin", distance=0.075)
            if len(ti) == 0:
                continue
            pla = np.array([near_pl[k]["lat"] for k in pi])
            plo = np.array([near_pl[k]["lon"] for k in pi])
            dist = geo.haversine_m(tla[ti], tlo[ti], pla, plo)
            ok = dist <= R_KM * 1000
            ti, pi, dist = ti[ok], pi[ok], dist[ok]
            o = np.lexsort((dist, ti))
            last, cnt = -1, 0
            for k in o:
                a = int(ti[k])
                if a != last:
                    last, cnt = a, 0
                if cnt >= 2:
                    continue
                e = tg[a]
                pid = near_pl[int(pi[k])]["id"]
                if pid == e.get("muni"):
                    continue
                if pid not in ctx[e["id"]]:
                    ctx[e["id"]][pid] = (3, round(1.0 - float(dist[k]) / (R_KM * 1000), 3))
                cnt += 1
    lap("nearby place context")

    # ---------------- FTS text, vocabulary ----------------
    vocab = collections.Counter()
    for e in entries:
        keys = []
        if e.get("unnamed"):
            # unnamed POIs are found through their category only
            e["name_key"], e["alt_keys"] = "", None
            continue
        for nmx in [e["name"]] + list(e.get("alt") or []):
            k = normalize.key(nmx)
            if k and k not in keys:
                keys.append(k)
            if e["kind"] == KIND_STREET and k:
                # 'north atherton street' is also found as 'atherton street'
                kt = k.split()
                if len(kt) >= 3 and kt[0] in DIRECTIONAL_WORDS:
                    k2 = "~" + " ".join(kt[1:])   # '~' marks a derived key (tier exact_alt)
                    if k2 not in keys:
                        keys.append(k2)
                m_ = ROUTE_KEY_RE.match(k)
                if m_:
                    for word in ROUTE_ALIASES[m_.group(1)]:
                        k2 = f"~{word} {m_.group(2)}"
                        if k2 not in keys:
                            keys.append(k2)
            if "-" in nmx:
                sq = normalize.squash(nmx)
                if sq and sq not in keys:
                    keys.append(sq)
            if e["kind"] == KIND_POI and k and NUMBER_NO_RE.search(k):
                k2 = "~" + NUMBER_NO_RE.sub(r"\1", k)     # 'beach no 1' -> 'beach 1'
                if k2 not in keys:
                    keys.append(k2)
        if e.get("park"):
            # 'presque isle beach', 'presque isle state park beach', 'presque isle beach no 1'
            pks, label = e["park"]
            forms = []
            for pk in pks:
                pkey = normalize.key(pk)
                forms.append(pkey)
                for suf in PARK_SUFFIXES:
                    if pkey.endswith(" " + suf):
                        forms.append(pkey[: -len(suf) - 1])
                        break
            own = [kk.lstrip("~") for kk in keys]
            for f_ in forms:
                for tail in (normalize.key(label), *own[:1]):
                    k2 = f"~{f_} {tail}"
                    if tail and not tail.startswith(f_) and k2 not in keys:
                        keys.append(k2)
        e["name_key"] = keys[0] if keys else ""
        e["alt_keys"] = "|".join(keys[1:]) or None
        text = " ".join(k.lstrip("~") for k in keys)
        fts_text[e["id"]] = text
        for tok in set(text.split()):
            vocab[tok] += 1
    lap(f"vocabulary: {len(vocab)} words")

    # ---------------- write SQLite ----------------
    os.makedirs(os.path.dirname(out), exist_ok=True)
    tmp = out + ".part"
    if os.path.exists(tmp):
        os.remove(tmp)
    db = sqlite3.connect(tmp)
    db.execute("PRAGMA journal_mode=OFF")
    db.execute("PRAGMA synchronous=OFF")
    db.execute("PRAGMA page_size=4096")
    db.execute("PRAGMA cache_size=-200000")
    db.executescript(SCHEMA)
    db.execute(FTS_SQL.format(detail=fts_detail, prefix=fts_prefix))
    with db:
        db.executemany("INSERT INTO entry VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            (e["id"], e["kind"], e["name"], e["name_key"], e["alt_keys"], e["type"],
             round(e["lat"], 7), round(e["lon"], 7), e["importance"], e.get("muni"), e.get("county"),
             e.get("brand"), e.get("subtitle")) for e in entries))
        db.executemany("INSERT INTO place VALUES (?,?,?,?,?)", place_rows)
        db.executemany("INSERT INTO street VALUES (?,?,?,?,?)", street_rows)
        db.executemany("INSERT INTO street_line VALUES (?,?)", street_line_rows)
        db.executemany("INSERT OR IGNORE INTO signal VALUES (?,?,?)", signal_rows)
        db.executemany("INSERT INTO poi VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", poi_rows)
        db.executemany("INSERT INTO poi_door VALUES (?,?,?,?)", ((e, *d) for e, d in sorted(door_rows.items())))
        db.executemany("INSERT OR IGNORE INTO poi_cat VALUES (?,?)", poicat_rows)
        db.executemany("INSERT INTO context VALUES (?,?,?,?)",
                       ((eid, pid, sw[0], sw[1]) for eid, d in ctx.items() for pid, sw in d.items()))
        db.executemany("INSERT INTO entry_rtree VALUES (?,?,?,?,?)", rtree_rows)
        db.executemany("INSERT INTO entry_fts(rowid, names) VALUES (?,?)", fts_text.items())
        pp_rows = sorted({((math.floor(e["lat"] * 10) + 900) * 4000 + math.floor(e["lon"] * 10) + 1800,
                           e["name_key"][:3], e["id"])
                          for e in entries if e["kind"] == KIND_POI and e["name_key"]})
        db.executemany("INSERT INTO poi_prefix VALUES (?,?,?)", pp_rows)
        db.executemany("INSERT INTO category VALUES (?,?,?,?,?,?)", (
            (cat_code_to_id[c["id"]], c["id"], c["label"], c.get("group"), 1 if c.get("umbrella") else 0,
             1 if c.get("pa") else 0) for c in rb.cats))
        db.executemany("INSERT OR REPLACE INTO category_term VALUES (?,?,?)",
                       ((k, cat_code_to_id[cid], w) for k, cid, w in rb.term_rows()))
        # brands
        bcount = collections.Counter(e["brand"] for e in entries if e.get("brand"))
        brows = []
        for bk, bid in brand_rows.items():
            nc = brand_name_counts[bid].most_common(1)
            bname = nc[0][0] if nc else bk[1]
            brows.append((bid, bk[1] if bk[0] == "wd" else None, bname, bcount.get(bid, 0)))
        db.executemany("INSERT INTO brand VALUES (?,?,?,?)", brows)
        arows = []
        alias_tot = collections.Counter()
        for bid, cnt in brand_alias_counts.items():
            for (src, alias), c in cnt.items():
                alias_tot[(bid, alias)] += c
        for bid, cnt in brand_alias_counts.items():
            npois = bcount.get(bid, 0) or 1
            bkeys = [a for (src, a) in cnt if src == "b"]
            for (bid_, alias), c in [((bid, a), alias_tot[(bid, a)]) for (src, a) in cnt]:
                if not alias:
                    continue
                from_brand = ("b", alias) in cnt
                # a name spelling counts as a brand alias only if it contains the brand name
                # ('walmart supercenter') or is common for the brand
                related = any(bk and bk in alias for bk in bkeys)
                if from_brand or (c >= 2 and related) or (c >= 3 and c / npois >= 0.3):
                    arows.append((alias, bid, c))
                    sq = alias.replace(" ", "")
                    if sq != alias:
                        arows.append((sq, bid, c))
        # possessive brand names are typed short: "Dick's Sporting Goods" -> "dicks",
        # "BJ's Wholesale Club" -> "bjs", "Ollie's Bargain Outlet" -> "ollies"
        for bid, cnt in brand_name_counts.items():
            for bname, c in cnt.items():
                words = bname.split()
                if len(words) >= 2 and POSSESSIVE_RE.search(words[0]):
                    a = normalize.key(words[0])
                    if a:
                        arows.append((a, bid, c))
        for alias, _, _ in arows:
            for tok in alias.split():
                vocab[tok] += 1
        for term, _, _ in rb.term_rows():
            for tok in term.split():
                vocab[tok] += 1
        db.executemany("INSERT OR IGNORE INTO brand_alias VALUES (?,?,?)", arows)
        # vocabulary + SymSpell-style deletion table
        vrows = []
        drows = []
        p = rk["search"]
        for i, (w, c) in enumerate(sorted(vocab.items()), start=1):
            vrows.append((i, w, c))
            if len(w) >= p["fuzzy_min_chars"] and not any(ch.isdigit() for ch in w):
                for dl in normalize.deletes1(w):
                    drows.append((normalize.fnv1a32(dl), i))
        db.executemany("INSERT INTO vocab VALUES (?,?,?)", vrows)
        drows = sorted(set(drows))
        db.executemany("INSERT INTO spell_del VALUES (?,?)", drows)
        db.executemany("INSERT INTO vocab_tri(rowid, word) VALUES (?,?)",
                       ((i, w) for i, w, c in vrows if len(w) >= p["trigram_fallback_from_chars"]))
        meta = {
            "schema_version": SCHEMA_VERSION, "state": cfg["abbr"], "state_name": cfg["name"],
            "osm_source": rel(path(cfg["pbf"])), "osm_timestamp": ex.get("timestamp") or "",
            "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "sqlite_version": sqlite3.sqlite_version,
            "fts_detail": fts_detail, "fts_prefix": fts_prefix, "state_boundary": state_src,
            "boundaries_rebuilt": "; ".join(rebuilt), "boundaries_not_rebuilt": "; ".join(unrebuilt),
            "normalize": "normalize.py + abbreviations.json",
            "license": "Map data (c) OpenStreetMap contributors, ODbL 1.0",
            "third_party_notices": rb.doc["attribution"] + "\n\n" + rb.doc["attribution_license"],
            "bbox": ",".join(f"{v:.5f}" for v in (state_poly.bounds[1], state_poly.bounds[3],
                                                    state_poly.bounds[0], state_poly.bounds[2])),
            "bbox_order": "minlat,maxlat,minlon,maxlon (state boundary)",
            "poi_prefix_cell": "cell = (floor(lat*10)+900)*4000 + floor(lon*10)+1800; p3 = first 3 characters of name_key",
            "street_line": "1: lines.py (varint line count; per line varint point count, then zigzag varint lat and lon "
                           "steps of 1e-5 degree from the point before, the first from 0,0)",
            "signal": "1: signals.py (cell = (floor(lat*100)+9000)*40000 + floor(lon*100)+18000)",
            "addr_key": json.dumps(addr_key_info),
            # pack format 3 (docs/architecture.md 2.11): copied into the manifest's search part
            "addr_key_table_version": addr_key_info.get("table_version", ""),
            "addr_key_fingerprint": addr_key_info.get("fingerprint", ""),
            "ranking_sha256": lf_sha256(os.path.join(HERE, "ranking.json")),
            "rules_sha256": json.dumps({f: lf_sha256(os.path.join(HERE, f)) for f in RULE_FILES}),
        }
        db.executemany("INSERT INTO meta VALUES (?,?)", ((k, str(v)) for k, v in meta.items()))
    lap("tables written")
    db.executescript(INDEX_SQL)
    db.execute("INSERT INTO entry_fts(entry_fts) VALUES('optimize')")
    db.execute("INSERT INTO vocab_tri(vocab_tri) VALUES('optimize')")
    db.commit()
    db.execute("ANALYZE")
    db.commit()
    db.execute("VACUUM")
    # counts as stored (INSERT OR IGNORE drops duplicates, so count the tables, not the lists)
    stored = {t: db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("entry", "context", "poi_cat", "brand", "brand_alias", "category_term", "vocab",
                        "spell_del", "poi_prefix")}
    n_addr_key = db.execute("SELECT COUNT(*) FROM street WHERE addr_key IS NOT NULL").fetchone()[0]
    db.close()
    os.replace(tmp, out)
    lap("indexes, optimize, vacuum")
    counts = {"entries": len(entries), "places": len(place_rows), "streets": len(street_rows),
              "signals": len(signal_rows),
              "street_lines": len(street_line_rows), "street_line_points": line_points,
              "street_line_bytes": sum(len(b) for _, b in street_line_rows),
              "pois": len(poi_rows), "poi_doors": len(door_rows),
              "poi_doors_by_how": {h: sum(1 for d in door_rows.values() if d[2] == h) for h in ("main", "entrance", "parking")},
              "context_rows": stored["context"], "poi_cat_rows": stored["poi_cat"],
              "vocab": stored["vocab"], "spell_del_rows": stored["spell_del"], "brands": stored["brand"],
              "brand_aliases": stored["brand_alias"], "category_terms": stored["category_term"],
              "poi_prefix_rows": stored["poi_prefix"], "streets_with_addr_key": n_addr_key,
              "zips": len(zip_eid), "counties": len(county_idx),
              "municipalities": len(muni_idx), "boundaries_rebuilt": rebuilt, "boundaries_not_rebuilt": unrebuilt}
    info = {"seconds": round(time.time() - t0, 1), "peak_rss_mb": round(peak_rss_bytes() / 2**20),
            "counts": counts, "timings": timings, "output": rel(out), "output_bytes": os.path.getsize(out),
            "sqlite_version": sqlite3.sqlite_version, "addr_key": addr_key_info}
    record_stats(state, "index", info)
    print("index:", json.dumps(info))


# --------------------------------------------------------------------------
def main() -> None:
    global OUT_DIR, WORK_DIR
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state", default="pa", choices=sorted(STATES))
    ap.add_argument("--step", choices=["check", "extract", "index", "all"], default="all")
    ap.add_argument("--force", nargs="*", default=None,
                    help="re-run these steps even if done (no names = all steps)")
    ap.add_argument("--fts-detail", default="none", choices=["full", "column", "none"])
    ap.add_argument("--fts-prefix", default="2 3")
    ap.add_argument("--no-addr-key", action="store_true",
                    help="do not fill street.addr_key (house numbers then cannot be joined)")
    ap.add_argument("--out-dir", default=OUT_DIR, help="folder of the index and its build stats (default data/search)")
    ap.add_argument("--work-dir", default=WORK_DIR, help="folder of the extract (default data/search/work)")
    args = ap.parse_args()
    OUT_DIR, WORK_DIR = args.out_dir, args.work_dir
    os.makedirs(path(OUT_DIR), exist_ok=True)
    forced = set()
    if args.force is not None:
        forced = set(args.force) or {"check", "extract", "index"}
    steps = ["check", "extract", "index"] if args.step == "all" else [args.step]
    if args.step == "all":
        for s in steps:
            cmd = [sys.executable, os.path.abspath(__file__), "--state", args.state, "--step", s,
                   "--fts-detail", args.fts_detail, "--fts-prefix", args.fts_prefix,
                   "--out-dir", args.out_dir, "--work-dir", args.work_dir]
            if s in forced:
                cmd += ["--force", s]
            if args.no_addr_key:
                cmd += ["--no-addr-key"]
            t0 = time.time()
            r = subprocess.run(cmd)
            if r.returncode != 0:
                sys.exit(f"step {s} failed ({r.returncode})")
            print(f"step {s}: {time.time() - t0:.1f}s")
        return
    s = steps[0]
    force = s in forced
    if s == "check":
        step_check(args.state, force)
    elif s == "extract":
        step_extract(args.state, force)
    else:
        step_index(args.state, force, args.fts_detail, args.fts_prefix, use_addr_key=not args.no_addr_key)


if __name__ == "__main__":
    main()
