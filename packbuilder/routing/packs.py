"""Install / update / uninstall per-state routing packs into the single tile set Valhalla loads.

Valhalla loads exactly one tile set: either one tar extract (mjolnir.tile_extract,
memory-mapped, with an index.bin as first member) or one tile directory
(mjolnir.tile_dir). A device can hold several state packs, so installing a
pack merges its tiles into that one tile set and uninstalling removes them.

Rules (see docs/experiments/01-routing.md):
  * Every pack carries every tile that intersects its state polygon, at every
    hierarchy level, so one pack alone routes anywhere inside its state.
    Tiles on a border are therefore in both neighbouring packs.
  * Packs cut from the same combined graph build carry byte-identical copies of
    shared tiles (checked by sha256 from the manifest). Install keeps one copy.
  * Packs from different graph builds are never mixed: edges in one tile refer
    to node indexes in neighbour tiles, and those indexes are only valid within
    one build. Installing a pack whose graph_build differs from the packs that
    stay installed is refused. A release update installs all packs in one call
    (`install pa oh`), which replaces the installed copies of those states.
  * Installing a state that is already installed is an update: its old tiles
    are replaced by the new pack's tiles, and tiles only the old copy listed
    are removed.
  * A pack must be built for the engine version the device runs (manifest
    valhalla_version and every tile header), otherwise it is refused: Valhalla
    itself does not reject tiles from another version.
  * Every tile written to the device is checked against the sha256 its
    manifest lists, in both layouts.
  * Uninstall deletes only tiles that no other installed pack lists.
  * The layout (tar or dir) is chosen at the first install and stored in
    device.json; later calls use it, and a different layout is refused.

Device layout (default data/routing/device/):
    device.json          layout + engine version of this device
    packs/<state>.json   installed pack manifests (the tile lists)
    routing.tar          layout "tar": the one tar extract the engine loads
    tiles/               layout "dir": the one tile directory the engine loads
    valhalla.json        runtime config; paths are relative to the device folder,
                         engine_config() resolves them

usage:
    python packs.py install pa [oh ...] [--layout tar|dir] [--device DIR]
    python packs.py uninstall oh [--device DIR]
    python packs.py list [--device DIR]
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import io
import json
import os
import shutil
import sys
import tarfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402

INDEX_FILE = "index.bin"
INDEX_ENTRY = 16  # "<QLL": offset u64, tile id u32, size u32
DEVICE_FILE = "device.json"
DEVICE_CONFIG_FILE = C.HERE / "device_config.json"
PRESETS_FILE = C.HERE / "presets.json"
LAYOUTS = ("tar", "dir")


class PackError(RuntimeError):
    pass


def engine_version() -> str:
    """Valhalla version of the engine that will load the tiles (here: the pyvalhalla wheel;
    on the phone: the Valhalla bundled in valhalla-mobile)."""
    import valhalla

    return valhalla.__version__


def installed(device: Path) -> dict[str, dict]:
    d = device / "packs"
    return {p.stem: C.load_json(p) for p in sorted(d.glob("*.json"))} if d.exists() else {}


def device_info(device: Path) -> dict | None:
    """device.json, or for a device installed before device.json existed, the layout found on disk."""
    f = device / DEVICE_FILE
    if f.exists():
        return C.load_json(f)
    if installed(device):
        if (device / "routing.tar").exists():
            return {"layout": "tar"}
        if (device / "tiles").exists():
            return {"layout": "dir"}
    return None


def _resolve_layout(device: Path, requested: str | None) -> str:
    info = device_info(device)
    if info is None or not installed(device):
        return requested or "tar"
    if requested and requested != info["layout"]:
        raise PackError(f"this device uses the {info['layout']} layout; refusing to use {requested} "
                        f"(uninstall every pack first to change layout)")
    return info["layout"]


# ---------------------------------------------------------------- runtime config
def _set_dotted(cfg: dict, key: str, value) -> None:
    node = cfg
    parts = key.split(".")
    for p in parts[:-1]:
        node = node.setdefault(p, {})
    node[parts[-1]] = value


def _get_dotted(cfg: dict, key: str):
    node = cfg
    for p in key.split("."):
        node = node[p]
    return node


def device_settings() -> dict:
    """Dotted config keys the device needs: device_config.json plus the presets' requires_config."""
    settings = dict(C.load_json(DEVICE_CONFIG_FILE)["set"])
    for k, v in C.load_json(PRESETS_FILE).get("requires_config", {}).items():
        if k in settings and settings[k] != v:
            raise PackError(f"device_config.json and presets.json disagree on {k}")
        settings[k] = v
    return settings


