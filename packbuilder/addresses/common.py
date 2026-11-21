"""Shared paths, step bookkeeping and measurement helpers for the address track.

Every build step is a separate script so that its wall time and peak memory
can be measured on its own. A step writes data/addresses/metrics/<step>.json
when it finishes; re-running a finished step is skipped unless --force is given.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
SOURCES = json.loads((HERE / "sources.json").read_text(encoding="utf-8"))

# Which state and which folders (environment, so that run.py's step processes all see the same):
#   BEAMLINE_ADDR_STATE   a key of sources.json "states" (default "pa")
#   BEAMLINE_ADDR_DATA    the data folder this run writes (default data/addresses)
#   BEAMLINE_ADDR_INPUTS  a data folder only read: a download or work file that is not in the data
#                         folder is taken from there (a rebuild reading another run's inputs)
# Relative folders are relative to the repository root.
STATE_KEY = os.environ.get("BEAMLINE_ADDR_STATE", "pa")
if STATE_KEY not in SOURCES["states"]:
    raise SystemExit(f"BEAMLINE_ADDR_STATE={STATE_KEY}: not a state of sources.json ({', '.join(SOURCES['states'])})")
STATE_INFO = SOURCES["states"][STATE_KEY]
SOURCES["state"] = {k: STATE_INFO[k] for k in ("name", "usps", "fips")}
# BEAMLINE_SNAPSHOT (YYMMDD): the weekly build's extract date replaces sources.json's 261114 in the
# OSM file names (data/osm/<state>-<snapshot>.osm.pbf).
_SNAP = os.environ.get("BEAMLINE_SNAPSHOT")
if _SNAP:
    if not (len(_SNAP) == 6 and _SNAP.isdigit()):
        raise SystemExit(f"BEAMLINE_SNAPSHOT={_SNAP!r}: expected YYMMDD")
    SOURCES["osm"] = {k: v.replace("-261114.", f"-{_SNAP}.") for k, v in SOURCES["osm"].items()}


def _folder(env: str, default: Path | None) -> Path | None:
    v = os.environ.get(env)
    if not v:
        return default
    p = Path(v)
    return p if p.is_absolute() else REPO / p


DATA = _folder("BEAMLINE_ADDR_DATA", REPO / "data" / "addresses")
INPUTS = _folder("BEAMLINE_ADDR_INPUTS", None)
DL = DATA / "download"
WORK = DATA / "work"
METRICS = DATA / "metrics"
REPORTS = DATA / "reports"
OSM_DIR = REPO / "data" / "osm"
OUT_DB = DATA / f"{STATE_KEY}-addresses.sqlite"


def work_file(name: str) -> Path:
    """A work file to read: in this run's work folder, else in BEAMLINE_ADDR_INPUTS's."""
    p = WORK / name
    if not p.exists() and INPUTS is not None and (INPUTS / "work" / name).exists():
        return INPUTS / "work" / name
    return p


def dl_file(name: str) -> Path:
    """A downloaded file to read: in this run's download folder, else in BEAMLINE_ADDR_INPUTS's."""
    p = DL / name
    if not p.exists() and INPUTS is not None and (INPUTS / "download" / name).exists():
        return INPUTS / "download" / name
    return p

# Web requests carry a generic User-Agent only (no project name, no address of this machine).
USER_AGENT = {"User-Agent": "Mozilla/5.0 (compatible; address-data-fetch/1.0)"}

# Cap for heavy jobs on the shared build machine.
MAX_THREADS = 4

for d in (DATA, DL, WORK, METRICS, REPORTS):
    d.mkdir(parents=True, exist_ok=True)

if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))


def rel(p: Path) -> str:
    """Path relative to the repo root, with forward slashes (for reports)."""
    try:
        return Path(p).resolve().relative_to(REPO).as_posix()
    except ValueError:
        return Path(p).as_posix()


def peak_rss_mb() -> float:
    """Peak resident memory of this process in MB.

    Windows: psutil peak working set (PeakWorkingSetSize).
    Linux: ru_maxrss from getrusage (KB).
    """
    try:
        import psutil  # build-time only
        mi = psutil.Process().memory_info()
        if hasattr(mi, "peak_wset"):
            return mi.peak_wset / 1e6
    except ImportError:
        pass
    try:
        import resource
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e3
    except ImportError:
        return float("nan")


def sha256_file(p: Path, bufsize: int = 1 << 22) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while True:
            b = f.read(bufsize)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def md5_file(p: Path, bufsize: int = 1 << 22) -> str:
    h = hashlib.md5()
    with open(p, "rb") as f:
        while True:
            b = f.read(bufsize)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def osm_pbf(state: str = STATE_INFO["osm"]) -> Path:
    return OSM_DIR / SOURCES["osm"][state]


def wait_for_osm(state: str = STATE_INFO["osm"], poll_s: int = 60, max_wait_s: int = 4 * 3600) -> Path:
    """Shared OSM extract is downloaded by the orchestrator: wait until its MD5 matches."""
    pbf = osm_pbf(state)
    md5f = pbf.with_name(pbf.name + ".md5")
    t0 = time.time()
    while True:
        if pbf.exists() and md5f.exists():
            want = md5f.read_text().split()[0].strip().lower()
            got = md5_file(pbf)
            if got == want:
                return pbf
            print(f"[osm] {pbf.name}: md5 {got} != {want} (still downloading?)", flush=True)
        else:
            print(f"[osm] waiting for {pbf.name}", flush=True)
        if time.time() - t0 > max_wait_s:
            raise SystemExit(f"OSM extract {pbf} not ready after {max_wait_s}s")
        time.sleep(poll_s)


class Step:
    """Context manager: skip-if-done, timing and peak-memory metrics for a build step."""

    def __init__(self, name: str, argv=None, extra_args=None):
        ap = argparse.ArgumentParser(description=f"address track step: {name}")
        ap.add_argument("--force", action="store_true", help="re-run even if already done")
        for a in extra_args or []:
            ap.add_argument(*a[0], **a[1])
        self.args = ap.parse_args(argv)
        self.name = name
        self.metrics_path = METRICS / f"{name}.json"
        self.info: dict = {}

    @property
    def done(self) -> bool:
        return self.metrics_path.exists() and not self.args.force

    def __enter__(self):
        if self.done:
            print(f"[{self.name}] already done ({rel(self.metrics_path)}); use --force to re-run")
            raise SystemExit(0)
        self.t0 = time.time()
        self.c0 = time.process_time()
        print(f"[{self.name}] start", flush=True)
        return self

    def __exit__(self, et, ev, tb):
        if et is not None:
            return False
        rec = {
            "step": self.name,
            "finished_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "wall_s": round(time.time() - self.t0, 1),
            "cpu_s": round(time.process_time() - self.c0, 1),
            "peak_rss_mb": round(peak_rss_mb(), 1),
            "peak_rss_how": "psutil peak_wset (Windows peak working set) of the step process",
            "note": "other heavy builds were running on the same machine at the same time",
        }
        rec.update(self.info)
        self.metrics_path.write_text(json.dumps(rec, indent=2), encoding="utf-8")
        print(f"[{self.name}] done in {rec['wall_s']} s, peak {rec['peak_rss_mb']} MB", flush=True)
        return False


def file_mb(p: Path) -> float:
    return round(Path(p).stat().st_size / 1e6, 2)
