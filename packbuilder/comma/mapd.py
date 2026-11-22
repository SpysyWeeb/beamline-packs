"""mapd.tar: the state's roads in Mapd's own format, for StarPilot's speed-limit map (docs/comma-pack-format.md 5).

    python packbuilder/comma/mapd.py setup --dest DIR [--source <StarPilot clone>/mapd_repo] [--go GOROOT] [--force]
    python packbuilder/comma/mapd.py build --extract <state>.osm.pbf --manifest <pack>/manifest.json
                                     --mapd DIR/mapd --out mapd.tar [--work DIR] [--clean] [--no-admit]
    python packbuilder/comma/mapd.py verify mapd.tar --manifest <pack>/manifest.json [--mapd DIR/mapd] [--work DIR]

setup   Builds Mapd's program (`mapd`) into DIR from StarPilot's public source at the commit pinned in
        pins.json (repository StarPilotHQ/StarPilot, folder mapd_repo): the folder is fetched with git
        (a shallow, blob-less, sparse fetch of that one commit; HEAD is checked against the pin), or,
        with --source, copied from a local clone whose HEAD is the pinned commit and whose folder has
        no changes. Go is the version pins.json names (Mapd's go.mod asks for 1.25.1): --go DIR uses an
        installed one (its `go version` is checked), else go.dev's archive for this machine is
        downloaded and installed only when its size and SHA-256 are the ones pins.json pins for the
        platform (linux-amd64, linux-arm64); a platform without a pin is refused, and go.dev's own
        list is never read. `go build -mod=readonly -trimpath` (modules from the Go proxy,
        checked against go.sum). Mapd's gzip library asks pkg-config for zlib, so zlib's developer
        files and pkg-config must be installed (Ubuntu: zlib1g-dev pkg-config), and a C compiler.
        DIR/mapd.json records what was built; a second run with the same pins does nothing.
build   Mapd's own preparation of the pack's OpenStreetMap extract, done with pyosmium (as
        mapd_repo/scripts/filter_planet.sh and add_locations.sh do with osmium-tool): only the ways
        whose highway is one of Mapd's road kinds, every way carrying its nodes' coordinates. Then
        `mapd generate --generate-empty-files` over the whole two-degree groups covering the pack's
        bounds, a check that every quarter-degree cell of every group is there and nothing else, one
        gzip tar per group (`<lat>/<lon>.tar.gz`, members `offline/<lat>/<lon>/<cell>`), and mapd.tar:
        an uncompressed USTAR tar of the group archives (`<lat>/<lon>.tar.gz`, sorted). Then `verify`
        (unless --no-admit). Writes mapd.json beside mapd.tar (box, source date, sizes).
verify  The member rule of mapd.tar, the group set of the box, every group's cells; and with --mapd,
        Mapd's own admission of the unpacked archives into a new offline root:
        mapd --snapshot-admit --offline-root R --min-lat .. --min-lon .. --max-lat .. --max-lon ..
             --archive-dir A --source-date YYYY-MM-DD

Linux only for setup, build and the admission (Mapd uses Unix calls). Run build with a Python that
has pyosmium (.venv/map). The member checks need only the standard library.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import common  # noqa: E402
from common import Refused  # noqa: E402

UA = {"User-Agent": "beamline-packs-build"}
# mapd_repo/scripts/filter_planet.sh: the road kinds Mapd's map holds
MAPD_ROADS = {"motorway", "trunk", "primary", "secondary", "tertiary", "unclassified", "residential",
              "motorway_link", "trunk_link", "primary_link", "secondary_link", "tertiary_link"}
GROUP_GZIP_LEVEL = 6
CELLS_PER_GROUP = 64


def log(msg: str) -> None:
    print(msg, flush=True)


def run(cmd: list, **kw) -> subprocess.CompletedProcess:
    log("$ " + " ".join(str(c) for c in cmd))
    return subprocess.run([str(c) for c in cmd], **kw)


# ------------------------------------------------------------------ setup: Go, the pinned source, the build
def go_platform() -> str:
    arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine().lower())
    if sys.platform != "linux" or arch is None:
        raise Refused(f"Mapd is built on Linux x86-64 or arm64 here, not {sys.platform} {platform.machine()}")
    return f"linux-{arch}"


def go_version_of(goroot: Path) -> str:
    try:
        r = subprocess.run([str(goroot / "bin" / "go"), "version"], capture_output=True, text=True)
    except OSError:  # no Go there (yet)
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def fetch(url: str, dest: Path, timeout: int = 900) -> None:
    tmp = dest.with_name(dest.name + ".part")
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r, open(tmp, "wb") as f:
        shutil.copyfileobj(r, f, common.CHUNK)
    tmp.replace(dest)


def get_go(dest: Path, pins: dict, goroot: str | None) -> Path:
    version = pins["go"]["version"]
    if goroot:
        root = Path(goroot).expanduser().resolve()
        have = go_version_of(root)
        if f"go{version} " not in have + " ":
            raise Refused(f"{root}: {have or 'no go'}; Mapd's go.mod asks for go{version}")
        return root
    root = dest / "go"
    if f"go{version} " in go_version_of(root) + " ":
        return root
    plat = go_platform()
    name = f"go{version}.{plat}.tar.gz"
    # Only the pin is trusted: the hash is never taken from the site that serves the archive.
    want = (pins["go"].get("sha256") or {}).get(plat)
    size = (pins["go"].get("bytes") or {}).get(plat)
    if not (isinstance(want, str) and re.fullmatch(r"[0-9a-f]{64}", want)):
        raise Refused(f"pins.json pins no SHA-256 for {name} (go.sha256.{plat}): Go is installed only from a "
                      f"pinned archive (or give --go with an installed Go {version})")
    log(f"Go {version}: {name}, pinned SHA-256 {want}")
    archive = dest / name
    fetch(f"https://go.dev/dl/{name}", archive)
    got_size, got = archive.stat().st_size, common.sha256_file(archive)
    if got != want or (size is not None and got_size != size):
        archive.unlink()
        raise Refused(f"{name}: {got_size} bytes, SHA-256 {got}; pins.json pins {size} bytes, SHA-256 {want}")
    shutil.rmtree(root, ignore_errors=True)
    with tarfile.open(archive, "r:gz") as t:
        t.extractall(dest, filter="data")
    archive.unlink()
    if f"go{version} " not in go_version_of(root) + " ":
        raise Refused(f"{root}: the unpacked Go does not run")
    return root


def git(*args, cwd: Path | None = None, check: bool = True) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                       env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_LFS_SKIP_SMUDGE": "1"})
    if check and r.returncode != 0:
        raise Refused(f"git {' '.join(args[:3])} failed: {r.stderr.strip()[:300]}")
    return r.stdout.strip()


def get_source(dest: Path, pins: dict, source: str | None) -> Path:
    """A fresh copy of Mapd's source at the pinned commit in dest/src."""
    sp = pins["starpilot"]
    target = dest / "src"
    shutil.rmtree(target, ignore_errors=True)
    if source:
        folder = Path(source).expanduser().resolve()
        top = Path(git("-c", f"safe.directory={folder.as_posix()}", "rev-parse", "--show-toplevel", cwd=folder))
        safe = f"safe.directory={top.as_posix()}"
        head = git("-c", safe, "rev-parse", "HEAD", cwd=folder)
        if head != sp["commit"]:
            raise Refused(f"{folder}: the clone is at {head}, the pin is {sp['commit']}")
        if folder.relative_to(top).as_posix() != sp["folder"]:
            raise Refused(f"{folder}: not the clone's {sp['folder']} folder")
        if git("-c", safe, "status", "--porcelain", "--", ".", cwd=folder):
            raise Refused(f"{folder}: has changes against the pinned commit")
        shutil.copytree(folder, target, ignore=shutil.ignore_patterns(".git"))
        log(f"source: {sp['folder']} of a local clone at {head}")
        return target
    clone = dest / "starpilot-git"
    shutil.rmtree(clone, ignore_errors=True)
    clone.mkdir(parents=True)
    git("init", "-q", cwd=clone)
    git("remote", "add", "origin", sp["repository"], cwd=clone)
    git("sparse-checkout", "set", sp["folder"], cwd=clone)
    git("fetch", "-q", "--depth", "1", "--filter=blob:none", "origin", sp["commit"], cwd=clone)
    git("-c", "advice.detachedHead=false", "checkout", "-q", "FETCH_HEAD", cwd=clone)
    head = git("rev-parse", "HEAD", cwd=clone)
    if head != sp["commit"]:
        raise Refused(f"fetched {head}, the pin is {sp['commit']}")
    if not (clone / sp["folder"] / "go.mod").is_file():
        raise Refused(f"{sp['repository']} at {head[:12]} has no {sp['folder']}/go.mod")
    shutil.copytree(clone / sp["folder"], target)
    shutil.rmtree(clone, ignore_errors=True)
    log(f"source: {sp['repository']} {sp['folder']} at {head}")
    return target


