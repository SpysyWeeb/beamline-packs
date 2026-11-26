"""Assemble a state pack, format 3 (docs/pack-format.md, docs/architecture.md 2.11), from the builders'
outputs: the map (packbuilder/map), the routing tiles and their index (packbuilder/routing), the
search index (packbuilder/search/build.py) and the house numbers (packbuilder/addresses, slim).

Standard library only. Run from the repository root:

    python packbuilder/pack/assemble.py pa               # the round-3 inputs of STATES below
    python packbuilder/pack/assemble.py oh --out data/r3-packs
    python packbuilder/pack/assemble.py pa --map M.pmtiles --routing DIR --search S.sqlite \\
        --addresses A.sqlite --out DIR [--no-addresses] [--no-archive] [--no-folder] [--created-utc T]

Writes into --out (default data/r3-packs):
  us-<state>/                     the pack folder: manifest.json and the part files (copies)
  us-<state>-<osm date>.tar       the archive: uncompressed ustar, manifest.json first, then the parts
                                  from the smallest to the largest (routing.json first, routing.tar last)

Refuses (nothing written): parts of different OpenStreetMap snapshots; a search and an address file
built with different street-key fingerprints (or without one); a part whose recorded size or SHA-256
(the map's .map.json, the routing index's tar_sha256) differs from the file; a file that is not what
its part says (PMTiles, tar, SQLite, JSON); a search or address file of another state.

The manifest's pack_version is the build time YYYYMMDDhhmm (UTC) of created_utc: a higher number is a
newer build of the same pack_id (docs/architecture-downloads.md 6.6).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import sys
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
OSM_CREDIT = "© OpenStreetMap contributors"
MANIFEST_VERSION = 3

# Round-3 inputs (one OSM snapshot, 2026-11-14; graph build 261114-pa-oh), relative to the repo root.
STATES = {
    "pa": {"name": "Pennsylvania", "usps": "PA",
           "map": "data/map/packs-v2/pa-261114-z14.pmtiles",
           "routing": "data/routing/packs/pa",
           "search": "data/r3-packs/pa-search.sqlite",
           "addresses": "data/r3-p/addresses-pa/pa-addresses-slim.sqlite"},
    "oh": {"name": "Ohio", "usps": "OH",
           "map": "data/map/packs-v2/oh-261114-z14.pmtiles",
           "routing": "data/routing/packs/oh",
           "search": "data/r3-packs/oh-search.sqlite",
           "addresses": "data/r3-p/addresses-oh/oh-addresses-slim.sqlite"},
    "ca": {"name": "California", "usps": "CA",
           "map": "data/map/packs-v2/ca-261114-z14.pmtiles",
           "routing": "data/routing/packs/ca",
           "search": "data/r3-packs/ca-search.sqlite",
           "addresses": "data/r3-p/addresses-ca/ca-addresses-slim.sqlite"},
}

# Part file names in the pack (docs/architecture.md 2.11).
FILES = {"map": "map.pmtiles", "routing": "routing.tar", "routing_index": "routing.json",
         "search": "search.sqlite", "addresses": "addresses.sqlite"}


class Refused(Exception):
    pass


def rel(p: Path) -> str:
    try:
        return Path(p).resolve().relative_to(REPO).as_posix()
    except ValueError:
        return Path(p).name


def sha256_file(p: Path) -> tuple[int, str]:
    h, n = hashlib.sha256(), 0
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
            n += len(chunk)
    return n, h.hexdigest()


def head(p: Path, n: int) -> bytes:
    with open(p, "rb") as f:
        return f.read(n)


def snapshot_of(text: str) -> str | None:
    """'2026-11-14', '261114' (as in pennsylvania-261114.osm.pbf) or a timestamp -> 'YYYY-MM-DD'."""
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", text or "")
    if m:
        return "-".join(m.groups())
    m = re.search(r"(?<!\d)(\d{2})(\d{2})(\d{2})(?!\d)", text or "")
    return f"20{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


def sqlite_meta(p: Path) -> dict:
    if head(p, 16) != b"SQLite format 3\x00":
        raise Refused(f"{rel(p)} is not a SQLite file")
    con = sqlite3.connect(f"file:{p.resolve().as_posix()}?mode=ro", uri=True)
    try:
        return dict(con.execute("SELECT key, value FROM meta").fetchall())
    finally:
        con.close()


def map_part(path: Path, info_path: Path, usps: str) -> tuple[dict, str]:
    if head(path, 8) != b"PMTiles\x03":
        raise Refused(f"{rel(path)} is not a PMTiles v3 file")
    info = json.loads(info_path.read_text(encoding="utf-8"))
    ext = info.get("schema_extensions", {})
    clip = info.get("clip", {})
    if clip.get("state") not in (None, usps):
        raise Refused(f"{rel(path)} was clipped to {clip.get('state')}, not {usps}")
    part = {
        "file": FILES["map"], "format": "pmtiles", "format_version": 3,
        "schema": info["schema"], "schema_version": info["schema_version"],
        "min_zoom": info["min_zoom"], "max_zoom": info["max_zoom"],
        "tile_type": info.get("tile_type", "mvt"),
        "schema_extensions": {"lanes_minzoom": ext.get("lanes_minzoom"), "top_zoom": ext.get("top_zoom", info["max_zoom"])},
        "clip": {"file": Path(clip.get("file", "")).name, "sha256": clip.get("sha256"), "state": clip.get("state", usps)},
        "_expect": (info.get("bytes"), info.get("sha256")),
        "attribution": OSM_CREDIT,
    }
    return part, snapshot_of(info.get("osm_snapshot", "")), info["bounds"]


def routing_parts(folder: Path, usps: str) -> tuple[dict, dict, str, Path, Path]:
    index = folder / "manifest.json"
    m = json.loads(index.read_text(encoding="utf-8"))
    if m.get("format") != "beamline-routing-pack/1":
        raise Refused(f"{rel(index)} is not a beamline-routing-pack/1 manifest")
    if m.get("state", "").upper() != usps:
        raise Refused(f"{rel(index)} is the routing of {m.get('state')}, not {usps}")
    tar = folder / m["tar"]
    if head(tar, 262)[257:262] != b"ustar":
        raise Refused(f"{rel(tar)} is not a tar file")
    routing = {"file": FILES["routing"], "format": "valhalla-tar", "format_version": 1,
               "valhalla_version": m["valhalla_version"], "graph_build": m["graph_build"],
               "_expect": (m.get("tar_bytes"), m.get("tar_sha256")), "attribution": OSM_CREDIT}
    index_part = {"file": FILES["routing_index"], "format": "beamline-routing-pack", "format_version": 1,
                  "attribution": OSM_CREDIT}
    return routing, index_part, snapshot_of(m.get("snapshot", "") or m.get("graph_build", "")), tar, index


def search_part(path: Path, usps: str) -> tuple[dict, str]:
    meta = sqlite_meta(path)
    if meta.get("schema_version") != "1":
        raise Refused(f"{rel(path)}: search schema_version {meta.get('schema_version')}, this tool writes format 1")
    if meta.get("state") != usps:
        raise Refused(f"{rel(path)} is the search index of {meta.get('state')}, not {usps}")
    for k in ("addr_key_fingerprint", "ranking_sha256", "addr_key_table_version"):
        if not meta.get(k):
            raise Refused(f"{rel(path)} has no meta {k}: rebuild it with packbuilder/search/build.py")
    part = {"file": FILES["search"], "format": "beamline-search-sqlite", "format_version": 1,
            "addr_key_table_version": int(meta["addr_key_table_version"]),
            "addr_key_fingerprint": meta["addr_key_fingerprint"], "ranking_sha256": meta["ranking_sha256"],
            "attribution": OSM_CREDIT}
    return part, snapshot_of(meta.get("osm_source", "")) or snapshot_of(meta.get("osm_timestamp", ""))


def addresses_part(path: Path, usps: str) -> tuple[dict, str]:
    meta = sqlite_meta(path)
    if meta.get("schema_version") != "1":
        raise Refused(f"{rel(path)}: addresses schema_version {meta.get('schema_version')}, this tool writes format 1")
    if meta.get("state") != usps:
        raise Refused(f"{rel(path)} holds the addresses of {meta.get('state')}, not {usps}")
    if not meta.get("key_fingerprint"):
        raise Refused(f"{rel(path)} has no meta key_fingerprint: rebuild it with packbuilder/addresses (step_build.py)")
    if not str(meta.get("variant", "")).startswith("slim"):
        raise Refused(f"{rel(path)} is the {meta.get('variant')} variant; a pack ships the slim file")
    credit = meta.get("credit") or meta.get("attribution", "").rstrip("; ")
    part = {"file": FILES["addresses"], "format": "beamline-addresses-sqlite", "format_version": 1,
            "normalisation_table_version": int(meta["normalisation_table_version"]),
            "key_fingerprint": meta["key_fingerprint"], "variant": "slim", "attribution": credit}
    return part, snapshot_of(meta.get("osm_extract", ""))


def assemble(state: str, src: dict, out: Path, with_addresses: bool, archive: bool, folder: bool,
             created: datetime) -> dict:
    cfg = STATES[state]
    usps = cfg["usps"]
    pack_id = f"us-{state}"
    t0 = time.time()
    map_path = Path(src["map"])
    mp, map_snap, bounds = map_part(map_path, map_path.with_suffix(".map.json"), usps)
    routing, index_part, route_snap, tar, index = routing_parts(Path(src["routing"]), usps)
    search, search_snap = search_part(Path(src["search"]), usps)
    files = {"map": map_path, "routing": tar, "routing_index": index, "search": Path(src["search"])}
    parts = {"map": mp, "routing": routing, "routing_index": index_part, "search": search}
    snaps = {"map": map_snap, "routing": route_snap, "search": search_snap}
    if with_addresses:
        addr, addr_snap = addresses_part(Path(src["addresses"]), usps)
        parts["addresses"] = addr
        files["addresses"] = Path(src["addresses"])
        snaps["addresses"] = addr_snap
        if addr["key_fingerprint"] != search["addr_key_fingerprint"]:
            raise Refused(f"the search index (fingerprint {search['addr_key_fingerprint'][:12]}) and the address file "
                          f"(fingerprint {addr['key_fingerprint'][:12]}) were built with different street keys")
    if None in snaps.values() or len(set(snaps.values())) != 1:
        raise Refused(f"the parts are not from one OpenStreetMap snapshot: {snaps}")
    snapshot = map_snap

    for name, part in parts.items():
        n, sha = sha256_file(files[name])
        exp_n, exp_sha = part.pop("_expect", (None, None))
        if (exp_n is not None and exp_n != n) or (exp_sha is not None and exp_sha != sha):
            raise Refused(f"{rel(files[name])}: {n} bytes, sha256 {sha[:12]}; its record says {exp_n} bytes, {str(exp_sha)[:12]}")
        part["bytes"] = n
        part["sha256"] = sha
        print(f"  {name:13} {rel(files[name])}: {n} bytes, sha256 {sha[:12]}", flush=True)

    created_utc = created.strftime("%Y-%m-%dT%H:%M:%SZ")
    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "pack_id": pack_id,
        "pack_version": int(created.strftime("%Y%m%d%H%M")),
        "name": cfg["name"],
        "country": "US",
        "state": usps,
        "osm_snapshot": snapshot,
        "created_utc": created_utc,
        "bounds": [round(float(v), 6) for v in bounds],
        # the credit of the required parts (the catalog's "credit"); each part has its own
        "attribution": OSM_CREDIT,
        "parts": parts,
    }
    text = (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    out.mkdir(parents=True, exist_ok=True)
    result = {"pack_id": pack_id, "pack_version": manifest["pack_version"], "osm_snapshot": snapshot,
              "parts": {k: {"bytes": v["bytes"], "sha256": v["sha256"]} for k, v in parts.items()}}
    # the archive and the folder hold the parts from the smallest to the largest, after the manifest
    order = sorted(parts, key=lambda k: parts[k]["bytes"])
    if folder:
        final = out / pack_id
        tmp = out / f".{pack_id}.tmp"
        if tmp.exists():
            shutil.rmtree(tmp)
        tmp.mkdir()
        (tmp / "manifest.json").write_bytes(text)
        for name in order:
            shutil.copyfile(files[name], tmp / parts[name]["file"])
        if final.exists():
            shutil.rmtree(final)
        tmp.rename(final)
        result["folder"] = rel(final)
        print(f"  folder  {rel(final)}", flush=True)
    if archive:
        final = out / f"{pack_id}-{snapshot}.tar"
        tmp = out / f".{final.name}.tmp"
        mtime = int(created.timestamp())
        with tarfile.open(tmp, "w", format=tarfile.USTAR_FORMAT) as t:
            def add(name: str, size: int, fileobj):
                ti = tarfile.TarInfo(name)
                ti.size, ti.mtime, ti.mode, ti.uid, ti.gid, ti.uname, ti.gname = size, mtime, 0o644, 0, 0, "", ""
                t.addfile(ti, fileobj)
            import io
            add("manifest.json", len(text), io.BytesIO(text))
            for name in order:
                with open(files[name], "rb") as f:
                    add(parts[name]["file"], parts[name]["bytes"], f)
        tmp.replace(final)
        result["archive"] = rel(final)
        result["archive_bytes"] = final.stat().st_size
        print(f"  archive {rel(final)}: {final.stat().st_size} bytes", flush=True)
    result["seconds"] = round(time.time() - t0, 1)
    return result


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("state", choices=sorted(STATES))
    for k in ("map", "routing", "search", "addresses"):
        ap.add_argument(f"--{k}", help=f"default: the round-3 input of STATES")
    ap.add_argument("--out", default="data/r3-packs")
    ap.add_argument("--no-addresses", action="store_true", help="a pack without house numbers")
    ap.add_argument("--no-archive", action="store_true")
    ap.add_argument("--no-folder", action="store_true")
    ap.add_argument("--created-utc", help="the build time (default now), e.g. 2026-11-16T09:00:00Z")
    a = ap.parse_args(argv)
    src = {k: str(REPO / (getattr(a, k) or STATES[a.state][k])) if not Path(getattr(a, k) or "").is_absolute()
           else getattr(a, k) for k in ("map", "routing", "search", "addresses")}
    created = (datetime.strptime(a.created_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
               if a.created_utc else datetime.now(timezone.utc).replace(second=0, microsecond=0))
    out = Path(a.out) if Path(a.out).is_absolute() else REPO / a.out
    print(f"assemble us-{a.state} (format {MANIFEST_VERSION}) -> {rel(out)}", flush=True)
    try:
        r = assemble(a.state, src, out, not a.no_addresses, not a.no_archive, not a.no_folder, created)
    except Refused as e:
        print(f"refused: {e}", file=sys.stderr)
        return 1
    print(json.dumps(r, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
