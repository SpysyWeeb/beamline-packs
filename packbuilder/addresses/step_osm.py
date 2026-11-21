"""Step: extract every OSM object with an address from the state extract.

    python packbuilder/addresses/step_osm.py [--force]

Reads the state's extract, e.g. data/osm/pennsylvania-261114.osm.pbf (waits until its MD5 matches the
.md5 file). Three passes with pyosmium (BSD-2):
  1. relations with addr:housenumber (multipolygon buildings) and
     type=associatedStreet relations (street name for members without addr:street);
  2. nodes and ways with addr:housenumber or addr:interpolation, with node
     locations; a way's position is its polygon centroid (or vertex mean);
  3. the outer member ways of the address relations, for their centroids.
Each point is then assigned a county (Census cartographic boundary) and a
county subdivision (TIGER COUSUB). Output: <data>/work/osm_<state>.sqlite (common.py: the state, the folders)
"""
from __future__ import annotations

import sqlite3
import time

import numpy as np
import osmium

from common import STATE_KEY, WORK, Step, file_mb, wait_for_osm, MAX_THREADS
from geoassign import assign, load_counties, load_cousubs

ADDR_KEYS = ("addr:housenumber", "addr:street", "addr:place", "addr:unit", "addr:city",
             "addr:postcode", "addr:state", "addr:flats")


def centroid(coords):
    """Polygon centroid for a closed ring, else vertex mean. coords: [(lon, lat)]."""
    if len(coords) >= 4 and coords[0] == coords[-1]:
        kx = np.cos(np.radians(coords[0][1]))
        a = cx = cy = 0.0
        for (x0, y0), (x1, y1) in zip(coords[:-1], coords[1:]):
            x0 *= kx
            x1 *= kx
            c = x0 * y1 - x1 * y0
            a += c
            cx += (x0 + x1) * c
            cy += (y0 + y1) * c
        if abs(a) > 1e-14:
            return (cx / (3 * a)) / kx, cy / (3 * a)
    n = len(coords)
    return sum(c[0] for c in coords) / n, sum(c[1] for c in coords) / n


def pool():
    return osmium.io.ThreadPool(MAX_THREADS)


def tagdict(o):
    return {k: o.tags.get(k) for k in ADDR_KEYS if k in o.tags}