def runtime_config(layout: str) -> dict:
    """Runtime config with paths relative to the device folder (portable: no builder paths).

    Starts from the build config (Valhalla's defaults plus our build settings), applies
    device_config.json and the presets' requires_config, and points mjolnir at the one tile set."""
    cfg = copy.deepcopy(C.load_json(C.BUILD_CONFIG))
    for k, v in device_settings().items():
        _set_dotted(cfg, k, v)
    cfg["mjolnir"]["tile_extract"] = "routing.tar" if layout == "tar" else ""
    cfg["mjolnir"]["tile_dir"] = "tiles" if layout == "dir" else ""
    return cfg


def engine_config(device: Path = C.DEVICE) -> dict:
    """The device's valhalla.json with its relative tile paths resolved against the device folder.
    This is what an engine is opened with (the app does the same with its files folder)."""
    cfg = C.load_json(device / "valhalla.json")
    m = cfg["mjolnir"]
    for k in ("tile_extract", "tile_dir"):
        if m.get(k) and not Path(m[k]).is_absolute():
            m[k] = str((device / m[k]).resolve())
    return cfg


def _write_device_files(device: Path, layout: str) -> None:
    C.save_json(device / DEVICE_FILE, {"format": "beamline-routing-device/1", "layout": layout,
                                       "engine_version": engine_version()})
    C.save_json(device / "valhalla.json", runtime_config(layout))


# ---------------------------------------------------------------- checks
def _tile_version(data: bytes) -> str:
    from valhalla._valhalla import GraphTileHeader

    return GraphTileHeader.from_bytes(data[:GraphTileHeader.byte_size()]).version


def _check_tile(name: str, data: bytes, sha: str, version: str | None, origin: str) -> None:
    if hashlib.sha256(data).hexdigest() != sha:
        raise PackError(f"{name} in {origin} does not match its manifest sha256")
    if version is None:
        return
    v = _tile_version(data)
    if v != version:
        raise PackError(f"{name} in {origin} was built by Valhalla {v}, expected {version}")


def _check_compatible(new: dict[str, dict], remaining: dict[str, dict], engine: str) -> None:
    builds = {m["graph_build"] for m in new.values()}
    if len(builds) != 1:
        raise PackError(f"the packs being installed come from different graph builds: {sorted(builds)}")
    build = builds.pop()
    for st, man in new.items():
        if man.get("valhalla_version") != engine:
            raise PackError(f"pack {st} was built for Valhalla {man.get('valhalla_version')} "
                            f"but this engine is Valhalla {engine}")
    for st, man in remaining.items():
        if man["graph_build"] != build:
            names = " ".join(sorted(new))
            raise PackError(
                f"pack {names} is from graph build {build} but installed pack {st} is from "
                f"{man['graph_build']}; update all packs to one build in one call "
                f"(packs.py install {names} {st}) or uninstall {st} first")
    seen: dict[str, tuple[str, str]] = {}
    for st, man in list(remaining.items()) + list(new.items()):
        for t in man["tiles"]:
            if t["path"] in seen and seen[t["path"]][0] != t["sha256"]:
                raise PackError(f"shared tile {t['path']} differs between {seen[t['path']][1]} and {st} "
                                f"although both claim build {build}")
            seen.setdefault(t["path"], (t["sha256"], st))


# ---------------------------------------------------------------- tar layout
def _tar_members(tar_path: Path) -> dict[str, tuple[int, int]]:
    with tarfile.open(tar_path) as tar:
        return {m.name: (m.offset_data, m.size) for m in tar.getmembers() if m.name.endswith(".gph")}


