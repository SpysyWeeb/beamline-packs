"""Step: build <data>/<state>-addresses.sqlite from OSM points, NAD points and TIGER ranges.

    python packbuilder/addresses/step_build.py [--force] [--no-nad] [--no-slim] [--out PATH]

Order of preference: OSM address points, then NAD points that OSM does not
already have (same county, house number and street key within 300 m), then
TIGER ADDRFEAT ranges (and OSM addr:interpolation ways) for interpolation at
lookup time. Schema: see SCHEMA below and packbuilder/addresses/README.md.

Writes two files: <state>-addresses.sqlite (full, with the diagnostic columns
addresses.source_id and addresses.zip_src) and <state>-addresses-slim.sqlite (the
same without those two columns: the file a device ships). The state and the folders
come from common.py (BEAMLINE_ADDR_STATE, BEAMLINE_ADDR_DATA, BEAMLINE_ADDR_INPUTS).

Pack format 3 (docs/architecture.md 2.11): meta key_fingerprint (key_fingerprint.py over
addrlib/fingerprint_spellings.json), normaliser_sha256 (LF-normalised hashes of the normaliser's
sources), attribution naming every address source used; indexes on localities(city_key) and
localities(muni_key) (decision 6.19).
"""
from __future__ import annotations

import collections
import json
import re
import shutil
import sqlite3
import string
import time
from pathlib import Path

import numpy as np
import shapely

from common import OUT_DB, SOURCES, STATE_INFO, STATE_KEY, Step, file_mb, rel, work_file
import key_fingerprint
from addrlib.geo import decode_line, encode_line
from addrlib.normalize import (house_number, is_placeholder_housenumber, place_key, source_city,
                               split_housenumbers, street_core, street_key, tables, zip_parts, clean)
from addrlib.parser import parse
from geoassign import KX, M_PER_DEG, assign, load_counties, load_cousubs

STATE = SOURCES["state"]["usps"]
_TRAILING_ZIP = re.compile(r"\b\d{5}(-\d{4})?\s*$")
_LEADING_NUMBER = re.compile(r"^\d+[A-Za-z]?\s")