def check_build_needs() -> None:
    missing = []
    if not shutil.which("pkg-config"):
        missing.append("pkg-config")
    elif subprocess.run(["pkg-config", "--exists", "zlib"]).returncode != 0:
        missing.append("zlib's developer files (pkg-config finds no zlib)")
    if not (os.environ.get("CC") or shutil.which("gcc") or shutil.which("cc")):
        missing.append("a C compiler")
    if not shutil.which("git"):
        missing.append("git")
    if missing:
        raise Refused("Mapd's build needs " + ", ".join(missing) + " (Ubuntu: the packs repository's "
                      "build/setup_ubuntu.sh installs them, or apt-get install gcc libc6-dev pkg-config zlib1g-dev git)")


def cmd_setup(a) -> int:
    pins = common.load_pins()
    dest = Path(a.dest).expanduser().resolve()
    dest.mkdir(parents=True, exist_ok=True)
    stamp_path, binary = dest / "mapd.json", dest / "mapd"
    want = {"starpilot_commit": pins["starpilot"]["commit"], "folder": pins["starpilot"]["folder"],
            "go": pins["go"]["version"]}
    if not a.force and binary.is_file() and stamp_path.is_file():
        old = json.loads(stamp_path.read_text(encoding="utf-8"))
        if {k: old.get(k) for k in want} == want and old.get("sha256") == common.sha256_file(binary):
            log(f"{binary}: already built from {want['starpilot_commit'][:12]} with Go {want['go']}")
            return 0
    go_platform()
    check_build_needs()
    t0 = time.time()
    goroot = get_go(dest, pins, a.go)
    src = get_source(dest, pins, a.source)
    env = {**os.environ, "GOTOOLCHAIN": "local", "GOFLAGS": "-mod=readonly", "CGO_ENABLED": "1",
           "PATH": str(goroot / "bin") + os.pathsep + os.environ.get("PATH", "")}
    env.setdefault("GOPATH", str(dest / "gopath"))
    env.setdefault("GOCACHE", str(dest / "gocache"))
    tmp = dest / "mapd.tmp"
    r = run([goroot / "bin" / "go", "build", "-trimpath", "-o", tmp, "."], cwd=src, env=env)
    if r.returncode != 0 or not tmp.is_file():
        raise Refused("go build of Mapd failed (output above)")
    smoke = subprocess.run([str(tmp), "--help"], capture_output=True, text=True, timeout=60)
    if smoke.returncode != 0 or "generate" not in smoke.stdout + smoke.stderr:
        raise Refused(f"the built mapd does not answer --help: {(smoke.stdout + smoke.stderr)[-300:]}")
    tmp.replace(binary)
    stamp = {**want, "go_version": go_version_of(goroot), "sha256": common.sha256_file(binary),
             "bytes": binary.stat().st_size, "seconds": round(time.time() - t0, 1)}
    stamp_path.write_text(json.dumps(stamp, indent=1) + "\n", encoding="utf-8")
    shutil.rmtree(src, ignore_errors=True)
    log(json.dumps(stamp, indent=1))
    return 0


