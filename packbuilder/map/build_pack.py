"""Build the PMTiles vector map of one state with Planetiler + the Protomaps profile (Beamline patch).

Usage (from the repo root):
    .venv/map/Scripts/python packbuilder/map/build_pack.py pa                    # recommended variant (build_config.json)
    .venv/map/Scripts/python packbuilder/map/build_pack.py pa --variant z15      # any variant in build_config.json
    .venv/map/Scripts/python packbuilder/map/build_pack.py oh
    options: --force (rebuild even if the output exists), --bounds W,S,E,N --tag NAME (sample area),
             --minzoom N, --wait-minutes N (wait for an OSM extract that is still downloading)

Two stages:
  1. Planetiler writes data/map/work/raw-v2/<name>.pmtiles (+ .build.json with its
     command, wall time, peak RAM and input hashes; its log goes to
     data/map/logs/<name>.log). Skipped when that raw pack and record exist.
     The pack is clipped to the state's US Census TIGER/Line outline
     (census_clip.py), not to Geofabrik's buffered .poly.
  2. finish_pack.py writes the final data/map/packs-v2/<name>.pmtiles: header
     bounds from the clip polygon, unsupported characters removed from label
     text (MVT only). The combined record is data/map/packs-v2/<name>.build.json,
     and <name>.map.json describes the map part for the app's pack manifest.
  <name> is <state>-<snapshot>-<variant>[-<tag>].

Variants (build_config.json "variants") set the max zoom, the clamp of
z15-only features into a lower max zoom, the POI kinds to leave out, building
detail at the max zoom and the tile format (mvt or mlt). Every variant carries
lane tags from build_config.json "lanes.minzoom".

Prerequisites (each step skips itself when already done):
    pwsh tools/fetch_osm_extracts.ps1 -States pennsylvania,ohio -Date 261114   # data/osm/ (shared inputs, not this track's script)
    python tools/fetch_map_tools.py
    .venv/map/Scripts/python packbuilder/map/build_profile.py
    .venv/map/Scripts/python packbuilder/map/fetch_sources.py
"""
import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_profile import jar_path  # noqa: E402
from census_clip import write_geojson  # noqa: E402
from finish_pack import finish  # noqa: E402
from measure import run_measured  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
CONFIG = json.loads((HERE / "build_config.json").read_text())
VERSIONS = json.loads((HERE / "versions.json").read_text())
MAP = REPO / "data" / "map"
WORK = MAP / "work"
SOURCES = WORK / "data" / "sources"
OUT_DIR = REPO / CONFIG["output_dir"]
RAW_DIR = REPO / CONFIG["raw_dir"]
# The weekly build (the packs repository) overrides from the environment: BEAMLINE_SNAPSHOT (YYMMDD of
# the extracts: the pack names and the state's data/osm/<name>-<snapshot>.osm.pbf), BEAMLINE_THREADS
# and BEAMLINE_JAVA_HEAP (e.g. 3g). Unset, build_config.json's values hold.
_SNAP = os.environ.get("BEAMLINE_SNAPSHOT")
if _SNAP:
    if not (len(_SNAP) == 6 and _SNAP.isdigit()):
        raise SystemExit(f"BEAMLINE_SNAPSHOT={_SNAP!r}: expected YYMMDD")
    for _st in CONFIG["states"].values():
        _st["osm"] = _st["osm"].replace(f"-{CONFIG['snapshot']}.", f"-{_SNAP}.")
    CONFIG["snapshot"] = _SNAP
if os.environ.get("BEAMLINE_THREADS"):
    CONFIG["build"]["threads"] = int(os.environ["BEAMLINE_THREADS"])
if os.environ.get("BEAMLINE_JAVA_HEAP"):
    CONFIG["build"]["java_heap"] = os.environ["BEAMLINE_JAVA_HEAP"]


