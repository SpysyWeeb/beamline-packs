"""Input robustness checks for the query engine (standard library only).

    python packbuilder/search/tests/check_inputs.py [--db data/search/pa-search.sqlite]

Checks, each printed as PASS/FAIL (exit code 1 if any fails):
  * bad positions (inf, nan, out of range, text) never raise and are ignored
    (position_ignored = true);
  * a position far outside the pack (London, 0/0) is flagged out_of_coverage, and a
    pure category query there returns nothing instead of junk name matches;
  * a position just outside the pack (Cleveland) still finds PA results;
  * the database opens from a path containing '#', '%' and spaces (hard link in a
    temporary folder next to the database);
  * hostile query strings (FTS5 syntax, quotes, SQL, NUL, very long input) never raise.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SEARCH = os.path.dirname(HERE)
REPO = os.path.abspath(os.path.join(SEARCH, "..", ".."))
sys.path.insert(0, SEARCH)
import query  # noqa: E402

FAILS = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("PASS " if ok else "FAIL ") + name + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILS.append(name)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(REPO, "data", "search", "pa-search.sqlite"))
    args = ap.parse_args()
    eng = query.SearchEngine(args.db)

    for lat, lon in [(float("inf"), 0.0), (float("nan"), 0.0), (0.0, float("-inf")), (95.0, 10.0), (40.0, 200.0),
                     ("abc", "def"), (None, -80.0), ([1], {2: 3})]:
        try:
            r = eng.search("gas", lat, lon)
            ok = r["position_ignored"] is True or (lat is None and r["position_ignored"] is False)
            ok = ok and all(x["distance_km"] is None for x in r["results"])
            check(f"bad position {lat!r},{lon!r}", ok, f"{len(r['results'])} results, no distances")
        except Exception as e:  # noqa: BLE001
            check(f"bad position {lat!r},{lon!r}", False, f"{type(e).__name__}: {e}")

    for q in ("gas", "rest stop", "hospital"):
        for name, (lat, lon) in (("London", (51.5074, -0.1278)), ("0,0", (0.0, 0.0))):
            r = eng.search(q, lat, lon)
            check(f"'{q}' at {name}: flagged, no junk", r["out_of_coverage"] is True and not r["results"],
                  f"out_of_coverage={r['out_of_coverage']}, {len(r['results'])} results")
    r = eng.search("walmart", 51.5074, -0.1278)
    check("'walmart' at London: brand still found, flagged", r["out_of_coverage"] is True and bool(r["results"])
          and r["results"][0]["title"].lower().startswith("walmart"), r["results"][0]["title"] if r["results"] else "")
    r = eng.search("gas", 41.4993, -81.6944)
    check("'gas' at Cleveland: PA stations, flagged", r["out_of_coverage"] is True and bool(r["results"])
          and r["results"][0]["category"] == "Gas station",
          f"{r['results'][0]['title']} {r['results'][0]['distance_km']} km" if r["results"] else "none")
    r = eng.search("gas", 42.1292, -80.0851)
    check("'gas' at Erie: not flagged", r["out_of_coverage"] is False and bool(r["results"]))

    tmp = tempfile.mkdtemp(prefix="search uri #% test ", dir=os.path.dirname(os.path.abspath(args.db)))
    odd = os.path.join(tmp, "pack #1 100% copy.sqlite")
    try:
        try:
            os.link(args.db, odd)
        except OSError:
            shutil.copyfile(args.db, odd)
        e2 = query.SearchEngine(odd)
        r = e2.search("erie", 42.1292, -80.0851)
        check("database path with '#', '%' and spaces", bool(r["results"]), odd.replace(REPO, "<repo>"))
        e2.db.close()
    except Exception as e:  # noqa: BLE001
        check("database path with '#', '%' and spaces", False, f"{type(e).__name__}: {e}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    hostile = ['"', "'", '"walmart', "wal*", "*", "(", ")", "NEAR(a b)", "a AND OR b", "names: walmart",
               "x'; DROP TABLE entry; --", "\x00", "walmart\x00erie", "🙂 gas", "東京", "Москва", "a" * 500,
               "gas " * 300, "16801", "-", "&", "7-11", "%", "_", "\\", "^walmart", "{names}: pizza"]
    bad = []
    for q in hostile:
        try:
            eng.search(q, 40.44, -79.99)
        except Exception as e:  # noqa: BLE001
            bad.append(f"{q[:20]!r}: {type(e).__name__}")
    check(f"{len(hostile)} hostile query strings", not bad, "; ".join(bad))
    print(f"\n{len(FAILS)} failed")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