# ------------------------------------------------------------------ build
def prepare(extract: Path, target: Path) -> dict:
    """Mapd's preparation (filter_planet.sh + add_locations.sh) with pyosmium."""
    import osmium
    seen = kept = 0
    target.unlink(missing_ok=True)
    ways = (osmium.FileProcessor(str(extract), osmium.osm.NODE | osmium.osm.WAY)
            .with_locations().with_filter(osmium.filter.KeyFilter("highway")))
    with osmium.SimpleWriter(osmium.io.File(str(target), "pbf,locations_on_ways=true"), overwrite=True) as writer:
        for obj in ways:
            if not obj.is_way():
                continue
            seen += 1
            if obj.tags.get("highway") in MAPD_ROADS and len(obj.nodes) > 1:
                if not all(n.location.valid() for n in obj.nodes):
                    raise Refused(f"{extract.name}: way {obj.id} has a node without a location")
                writer.add_way(obj)
                kept += 1
    checked = placed = 0
    for obj in osmium.FileProcessor(str(target), osmium.osm.WAY):
        checked += 1
        placed += all(n.location.valid() for n in obj.nodes)
        if checked >= 2000:
            break
    if not kept or placed != checked:
        raise Refused(f"{target.name}: {kept} ways kept, {placed} of {checked} read back with coordinates")
    return {"ways_with_highway": seen, "ways_kept": kept, "bytes": target.stat().st_size}