def rel(p: Path) -> str:
    try:
        return p.resolve().relative_to(REPO).as_posix()
    except ValueError:
        return str(p)


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def md5_ok(path: Path, wait_minutes: int = 0) -> str:
    """Compare a shared OSM input with its .md5 file; optionally wait while it is still downloading."""
    md5_file = Path(str(path) + ".md5")
    if wait_minutes <= 0 and not (path.exists() and md5_file.exists()):
        raise SystemExit(f"{rel(path)} or its .md5 is missing. Fetch the shared OSM extracts first "
                         "(pwsh tools/fetch_osm_extracts.ps1 -States pennsylvania,ohio -Date 261114), "
                         "or pass --wait-minutes N while a download runs.")
    cache = WORK / "md5-checked.json"
    checked = json.loads(cache.read_text()) if cache.exists() else {}
    for _ in range(max(1, wait_minutes)):
        if path.exists() and md5_file.exists():
            key = f"{path.name}:{path.stat().st_size}:{path.stat().st_mtime_ns}"
            want = md5_file.read_text().split()[0].lower()
            if checked.get(key) == want:
                return want
            h = hashlib.md5()
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 22), b""):
                    h.update(chunk)
            if h.hexdigest() == want:
                checked[key] = want
                WORK.mkdir(parents=True, exist_ok=True)
                cache.write_text(json.dumps(checked, indent=1))
                return want
            if wait_minutes <= 0:
                break
            print(f"{path.name}: MD5 does not match yet, waiting a minute", flush=True)
        else:
            print(f"{rel(path)} or its .md5 missing, waiting a minute", flush=True)
        if wait_minutes > 0:
            time.sleep(60)
    raise SystemExit(f"{rel(path)} does not match its .md5")


def empty_geopackage(path: Path) -> Path:
    """A valid GeoPackage with one empty polygon table: the profile reads zero landcover features."""
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript("""
    PRAGMA application_id = 1196444487;  -- 'GPKG'
    PRAGMA user_version = 10400;
    CREATE TABLE gpkg_spatial_ref_sys (srs_name TEXT NOT NULL, srs_id INTEGER PRIMARY KEY,
      organization TEXT NOT NULL, organization_coordsys_id INTEGER NOT NULL,
      definition TEXT NOT NULL, description TEXT);
    INSERT INTO gpkg_spatial_ref_sys VALUES
      ('Undefined cartesian SRS', -1, 'NONE', -1, 'undefined', NULL),
      ('Undefined geographic SRS', 0, 'NONE', 0, 'undefined', NULL),
      ('WGS 84 geodetic', 4326, 'EPSG', 4326, 'GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563,AUTHORITY["EPSG","7030"]],AUTHORITY["EPSG","6326"]],PRIMEM["Greenwich",0,AUTHORITY["EPSG","8901"]],UNIT["degree",0.0174532925199433,AUTHORITY["EPSG","9122"]],AUTHORITY["EPSG","4326"]]', NULL);
    CREATE TABLE gpkg_contents (table_name TEXT NOT NULL PRIMARY KEY, data_type TEXT NOT NULL,
      identifier TEXT UNIQUE, description TEXT DEFAULT '',
      last_change DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
      min_x DOUBLE, min_y DOUBLE, max_x DOUBLE, max_y DOUBLE, srs_id INTEGER);
    CREATE TABLE gpkg_geometry_columns (table_name TEXT NOT NULL, column_name TEXT NOT NULL,
      geometry_type_name TEXT NOT NULL, srs_id INTEGER NOT NULL, z TINYINT NOT NULL, m TINYINT NOT NULL,
      PRIMARY KEY (table_name, column_name));
    CREATE TABLE landcover (fid INTEGER PRIMARY KEY AUTOINCREMENT, geom MULTIPOLYGON, class TEXT);
    INSERT INTO gpkg_contents (table_name, data_type, identifier, min_x, min_y, max_x, max_y, srs_id)
      VALUES ('landcover', 'features', 'landcover', -180, -90, 180, 90, 4326);
    INSERT INTO gpkg_geometry_columns VALUES ('landcover', 'geom', 'MULTIPOLYGON', 4326, 0, 0);
    """)
    con.commit()
    con.close()
    return path


