"""Regression tests for the search engine.

    python packbuilder/search/tests/run_tests.py                 # all cases, summary + failures
    python packbuilder/search/tests/run_tests.py --type typo -v   # one type, show every case
    python packbuilder/search/tests/run_tests.py --json data/search/test-results.json

Each case in cases.json has a query, an optional bias point ("at": a name from
points.json, or "near": [lat, lon]) and one expectation. The expectation is
checked against result #1 (top-1) and against results #1-#5 (top-5).

Expectations (all mechanical):
  place: NAME          a place whose title equals NAME (compared as normalize.key;
                       a list = any of the spellings);
                       optional place_type
  street: NAME         a street whose title equals NAME (normalize.key); optional "in": text
                       that must appear in its subtitle (municipality/county)
  brand_wikidata: Q    a POI with that brand:wikidata
  category: CODE       a POI in that category (codes from categories.json; a list = any of)
  addr_street: NAME    (with brand_wikidata or category) the POI's addr:street is NAME
  title_contains: TXT  any result whose title contains TXT (case-insensitive)
  zip: CODE            the ZIP place CODE
  street_ref: KEY      a street whose name key or one of its alternative keys equals
                       KEY (normalised, e.g. "us 30", "pa 8", "interstate 80")
  house: NUMBER        with house_at: [lat, lon]: a street result whose house number
                       was located through the addresses database (house_located), whose
                       point lies within 0.1 km of house_at. Skipped (not counted) when
                       the addresses database is not attached.
  (type "crossing": two streets and the word between them; these cases are skipped for a pack
   built before the streets' lines, which cannot answer them)
  optional within_km   the matching result must lie within this distance of
                       the bias point (or of "of": a point name, or "of_point": [lat, lon])

Data gaps: before judging a failure, the runner asks the database whether ANY
entry satisfies the expectation (ignoring the query). If none does, the case
is counted as a data gap, not as an engine failure.

Invariant checked on every result of every case: distance_km must equal the
distance from the bias point to the result's own lat/lon (within 0.02 km).
Violations are counted and printed.
"""
from __future__ import annotations

import argparse
import collections
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SEARCH = os.path.dirname(HERE)
REPO = os.path.abspath(os.path.join(SEARCH, "..", ".."))
sys.path.insert(0, SEARCH)
import normalize  # noqa: E402
import query  # noqa: E402


def load(name):
    with open(os.path.join(HERE, name), encoding="utf-8") as f:
        return json.load(f)