def check_generated(offline: Path, box: dict) -> dict:
    """Every cell of every group present (empty cells too), nothing else; returns counts."""
    want = {f"{lat}/{lon}/{cell}" for lat, lon in common.mapd_groups(box) for cell in common.mapd_cells(lat, lon)}
    have, sizes = set(), {}
    for p in offline.rglob("*"):
        if p.is_symlink():
            raise Refused(f"{p}: a link in Mapd's output")
        if p.is_file():
            rel = p.relative_to(offline).as_posix()
            have.add(rel)
            sizes[rel] = p.stat().st_size
    if have != want:
        missing, extra = sorted(want - have), sorted(have - want)
        raise Refused(f"Mapd's output: {len(missing)} cells missing (first {missing[:3]}), {len(extra)} other files "
                      f"(first {extra[:3]})")
    if any(n <= 0 for n in sizes.values()):
        raise Refused("Mapd's output has an empty cell file")
    return {"cells": len(have), "cell_bytes": sum(sizes.values())}


def pack_groups(offline: Path, archives: Path, box: dict, mtime: int) -> list[Path]:
    shutil.rmtree(archives, ignore_errors=True)
    out = []
    for lat, lon in common.mapd_groups(box):
        path = archives / str(lat) / f"{lon}.tar.gz"
        with common.TarOut(path, mtime=mtime, gzip_level=GROUP_GZIP_LEVEL) as t:
            for cell in sorted(common.mapd_cells(lat, lon)):
                t.add_file(f"offline/{lat}/{lon}/{cell}", offline / str(lat) / str(lon) / cell)
        out.append(path)
    return out


def write_outer(archives: Path, out: Path, box: dict, mtime: int) -> None:
    with common.TarOut(out, mtime=mtime) as t:
        for lat, lon in common.mapd_groups(box):
            t.add_file(f"{lat}/{lon}.tar.gz", archives / str(lat) / f"{lon}.tar.gz")


def box_and_date(a) -> tuple[dict, str]:
    if a.manifest:
        m = json.loads(Path(a.manifest).read_text(encoding="utf-8"))
        return common.mapd_box(m["bounds"]), m["osm_snapshot"]
    if not (a.bounds and a.date):
        raise SystemExit("give --manifest, or --bounds W,S,E,N and --date YYYY-MM-DD")
    return common.mapd_box([float(v) for v in a.bounds.split(",")]), a.date