def _write_tar(out: Path, wanted: dict[str, tuple[Path, str, str]], version: str | None) -> None:
    """Write a Valhalla tile tar: index.bin first, then the tiles (sorted), then fill the index.

    wanted: tile path -> (source tar, expected sha256, origin label). Every tile is
    checked against its sha256 (and, unless version is None, the engine version) while
    it is copied."""
    from valhalla.baldr.utils import write_tar_index  # same layout valhalla_build_extract writes

    members = {tp: _tar_members(tp) for tp in {v[0] for v in wanted.values()}}
    tmp = out.with_suffix(".tar.new")
    handles: dict[Path, object] = {}
    try:
        with tarfile.open(tmp, "w", format=tarfile.USTAR_FORMAT) as tar:
            idx = tarfile.TarInfo(INDEX_FILE)
            idx.size = INDEX_ENTRY * len(wanted)
            idx.mtime = int(time.time())
            tar.addfile(idx, io.BytesIO(b"\0" * idx.size))
            for name in sorted(wanted):
                tp, sha, origin = wanted[name]
                if name not in members[tp]:
                    raise PackError(f"{name} is listed in the manifest but missing from {origin}")
                off, size = members[tp][name]
                f = handles.setdefault(tp, open(tp, "rb"))
                f.seek(off)
                data = f.read(size)
                _check_tile(name, data, sha, version, origin)
                ti = tarfile.TarInfo(name)
                ti.size = size
                ti.mtime = int(time.time())
                tar.addfile(ti, io.BytesIO(data))
        write_tar_index(tmp)
        with open(tmp, "r+b") as f:
            os.fsync(f.fileno())
    except BaseException:
        for f in handles.values():
            f.close()
        handles.clear()
        tmp.unlink(missing_ok=True)  # leave the installed tile set untouched
        raise
    for f in handles.values():
        f.close()
    # atomic swap. This works on Windows too while an engine still maps the old file;
    # an engine opened before the swap keeps routing on the old tiles until it is reopened.
    os.replace(tmp, out)


# ---------------------------------------------------------------- dir layout
def _write_dir_tiles(pack_tar: Path, tile_dir: Path, names: set[str], expect: dict[str, str],
                     version: str) -> int:
    n = 0
    with tarfile.open(pack_tar) as tar:
        for m in tar.getmembers():
            if m.name not in names:
                continue
            data = tar.extractfile(m).read()
            _check_tile(m.name, data, expect[m.name], version, pack_tar.name)
            dest = tile_dir / m.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(".part")
            tmp.write_bytes(data)
            os.replace(tmp, dest)
            n += 1
    if n != len(names):
        raise PackError(f"{pack_tar.name} lacks {len(names) - n} tiles that its manifest lists")
    return n


def _remove_empty_dirs(root: Path) -> None:
    if not root.exists():
        return
    for p in sorted((q for q in root.rglob("*") if q.is_dir()), key=lambda q: len(q.parts), reverse=True):
        if not any(p.iterdir()):
            p.rmdir()


# ---------------------------------------------------------------- commands
def install(states: str | list[str], device: Path = C.DEVICE, layout: str | None = None,
            pack_dir: Path | None = None, pack_dirs: dict[str, Path] | None = None,
            engine: str | None = None) -> dict:
    """Install (or update) one or more state packs in one step.

    A state that is already installed is replaced by the new pack. Packs that stay
    installed must come from the same graph build as the new ones."""
    t0 = time.perf_counter()
    states = [states] if isinstance(states, str) else list(states)
    pack_dirs = dict(pack_dirs or {})
    if pack_dir is not None:
        if len(states) != 1:
            raise ValueError("pack_dir is for a single state; use pack_dirs")
        pack_dirs[states[0]] = pack_dir
    engine = engine or engine_version()
    layout = _resolve_layout(device, layout)
    if layout not in LAYOUTS:
        raise PackError(f"unknown layout {layout}")

    new: dict[str, dict] = {}
    tars: dict[str, Path] = {}
    for st in states:
        pdir = pack_dirs.get(st) or (C.PACKS / st)
        man = C.load_json(pdir / "manifest.json")
        if man["state"] != st:
            raise PackError(f"{pdir} holds the {man['state']} pack, not {st}")
        tars[st] = pdir / man["tar"]
        if C.file_hash(tars[st]) != man["tar_sha256"]:
            raise PackError(f"{tars[st].name} does not match its manifest sha256")
        new[st] = man

    current = installed(device)
    remaining = {st: m for st, m in current.items() if st not in new}
    replaced = {st: m for st, m in current.items() if st in new}
    _check_compatible(new, remaining, engine)

    keep = {t["path"]: t["sha256"] for m in remaining.values() for t in m["tiles"]}
    incoming: dict[str, tuple[str, str]] = {}  # path -> (sha, state)
    for st, man in new.items():
        for t in man["tiles"]:
            incoming.setdefault(t["path"], (t["sha256"], st))
    old_present = {t["path"]: t["sha256"] for m in current.values() for t in m["tiles"]}
    final = set(keep) | set(incoming)
    stale = sorted(set(old_present) - final)
    changed = sorted(p for p, (sha, _) in incoming.items() if p in old_present and old_present[p] != sha)
    added = sorted(set(incoming) - set(old_present))

    device.mkdir(parents=True, exist_ok=True)
    (device / "packs").mkdir(parents=True, exist_ok=True)
    if layout == "tar":
        dev_tar = device / "routing.tar"
        wanted: dict[str, tuple[Path, str, str]] = {}
        for p, sha in keep.items():
            wanted[p] = (dev_tar, sha, "the installed routing.tar")
        for p, (sha, st) in incoming.items():  # new pack bytes win over the installed copy
            wanted[p] = (tars[st], sha, tars[st].name)
        _write_tar(dev_tar, wanted, engine)
        written = len(incoming)
    else:
        tile_dir = device / "tiles"
        written = 0
        for st, man in new.items():
            need = {p for p, (sha, s) in incoming.items() if s == st
                    and not (old_present.get(p) == sha and (tile_dir / p).exists())}
            written += _write_dir_tiles(tars[st], tile_dir, need, {t["path"]: t["sha256"] for t in man["tiles"]},
                                        engine)
    for st, man in new.items():
        C.save_json(device / "packs" / f"{st}.json", man)
    if layout == "dir":
        for p in stale:
            (device / "tiles" / p).unlink(missing_ok=True)
        _remove_empty_dirs(device / "tiles")
    _write_device_files(device, layout)
    return {"installed": states, "replaced": sorted(replaced), "tiles_in_packs": len(incoming),
            "tiles_added": len(added), "tiles_changed": len(changed), "tiles_removed_stale": len(stale),
            "tiles_written": written, "tiles_already_present": len(incoming) - len(added) - len(changed),
            "layout": layout, "graph_build": next(iter(new.values()))["graph_build"],
            "wall_s": round(time.perf_counter() - t0, 2), "tileset_bytes": _tileset_bytes(device, layout)}


