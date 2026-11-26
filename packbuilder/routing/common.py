"""Shared paths and helpers for the routing pack builder.

Everything here is plain Python plus pyvalhalla, so it runs the same on
Windows and on Linux (x86_64 or aarch64).
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil

REPO = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
DATA = REPO / "data" / "routing"
OSM_IN = REPO / "data" / "osm"

# Geofabrik extract date (YYMMDD) shared by all the pack parts. The weekly build (the packs
# repository) sets BEAMLINE_SNAPSHOT to the date of the extracts it fetched; 261114 is round 3's.
SNAPSHOT = os.environ.get("BEAMLINE_SNAPSHOT", "261114")
if not (len(SNAPSHOT) == 6 and SNAPSHOT.isdigit()):
    raise SystemExit(f"BEAMLINE_SNAPSHOT={SNAPSHOT!r}: expected YYMMDD")
ALL_STATES = {"pa": "pennsylvania", "oh": "ohio", "ca": "california"}
# The states of the one graph (BEAMLINE_ROUTING_STATES, comma-separated; default PA and OH together).
# Packs cut from one graph share its graph build id; a phone mixes no two graph builds.
_STATES_ENV = [s.strip() for s in os.environ.get("BEAMLINE_ROUTING_STATES", "pa,oh").split(",") if s.strip()]
if not _STATES_ENV or any(s not in ALL_STATES for s in _STATES_ENV):
    raise SystemExit(f"BEAMLINE_ROUTING_STATES: states of {sorted(ALL_STATES)}, comma-separated")
STATES = {s: ALL_STATES[s] for s in _STATES_ENV}
GRAPH_NAME = "-".join(STATES)  # "pa-oh" (round 3), "pa" for Pennsylvania alone

# Other heavy jobs share this machine: cap threads (override with BEAMLINE_THREADS).
THREADS = int(os.environ.get("BEAMLINE_THREADS", "4"))

# Build layout under data/routing/
MERGED_PBF = DATA / "osm" / f"{GRAPH_NAME}-{SNAPSHOT}.osm.pbf"
BOUNDARIES_PBF = DATA / "osm" / f"boundaries-{SNAPSHOT}.osm.pbf"
BUILD = DATA / "build"
TILE_DIR = BUILD / "valhalla_tiles"
ADMIN_DB = BUILD / "admins.sqlite"
TZ_DB = BUILD / "tz_world.sqlite"
BUILD_CONFIG = BUILD / "valhalla.json"
DOWNLOADS = DATA / "downloads"
PACKS = DATA / "packs"
DEVICE = DATA / "device"
RESULTS = DATA / "results"
LOGS = DATA / "logs"

IS_WIN = platform.system() == "Windows"


def rel(p: Path | str) -> str:
    """Repo-relative POSIX path (for anything written into files or reports)."""
    p = Path(p).resolve()
    try:
        return p.relative_to(REPO).as_posix()
    except ValueError:
        return p.as_posix()


def pyvalhalla_dirs() -> tuple[Path, Path]:
    """(bin dir, vendored shared-library dir) of the installed pyvalhalla wheel."""
    import valhalla

    pkg = Path(valhalla.__file__).resolve().parent
    return pkg / "bin", pkg.parent / "pyvalhalla.libs"


def valhalla_exe(name: str) -> Path:
    bindir, _ = pyvalhalla_dirs()
    exe = bindir / (name + (".exe" if IS_WIN else ""))
    if not exe.exists():
        raise FileNotFoundError(f"{name} is not shipped in this pyvalhalla wheel ({bindir})")
    return exe


def valhalla_env() -> dict:
    """Environment for running the wheel's C++ tools directly.

    The wheel's own console-script wrappers look the tool up on PATH, which
    fails in a venv that is not activated, so we call bin/*.exe ourselves and
    put the vendored DLL folder first on PATH (Windows only).
    """
    env = dict(os.environ)
    _, libs = pyvalhalla_dirs()
    if IS_WIN:
        env["PATH"] = str(libs) + os.pathsep + env.get("PATH", "")
    return env


def file_hash(path: Path, algo: str = "sha256", chunk: int = 1 << 22) -> str:
    h = hashlib.new(algo)
    with open(path, "rb") as f:
        while b := f.read(chunk):
            h.update(b)
    return h.hexdigest()


def wait_for_osm_input(name: str, max_wait_s: int = 3600) -> Path:
    """Return data/osm/<name> once its MD5 matches data/osm/<name>.md5.

    The orchestrator downloads these files; poll once a minute until done.
    """
    pbf = OSM_IN / name
    md5file = OSM_IN / (name + ".md5")
    t0 = time.time()
    while True:
        if pbf.exists() and md5file.exists():
            want = md5file.read_text().split()[0].strip().lower()
            got = file_hash(pbf, "md5")
            if got == want:
                return pbf
            print(f"[wait] {name}: md5 {got} != {want} (still downloading?)", flush=True)
        else:
            print(f"[wait] {name}: not there yet", flush=True)
        if time.time() - t0 > max_wait_s:
            raise TimeoutError(f"{name} did not appear with a matching md5")
        time.sleep(60)


def run_measured(cmd: list, log: Path, env: dict | None = None, cwd: Path | None = None) -> dict:
    """Run a command, tee its output to a log, and measure wall time and memory.

    Memory is sampled every 0.25 s with psutil over the process and its
    children: the peak sum of resident set size (working set on Windows).
    On Windows the OS also keeps a per-process peak working set and peak
    commit (private bytes); the last sample before exit is recorded too.
    """
    log.parent.mkdir(parents=True, exist_ok=True)
    peak = {"rss": 0, "peak_wset": 0, "peak_private": 0}
    t0 = time.perf_counter()
    with open(log, "w", encoding="utf-8", errors="replace") as lf:
        lf.write("$ " + " ".join(str(c) for c in cmd) + "\n")
        lf.flush()
        proc = subprocess.Popen([str(c) for c in cmd], stdout=lf, stderr=subprocess.STDOUT,
                                env=env, cwd=cwd)
        stop = threading.Event()

        def sample():
            try:
                p = psutil.Process(proc.pid)
            except psutil.Error:
                return
            while not stop.is_set():
                try:
                    procs = [p] + p.children(recursive=True)
                    total = 0
                    for q in procs:
                        mi = q.memory_info()
                        total += mi.rss
                        if IS_WIN:
                            peak["peak_wset"] = max(peak["peak_wset"], getattr(mi, "peak_wset", 0))
                            peak["peak_private"] = max(peak["peak_private"], getattr(mi, "peak_pagefile", 0))
                    peak["rss"] = max(peak["rss"], total)
                except psutil.Error:
                    pass
                stop.wait(0.25)

        th = threading.Thread(target=sample, daemon=True)
        th.start()
        rc = proc.wait()
        stop.set()
        th.join()
    wall = time.perf_counter() - t0
    res = {
        # basenames only, so no local absolute paths end up in results
        "cmd": [Path(c).name if Path(str(c)).is_absolute() else
                str(c).replace(str(REPO), "<repo>").replace(str(REPO).replace("\\", "\\\\"), "<repo>")[:160]
                for c in cmd],
        "returncode": rc,
        "wall_s": round(wall, 1),
        "peak_rss_mb": round(peak["rss"] / 2**20),
        "log": rel(log),
    }
    if IS_WIN:
        res["peak_working_set_mb"] = round(peak["peak_wset"] / 2**20)
        res["peak_private_mb"] = round(peak["peak_private"] / 2**20)
    return res


def save_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2) + "\n", encoding="utf-8")


def load_json(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def record_measurement(key: str, value: dict) -> None:
    """Accumulate build measurements in data/routing/results/measurements.json."""
    f = RESULTS / "measurements.json"
    data = load_json(f) if f.exists() else {}
    value = dict(value)
    value["recorded_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    data[key] = value
    save_json(f, data)


def dir_size(path: Path) -> int:
    return sum(p.stat().st_size for p in Path(path).rglob("*") if p.is_file())


def mb(n: int) -> float:
    return round(n / 2**20, 1)


def tool_versions() -> dict:
    import valhalla

    return {"pyvalhalla": valhalla.__version__, "python": sys.version.split()[0],
            "platform": platform.platform()}