def cmd_build(a) -> int:
    box, date = box_and_date(a)
    extract, out = Path(a.extract).resolve(), Path(a.out).resolve()
    mapd = Path(a.mapd).resolve()
    work = Path(a.work).resolve() if a.work else out.parent / ".mapd-work"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    t0 = time.time()
    log(f"box: latitude {box['min_lat']}..{box['max_lat']}, longitude {box['min_lon']}..{box['max_lon']} "
        f"({len(common.mapd_groups(box))} groups); source date {date}")
    roads = work / "roads.osm.pbf"
    prep = prepare(extract, roads)
    log(f"prepared: {prep}")
    offline = work / "offline"
    r = run([mapd, "generate", "--input-file", roads, "--output-directory", offline,
             "--minlat", box["min_lat"], "--minlon", box["min_lon"], "--maxlat", box["max_lat"],
             "--maxlon", box["max_lon"], "--generate-empty-files"], cwd=work, capture_output=True, text=True)
    if r.returncode != 0:
        log((r.stdout + r.stderr)[-3000:])
        raise Refused(f"mapd generate failed (exit {r.returncode})")
    gen = check_generated(offline, box)
    log(f"generated: {gen}")
    mtime = common.utc_epoch(date)
    pack_groups(offline, work / "archives", box, mtime)
    write_outer(work / "archives", out, box, mtime)
    rep = verify(out, box, None if a.no_admit else mapd, work / "verify", date)
    info = {**box, "source_date": date, "groups": len(common.mapd_groups(box)), **gen,
            "ways_kept": prep["ways_kept"], "bytes": out.stat().st_size, "sha256": common.sha256_file(out),
            "admitted": rep.get("admitted", False), "starpilot_commit": common.load_pins()["starpilot"]["commit"],
            "seconds": round(time.time() - t0, 1)}
    out.with_suffix(".json").write_text(json.dumps(info, indent=1) + "\n", encoding="utf-8")
    log(json.dumps(info, indent=1))
    if a.clean:
        shutil.rmtree(work, ignore_errors=True)
    return 0


# ------------------------------------------------------------------ verify
def check_group(data: bytes, lat: int, lon: int) -> int:
    """A group archive as Mapd's checked extractor reads it: one gzip member, regular files under
    offline/<lat>/<lon>/ only, the 64 cells, nothing after the tar's end. Returns the cell bytes."""
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        raw = d.decompress(data)
    except zlib.error as e:
        raise Refused(f"{lat}/{lon}.tar.gz: not gzip ({e})") from None
    if not d.eof or d.unused_data:
        raise Refused(f"{lat}/{lon}.tar.gz: cut short, more than one gzip member or trailing data")
    names, total = set(), 0
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as t:
        for m in t:
            if not m.isreg():
                raise Refused(f"{lat}/{lon}.tar.gz: {m.name!r} is not a regular file")
            if not m.name.startswith(f"offline/{lat}/{lon}/") or m.name.count("/") != 3 or m.name in names:
                raise Refused(f"{lat}/{lon}.tar.gz: unexpected member {m.name!r}")
            names.add(m.name)
            total += m.size
        end = t.offset
    if any(raw[end:]):
        raise Refused(f"{lat}/{lon}.tar.gz: content after the tar's end")
    want = {f"offline/{lat}/{lon}/{c}" for c in common.mapd_cells(lat, lon)}
    if names != want:
        raise Refused(f"{lat}/{lon}.tar.gz: {len(want - names)} cells missing, {len(names - want)} other members")
    return total


