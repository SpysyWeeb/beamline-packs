"""Step: fetch extra open address-point sources listed in sources.json "extra_point_sources".

    python packbuilder/addresses/step_extra.py [--force]

Today one source qualifies: the City of Scranton's 911 address layer (covers all of
Lackawanna County), dedicated to the public domain (PDDL). Only address fields are
requested (house number, street parts, unit, municipality, ZIP, position); the layer
also carries owner names and phone fields, which are never fetched.
Output: data/addresses/work/extra_pa.sqlite  table extra(source, src_id, hn, street, unit, city, zip, lon, lat)
"""
from __future__ import annotations

import json
import sqlite3
import time
import urllib.parse
import urllib.request

from common import DL, SOURCES, STATE_KEY, USER_AGENT, WORK, Step, file_mb

UA = USER_AGENT


def call(url, params, retries=6):
    data = urllib.parse.urlencode({**params, "f": "json"}).encode()
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url + "/query", data=data, headers=UA),
                                        timeout=300) as r:
                d = json.loads(r.read())
            if "error" in d:
                raise RuntimeError(d["error"])
            return d
        except Exception as e:
            print(f"  retry {attempt + 1}: {str(e)[:200]}", flush=True)
            time.sleep(10 * (attempt + 1))
    raise RuntimeError("query failed")


def clean(v):
    v = (v or "").strip() if isinstance(v, str) else v
    return v or None


def main():
    with Step("extra") as st:
        db = WORK / f"extra_{STATE_KEY}.sqlite"
        if db.exists():
            db.unlink()
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE extra (source TEXT, src_id TEXT, hn TEXT, street TEXT, unit TEXT, city TEXT, "
                    "zip TEXT, lon REAL, lat REAL, municipality TEXT)")
        info = {}
        for src in [s for s in SOURCES["extra_point_sources"] if s.get("state", "pa") == STATE_KEY]:
            f = src["fields"]
            ids = sorted(call(src["service"], {"where": "1=1", "returnIdsOnly": "true"})["objectIds"])
            raw = DL / f"extra_{src['tag']}.jsonl"
            n = 0
            with open(raw, "w", encoding="utf-8") as out:
                for i in range(0, len(ids), 2000):
                    d = call(src["service"], {"objectIds": ",".join(map(str, ids[i:i + 2000])),
                                              "outFields": ",".join(["OBJECTID"] + list(f.values())),
                                              "outSR": "4326", "returnGeometry": "true"})
                    rows = []
                    for feat in d["features"]:
                        a, g = feat["attributes"], feat.get("geometry") or {}
                        out.write(json.dumps({"a": a, "g": g}) + "\n")
                        street = " ".join(x for x in (clean(a.get(f["prefix"])), clean(a.get(f["street"])),
                                                      clean(a.get(f["suffix"])), clean(a.get(f["postdir"]))) if x)
                        rows.append((src["tag"], str(a["OBJECTID"]), clean(str(a.get(f["hn"]) or "")), street or None,
                                     clean(a.get(f["unit"])), None, clean(a.get(f["zip"])), g.get("x"), g.get("y"),
                                     clean(a.get(f["municipality"]))))
                    con.executemany("INSERT INTO extra VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
                    n += len(rows)
            con.commit()
            info[src["tag"]] = {"rows": n, "ids": len(ids), "raw_mb": file_mb(raw)}
            print(f"[extra] {src['tag']}: {n} rows", flush=True)
        con.close()
        st.info.update({"sources": info, "work_db_mb": file_mb(db)})


if __name__ == "__main__":
    main()