def main():
    with Step("osm") as st:
        pbf = wait_for_osm()
        t0 = time.time()
        # pass 1: relations
        rel_addr = {}       # rel id -> (tags, [outer way ids])
        assoc_street = {}   # ('n'|'w', id) -> street name
        for r in osmium.FileProcessor(str(pbf), osmium.osm.RELATION, thread_pool=pool()):
            if "addr:housenumber" in r.tags:
                ways = [m.ref for m in r.members if m.type == "w" and m.role in ("outer", "")]
                rel_addr[r.id] = (tagdict(r), ways)
            elif r.tags.get("type") == "associatedStreet" and "name" in r.tags:
                for m in r.members:
                    if m.role == "house" and m.type in ("n", "w"):
                        assoc_street[(m.type, m.ref)] = r.tags["name"]
        t1 = time.time()
        print(f"[osm] pass 1: {len(rel_addr)} address relations, {len(assoc_street)} associatedStreet members, "
              f"{t1 - t0:.0f}s", flush=True)

        # pass 2: nodes and ways with addresses
        rows = []
        interp = []
        nodes_hn = {}
        fp = (osmium.FileProcessor(str(pbf), osmium.osm.NODE | osmium.osm.WAY, thread_pool=pool())
              .with_locations()
              .with_filter(osmium.filter.KeyFilter("addr:housenumber", "addr:interpolation")))
        for o in fp:
            if o.is_node():
                if "addr:housenumber" not in o.tags or not o.location.valid():
                    continue
                tg = tagdict(o)
                rows.append(("n", o.id, tg, o.location.lon, o.location.lat, "building" in o.tags))
                nodes_hn[o.id] = tg["addr:housenumber"]
            else:
                coords = [(n.lon, n.lat) for n in o.nodes if n.location.valid()]
                if not coords:
                    continue
                if "addr:housenumber" in o.tags:
                    lon, lat = centroid(coords)
                    rows.append(("w", o.id, tagdict(o), lon, lat, "building" in o.tags))
                if "addr:interpolation" in o.tags and len(coords) >= 2:
                    interp.append((o.id, o.tags.get("addr:interpolation"), tagdict(o),
                                   o.nodes[0].ref, o.nodes[-1].ref, coords))
        t2 = time.time()
        print(f"[osm] pass 2: {len(rows)} node/way addresses, {len(interp)} interpolation ways, "
              f"{t2 - t1:.0f}s", flush=True)

        # pass 3: member ways of address relations
        need = {w for _, ws in rel_addr.values() for w in ws}
        way_coords = {}
        if need:
            fp = (osmium.FileProcessor(str(pbf), osmium.osm.NODE | osmium.osm.WAY, thread_pool=pool())
                  .with_locations()
                  .with_filter(osmium.filter.EntityFilter(osmium.osm.WAY))
                  .with_filter(osmium.filter.IdFilter(need)))
            for w in fp:
                way_coords[w.id] = [(n.lon, n.lat) for n in w.nodes if n.location.valid()]
        for rid, (tg, ws) in rel_addr.items():
            pts = [c for w in ws for c in way_coords.get(w, [])]
            if pts:
                lon, lat = sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)
                rows.append(("r", rid, tg, lon, lat, True))
        t3 = time.time()
        print(f"[osm] pass 3: {len(way_coords)} relation member ways, {t3 - t2:.0f}s", flush=True)

        # county + county subdivision
        lon = np.array([r[3] for r in rows])
        lat = np.array([r[4] for r in rows])
        counties = load_counties()
        cousubs = load_cousubs()
        ci = assign(counties, lon, lat)
        mi = assign(cousubs, lon, lat)
        t4 = time.time()
        print(f"[osm] county assignment {t4 - t3:.0f}s", flush=True)

        WORK.mkdir(parents=True, exist_ok=True)
        db = WORK / f"osm_{STATE_KEY}.sqlite"
        if db.exists():
            db.unlink()
        con = sqlite3.connect(db)
        con.execute("""CREATE TABLE osm (osm_type TEXT, osm_id INTEGER, hn_raw TEXT, street TEXT,
            street_from TEXT, place TEXT, unit TEXT, city TEXT, postcode TEXT, state TEXT, flats TEXT,
            is_building INTEGER, lat REAL, lon REAL, county_fips TEXT, cousub_geoid TEXT,
            cousub_name TEXT)""")
        out = []
        for k, (typ, oid, tg, x, y, bld) in enumerate(rows):
            street = tg.get("addr:street")
            sfrom = "addr:street" if street else None
            if not street and (typ, oid) in assoc_street:
                street, sfrom = assoc_street[(typ, oid)], "associatedStreet"
            c = counties[ci[k]][0] if ci[k] >= 0 else None
            m = cousubs[mi[k]] if mi[k] >= 0 else None
            out.append((typ, oid, tg.get("addr:housenumber"), street, sfrom, tg.get("addr:place"),
                        tg.get("addr:unit"), tg.get("addr:city"), tg.get("addr:postcode"),
                        tg.get("addr:state"), tg.get("addr:flats"), int(bld), y, x, c,
                        m[0] if m else None, m[1] if m else None))
        con.executemany("INSERT INTO osm VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", out)
        con.execute("""CREATE TABLE osm_interp (way_id INTEGER, interpolation TEXT, street TEXT,
            first_node INTEGER, last_node INTEGER, first_hn TEXT, last_hn TEXT, coords TEXT)""")
        con.executemany("INSERT INTO osm_interp VALUES (?,?,?,?,?,?,?,?)",
                        [(wid, it, tg.get("addr:street"), a, b, nodes_hn.get(a), nodes_hn.get(b),
                          ";".join(f"{x:.7f},{y:.7f}" for x, y in coords))
                         for wid, it, tg, a, b, coords in interp])
        con.commit()
        n_in = con.execute("SELECT count(*) FROM osm WHERE county_fips IS NOT NULL").fetchone()[0]
        con.close()
        st.info.update({
            "pbf": pbf.name, "objects_with_housenumber": len(rows),
            "by_type": {t: sum(1 for r in rows if r[0] == t) for t in "nwr"},
            "inside_state_counties": n_in, "interpolation_ways": len(interp),
            "pass_s": [round(t1 - t0), round(t2 - t1), round(t3 - t2), round(t4 - t3)],
            "work_db_mb": file_mb(db),
        })


if __name__ == "__main__":
    main()