def verify(tar_path: Path, box: dict, mapd: Path | None, work: Path | None = None, date: str | None = None) -> dict:
    tar_path = Path(tar_path)
    members = common.checked_members(tar_path, "mapd")
    names = [m.name for m in members]
    want = [f"{lat}/{lon}.tar.gz" for lat, lon in common.mapd_groups(box)]
    if sorted(names) != sorted(want):
        raise Refused(f"{tar_path.name}: groups {sorted(names)}; the box needs {sorted(want)}")
    cells = 0
    with tarfile.open(tar_path, "r:") as t:
        for m in members:
            lat, lon = (int(v) for v in m.name[:-len(".tar.gz")].split("/"))
            cells += check_group(t.extractfile(m).read(), lat, lon)
    rep = {"groups": len(names), "cell_bytes": cells, "admitted": False}
    if mapd is None:
        return rep
    if not date:
        raise Refused("the admission needs the source date")
    own = work is None
    work = Path(tempfile.mkdtemp(prefix="mapd-verify-")) if own else Path(work)
    try:
        shutil.rmtree(work, ignore_errors=True)
        (work / "root").mkdir(parents=True)
        common.unpack(tar_path, work / "archives", "mapd")
        r = run([Path(mapd).resolve(), "--snapshot-admit", "--offline-root", (work / "root").resolve(),
                 "--min-lat", box["min_lat"], "--min-lon", box["min_lon"], "--max-lat", box["max_lat"],
                 "--max-lon", box["max_lon"], "--archive-dir", (work / "archives").resolve(), "--source-date", date],
                capture_output=True, text=True, timeout=1800)
        text = (r.stdout + r.stderr).strip()
        current = work / "root" / "current.json"
        if r.returncode != 0 or not current.is_file():
            raise Refused(f"Mapd's admission refused {tar_path.name} (exit {r.returncode}): {text[-600:]}")
        gen = json.loads(current.read_text(encoding="utf-8"))["generation"]
        receipt = json.loads((work / "root" / "generations" / gen / "receipt.json").read_text(encoding="utf-8"))
        n = len(receipt.get("tiles", []))
        if n != CELLS_PER_GROUP * len(names) or receipt.get("sourceKind") != "provided-group-archives":
            raise Refused(f"Mapd admitted {n} cells ({receipt.get('sourceKind')}); expected "
                          f"{CELLS_PER_GROUP * len(names)} from group archives")
        rep.update(admitted=True, generation=gen, cells=n,
                   cells_with_roads=sum(1 for x in receipt["tiles"] if not x.get("empty")))
        log(f"Mapd admitted {tar_path.name}: generation {gen[:12]}, {n} cells, {rep['cells_with_roads']} with roads")
    finally:
        if own:
            shutil.rmtree(work, ignore_errors=True)
    return rep


def cmd_verify(a) -> int:
    box, date = box_and_date(a)
    rep = verify(Path(a.tar), box, Path(a.mapd) if a.mapd else None, Path(a.work) if a.work else None, date)
    log(json.dumps(rep, indent=1))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("setup")
    s.add_argument("--dest", required=True)
    s.add_argument("--source", help="the mapd_repo folder of a local StarPilot clone at the pinned commit")
    s.add_argument("--go", help="an installed Go of the pinned version (its GOROOT)")
    s.add_argument("--force", action="store_true")
    b = sub.add_parser("build")
    b.add_argument("--extract", required=True)
    b.add_argument("--mapd", required=True, help="the mapd program (setup's DIR/mapd)")
    b.add_argument("--out", required=True)
    b.add_argument("--work")
    b.add_argument("--clean", action="store_true", help="delete the work folder at the end")
    b.add_argument("--no-admit", action="store_true", help="skip Mapd's admission check")
    v = sub.add_parser("verify")
    v.add_argument("tar")
    v.add_argument("--mapd", help="run Mapd's admission too")
    v.add_argument("--work")
    for x in (b, v):
        x.add_argument("--manifest", help="the pack's manifest.json (bounds, osm_snapshot)")
        x.add_argument("--bounds", help="W,S,E,N instead of --manifest (write it as --bounds=W,S,E,N)")
        x.add_argument("--date", help="YYYY-MM-DD instead of --manifest")
    a = ap.parse_args(argv)
    try:
        return {"setup": cmd_setup, "build": cmd_build, "verify": cmd_verify}[a.cmd](a)
    except Refused as e:
        print(f"refused: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
