"""Step: extract one state's rows of the USDOT National Address Database (release 24).

    python packbuilder/addresses/step_nad.py [--force]

Source: USDOT's hosted view of NAD r24 on ArcGIS Online (same release as the
8.5 GB national TXT.zip, compiled 2026-09-30). For one state the hosted view is
far cheaper than the national file: per county we ask for the object IDs
(State=<usps> AND County=<name>) and then fetch them in pages of 2,000.
Raw pages are kept as gzip JSON lines in data/addresses/download/nad_service/
(one file per county, re-used on re-runs). Output: <data>/work/nad_<state>.sqlite
(the state and the folders: common.py, BEAMLINE_ADDR_STATE and BEAMLINE_ADDR_DATA)
"""
from __future__ import annotations

import concurrent.futures as cf
import gzip
import json
import sqlite3
import time
import urllib.parse
import urllib.request
from pathlib import Path

from common import DL, REPORTS, SOURCES, STATE_KEY, USER_AGENT, WORK, Step, file_mb

SERVICE = ("https://services.arcgis.com/xOi1kZaI0eWDREZv/ArcGIS/rest/services/"
           "Address_Points_from_National_Address_Database_view/FeatureServer/0")
USPS = SOURCES["state"]["usps"]
# a box around the state: the whole state's counties, padded (county_envelopes); PA's as it was
STATE_ENVELOPE = {"PA": "-80.52,39.71,-74.68,42.27"}.get(USPS)
FIELDS = ["OBJECTID", "AddNum_Pre", "Add_Number", "AddNum_Suf", "AddNo_Full", "St_PreMod", "St_PreDir",
          "St_PreTyp", "St_PreSep", "St_Name", "St_PosTyp", "St_PosDir", "St_PosMod", "StNam_Full",
          "Building", "Floor", "Unit", "Room", "SubAddress", "County", "Inc_Muni", "Post_City",
          "Uninc_Comm", "State", "Zip_Code", "Plus_4", "UUID", "Placement", "Addr_Type", "DateUpdate",
          "NAD_Source", "DataSet_ID", "Longitude", "Latitude"]
UA = USER_AGENT
PAGE = 2000
THREADS = 2  # be polite to the hosted service


def call(params: dict, retries: int = 6) -> dict:
    data = urllib.parse.urlencode({**params, "f": "json"}).encode()
    for attempt in range(retries):
        try:
            req = urllib.request.Request(SERVICE + "/query", data=data, headers=UA)
            with urllib.request.urlopen(req, timeout=300) as r:
                d = json.loads(r.read())
            if "error" in d:
                raise RuntimeError(d["error"])
            return d
        except Exception as e:
            print(f"  retry {attempt + 1}: {str(e)[:200]}", flush=True)
            time.sleep(10 * (attempt + 1))
    raise RuntimeError("service query failed")


def county_counts() -> dict:
    # grouping inside a where-filter on State times out on the hosted view; group by State too
    d = call({"where": "1=1", "geometry": STATE_ENVELOPE, "geometryType": "esriGeometryEnvelope",
              "inSR": "4326", "groupByFieldsForStatistics": "State,County",
              "outStatistics": json.dumps([{"statisticType": "count", "onStatisticField": "OBJECTID",
                                            "outStatisticFieldName": "n"}])})
    return {f["attributes"]["County"]: f["attributes"]["n"] for f in d["features"]
            if f["attributes"]["State"] == USPS}


def county_envelopes() -> dict:
    """County name -> 'xmin,ymin,xmax,ymax' (degrees, padded 0.05) from the Census county file."""
    from geoassign import KX, load_counties
    out = {}
    for _, name, _, geom in load_counties():
        x0, y0, x1, y1 = geom.bounds
        out[name] = f"{x0 / KX - 0.05:.4f},{y0 - 0.05:.4f},{x1 / KX + 0.05:.4f},{y1 + 0.05:.4f}"
    return out


def fetch_county(county: str, expect: int, out_dir: Path, envelope: str) -> int:
    out = out_dir / f"{county}.jsonl.gz"
    if out.exists():
        with gzip.open(out, "rt", encoding="utf-8") as f:
            n = sum(1 for _ in f)
        if n == expect:
            return n
    where = f"State='{USPS}' AND County='{county}'"
    ids = call({"where": where, "geometry": envelope, "geometryType": "esriGeometryEnvelope",
                "inSR": "4326", "returnIdsOnly": "true"})["objectIds"] or []
    ids.sort()
    pages = [ids[i:i + PAGE] for i in range(0, len(ids), PAGE)]

    def get(page):
        d = call({"objectIds": ",".join(map(str, page)), "outFields": ",".join(FIELDS),
                  "returnGeometry": "false"})
        return [f["attributes"] for f in d["features"]]

    tmp = out.with_name(out.name + ".part")
    n = 0
    t0 = time.time()
    with gzip.open(tmp, "wt", encoding="utf-8") as f, cf.ThreadPoolExecutor(THREADS) as ex:
        for k, rows in enumerate(ex.map(get, pages), 1):
            for r in rows:
                f.write(json.dumps(r, separators=(",", ":")) + "\n")
            n += len(rows)
            if k % 25 == 0:
                print(f"  {county}: {n}/{len(ids)} in {time.time() - t0:.0f}s", flush=True)
    tmp.replace(out)
    return n


def main():
    global STATE_ENVELOPE
    with Step("nad") as st:
        out_dir = DL / "nad_service"
        out_dir.mkdir(parents=True, exist_ok=True)
        env = county_envelopes()
        if STATE_ENVELOPE is None:
            boxes = [[float(v) for v in e.split(",")] for e in env.values()]
            STATE_ENVELOPE = ",".join(f"{v:.4f}" for v in (min(b[0] for b in boxes), min(b[1] for b in boxes),
                                                             max(b[2] for b in boxes), max(b[3] for b in boxes)))
        counts = county_counts()
        (REPORTS / f"nad_{STATE_KEY}_county_counts.json").write_text(json.dumps(counts, indent=2), encoding="utf-8")
        total = sum(counts.values())
        print(f"[nad] {USPS} rows in NAD r24: {total} in {len(counts)} counties", flush=True)
        got = {}
        for county, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            got[county] = fetch_county(county, n, out_dir, env.get(county, STATE_ENVELOPE))
            print(f"  {county}: {got[county]} rows (expected {n})", flush=True)
        dl_bytes = sum(p.stat().st_size for p in out_dir.glob("*.jsonl.gz"))

        db = WORK / f"nad_{STATE_KEY}.sqlite"
        if db.exists():
            db.unlink()
        con = sqlite3.connect(db)
        cols = [c for c in FIELDS]
        con.execute(f"CREATE TABLE nad ({', '.join(c + ' TEXT' for c in cols)})")
        for p in sorted(out_dir.glob("*.jsonl.gz")):
            with gzip.open(p, "rt", encoding="utf-8") as f:
                batch = [tuple(None if (v := json.loads(line).get(c)) is None else str(v) for c in cols)
                         for line in f]
            con.executemany(f"INSERT INTO nad VALUES ({','.join('?' * len(cols))})", batch)
        con.commit()
        nrows = con.execute("SELECT count(*) FROM nad").fetchone()[0]
        con.close()
        st.info.update({"state": USPS, "nad_rows": nrows, "nad_counties": len(counts),
                        "expected_rows": total, "download_mb_gzip_jsonl": round(dl_bytes / 1e6, 1),
                        "work_db_mb": file_mb(db)})


if __name__ == "__main__":
    main()