def beamline_args(variant: dict) -> list:
    """Planetiler arguments for the Beamline profile patch and the tile format."""
    lanes = CONFIG["lanes"]["minzoom"] if variant.get("lanes", True) else -1
    args = [f"--beamline_lanes_minzoom={lanes}",
            f"--beamline_top_zoom_clamp={str(variant['clamp']).lower()}",
            f"--beamline_buildings_detail_at_top={str(variant['buildings_detail_at_top']).lower()}"]
    if variant["poi_drop"]:
        args.append("--beamline_poi_drop_kinds=" + ",".join(CONFIG["poi_drop_kinds"]))
    if variant["format"] == "mlt":
        args.append("--tile_format=mlt")
        args += variant.get("mlt_flags", [])
    elif variant["format"] != "mvt":
        raise SystemExit(f"unknown format {variant['format']}")
    return args


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("state", choices=sorted(CONFIG["states"]))
    ap.add_argument("--variant", default=CONFIG["recommended_variant"],
                    choices=sorted(k for k in CONFIG["variants"] if not k.startswith("_")))
    ap.add_argument("--minzoom", type=int, default=0)
    ap.add_argument("--bounds", help="W,S,E,N: build only this sample area")
    ap.add_argument("--tag", default="", help="extra name suffix, e.g. 'erie' for a sample area")
    ap.add_argument("--force", action="store_true", help="run Planetiler and the finish step again")
    ap.add_argument("--map-json-only", action="store_true", help="only (re)write <name>.map.json of a finished pack")
    ap.add_argument("--wait-minutes", type=int, default=0,
                    help="wait up to N minutes for the OSM extract to finish downloading (default: fail at once)")
    a = ap.parse_args()

    st = CONFIG["states"][a.state]
    b = CONFIG["build"]
    variant = CONFIG["variants"][a.variant]
    name = f"{a.state}-{CONFIG['snapshot']}-{a.variant}" + (f"-min{a.minzoom}" if a.minzoom else "") \
        + (f"-{a.tag}" if a.tag else "")
    out = OUT_DIR / f"{name}.pmtiles"
    record = out.with_suffix(".build.json")
    raw = RAW_DIR / f"{name}.pmtiles"
    raw_record = raw.with_suffix(".build.json")
    clip = WORK / "clip-v2" / f"{a.state}.geojson"
    if a.map_json_only:
        rec = json.loads(record.read_text(encoding="utf-8"))
        mp = map_part(out, rec["planetiler"], rec["finish"])
        out.with_suffix(".map.json").write_text(json.dumps(mp, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"ok {rel(out.with_suffix('.map.json'))}")
        return 0
    if out.exists() and record.exists() and not a.force:
        print(f"skip: {rel(out)} exists (use --force to rebuild)")
        return 0
    if raw.exists() and raw_record.exists() and not a.force:
        print(f"skip Planetiler: {rel(raw)} exists; running the finish step only")
        return finish_and_record(raw, raw_record, out, record, clip, a.variant)

    jar = jar_path()
    if not jar.exists():
        raise SystemExit("profile jar missing: run packbuilder/map/build_profile.py")
    manifest = json.loads((SOURCES / "manifest.json").read_text())
    for need in ("natural_earth_vector.gpkg.zip", "water-polygons-split-3857.zip",
                 "land-polygons-split-3857.zip", "qrank.csv.gz", "pgf-encoding.zip"):
        if need not in manifest or not (SOURCES / need).exists():
            raise SystemExit(f"{need} missing: run packbuilder/map/fetch_sources.py")
    clip_src = REPO / CONFIG["clip"]["file"]
    if not clip_src.exists():
        raise SystemExit(f"{rel(clip_src)} missing: run packbuilder/map/fetch_sources.py")

    osm = REPO / st["osm"]
    osm_md5 = md5_ok(osm, a.wait_minutes)
    write_geojson(st["usps"], clip)

    if b["landcover"]:
        landcover = SOURCES / "daylight-landcover.gpkg"
        if not landcover.exists():
            raise SystemExit("landcover enabled but daylight-landcover.gpkg missing")
    else:
        landcover = empty_geopackage(WORK / "empty-landcover.gpkg")

    tmp = WORK / "tmp" / name
    raw.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["java", f"-Xmx{b['java_heap']}", "-jar", jar,
           f"--area={a.state}",
           f"--osm-path={osm}",
           f"--output={raw}",
           f"--minzoom={a.minzoom}", f"--maxzoom={variant['maxzoom']}",
           f"--threads={b['threads']}",
           f"--clip={clip}", f"--clip-buffer={b['clip_buffer']}",
           f"--ne-path={SOURCES / 'natural_earth_vector.gpkg.zip'}",
           f"--osm-water-path={SOURCES / 'water-polygons-split-3857.zip'}",
           f"--osm-land-path={SOURCES / 'land-polygons-split-3857.zip'}",
           f"--landcover-path={landcover}",
           f"--tmpdir={tmp}",
           "--force"] + beamline_args(variant)
    if a.bounds:
        cmd.append(f"--bounds={a.bounds}")
    print("building", name, flush=True)
    res = run_measured(cmd, MAP / "logs" / f"{name}.log", cwd=WORK)
    res["command"] = [c.replace(str(REPO) + "\\", "").replace(str(REPO) + "/", "").replace("\\", "/")
                      for c in res["command"]]
    res.update({
        "pack": rel(raw),
        "state": a.state,
        "variant": a.variant,
        "variant_settings": variant,
        "minzoom": a.minzoom,
        "maxzoom": variant["maxzoom"],
        "bounds": a.bounds,
        "osm": {"file": st["osm"], "md5": osm_md5},
        "clip": {"file": CONFIG["clip"]["file"], "sha256": sha256_of(clip_src), "state": st["usps"]},
        "sources": {k: {kk: v[kk] for kk in ("sha256", "bytes", "last_modified", "url")}
                    for k, v in manifest.items()},
        "landcover": b["landcover"],
        "versions": VERSIONS,
        "profile_jar": {"file": rel(jar), "sha256": sha256_of(jar)},
        "note": "other engineers' heavy jobs (Gradle, an Android emulator, other pack builds) were running on the same machine",
    })
    if res["returncode"] == 0 and raw.exists():
        res["output_bytes"] = raw.stat().st_size
        raw_record.write_text(json.dumps(res, indent=2))
        print(f"Planetiler ok {rel(raw)}: {res['output_bytes'] / 1e6:.1f} MB, {res['wall_seconds']} s, "
              f"peak {res['peak_rss_gib']} GiB")
        return finish_and_record(raw, raw_record, out, record, clip, a.variant)
    print(json.dumps(res, indent=2))
    print(f"FAILED, see {rel(MAP / 'logs' / (name + '.log'))}")
    return 1


def map_part(out: Path, planetiler: dict, fin: dict) -> dict:
    """What the map part of an app pack manifest needs, read back from the finished file."""
    from pmtiles.reader import MmapSource, Reader
    with open(out, "rb") as f:
        r = Reader(MmapSource(f))
        h, meta = r.header(), r.metadata()
    tt, tc = getattr(h["tile_type"], "value", h["tile_type"]), getattr(h["tile_compression"], "value", h["tile_compression"])
    tile_type = {1: "mvt", 6: "mlt"}.get(int(tt), str(tt))
    return {
        "file": out.name,
        "format": "pmtiles",
        "format_version": 3,
        "tile_type": tile_type,
        "tile_compression": {1: "none", 2: "gzip"}.get(int(tc), str(tc)),
        "schema": "protomaps-basemap",
        "schema_version": str(meta.get("version", "")),
        "schema_extensions": {
            "beamline_profile_patch": meta.get("beamline:profile_extensions"),
            "lanes_minzoom": int(meta.get("beamline:lanes_minzoom", -1)),
            "top_zoom": int(meta.get("beamline:top_zoom", 15)),
            "poi_drop_kinds": [k for k in str(meta.get("beamline:poi_drop_kinds", "")).split(",") if k],
        },
        "source_name": "protomaps",
        "min_zoom": h["min_zoom"],
        "max_zoom": h["max_zoom"],
        "bounds": [round(h[k] / 1e7, 6) for k in ("min_lon_e7", "min_lat_e7", "max_lon_e7", "max_lat_e7")],
        "clip": planetiler.get("clip"),
        "osm_snapshot": str(meta.get("planetiler:osm:osmosisreplicationtime", ""))[:10],
        "layers": [l["id"] for l in meta.get("vector_layers", [])],
        "bytes": out.stat().st_size,
        "sha256": sha256_of(out),
        "attribution": "© OpenStreetMap contributors",
        "finish": {k: fin.get(k) for k in ("tiles_changed", "strings_changed", "text_cleanup")},
    }


def finish_and_record(raw: Path, raw_record: Path, out: Path, record: Path, clip: Path, variant: str) -> int:
    """Stage 2: finish_pack.py on the raw pack; write the combined build record and the map-part description."""
    import psutil
    planetiler = json.loads(raw_record.read_text())
    out.parent.mkdir(parents=True, exist_ok=True)
    # a --bounds sample build keeps Planetiler's header bounds (they are the sample box)
    fin = finish(raw, out, None if planetiler.get("bounds") else clip, force=True)
    proc = psutil.Process()
    peak = getattr(proc.memory_info(), "peak_wset", None) or proc.memory_info().rss
    fin["peak_memory_gib"] = round(peak / 1024**3, 2)
    fin["peak_method"] = "psutil peak working set of the Python process (Windows)"
    rec = {
        "pack": rel(out),
        "variant": variant,
        "output_bytes": out.stat().st_size,
        "planetiler": planetiler,
        "finish": {k: v for k, v in fin.items() if k != "samples_before"},
        "finish_samples_before": fin.get("samples_before", []),
        "wall_seconds_total": round(planetiler["wall_seconds"] + fin["seconds"], 1),
    }
    record.write_text(json.dumps(rec, indent=2, ensure_ascii=False), encoding="utf-8")
    mp = map_part(out, planetiler, fin)
    out.with_suffix(".map.json").write_text(json.dumps(mp, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"ok {rel(out)}: {rec['output_bytes'] / 1e6:.1f} MB; finish step {fin['seconds']} s, "
          f"{fin['tiles_changed']} tiles changed, bounds {fin.get('header_bounds')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