def street_values(raw):
    """Street names in one source value: 'A;B' -> both; 'Walnut St, Suite' -> 'Walnut St'; a whole address
    in the street field ('261 Main St Blandon PA 19510') -> its street ('Main St'). Most values pass as is."""
    out = []
    for part in (raw or "").split(";"):
        part = part.strip()
        if not part:
            continue
        if "," in part or (_LEADING_NUMBER.match(part) and _TRAILING_ZIP.search(part)):
            p = parse(part)
            if not p["street"]:
                continue
            part = string.capwords(p["street"])
        if part not in out:
            out.append(part)
    return out

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
-- one row per (ZIP, postal city, municipality, county) combination
CREATE TABLE localities (
  id INTEGER PRIMARY KEY,
  zip TEXT,                 -- 5-digit ZIP or NULL
  city TEXT,                -- city / postal city as written by the source, else derived (see city_src)
  city_key TEXT,            -- normalize.place_key(city)
  city_src TEXT,            -- 'source' | 'zip' (most common city of this ZIP) | 'municipality'
  municipality TEXT,        -- TIGER county subdivision name (township, borough, city)
  muni_key TEXT,
  county_fips TEXT NOT NULL -- 5-digit county FIPS
);
-- one row per street spelling per locality; the join target for full-text search
CREATE TABLE streets (
  id INTEGER PRIMARY KEY,
  street TEXT NOT NULL,     -- street name as written by the source
  street_key TEXT NOT NULL, -- normalize.street_key(street)
  street_core TEXT NOT NULL,-- key without directionals and trailing street type
  locality_id INTEGER NOT NULL REFERENCES localities(id),
  n_points INTEGER NOT NULL,
  n_ranges INTEGER NOT NULL,
  lat_e6 INTEGER, lon_e6 INTEGER  -- a representative position (first point / range midpoint)
);
-- address points (accuracy class 'point')
CREATE TABLE addresses (
  id INTEGER PRIMARY KEY,
  street_id INTEGER NOT NULL REFERENCES streets(id),
  hn TEXT NOT NULL,         -- house number, canonical: '123', '123a', '123-125', '123 1/2'
  hn_int INTEGER,           -- leading integer of hn
  unit TEXT,                -- canonical unit ('apt 4', '# 4b') or NULL
  lat_e6 INTEGER NOT NULL,  -- latitude * 1e6
  lon_e6 INTEGER NOT NULL,  -- longitude * 1e6
  source TEXT NOT NULL,     -- 'osm' | 'nad'
  source_id TEXT,           -- OSM 'n123' / 'w123' / 'r123', NAD UUID
  zip_src TEXT              -- where the ZIP came from: 'source' | 'nad' | 'tiger_street' | 'tiger_edge' | NULL
);
-- street edge geometry: little-endian int32 (lon, lat) micro-degree pairs
CREATE TABLE edges (id INTEGER PRIMARY KEY, geom BLOB NOT NULL);
-- house-number ranges for interpolation (accuracy class 'interpolated')
CREATE TABLE ranges (
  id INTEGER PRIMARY KEY,
  street_id INTEGER NOT NULL REFERENCES streets(id),
  edge_id INTEGER NOT NULL REFERENCES edges(id), -- TIGER TLID, or -(OSM way id)
  side TEXT,                -- 'L' | 'R' of the edge direction, NULL for OSM interpolation lines
  hn_from INTEGER NOT NULL, -- number at the edge's first vertex
  hn_to INTEGER NOT NULL,   -- number at the edge's last vertex
  hn_lo INTEGER NOT NULL, hn_hi INTEGER NOT NULL,
  parity TEXT,              -- 'O' odd, 'E' even, 'B' both
  source TEXT NOT NULL      -- 'tiger' | 'osm'
);
-- place name -> ZIP (postal city names and municipalities), for typed city names
CREATE TABLE places (place_key TEXT NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL, zip TEXT, county_fips TEXT, n INTEGER);
CREATE VIEW address_rows AS
  SELECT a.id, a.hn, s.street, s.street_key, a.unit, l.city, l.municipality, l.zip, l.county_fips,
         a.lat_e6 / 1e6 AS lat, a.lon_e6 / 1e6 AS lon, a.source, 'point' AS accuracy
  FROM addresses a JOIN streets s ON s.id = a.street_id JOIN localities l ON l.id = s.locality_id;
