"""Build the combined PA+OH Valhalla graph and cut per-state routing packs.

usage (from the repo root, with the routing venv):
    .venv/routing/Scripts/python packbuilder/routing/build.py all
    .venv/routing/Scripts/python packbuilder/routing/build.py tiles --force

steps, in order (each is skipped when its output exists, unless --force):
    inputs     wait for data/osm/*.osm.pbf to match their .md5, merge PA+OH (merge_osm.py), open the
               entrance pieces tagged as driveways (driveways.py)
    timezones  download the pinned timezone shapefile, build tz_world.sqlite (tzdb.py)
    config     write data/routing/build/valhalla.json with valhalla_build_config
    boundaries complete US/PA/OH/neighbour boundary relations at the snapshot time (fetch_boundaries.py)
    admins     valhalla_build_admins on those boundaries
    tiles      valhalla_build_tiles on the merged extract (one graph for both states)
    packs      per-state packs with valhalla_build_extract -g (state .poly -> GeoJSON)
    all        everything above

environment (common.py): BEAMLINE_SNAPSHOT (YYMMDD of the extracts, default 261114),
BEAMLINE_ROUTING_STATES (the graph's states, default "pa,oh"; one state uses its extract as it is),
BEAMLINE_THREADS (default 4).

experiments (not part of `all`):
    tiles-unmerged   build from the two state PBFs passed separately, into
                     data/routing/exp_unmerged/, to compare with the merged build
    tiles-allmodes   keep walk/bike edges (Valhalla default) into data/routing/exp_allmodes/,
                     to measure what driving-only saves
    tiles-disk       rebuild into data/routing/exp_disk/, stop before cleanup, measure the
                     temporary files + tiles on disk, then clean up
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tarfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402
import driveways  # noqa: E402

STAMPS = C.BUILD / "stamps"


def signature(paths: list[Path], **extra) -> dict:
    """What a step was built from: size and mtime of each input file, plus pinned values."""
    sig = {}
    for p in paths:
        p = Path(p)
        sig[C.rel(p)] = [p.stat().st_size, p.stat().st_mtime_ns] if p.exists() else None
    sig.update(extra)
    return sig


def done(step: str, sig: dict | None = None) -> bool:
    """True when the step has a stamp and (if the stamp recorded one) its inputs are unchanged.
    Stamps written before input signatures existed are trusted as they are."""
    f = STAMPS / f"{step}.json"
    if not f.exists():
        return False
    old = C.load_json(f).get("input_signature")
    if sig is None or old is None:
        return True
    if old != sig:
        changed = sorted(k for k in set(old) | set(sig) if old.get(k) != sig.get(k))
        print(f"[{step}] inputs changed since the last build ({', '.join(changed)}): rebuilding", flush=True)
        return False
    return True


def stamp(step: str, info: dict, sig: dict | None = None) -> None:
    if sig is not None:
        info["input_signature"] = sig
    C.save_json(STAMPS / f"{step}.json", info)
    C.record_measurement(step, info)
    print(f"[{step}] {json.dumps(info)}", flush=True)


# ---------------------------------------------------------------- inputs
def step_inputs(force: bool) -> None:
    raw = [C.OSM_IN / f"{name}-{C.SNAPSHOT}.osm.pbf" for name in C.STATES.values()]
    sig = signature(raw, driveways_rule=driveways.RULE)
    if done("inputs", sig) and C.MERGED_PBF.exists() and not force:
        print("[inputs] the graph's input exists, skipping")
        return
    pbfs = [C.wait_for_osm_input(f"{name}-{C.SNAPSHOT}.osm.pbf") for name in C.STATES.values()]
    C.MERGED_PBF.parent.mkdir(parents=True, exist_ok=True)
    if len(pbfs) == 1:
        # one state: its extract is the graph's input, with the entrance driveways opened (driveways.py)
        opened = driveways.open_entrances(pbfs[0], C.MERGED_PBF)
        print(f"[inputs] {opened['driveways_opened']} entrance driveways opened", flush=True)
        stamp("inputs", {"single_state": pbfs[0].name, "inputs": {pbfs[0].name: pbfs[0].stat().st_size},
                         "driveways": opened}, sig)
        return
    print(f"[inputs] md5 ok: {[p.name for p in pbfs]}; merging", flush=True)
    merged = C.MERGED_PBF.with_name(C.MERGED_PBF.name.replace(".osm.pbf", ".merged.osm.pbf"))
    res = C.run_measured([sys.executable, C.HERE / "merge_osm.py", merged, *pbfs],
                         C.LOGS / "merge_osm.log")
    if res["returncode"] != 0:
        sys.exit(f"merge failed, see {res['log']}")
    log = (C.LOGS / "merge_osm.log").read_text()
    merge_info = json.loads(log[log.index("{"):])
    opened = driveways.open_entrances(merged, C.MERGED_PBF)
    merged.unlink()
    print(f"[inputs] {opened['driveways_opened']} entrance driveways opened", flush=True)
    res.update(merge=merge_info, inputs={p.name: p.stat().st_size for p in pbfs}, driveways=opened)
    stamp("inputs", res, sig)


# ---------------------------------------------------------------- timezones
def step_timezones(force: bool) -> None:
    import tzdb

    sig = signature([], tz_release=tzdb.TZ_RELEASE, tz_sha256=tzdb.TZ_SHA256)
    if done("timezones", sig) and C.TZ_DB.exists() and not force:
        print("[timezones] exists, skipping")
        return
    stem = tzdb.fetch_shapefile(C.DOWNLOADS)
    env = dict(os.environ, SPATIALITE_SECURITY="relaxed")
    res = C.run_measured([sys.executable, C.HERE / "tzdb.py", stem, C.TZ_DB], C.LOGS / "timezones.log", env=env)
    if res["returncode"] != 0:
        sys.exit(f"timezone db failed, see {res['log']}")
    out = (C.LOGS / "timezones.log").read_text().strip().splitlines()[-1]
    res.update(tz=json.loads(out), source=f"timezone-boundary-builder {tzdb.TZ_RELEASE} {tzdb.TZ_ZIP}")
    stamp("timezones", res, sig)


# ---------------------------------------------------------------- config
def build_config_dict() -> dict:
    """valhalla.json for building, produced by valhalla_build_config (the wheel's CLI)."""
    args = [
        sys.executable, "-m", "valhalla.valhalla_build_config",
        "--mjolnir-tile-dir", str(C.TILE_DIR),
        "--mjolnir-tile-extract", str(C.BUILD / "valhalla_tiles.tar"),
        "--mjolnir-admin", str(C.ADMIN_DB),
        "--mjolnir-timezone", str(C.TZ_DB),
        "--mjolnir-concurrency", str(C.THREADS),
        # per-thread tile cache while building; 4 threads x 512 MB keeps us near the 6 GB budget
        "--mjolnir-max-cache-size", str(512 * 2**20),
        # driving-first app: drop walk/bike-only edges from the graph (smaller packs)
        "--mjolnir-include-pedestrian", "False",
        "--mjolnir-include-bicycle", "False",
        "--logging-type", "std_out",
        "--logging-color", "False",
    ]
    out = subprocess.run(args, check=True, capture_output=True, text=True).stdout
    return json.loads(out)


def step_config(force: bool) -> None:
    if C.BUILD_CONFIG.exists() and not force:
        print("[config] exists, skipping")
        return
    cfg = build_config_dict()
    C.save_json(C.BUILD_CONFIG, cfg)
    print(f"[config] wrote {C.rel(C.BUILD_CONFIG)}")


# ---------------------------------------------------------------- boundaries + admins
def step_boundaries(force: bool) -> None:
    """Complete boundary relations (US, PA, OH, neighbours) at the snapshot time, via Overpass."""
    snap = C.OSM_IN / f"{next(iter(C.STATES.values()))}-{C.SNAPSHOT}.osm.pbf"
    sig = signature([snap])
    if done("boundaries", sig) and C.BOUNDARIES_PBF.exists() and not force:
        print("[boundaries] exists, skipping")
        return
    res = C.run_measured([sys.executable, C.HERE / "fetch_boundaries.py", C.BOUNDARIES_PBF, snap],
                         C.LOGS / "fetch_boundaries.log")
    if res["returncode"] != 0:
        sys.exit(f"boundary fetch failed, see {res['log']}")
    log = (C.LOGS / "fetch_boundaries.log").read_text()
    res["boundaries"] = json.loads(log[log.index("\n{") + 1:])
    stamp("boundaries", res, sig)


def step_admins(force: bool) -> None:
    """Admin DB from the complete boundary relations.

    Building it from the state extracts themselves (first attempt) kept only Ohio:
    PA and US relations are cut at the extract edge and get skipped as degenerate."""
    sig = signature([C.BOUNDARIES_PBF, C.BUILD_CONFIG])
    if done("admins", sig) and C.ADMIN_DB.exists() and not force:
        print("[admins] exists, skipping")
        return
    if C.ADMIN_DB.exists():
        C.ADMIN_DB.unlink()
    res = C.run_measured([C.valhalla_exe("valhalla_build_admins"), "-c", C.BUILD_CONFIG, C.BOUNDARIES_PBF],
                         C.LOGS / "build_admins.log", env=C.valhalla_env())
    if res["returncode"] != 0:
        sys.exit(f"valhalla_build_admins failed, see {res['log']}")
    res["size_bytes"] = C.ADMIN_DB.stat().st_size
    import sqlite3
    with sqlite3.connect(C.ADMIN_DB) as db:
        res["admins"] = [list(r) for r in db.execute("select admin_level, name, iso_code from admins order by admin_level, name")]
    stamp("admins", res, sig)


# ---------------------------------------------------------------- tiles
def tile_stats(tile_dir: Path) -> dict:
    levels = {}
    for lvl in sorted(p.name for p in tile_dir.iterdir() if p.is_dir()):
        files = list((tile_dir / lvl).rglob("*.gph"))
        levels[lvl] = {"tiles": len(files), "bytes": sum(f.stat().st_size for f in files)}
    total = sum(v["bytes"] for v in levels.values())
    return {"levels": levels, "total_bytes": total, "total_tiles": sum(v["tiles"] for v in levels.values())}


def run_build_tiles(cfg_path: Path, pbfs: list[Path], log: Path) -> dict:
    res = C.run_measured([C.valhalla_exe("valhalla_build_tiles"), "-c", cfg_path, "-j", str(C.THREADS), *pbfs],
                         log, env=C.valhalla_env())
    if res["returncode"] != 0:
        sys.exit(f"valhalla_build_tiles failed with exit code {res['returncode']} "
                 f"(peak working set {res.get('peak_working_set_mb')} MiB), see {res['log']}")
    return res


def step_tiles(force: bool) -> None:
    sig = signature([C.MERGED_PBF, C.ADMIN_DB, C.TZ_DB, C.BUILD_CONFIG])
    if done("tiles", sig) and C.TILE_DIR.exists() and not force:
        print("[tiles] exists, skipping")
        return
    res = run_build_tiles(C.BUILD_CONFIG, [C.MERGED_PBF], C.LOGS / "build_tiles.log")
    res["tiles"] = tile_stats(C.TILE_DIR)
    stamp("tiles", res, sig)


def step_tiles_unmerged(force: bool) -> None:
    """Experiment: hand valhalla_build_tiles the two overlapping state extracts directly."""
    out = C.DATA / "exp_unmerged"
    if (out / "stamp.json").exists() and not force:
        print("[tiles-unmerged] exists, skipping")
        return
    cfg = C.load_json(C.BUILD_CONFIG)
    cfg["mjolnir"]["tile_dir"] = str(out / "valhalla_tiles")
    cfg["mjolnir"]["tile_extract"] = str(out / "valhalla_tiles.tar")
    # private copy of the admin db so a concurrent admins rebuild cannot interfere
    import shutil
    out.mkdir(parents=True, exist_ok=True)
    shutil.copy2(C.ADMIN_DB, out / "admins.sqlite")
    cfg["mjolnir"]["admin"] = str(out / "admins.sqlite")
    cfg_path = out / "valhalla.json"
    C.save_json(cfg_path, cfg)
    pbfs = [C.OSM_IN / f"{name}-{C.SNAPSHOT}.osm.pbf" for name in C.STATES.values()]
    res = run_build_tiles(cfg_path, pbfs, C.LOGS / "build_tiles_unmerged.log")
    res["tiles"] = tile_stats(out / "valhalla_tiles")
    C.save_json(out / "stamp.json", res)
    C.record_measurement("tiles-unmerged", res)
    print(json.dumps(res, indent=2))


def step_tiles_allmodes(force: bool) -> None:
    """Experiment: same merged input, but keep walking/cycling edges (Valhalla's default),
    to see what the driving-only choice saves."""
    out = C.DATA / "exp_allmodes"
    if (out / "stamp.json").exists() and not force:
        print("[tiles-allmodes] exists, skipping")
        return
    cfg = C.load_json(C.BUILD_CONFIG)
    cfg["mjolnir"]["tile_dir"] = str(out / "valhalla_tiles")
    cfg["mjolnir"]["tile_extract"] = str(out / "valhalla_tiles.tar")
    cfg["mjolnir"]["include_pedestrian"] = True
    cfg["mjolnir"]["include_bicycle"] = True
    out.mkdir(parents=True, exist_ok=True)
    cfg_path = out / "valhalla.json"
    C.save_json(cfg_path, cfg)
    res = run_build_tiles(cfg_path, [C.MERGED_PBF], C.LOGS / "build_tiles_allmodes.log")
    res["tiles"] = tile_stats(out / "valhalla_tiles")
    C.save_json(out / "stamp.json", res)
    C.record_measurement("tiles-allmodes", res)
    print(json.dumps(res, indent=2))


def step_tiles_disk(force: bool) -> None:
    """Experiment: build disk footprint. Builds the driving-only graph again into
    data/routing/exp_disk/ but stops before the cleanup stage, so the temporary *.bin
    files are still there; measures the directory, then runs the cleanup stage.
    (Walking the directory *while* the build runs made the first attempt fail.)"""
    out = C.DATA / "exp_disk"
    if (out / "stamp.json").exists() and not force:
        print("[tiles-disk] exists, skipping")
        return
    cfg = C.load_json(C.BUILD_CONFIG)
    cfg["mjolnir"]["tile_dir"] = str(out / "valhalla_tiles")
    cfg["mjolnir"]["tile_extract"] = str(out / "valhalla_tiles.tar")
    out.mkdir(parents=True, exist_ok=True)
    cfg_path = out / "valhalla.json"
    C.save_json(cfg_path, cfg)
    exe = C.valhalla_exe("valhalla_build_tiles")
    res = C.run_measured([exe, "-c", cfg_path, "-j", str(C.THREADS), "-e", "validate", C.MERGED_PBF],
                         C.LOGS / "build_tiles_disk.log", env=C.valhalla_env())
    if res["returncode"] != 0:
        sys.exit(f"build failed ({res['returncode']}), see {res['log']}")
    tdir = out / "valhalla_tiles"
    temp = sum(p.stat().st_size for p in tdir.iterdir() if p.is_file())
    res["before_cleanup_bytes"] = C.dir_size(tdir)
    res["temp_files_bytes"] = temp
    res["temp_files"] = {p.name: p.stat().st_size for p in tdir.iterdir() if p.is_file()}
    clean = C.run_measured([exe, "-c", cfg_path, "-s", "cleanup", "-e", "cleanup", C.MERGED_PBF],
                           C.LOGS / "build_tiles_disk_cleanup.log", env=C.valhalla_env())
    res["after_cleanup_bytes"] = C.dir_size(tdir)
    res["cleanup_returncode"] = clean["returncode"]
    C.save_json(out / "stamp.json", res)
    C.record_measurement("tiles-disk", res)
    print(json.dumps(res, indent=2))


# ---------------------------------------------------------------- packs
def poly_to_geojson(poly: Path) -> dict:
    """Osmosis .poly -> GeoJSON MultiPolygon (holes, marked with '!', are kept as holes)."""
    lines = [ln.strip() for ln in poly.read_text().splitlines()]
    polys, i = [], 1  # line 0 is the file name
    while i < len(lines) and lines[i] != "END":
        hole = lines[i].startswith("!")
        i += 1
        ring = []
        while lines[i] != "END":
            x, y = lines[i].split()[:2]
            ring.append([float(x), float(y)])
            i += 1
        i += 1
        if ring[0] != ring[-1]:
            ring.append(ring[0])
        if hole and polys:
            polys[-1].append(ring)
        else:
            polys.append([ring])
    return {"type": "FeatureCollection",
            "features": [{"type": "Feature", "properties": {"name": poly.stem},
                          "geometry": {"type": "MultiPolygon", "coordinates": polys}}]}


def tar_manifest(tar_path: Path) -> list[dict]:
    """Per-tile list (path, level, bytes, sha256, Valhalla build id) for a tile tar."""
    import hashlib

    from valhalla.baldr.utils import GraphUtils  # noqa: F401  (import check only)
    from valhalla._valhalla import GraphTileHeader

    tiles = []
    with tarfile.open(tar_path) as tar:
        for m in tar.getmembers():
            if not m.name.endswith(".gph"):
                continue
            data = tar.extractfile(m).read()
            hdr = GraphTileHeader.from_bytes(data[:GraphTileHeader.byte_size()])
            tiles.append({"path": m.name, "level": int(m.name.split("/")[0]), "bytes": m.size,
                          "sha256": hashlib.sha256(data).hexdigest(), "build_id": hdr.build_id,
                          "dataset_id": hdr.dataset_id, "version": hdr.version})
    return tiles


def make_pack(state: str, polygon_geojson_dir: Path) -> dict:
    out = C.PACKS / state
    out.mkdir(parents=True, exist_ok=True)
    tar_path = out / f"routing-{state}-{C.SNAPSHOT}.tar"
    inline = json.dumps({"mjolnir": {"tile_dir": str(C.TILE_DIR), "tile_extract": str(tar_path)}})
    res = C.run_measured([sys.executable, "-m", "valhalla.valhalla_build_extract", "-i", inline,
                          "-g", polygon_geojson_dir, "--overwrite", "-v"], C.LOGS / f"extract_{state}.log")
    if res["returncode"] != 0:
        sys.exit(f"valhalla_build_extract failed for {state}, see {res['log']}")
    tiles = tar_manifest(tar_path)
    build_ids = sorted({t["build_id"] for t in tiles})
    manifest = {
        "format": "beamline-routing-pack/1",
        "state": state,
        "snapshot": C.SNAPSHOT,
        # all packs cut from one combined build share this id; install refuses to mix ids
        "graph_build": f"{C.SNAPSHOT}-{C.GRAPH_NAME}-v{tiles[0]['version']}-b{build_ids[0]}",
        "valhalla_version": tiles[0]["version"],
        "valhalla_build_ids": build_ids,
        "tar": tar_path.name,
        "tar_bytes": tar_path.stat().st_size,
        "tar_sha256": C.file_hash(tar_path),
        "tiles": [{k: t[k] for k in ("path", "level", "bytes", "sha256")} for t in tiles],
    }
    C.save_json(out / "manifest.json", manifest)
    by_level = {}
    for t in tiles:
        lv = by_level.setdefault(str(t["level"]), {"tiles": 0, "bytes": 0})
        lv["tiles"] += 1
        lv["bytes"] += t["bytes"]
    res.update(tar_bytes=manifest["tar_bytes"], tiles=len(tiles), levels=by_level, graph_build=manifest["graph_build"])
    return res


def step_packs(force: bool) -> None:
    sig = signature([STAMPS / "tiles.json"] + [C.OSM_IN / f"{name}.poly" for name in C.STATES.values()])
    if done("packs", sig) and all((C.PACKS / s / "manifest.json").exists() for s in C.STATES) and not force:
        print("[packs] exist, skipping")
        return
    info = {}
    for state, name in C.STATES.items():
        gj_dir = C.PACKS / "_polygons" / state
        gj_dir.mkdir(parents=True, exist_ok=True)
        C.save_json(gj_dir / f"{state}.geojson", poly_to_geojson(C.OSM_IN / f"{name}.poly"))
        info[state] = make_pack(state, gj_dir)
    if {"pa", "oh"} <= set(C.STATES):
        # shared tiles: same path in both packs; must be byte-identical (same build)
        man = {s: C.load_json(C.PACKS / s / "manifest.json") for s in ("pa", "oh")}
        sets = {s: {t["path"]: t["sha256"] for t in m["tiles"]} for s, m in man.items()}
        shared = sorted(set(sets["pa"]) & set(sets["oh"]))
        info["shared_tiles"] = len(shared)
        info["shared_identical"] = all(sets["pa"][p] == sets["oh"][p] for p in shared)
        info["shared_bytes"] = sum(t["bytes"] for t in man["pa"]["tiles"] if t["path"] in set(shared))
    stamp("packs", info, sig)


STEPS = {
    "inputs": step_inputs,
    "timezones": step_timezones,
    "config": step_config,
    "boundaries": step_boundaries,
    "admins": step_admins,
    "tiles": step_tiles,
    "packs": step_packs,
}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("steps", nargs="+", choices=list(STEPS) + ["all", "tiles-unmerged", "tiles-allmodes", "tiles-disk"])
    ap.add_argument("--force", action="store_true", help="redo the named steps even if done")
    a = ap.parse_args()
    steps = list(STEPS) if "all" in a.steps else a.steps
    for s in steps:
        t0 = time.time()
        if s == "tiles-unmerged":
            step_tiles_unmerged(a.force)
        elif s == "tiles-allmodes":
            step_tiles_allmodes(a.force)
        elif s == "tiles-disk":
            step_tiles_disk(a.force)
        else:
            STEPS[s](a.force)
        print(f"[{s}] finished in {time.time() - t0:.1f}s", flush=True)


if __name__ == "__main__":
    main()