class Checker:
    def __init__(self, eng: query.SearchEngine):
        self.eng = eng
        self.db = eng.db

    def poi_info(self, eid):
        r = self.db.execute("SELECT cats, brand_wikidata FROM poi WHERE entry_id = ?", (eid,)).fetchone()
        return (r[0] or "").split(), r[1]

    def match(self, res: dict, exp: dict, anchor) -> bool:
        within = exp.get("within_km")
        if within is not None and anchor is not None:
            if query.haversine_km(anchor[0], anchor[1], res["lat"], res["lon"]) > within:
                return False
        if "place" in exp:
            names = exp["place"] if isinstance(exp["place"], list) else [exp["place"]]
            if res["kind"] != "place" or normalize.key(res["title"]) not in {normalize.key(n) for n in names}:
                return False
            if exp.get("place_type") and res["type"] != exp["place_type"]:
                return False
            return True
        if "zip" in exp:
            return res["kind"] == "place" and res["type"] == "zip" and res["title"] == exp["zip"]
        if "house" in exp:
            if res["kind"] != "street" or not res.get("house_located") or res.get("house_number") != exp["house"]:
                return False
            hl = exp["house_at"]
            return query.haversine_km(hl[0], hl[1], res["lat"], res["lon"]) <= 0.1
        if "street_ref" in exp:
            if res["kind"] != "street":
                return False
            k, alt = self.db.execute("SELECT name_key, alt_keys FROM entry WHERE id = ?", (res["id"],)).fetchone()
            keys = {k} | {a.lstrip("~") for a in (alt or "").split("|") if a}
            return exp["street_ref"] in keys
        if "street" in exp:
            names = exp["street"] if isinstance(exp["street"], list) else [exp["street"]]
            title = res.get("street_name", res["title"])   # '600 Grant Street' when the house number was found
            if res["kind"] != "street" or normalize.key(title) not in {normalize.key(n) for n in names}:
                return False
            if exp.get("in") and exp["in"].lower() not in res["subtitle"].lower():
                return False
            return True
        if "addr_street" in exp:
            if res["kind"] != "poi":
                return False
            st = self.db.execute("SELECT addr_street FROM poi WHERE entry_id = ?", (res["id"],)).fetchone()[0]
            if not st or normalize.key(st) != normalize.key(exp["addr_street"]):
                return False
        if "brand_wikidata" in exp:
            if res["kind"] != "poi":
                return False
            return self.poi_info(res["id"])[1] == exp["brand_wikidata"]
        if "category" in exp:
            if res["kind"] != "poi":
                return False
            want = exp["category"] if isinstance(exp["category"], list) else [exp["category"]]
            return any(w in self.poi_info(res["id"])[0] for w in want)
        if "title_contains" in exp:
            if exp.get("kind") and res["kind"] != exp["kind"]:
                return False
            return exp["title_contains"].lower() in res["title"].lower()
        raise ValueError(f"unknown expectation {exp}")

    def exists(self, exp: dict, anchor) -> bool:
        """Does any entry in the database satisfy the expectation?"""
        within = exp.get("within_km")
        rows = []
        if "place" in exp:
            names = exp["place"] if isinstance(exp["place"], list) else [exp["place"]]
            keys = [normalize.key(n) for n in names]
            rows = self.db.execute("SELECT id, lat, lon, type FROM entry WHERE kind = 1 AND name_key IN "
                                   f"({','.join('?' * len(keys))})", keys).fetchall()
            if exp.get("place_type"):
                rows = [r for r in rows if r[3] == exp["place_type"]]
        elif "zip" in exp:
            rows = self.db.execute("SELECT id, lat, lon, type FROM entry WHERE kind = 1 AND type = 'zip' AND name = ?",
                                   (exp["zip"],)).fetchall()
        elif "house" in exp:
            # the address exists if the addresses database has that number within 0.1 km of house_at
            hl = exp["house_at"]
            d = 0.0015
            try:
                rows = self.db.execute(
                    "SELECT a.id, a.lat_e6 / 1e6, a.lon_e6 / 1e6 FROM addr.addresses a WHERE a.hn = ? "
                    "AND a.lat_e6 BETWEEN ? AND ? AND a.lon_e6 BETWEEN ? AND ?",
                    (exp["house"], int((hl[0] - d) * 1e6), int((hl[0] + d) * 1e6),
                     int((hl[1] - d) * 1e6), int((hl[1] + d) * 1e6))).fetchall()
            except Exception:
                rows = []
            rows = [r for r in rows if query.haversine_km(hl[0], hl[1], r[1], r[2]) <= 0.1]
            return bool(rows)
        elif "street_ref" in exp:
            k = exp["street_ref"]
            rows = self.db.execute("SELECT id, lat, lon, NULL FROM entry WHERE kind = 2 AND (name_key = ? "
                                   "OR '|' || alt_keys || '|' LIKE ? OR '|' || alt_keys || '|' LIKE ?)",
                                   (k, f"%|{k}|%", f"%|~{k}|%")).fetchall()
        elif "street" in exp:
            names = exp["street"] if isinstance(exp["street"], list) else [exp["street"]]
            keys = [normalize.key(n) for n in names]
            rows = self.db.execute("SELECT id, lat, lon, subtitle FROM entry WHERE kind = 2 AND name_key IN "
                                   f"({','.join('?' * len(keys))})", keys).fetchall()
            if exp.get("in"):
                rows = [r for r in rows if exp["in"].lower() in (r[3] or "").lower()]
        elif "brand_wikidata" in exp:
            rows = self.db.execute("SELECT e.id, e.lat, e.lon, p.addr_street FROM poi p JOIN entry e ON e.id = p.entry_id "
                                   "WHERE p.brand_wikidata = ?", (exp["brand_wikidata"],)).fetchall()
        elif "category" in exp:
            want = exp["category"] if isinstance(exp["category"], list) else [exp["category"]]
            rows = self.db.execute("SELECT e.id, e.lat, e.lon, p.addr_street FROM poi_cat c JOIN category k ON k.id = c.cat_id "
                                   "JOIN entry e ON e.id = c.entry_id JOIN poi p ON p.entry_id = e.id "
                                   f"WHERE k.code IN ({','.join('?' * len(want))})", want).fetchall()
        if "addr_street" in exp:
            rows = [r for r in rows if r[3] and normalize.key(r[3]) == normalize.key(exp["addr_street"])]
        elif "title_contains" in exp:
            kind = {"place": 1, "street": 2, "poi": 3}.get(exp.get("kind"))
            rows = self.db.execute("SELECT e.id, e.lat, e.lon, p.addr_street FROM entry e "
                                   "LEFT JOIN poi p ON p.entry_id = e.id WHERE instr(lower(e.name), ?) > 0" +
                                   (f" AND e.kind = {kind}" if kind else ""),
                                   (exp["title_contains"].lower(),)).fetchall()
        if within is not None and anchor is not None:
            rows = [r for r in rows if query.haversine_km(anchor[0], anchor[1], r[1], r[2]) <= within]
        return bool(rows)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(REPO, "data", "search", "pa-search.sqlite"))
    ap.add_argument("--addresses", default=os.path.join(REPO, "data", "addresses", "pa-addresses.sqlite"))
    ap.add_argument("--type")
    ap.add_argument("--id")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--json", help="write per-case results here")
    ap.add_argument("--cases", default="cases.json",
                    help="cases file in this folder (cases_holdout.json = held-out set, cases_review.json = review set)")
    ap.add_argument("--no-addresses", action="store_true", help="do not attach the addresses database")
    ap.add_argument("--allow-addr-version-mismatch", action="store_true",
                    help="attach the addresses database even if its normalisation version differs (prints a warning)")
    args = ap.parse_args()
    points = load("points.json")["points"]
    cases = load(args.cases)["cases"]
    eng = query.SearchEngine(args.db, addresses_path=None if args.no_addresses else args.addresses,
                             allow_addr_version_mismatch=args.allow_addr_version_mismatch)
    if eng.addr_warning:
        print("WARNING:", eng.addr_warning)
    chk = Checker(eng)
    by_type = collections.defaultdict(lambda: collections.Counter())
    out = []
    skipped = []
    bad_distance = []
    no_lines = []
    for c in cases:
        if args.type and c["type"] != args.type:
            continue
        if args.id and c["id"] != args.id:
            continue
        if "house" in c["expect"] and not eng.addr_ok:
            skipped.append(c["id"])
            continue
        if c["type"] == "crossing" and not eng.has_lines:
            no_lines.append(c["id"])   # a pack built before the streets' lines (table street_line)
            continue
        bias = None
        if "at" in c:
            bias = (points[c["at"]]["lat"], points[c["at"]]["lon"])
        elif "near" in c:
            bias = tuple(c["near"])
        exp = c["expect"]
        anchor = bias
        if "of" in exp:
            anchor = (points[exp["of"]]["lat"], points[exp["of"]]["lon"])
        elif "of_point" in exp:
            anchor = tuple(exp["of_point"])
        res = eng.search(c["q"], bias[0] if bias else None, bias[1] if bias else None, limit=10)
        for r in res["results"]:
            if not bias or r["distance_km"] is None:
                continue
            # every result is as far away as the point it gives (a street: the nearest point of its line)
            if abs(r["distance_km"] - query.haversine_km(bias[0], bias[1], r["lat"], r["lon"])) > 0.02:
                bad_distance.append((c["id"], r["title"], r["distance_km"]))
        hits = [chk.match(r, exp, anchor) for r in res["results"][:5]]
        top1 = bool(hits[:1] and hits[0])
        top5 = any(hits)
        gap = False
        if not top5:
            gap = not chk.exists(exp, anchor)
        t = by_type[c["type"]]
        t["n"] += 1
        t["top1"] += top1
        t["top5"] += top5
        t["gap"] += gap
        rec = {"id": c["id"], "type": c["type"], "q": c["q"], "at": c.get("at"), "expect": exp,
               "top1": top1, "top5": top5, "data_gap": gap, "ms": res["ms"],
               "got": [f"{r['title']} | {r['category']} | {r['subtitle']} | {r['distance_km']}" for r in res["results"][:5]],
               "parse": res["parse"], "note": c.get("note")}
        out.append(rec)
        if args.verbose or not top5:
            mark = "PASS1" if top1 else ("PASS5" if top5 else ("GAP  " if gap else "FAIL "))
            print(f"{mark} [{c['type']}] {c['id']}: {c['q']!r} @{c.get('at')} expect {exp}")
            if not top1 or args.verbose:
                for g in rec["got"]:
                    print("        ", g)
    tot = collections.Counter()
    print()
    print(f"{'type':<16}{'cases':>6}{'top1':>8}{'top5':>8}{'gaps':>6}{'top5 excl. gaps':>17}")
    for typ in sorted(by_type):
        t = by_type[typ]
        tot.update(t)
        ng = t["n"] - t["gap"]
        print(f"{typ:<16}{t['n']:>6}{t['top1'] / t['n']:>8.0%}{t['top5'] / t['n']:>8.0%}{t['gap']:>6}"
              f"{(t['top5'] / ng if ng else 0):>17.0%}")
    if tot["n"]:
        ng = tot["n"] - tot["gap"]
        print(f"{'ALL':<16}{tot['n']:>6}{tot['top1'] / tot['n']:>8.0%}{tot['top5'] / tot['n']:>8.0%}{tot['gap']:>6}"
              f"{(tot['top5'] / ng if ng else 0):>17.0%}")
    if skipped:
        print(f"skipped {len(skipped)} house-number cases: addresses database not attached")
    if no_lines:
        print(f"skipped {len(no_lines)} crossing cases: the pack has no street lines")
    if eng.addr_warning:
        print("WARNING:", eng.addr_warning)
    print(f"results whose distance_km disagrees with their coordinates: {len(bad_distance)}")
    for x in bad_distance[:10]:
        print("   ", x)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({"summary": {k: dict(v) for k, v in by_type.items()}, "cases": out, "skipped": skipped,
                       "bad_distance": bad_distance}, f, indent=1)


if __name__ == "__main__":
    main()
