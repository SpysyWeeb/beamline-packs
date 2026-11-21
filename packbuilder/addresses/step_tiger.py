"""Step: load TIGER/Line 2026 ADDRFEAT address ranges for the state's counties.

    python packbuilder/addresses/step_tiger.py [--force]

One ADDRFEAT record = one street edge with left/right house-number ranges
(FROM at the edge's first vertex, TO at its last), parity and ZIP per side.
An edge can appear in several records (one per street name it carries).
Output: <data>/work/tiger_<state>.sqlite (common.py: the state, the folders)
  edges(tlid, county_fips, cousub_geoid, mtfcc, nvert, geom BLOB)
  ranges(tlid, side, fullname, street_key, hn_from, hn_to, parity, zip, plus4)
"""
from __future__ import annotations

import sqlite3

import numpy as np
import shapefile

from common import SOURCES, STATE_INFO, STATE_KEY, WORK, Step, dl_file, file_mb
from addrlib.geo import encode_line
from addrlib.normalize import house_number, street_key
from geoassign import assign, load_cousubs


def main():
    with Step("tiger") as st:
        db = WORK / f"tiger_{STATE_KEY}.sqlite"
        if db.exists():
            db.unlink()
        con = sqlite3.connect(db)
        con.execute("""CREATE TABLE edges (tlid INTEGER PRIMARY KEY, county_fips TEXT, cousub_geoid TEXT,
                       mtfcc TEXT, nvert INTEGER, geom BLOB, mid_lon REAL, mid_lat REAL)""")
        con.execute("""CREATE TABLE ranges (tlid INTEGER, side TEXT, fullname TEXT, street_key TEXT,
                       hn_from INTEGER, hn_to INTEGER, hn_from_raw TEXT, hn_to_raw TEXT,
                       parity TEXT, zip TEXT, plus4 TEXT)""")
        n_rec = n_noname = n_nonnum = 0
        key_cache = {}
        edges = {}
        for c3 in STATE_INFO["county_fips3"]:
            c5 = SOURCES["state"]["fips"] + c3
            r = shapefile.Reader(str(dl_file(f"addrfeat/tl_2026_{c5}_addrfeat.zip")))
            fields = [f[0] for f in r.fields[1:]]
            rows = []
            for sr in r.iterShapeRecords():
                rec = dict(zip(fields, sr.record))
                n_rec += 1
                tlid = int(rec["TLID"])
                if tlid not in edges:
                    pts = sr.shape.points
                    mid = pts[len(pts) // 2]
                    edges[tlid] = (c5, rec["EDGE_MTFCC"], len(pts), encode_line(pts), mid[0], mid[1])
                name = (rec["FULLNAME"] or "").strip()
                if not name:
                    n_noname += 1
                    continue
                k = key_cache.get(name)
                if k is None:
                    k = key_cache[name] = street_key(name)
                for side in ("L", "R"):
                    a, b = rec[f"{side}FROMHN"].strip(), rec[f"{side}TOHN"].strip()
                    if not a or not b:
                        continue
                    ha, hb = house_number(a)[1], house_number(b)[1]
                    if ha is None or hb is None:
                        n_nonnum += 1
                        continue
                    rows.append((tlid, side, name, k, ha, hb, a, b, rec[f"PARITY{side}"] or None,
                                 rec[f"ZIP{side}"] or None, rec[f"PLUS4{side}"] or None))
            con.executemany("INSERT INTO ranges VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
        # municipality of each edge by its middle vertex
        tl = list(edges)
        lon = np.array([edges[t][4] for t in tl])
        lat = np.array([edges[t][5] for t in tl])
        cs = load_cousubs()
        mi = assign(cs, lon, lat)
        con.executemany("INSERT INTO edges VALUES (?,?,?,?,?,?,?,?)",
                        [(t, edges[t][0], cs[mi[i]][0] if mi[i] >= 0 else None, edges[t][1], edges[t][2],
                          edges[t][3], edges[t][4], edges[t][5]) for i, t in enumerate(tl)])
        con.execute("CREATE INDEX ranges_key ON ranges(street_key)")
        con.commit()
        nr = con.execute("SELECT count(*) FROM ranges").fetchone()[0]
        nk = con.execute("SELECT count(DISTINCT street_key) FROM ranges").fetchone()[0]
        con.close()
        st.info.update({"addrfeat_records": n_rec, "records_without_name": n_noname,
                        "range_sides_non_numeric": n_nonnum, "range_rows": nr, "edges": len(edges),
                        "distinct_street_keys": nk, "work_db_mb": file_mb(db)})


if __name__ == "__main__":
    main()
