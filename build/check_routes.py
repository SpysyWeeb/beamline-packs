"""The test routes of the weekly check: a fixed set of trips planned on one pack's own routing part.

    .venv/routing/bin/python build/check_routes.py <pack folder> <state> [--out FILE] [--write-reference]

The pack's routing part is installed the way the app installs it (packbuilder/routing/packs.py: every
tile checked against the routing index's SHA-256 and the engine's Valhalla version) into a temporary
device folder; then every trip of build/route_checks.json for that state is planned with the plain
Fastest request of packbuilder/routing/route_lib.py. A trip passes when Valhalla finds a route, both
ends lie within max_end_gap_m of a road and the length is within the tolerance of the reference
(build/config.json "route_check"). --write-reference stores this pack's lengths as the references.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "packbuilder" / "routing"))
import packs as P  # noqa: E402
import route_lib as R  # noqa: E402

CONFIG = json.loads((REPO / "build" / "config.json").read_text(encoding="utf-8"))["route_check"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("pack", type=Path)
    ap.add_argument("state")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--checks", type=Path, default=REPO / CONFIG["file"])
    ap.add_argument("--write-reference", action="store_true")
    a = ap.parse_args()
    spec = json.loads(a.checks.read_text(encoding="utf-8"))
    trips = spec["states"].get(a.state, [])
    index = json.loads((a.pack / "routing.json").read_text(encoding="utf-8"))
    work = Path(tempfile.mkdtemp(prefix="routes-", dir=REPO / "data"))
    results, ok = [], True
    try:
        pdir = work / "pack"
        pdir.mkdir()
        shutil.copyfile(a.pack / "routing.json", pdir / "manifest.json")
        try:
            os.link(a.pack / "routing.tar", pdir / index["tar"])
        except OSError:
            shutil.copyfile(a.pack / "routing.tar", pdir / index["tar"])
        inst = P.install(a.state, device=work / "device", layout="tar", pack_dir=pdir)
        actor = R.actor(P.engine_config(work / "device"))
        for t in trips:
            s = R.summarize(R.route(actor, R.request([R.loc(t["from"]), R.loc(t["to"])])))
            r = {"id": t["id"], "what": t["what"], "ok": False}
            if s["ok"]:
                km, gaps = s["length_km"], s["endpoint_gap_m"] or [0, 0]
                r.update(length_km=km, time_s=s["time_s"], end_gap_m=gaps)
                ref = t.get("ref_km")
                within = ref is None or abs(km - ref) <= CONFIG["tolerance"] * ref
                r["ok"] = within and max(gaps) <= CONFIG["max_end_gap_m"]
                if ref is not None:
                    r["ref_km"] = ref
            else:
                r["error"] = str(s["error"])[:200]
            ok &= r["ok"] or a.write_reference
            results.append(r)
            print(f"{'ok  ' if r['ok'] else 'FAIL'} {t['id']:6} {r.get('length_km', '-'):>8} km "
                  f"(ref {t.get('ref_km', '-')}) {t['what']}", flush=True)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if not trips:
        ok = True
    summary = {"ok": bool(ok), "trips": len(trips), "passed": sum(r["ok"] for r in results),
               "graph_build": index.get("graph_build"), "tiles": inst.get("tiles_in_packs") if trips else None}
    if a.write_reference:
        for t, r in zip(trips, results):
            if "length_km" in r:
                t["ref_km"] = r["length_km"]
        a.checks.write_text(json.dumps(spec, indent=1) + "\n", encoding="utf-8")
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(json.dumps({"summary": summary, "trips": results}, indent=1) + "\n", encoding="utf-8")
    print(json.dumps(summary))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