"""
INDEXES = """
CREATE INDEX streets_key ON streets(street_key);
CREATE INDEX streets_core ON streets(street_core);
CREATE INDEX streets_locality ON streets(locality_id);
CREATE INDEX addresses_street_hn ON addresses(street_id, hn);
CREATE INDEX ranges_street ON ranges(street_id, hn_lo);
CREATE INDEX places_key ON places(place_key);
CREATE INDEX localities_zip ON localities(zip);
CREATE INDEX localities_city ON localities(city_key);
CREATE INDEX localities_muni ON localities(muni_key);
"""


def unit_from_free(s):
    if not s:
        return None
    t = tables()
    toks = clean(s, keep="#").replace("#", " # ").split()
    if not toks:
        return None
    if toks[0] in t.units_source:
        d = t.units_source[toks[0]]
        rest = "".join(toks[1:]).replace("#", "")
        if d == "#":
            return f"# {rest}" if rest else None
        return f"{d} {rest}".strip()
    return "# " + "".join(toks).replace("#", "")


def load_osm():
    con = sqlite3.connect(work_file(f"osm_{STATE_KEY}.sqlite"))
    rows = con.execute("""SELECT osm_type, osm_id, hn_raw, street, place, unit, city, postcode, lat, lon,
                          county_fips, cousub_geoid FROM osm WHERE county_fips IS NOT NULL""").fetchall()
    interp = con.execute("SELECT way_id, interpolation, street, first_hn, last_hn, coords FROM osm_interp").fetchall()
    con.close()
    return rows, interp


def nad_street(r):
    s = (r["StNam_Full"] or "").strip()
    if s:
        return s
    parts = [r[k] for k in ("St_PreMod", "St_PreDir", "St_PreTyp", "St_PreSep", "St_Name", "St_PosTyp",
                            "St_PosDir", "St_PosMod")]
    return " ".join(p for p in parts if p and p.strip()).strip()


def nad_hn(r):
    s = (r["AddNo_Full"] or "").strip()
    if s:
        return s
    n = r["Add_Number"]
    if not n:
        return None
    n = str(n).split(".")[0]
    return f"{(r['AddNum_Pre'] or '').strip()}{n}{(r['AddNum_Suf'] or '').strip()}"


def nad_unit(r):
    for k in ("Unit", "SubAddress", "Room", "Floor", "Building"):
        if r[k] and r[k].strip():
            return unit_from_free(r[k])
    return None


def load_nad_rows(db):
    """NAD rows in the generic extra-source shape (hn, street, unit, city, zip, lon, lat, id)."""
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    out = []
    for r in con.execute("SELECT * FROM nad"):
        out.append({"id": r["UUID"], "hn": nad_hn(r), "street": nad_street(r), "unit": nad_unit(r),
                    "city": city_ok(r["Post_City"]) or city_ok(r["Inc_Muni"]), "zip": r["Zip_Code"],
                    "lon": r["Longitude"], "lat": r["Latitude"]})
    con.close()
    return out


def city_ok(c):
    """Source city, or None for placeholders ('Not stated'), digits-only values ('19428'); 'Franklin, PA' -> 'Franklin'."""
    return source_city(c, STATE)


def write_slim(full: Path, slim: Path) -> None:
    """The device file: the full pack without the diagnostic columns source_id and zip_src."""
    tmp = slim.with_name(slim.name + ".tmp")
    shutil.copyfile(full, tmp)
    con = sqlite3.connect(tmp)
    con.executescript("ALTER TABLE addresses DROP COLUMN source_id; ALTER TABLE addresses DROP COLUMN zip_src;")
    con.execute("INSERT OR REPLACE INTO meta VALUES ('variant', 'slim (device file): no addresses.source_id, zip_src')")
    con.commit()
    con.execute("VACUUM")
    con.close()
    if slim.exists():
        slim.unlink()
    tmp.replace(slim)


def table_sizes(db: Path) -> dict:
    """MB per table including its indexes, measured by copying each table into an empty file."""
    src = sqlite3.connect(db)
    tabs = [r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    out = {}
    for t in tabs:
        cols_sql = src.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone()[0]
        idx_sql = [r[0] for r in src.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL", (t,))]
        tmp = db.with_name(f"_size_{t}.sqlite")
        if tmp.exists():
            tmp.unlink()
        c = sqlite3.connect(tmp)
        c.execute(cols_sql)
        c.execute(f"ATTACH DATABASE ? AS src", (str(db),))
        c.execute(f"INSERT INTO main.{t} SELECT * FROM src.{t}")
        c.commit()
        c.execute("DETACH DATABASE src")
        for q in idx_sql:
            c.execute(q)
        c.commit()
        c.execute("VACUUM")
        c.close()
        out[t] = round(tmp.stat().st_size / 1e6, 2)
        tmp.unlink()
    src.close()
    return out


def main():
    st = Step("build", extra_args=[(("--no-nad",), {"action": "store_true"}),
                                   (("--no-extra",), {"action": "store_true"}),
                                   (("--no-slim",), {"action": "store_true"}),
                                   (("--out",), {"default": str(OUT_DB)})])
    if st.args.no_nad:
        st.name = "build_no_nad"
        st.metrics_path = st.metrics_path.with_name("build_no_nad.json")
    with st:
        t0 = time.time()
        out_path = Path(st.args.out)
        counties = load_counties()
        cousubs = load_cousubs()
        cs_by_geoid = {c[0]: c for c in cousubs}
        stats = collections.Counter()
        key_cache = {}

        def skey(s):
            k = key_cache.get(s)
            if k is None:
                k = key_cache[s] = street_key(s)
            return k

        # ------------------------------------------------ OSM points
        osm_rows, osm_interp = load_osm()
        pts = []  # dict rows
        for typ, oid, hn_raw, street, place, unit, city, postcode, lat, lon, cfips, cous in osm_rows:
            raw_street = (street or "").strip() or (place or "").strip()
            names = street_values(raw_street)
            if not names:
                stats["osm_no_street"] += 1
                continue
            if len(names) > 1 or names[0] != raw_street:
                stats["osm_street_value_cleaned"] += 1
            z = zip_parts(postcode)[0] if postcode else None
            if postcode and not z:
                stats["osm_bad_postcode"] += 1
            for hn in split_housenumbers(hn_raw):
                if is_placeholder_housenumber(hn):
                    stats["osm_block_placeholder_dropped"] += 1
                    continue
                hnn, hni = house_number(hn)
                if hnn is None:
                    stats["osm_bad_housenumber"] += 1
                    continue
                for street in names:  # 'A;B': the address is on both names
                    pts.append({"hn": hnn, "hn_int": hni, "street": street, "key": skey(street),
                                "unit": unit_from_free(unit),
                                "city": city_ok(city), "zip": z, "zip_src": "source" if z else None,
                                "lat": lat, "lon": lon, "county": cfips, "cousub": cous,
                                "source": "osm", "source_id": f"{typ}{oid}"})
        kept, seen_pos = [], collections.defaultdict(list)
        for p in pts:
            k = (p["county"], p["hn"], p["key"], p["unit"])
            if any(np.hypot((p["lon"] - x) * KX, p["lat"] - y) * M_PER_DEG < 50 for x, y in seen_pos[k]):
                stats["osm_duplicates_dropped"] += 1
                continue
            seen_pos[k].append((p["lon"], p["lat"]))
            kept.append(p)
        pts = kept
        del seen_pos, kept, osm_rows  # memory: the raw OSM rows are not needed any more (the weekly build has ~6.5 GB)
        stats["osm_points"] = len(pts)
        print(f"[build] OSM points {len(pts)}  ({time.time() - t0:.0f}s)", flush=True)

        # ------------------------------------------------ additional point sources (NAD, then extras)
        nad_db = work_file(f"nad_{STATE_KEY}.sqlite")
        use_nad = nad_db.exists() and not st.args.no_nad
        extra_db = work_file(f"extra_{STATE_KEY}.sqlite")
        sources_used = []
        if use_nad:
            sources_used.append(("nad", load_nad_rows(nad_db)))
        if extra_db.exists() and not st.args.no_extra:
            con = sqlite3.connect(extra_db)
            for (tag,) in con.execute("SELECT DISTINCT source FROM extra ORDER BY source").fetchall():
                rows = [dict(zip(("id", "hn", "street", "unit", "city", "zip", "lon", "lat"), r)) for r in con.execute(
                    "SELECT src_id, hn, street, unit, city, zip, lon, lat FROM extra WHERE source = ?", (tag,))]
                sources_used.append((tag, rows))
            con.close()
        osm_idx = collections.defaultdict(list)
        for i, p in enumerate(pts):
            osm_idx[(p["county"], p["hn"], p["key"])].append(i)
        for tag, rows in sources_used:
            lon = np.array([float(r["lon"]) if r["lon"] is not None else np.nan for r in rows])
            lat = np.array([float(r["lat"]) if r["lat"] is not None else np.nan for r in rows])
            ok = np.isfinite(lon) & np.isfinite(lat)
            lon[~ok] = 0
            lat[~ok] = 0
            ci = assign(counties, lon, lat)
            mi = assign(cousubs, lon, lat)
            seen = set()
            for j, r in enumerate(rows):
                if not ok[j] or ci[j] < 0:
                    stats[f"{tag}_outside_state"] += 1
                    continue
                if r["hn"] and is_placeholder_housenumber(r["hn"]):
                    stats[f"{tag}_block_placeholder_dropped"] += 1  # '700blk'
                    continue
                hnn, hni = house_number(r["hn"]) if r["hn"] else (None, None)
                raw_street = (r["street"] or "").strip()
                names = street_values(raw_street)
                if hnn is None or not names:
                    stats[f"{tag}_incomplete"] += 1
                    continue
                if len(names) > 1 or names[0] != raw_street:
                    stats[f"{tag}_street_value_cleaned"] += 1
                street = names[0]
                k = skey(street)
                cf = counties[ci[j]][0]
                z = zip_parts(r["zip"])[0] if r["zip"] else None
                base = (cf, hnn, k, z)
                if base in seen:  # unit-level rows of one building
                    stats[f"{tag}_unit_rows_collapsed"] += 1
                    continue
                seen.add(base)
                stats[f"{tag}_base_addresses"] += 1
                dup = None
                for i in osm_idx.get((cf, hnn, k), ()):
                    p = pts[i]
                    if np.hypot((p["lon"] - lon[j]) * KX, p["lat"] - lat[j]) * M_PER_DEG < 300:
                        dup = i
                        break
                ncity = city_ok(r["city"])
                if dup is not None:
                    p = pts[dup]
                    stats[f"{tag}_already_present"] += 1
                    if not p["zip"] and z:
                        p["zip"], p["zip_src"] = z, tag
                        stats[f"zip_from_{tag}"] += 1
                    if not p["city"] and ncity:
                        p["city"] = ncity
                    continue
                pts.append({"hn": hnn, "hn_int": hni, "street": street, "key": k, "unit": None,
                            "city": ncity, "zip": z, "zip_src": "source" if z else None,
                            "lat": float(lat[j]), "lon": float(lon[j]), "county": cf,
                            "cousub": cousubs[mi[j]][0] if mi[j] >= 0 else None,
                            "source": tag, "source_id": r["id"]})
                osm_idx[(cf, hnn, k)].append(len(pts) - 1)
                stats[f"{tag}_added"] += 1
            del rows
        source_tags = [t for t, _ in sources_used]
        sources_used.clear()  # free the raw rows
        del osm_idx
        print(f"[build] + {source_tags} -> {len(pts)} points  ({time.time() - t0:.0f}s)", flush=True)

        # ------------------------------------------------ TIGER edges and ranges
        tcon = sqlite3.connect(work_file(f"tiger_{STATE_KEY}.sqlite"))
        edges = {tl: (cf, cous, blob) for tl, cf, cous, blob in
                 tcon.execute("SELECT tlid, county_fips, cousub_geoid, geom FROM edges")}
        tranges = tcon.execute("SELECT tlid, side, fullname, street_key, hn_from, hn_to, parity, zip FROM ranges").fetchall()
        tcon.close()
        edge_zip = {}
        edge_keys = collections.defaultdict(set)
        for tl, side, name, k, a, b, par, z in tranges:
            if z:
                edge_zip.setdefault((tl, side), z)
            edge_keys[tl].add(k)

        # ZIP for points without one: the nearest TIGER edge of the same street (same county) within
        # 300 m, else the nearest edge of any street within 150 m; the side (L/R) by cross product.
        need = [i for i, p in enumerate(pts) if not p["zip"]]
        if need:
            tl_list = list(edges)
            tl_index = {t: n for n, t in enumerate(tl_list)}
            lines = np.array([shapely.LineString([(x * KX, y) for x, y in decode_line(edges[t][2])])
                              for t in tl_list], dtype=object)
            P = shapely.points(np.array([pts[i]["lon"] * KX for i in need]), np.array([pts[i]["lat"] for i in need]))
            # (a) same street: candidate pairs from (county, street key)
            by_ck = collections.defaultdict(list)
            for tl, side, name, k, a, b, par, z in tranges:
                by_ck[(edges[tl][0], k)].append(tl_index[tl])
            groups = collections.defaultdict(list)
            for n, i in enumerate(need):
                groups[(pts[i]["county"], pts[i]["key"])].append(n)
            pi_l, ei_l = [], []
            for ck, ns in groups.items():
                es = by_ck.get(ck)
                if es:
                    es = np.unique(es)
                    pi_l.append(np.repeat(np.array(ns), len(es)))
                    ei_l.append(np.tile(es, len(ns)))
            chosen = {}
            if pi_l:
                pi_a, ei_a = np.concatenate(pi_l), np.concatenate(ei_l)
                d = shapely.distance(P[pi_a], lines[ei_a])
                ok = d <= 300 / M_PER_DEG
                pi_a, ei_a, d = pi_a[ok], ei_a[ok], d[ok]
                order = np.lexsort((d, pi_a))
                first = np.ones(len(order), dtype=bool)
                first[1:] = pi_a[order][1:] != pi_a[order][:-1]
                for n, e in zip(pi_a[order][first], ei_a[order][first]):
                    chosen[int(n)] = (int(e), "tiger_street")
                del pi_a, ei_a, d
            # (b) any street
            rest = np.array([n for n in range(len(need)) if n not in chosen], dtype=np.int64)
            if len(rest):
                tree = shapely.STRtree(lines)
                pi, ei = tree.query_nearest(P[rest], max_distance=150 / M_PER_DEG, all_matches=False)
                for n, e in zip(rest[pi], ei):
                    chosen[int(n)] = (int(e), "tiger_edge")
                del tree
            ns = np.array(sorted(chosen), dtype=np.int64)
            es = np.array([chosen[n][0] for n in ns], dtype=np.int64)
            L, PP = lines[es], P[ns]
            sl = shapely.line_locate_point(L, PP)
            a = shapely.line_interpolate_point(L, np.maximum(sl - 1e-6, 0))
            b = shapely.line_interpolate_point(L, sl + 1e-6)
            ax, ay, bx, by = shapely.get_x(a), shapely.get_y(a), shapely.get_x(b), shapely.get_y(b)
            px, py = shapely.get_x(PP), shapely.get_y(PP)
            left = ((bx - ax) * (py - ay) - (by - ay) * (px - ax)) > 0
            for n, e, lf in zip(ns, es, left):
                p = pts[need[n]]
                tl = tl_list[e]
                side = "L" if lf else "R"
                z = edge_zip.get((tl, side)) or edge_zip.get((tl, "R" if side == "L" else "L"))
                if z:
                    p["zip"] = z
                    p["zip_src"] = chosen[int(n)][1]
                    stats["zip_from_" + p["zip_src"]] += 1
            del lines, P, L, PP
        stats["points_without_zip"] = sum(1 for p in pts if not p["zip"])
        print(f"[build] ZIP fill done ({time.time() - t0:.0f}s)", flush=True)

        # postal city per ZIP: most common source city among points with that ZIP
        zc = collections.defaultdict(collections.Counter)
        for p in pts:
            if p["zip"] and p["city"]:
                zc[p["zip"]][p["city"].strip().title() if p["city"].isupper() else p["city"].strip()] += 1
        zip_city = {z: c.most_common(1)[0][0] for z, c in zc.items()}

        # ------------------------------------------------ localities and streets
        loc_ids, street_ids = {}, {}
        loc_rows, street_rows = [], []

        def locality(z, city, city_src, cous, cf):
            muni = cs_by_geoid[cous][1] if cous in cs_by_geoid else None
            if not city:
                if z and z in zip_city:
                    city, city_src = zip_city[z], "zip"
                elif muni:
                    city, city_src = muni, "municipality"
            key = (z, city, muni, cf)
            lid = loc_ids.get(key)
            if lid is None:
                lid = loc_ids[key] = len(loc_rows) + 1
                loc_rows.append((lid, z, city, place_key(city) if city else None, city_src if city else None,
                                 muni, place_key(muni) if muni else None, cf))
            return lid

        def street_id(name, k, lid, lat, lon):
            key = (name, lid)
            sid = street_ids.get(key)
            if sid is None:
                sid = street_ids[key] = len(street_rows) + 1
                street_rows.append([sid, name, k, street_core(k), lid, 0, 0, round(lat * 1e6), round(lon * 1e6)])
            return sid

        addr_rows = []
        for n, p in enumerate(pts, 1):
            lid = locality(p["zip"], p["city"], "source", p["cousub"], p["county"])
            sid = street_id(p["street"], p["key"], lid, p["lat"], p["lon"])
            street_rows[sid - 1][5] += 1
            addr_rows.append((n, sid, p["hn"], p["hn_int"], p["unit"], round(p["lat"] * 1e6),
                              round(p["lon"] * 1e6), p["source"], p["source_id"], p["zip_src"]))
            pts[n - 1] = None  # memory: each point dict is freed once it is a row (pts is not used after this)
        del pts
        print(f"[build] {len(addr_rows)} address rows, {len(street_rows)} streets so far ({time.time() - t0:.0f}s)",
              flush=True)

        range_rows = []
        edge_used = set()
        for tl, side, name, k, a, b, par, z in tranges:
            cf, cous, blob = edges[tl]
            mid = decode_line(blob)
            mid = mid[len(mid) // 2]
            lid = locality(z, None, None, cous, cf)
            sid = street_id(name, k, lid, mid[1], mid[0])
            street_rows[sid - 1][6] += 1
            range_rows.append((len(range_rows) + 1, sid, tl, side, a, b, min(a, b), max(a, b), par, "tiger"))
            edge_used.add(tl)
        stats["tiger_ranges"] = len(range_rows)

        # OSM addr:interpolation ways
        par_map = {"odd": "O", "even": "E", "all": "B"}
        extra_edges = {}
        usable = []
        for wid, interp, street, fhn, lhn, coords in osm_interp:
            a, b = house_number(fhn or "")[1], house_number(lhn or "")[1]
            if not street or a is None or b is None or interp not in par_map:
                stats["osm_interp_unusable"] += 1
                continue
            cl = [tuple(map(float, c.split(","))) for c in coords.split(";")]
            usable.append((wid, interp, street, a, b, cl, cl[len(cl) // 2]))
        if usable:
            mlon = np.array([u[6][0] for u in usable])
            mlat = np.array([u[6][1] for u in usable])
            cia, mia = assign(counties, mlon, mlat), assign(cousubs, mlon, mlat)
            for (wid, interp, street, a, b, cl, mid), ci, mi in zip(usable, cia, mia):
                if ci < 0:
                    stats["osm_interp_outside_state"] += 1
                    continue
                lid = locality(None, None, None, cousubs[mi][0] if mi >= 0 else None, counties[ci][0])
                sid = street_id(street, skey(street), lid, mid[1], mid[0])
                street_rows[sid - 1][6] += 1
                extra_edges[-wid] = encode_line(cl)
                range_rows.append((len(range_rows) + 1, sid, -wid, None, a, b, min(a, b), max(a, b),
                                   par_map[interp], "osm"))
                stats["osm_interp_ranges"] += 1

        # places: postal city names and municipalities -> ZIP
        pc = collections.Counter()
        for lid, z, city, ck, csrc, muni, mk, cf in loc_rows:
            if city and csrc in ("source", "zip"):
                pc[(ck, city, "postal", z, cf)] += 1
            if muni:
                pc[(mk, muni, "municipality", z, cf)] += 1
        place_rows = [(k[0], k[1], k[2], k[3], k[4], n) for k, n in pc.items() if k[0]]

        # ------------------------------------------------ write
        tmp = out_path.with_name(out_path.name + ".tmp")
        if tmp.exists():
            tmp.unlink()
        con = sqlite3.connect(tmp)
        con.execute("PRAGMA page_size=4096")
        con.executescript(SCHEMA)
        con.executemany("INSERT INTO localities VALUES (?,?,?,?,?,?,?,?)", loc_rows)
        con.executemany("INSERT INTO streets VALUES (?,?,?,?,?,?,?,?,?)", street_rows)
        con.executemany("INSERT INTO addresses VALUES (?,?,?,?,?,?,?,?,?,?)", addr_rows)
        con.executemany("INSERT INTO edges VALUES (?,?)", [(tl, edges[tl][2]) for tl in sorted(edge_used)])
        con.executemany("INSERT INTO edges VALUES (?,?)", list(extra_edges.items()))
        con.executemany("INSERT INTO ranges VALUES (?,?,?,?,?,?,?,?,?,?)", range_rows)
        con.executemany("INSERT INTO places VALUES (?,?,?,?,?,?)", place_rows)
        con.executescript(INDEXES)
        lock = json.loads((Path(__file__).with_name("sources.lock.json")).read_text(encoding="utf-8"))
        meta = {
            "schema_version": "1",
            "state": SOURCES["state"]["usps"],
            "osm_extract": SOURCES["osm"][STATE_INFO["osm"]],
            "tiger": f"TIGER/Line {SOURCES['tiger_vintage']} ADDRFEAT",
            "nad": "USDOT National Address Database release 24 (2026-09-30)" if use_nad else "not used",
            "built_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "point_sources": json.dumps(["osm"] + source_tags),
            "attribution": ("Address data (c) OpenStreetMap contributors, ODbL 1.0; "
                            "U.S. Census Bureau TIGER/Line (public domain); "
                            + ("USDOT National Address Database (public domain); " if use_nad else "")
                            + "".join(x["attribution"] + "; " for x in SOURCES.get("extra_point_sources", [])
                                      if x["tag"] in source_tags)),
            # the short credit of the pack manifest's addresses part (docs/architecture.md 2.11)
            "credit": "; ".join(["© OpenStreetMap contributors", "U.S. Census Bureau TIGER/Line"]
                                + (["USDOT National Address Database"] if use_nad else [])
                                + [x.get("credit", x["attribution"]) for x in SOURCES.get("extra_point_sources", [])
                                   if x["tag"] in source_tags]),
            "licence": "ODbL 1.0 (database); see attribution",
            "normalisation_table_version": str(tables().raw["version"]),
            # pack format 3: the street-key fingerprint and LF-normalised hashes of the normaliser
            "key_fingerprint": key_fingerprint.key_fingerprint(street_key),
            "normaliser_sha256": json.dumps({rel(f): key_fingerprint.lf_sha256(f) for f in (
                Path(key_fingerprint.HERE) / "addrlib" / "normalize.py",
                Path(key_fingerprint.HERE) / "addrlib" / "parser.py",
                Path(key_fingerprint.HERE) / "addrlib" / "us_street_norm.json",
                key_fingerprint.SPELLINGS_FILE)}),
            "variant": "full (with diagnostic columns addresses.source_id, zip_src)",
            "geom_encoding": "edges.geom: little-endian int32 (lon, lat) pairs, micro-degrees",
            "stats": json.dumps(dict(stats)),
            "inputs_sha256": json.dumps({k: v["sha256"] for k, v in lock.items()
                                         if k in STATE_INFO["files"]}),
        }
        con.executemany("INSERT INTO meta VALUES (?,?)", list(meta.items()))
        con.commit()
        con.execute("VACUUM")
        con.close()
        if out_path.exists():
            out_path.unlink()
        tmp.replace(out_path)

        sizes = table_sizes(out_path)
        if not st.args.no_slim:
            slim = out_path.with_name(out_path.stem + "-slim.sqlite")
            write_slim(out_path, slim)
            st.info.update({"slim_output": rel(slim), "slim_output_mb": file_mb(slim)})
        st.info.update({"output": rel(out_path), "output_mb": file_mb(out_path), "use_nad": use_nad,
                        "rows": {"addresses": len(addr_rows), "streets": len(street_rows),
                                 "localities": len(loc_rows), "ranges": len(range_rows),
                                 "edges": len(edge_used) + len(extra_edges), "places": len(place_rows)},
                        "by_source": dict(collections.Counter(r[7] for r in addr_rows)),
                        "table_mb": sizes, "stats": dict(stats)})


if __name__ == "__main__":
    main()