def uninstall(state: str, device: Path = C.DEVICE, layout: str | None = None) -> dict:
    t0 = time.perf_counter()
    current = installed(device)
    if state not in current:
        raise PackError(f"{state} is not installed")
    layout = _resolve_layout(device, layout)
    keep = {t["path"]: t["sha256"] for st, m in current.items() if st != state for t in m["tiles"]}
    mine = {t["path"] for t in current[state]["tiles"]}
    drop = sorted(mine - set(keep))
    if layout == "tar":
        dev_tar = device / "routing.tar"
        if keep:
            # the kept tiles were version-checked at install; uninstall only re-checks their hashes
            _write_tar(dev_tar, {p: (dev_tar, sha, "the installed routing.tar") for p, sha in keep.items()}, None)
        else:
            dev_tar.unlink(missing_ok=True)
    else:
        for p in drop:
            (device / "tiles" / p).unlink(missing_ok=True)
        _remove_empty_dirs(device / "tiles")
        if not keep and (device / "tiles").exists() and not any((device / "tiles").iterdir()):
            (device / "tiles").rmdir()
    (device / "packs" / f"{state}.json").unlink()
    if keep:
        _write_device_files(device, layout)
    else:  # nothing left: the device has no tile set and may pick a new layout next time
        for f in (DEVICE_FILE, "valhalla.json"):
            (device / f).unlink(missing_ok=True)
    return {"uninstalled": state, "tiles_removed": len(drop), "tiles_kept_shared": len(mine) - len(drop),
            "layout": layout, "wall_s": round(time.perf_counter() - t0, 2),
            "tileset_bytes": _tileset_bytes(device, layout)}


def _tileset_bytes(device: Path, layout: str) -> int:
    if layout == "tar":
        p = device / "routing.tar"
        return p.stat().st_size if p.exists() else 0
    return C.dir_size(device / "tiles") if (device / "tiles").exists() else 0


def list_installed(device: Path = C.DEVICE) -> dict:
    return {"device": device_info(device),
            "packs": {st: {"graph_build": m["graph_build"], "valhalla_version": m.get("valhalla_version"),
                           "tiles": len(m["tiles"])} for st, m in installed(device).items()}}


def reset(device: Path) -> None:
    """Remove a device folder entirely (tests)."""
    if device.exists():
        shutil.rmtree(device)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["install", "uninstall", "list"])
    ap.add_argument("states", nargs="*")
    ap.add_argument("--layout", choices=LAYOUTS, default=None,
                    help="only at the first install (default tar); later calls use the stored layout")
    ap.add_argument("--device", type=Path, default=C.DEVICE)
    a = ap.parse_args()
    try:
        if a.command == "list":
            print(json.dumps(list_installed(a.device), indent=2))
        elif a.command == "install":
            if not a.states:
                ap.error("name at least one state")
            print(json.dumps(install(a.states, a.device, a.layout), indent=2))
        else:
            if len(a.states) != 1:
                ap.error("uninstall takes one state")
            print(json.dumps(uninstall(a.states[0], a.device, a.layout), indent=2))
    except PackError as e:
        sys.exit(f"error: {e}")


if __name__ == "__main__":
    main()
