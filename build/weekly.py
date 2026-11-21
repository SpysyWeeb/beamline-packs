#!/usr/bin/env python3
"""The weekly build of Beamline's state packs: one entry point for the workflow and for a local run.

    python3 build/weekly.py setup                    the Python environments and the map tools (idempotent)
    python3 build/weekly.py run   --store DIR | --store-release TAG   [--previous-catalog FILE]
                                  [--allow-smaller] [--keep-work]
    python3 build/weekly.py publish --github | --local DIR   (--store DIR | --store-release TAG)
    python3 build/weekly.py cleanup-drafts            delete unpublished (draft) pack releases (after a failed publish)

run      Restores the slower-changing inputs from the store, fetches this week's Geofabrik extract of
         every state of build/config.json "states" (checked against its MD5), refreshes an input whose
         publisher has a newer file (config "inputs"), builds the map, routing, search and house-number
         parts, assembles each state's pack (format 3), checks it (read_pack.py --verify, the search
         tests, the test routes, not much smaller than the published pack) and writes the catalog site
         (packbuilder/catalog/build_catalog.py --check) into data/out/site. Nothing is published.
publish  Only after a run whose checks all passed (data/out/run.json "checks_passed"). --github: one
         release per pack build (created as a draft, every file uploaded and compared, then
         published), then catalog.json of the release tagged "catalog" replaced, then pack releases
         older than the newest config "keep_builds" of each state deleted, then the changed inputs
         saved to the store. Uses the GitHub CLI with the workflow's GITHUB_TOKEN. --local DIR: the
         same into a folder laid out like GitHub Releases (DIR/<tag>/..., DIR/catalog/catalog.json).

The store holds the slower-changing inputs between runs: --store DIR (a folder) or --store-release
TAG (the assets of that GitHub release). Everything this script writes is under data/ (git-ignored).
Linux only (Ubuntu 24.04, like GitHub's ubuntu-24.04 runner); standard library only.
"""
from __future__ import annotations

import argparse
import fnmatch
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CONFIG = json.loads((REPO / "build" / "config.json").read_text(encoding="utf-8"))
DATA = REPO / "data"
OUT = DATA / "out"
LOGS = OUT / "logs"
RUN_JSON = OUT / "run.json"
VENV = REPO / ".venv"
UA = {"User-Agent": "beamline-packs-build"}
PACK_TAG = re.compile(r"^(us-[a-z]{2})-(\d{12})$")


# ------------------------------------------------------------------ small helpers
def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def md5_file(p: Path) -> str:
    h = hashlib.md5()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def rel(p: Path) -> str:
    try:
        return Path(p).resolve().relative_to(REPO).as_posix()
    except ValueError:
        return Path(p).name


def tree_bytes(p: Path) -> int:
    if not p.exists():
        return 0
    if p.is_file():
        return p.stat().st_size
    total = 0
    for root, _dirs, files in os.walk(p):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def py(builder: str) -> str:
    return str(VENV / builder / "bin" / "python")


def states() -> list[str]:
    st = CONFIG["states"]
    bad = [s for s in st if s not in CONFIG["state_info"]]
    if not st or bad:
        raise SystemExit(f"config states {st}: unknown {bad}")
    return list(st)


def load_run() -> dict:
    return json.loads(RUN_JSON.read_text(encoding="utf-8")) if RUN_JSON.exists() else {}


def save_run(run: dict) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    tmp = RUN_JSON.with_suffix(".tmp")
    tmp.write_text(json.dumps(run, indent=1) + "\n", encoding="utf-8")
    tmp.replace(RUN_JSON)


# ------------------------------------------------------------------ measured steps
class Sampler:
    """Peak resident memory of a process tree (Linux /proc, every 0.5 s) and the lowest free disk."""

    def __init__(self, pid: int):
        self.pid, self.peak_rss, self.min_free = pid, 0, shutil.disk_usage(REPO).free
        self.stop = threading.Event()
        self.th = threading.Thread(target=self.loop, daemon=True)
        self.th.start()

    def tree(self) -> list[int]:
        kids: dict[int, list[int]] = {}
        for d in os.listdir("/proc"):
            if d.isdigit():
                try:
                    with open(f"/proc/{d}/stat") as f:
                        ppid = int(f.read().rsplit(")", 1)[1].split()[1])
                    kids.setdefault(ppid, []).append(int(d))
                except (OSError, ValueError, IndexError):
                    pass
        out, todo = [], [self.pid]
        while todo:
            p = todo.pop()
            out.append(p)
            todo += kids.get(p, [])
        return out

    def loop(self) -> None:
        while not self.stop.is_set():
            total = 0
            for p in self.tree():
                try:
                    with open(f"/proc/{p}/status") as f:
                        for line in f:
                            if line.startswith("VmRSS:"):
                                total += int(line.split()[1]) * 1024
                                break
                except OSError:
                    pass
            self.peak_rss = max(self.peak_rss, total)
            self.min_free = min(self.min_free, shutil.disk_usage(REPO).free)
            self.stop.wait(0.5)

    def done(self) -> None:
        self.stop.set()
        self.th.join()


def step(run: dict, name: str, cmd: list, env: dict | None = None, check: bool = True) -> int:
    """Run one build step: output to data/out/logs/<name>.log, wall time, peak memory and disk recorded."""
    LOGS.mkdir(parents=True, exist_ok=True)
    log = LOGS / f"{name}.log"
    free0 = shutil.disk_usage(REPO).free
    t0 = time.time()
    print(f"== {name}", flush=True)
    with open(log, "w", encoding="utf-8", errors="replace") as lf:
        lf.write("$ " + " ".join(rel(Path(c)) if os.path.isabs(str(c)) else str(c) for c in cmd) + "\n")
        lf.flush()
        proc = subprocess.Popen([str(c) for c in cmd], stdout=lf, stderr=subprocess.STDOUT, cwd=REPO,
                                env={**os.environ, **(env or {})})
        s = Sampler(proc.pid)
        rc = proc.wait()
        s.done()
    rec = {"wall_s": round(time.time() - t0, 1), "peak_rss_mb": round(s.peak_rss / 2**20),
           "disk_peak_extra_mb": round(max(0, free0 - s.min_free) / 2**20),
           "data_mb_after": round(tree_bytes(DATA) / 2**20), "returncode": rc}
    run.setdefault("steps", {})[name] = rec
    save_run(run)
    print(f"   {rec['wall_s']} s, peak {rec['peak_rss_mb']} MiB, data/ {rec['data_mb_after']} MiB, exit {rc}",
          flush=True)
    if rc != 0 and check:
        tail = log.read_text(encoding="utf-8", errors="replace").splitlines()[-40:]
        print("\n".join("   | " + t for t in tail))
        raise SystemExit(f"step {name} failed (exit {rc}); log {rel(log)}")
    return rc


# ------------------------------------------------------------------ the store of slower-changing inputs
class Store:
    """Assets by name plus one index, inputs.json. A folder, or a GitHub release's assets (gh CLI)."""

    INDEX = "inputs.json"

    def __init__(self, folder: str | None = None, release: str | None = None):
        if bool(folder) == bool(release):
            raise SystemExit("give --store DIR or --store-release TAG")
        self.folder = Path(folder).expanduser().resolve() if folder else None
        self.release = release
        self.tmp = DATA / "store-tmp"

    def names(self) -> set[str]:
        if self.folder:
            return {p.name for p in self.folder.iterdir()} if self.folder.exists() else set()
        r = subprocess.run(["gh", "release", "view", self.release, "--json", "assets"], capture_output=True, text=True)
        if r.returncode != 0:
            return set()  # no such release yet: the first run
        return {a["name"] for a in json.loads(r.stdout)["assets"]}

    def get(self, name: str, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if self.folder:
            shutil.copyfile(self.folder / name, dest)
            return
        self.tmp.mkdir(parents=True, exist_ok=True)
        subprocess.run(["gh", "release", "download", self.release, "--pattern", name, "--dir", str(self.tmp),
                        "--clobber"], check=True)
        (self.tmp / name).replace(dest)

    def index(self) -> dict:
        if self.INDEX not in self.names():
            return {"format": "beamline-inputs/1", "assets": {}}
        p = self.tmp / ("index-" + self.INDEX)
        self.get(self.INDEX, p)
        return json.loads(p.read_text(encoding="utf-8"))

    def put(self, name: str, src: Path) -> None:
        if self.folder:
            self.folder.mkdir(parents=True, exist_ok=True)
            tmp = self.folder / (name + ".part")
            shutil.copyfile(src, tmp)
            tmp.replace(self.folder / name)
            return
        if subprocess.run(["gh", "release", "view", self.release], capture_output=True).returncode != 0:
            subprocess.run(["gh", "release", "create", self.release, "--prerelease", "--title", "Build inputs",
                            "--notes", INPUTS_NOTES], check=True)
        self.tmp.mkdir(parents=True, exist_ok=True)
        staged = self.tmp / name
        if staged.resolve() != src.resolve():
            staged.unlink(missing_ok=True)
            try:
                os.link(src, staged)
            except OSError:
                shutil.copyfile(src, staged)
        subprocess.run(["gh", "release", "upload", self.release, str(staged), "--clobber"], check=True)


INPUTS_NOTES = ("The slower-changing inputs of the weekly build, kept between runs so that a run does not "
                "download them from their publishers again (README, 'Inputs'). Not a pack: the app never "
                "reads this release. Sources and licences: ATTRIBUTION.md.")


def items() -> list[dict]:
    """The config's input items, per-state ones expanded for every state of the build."""
    out = []
    for it in CONFIG["inputs"]["items"]:
        if it.get("per_state"):
            for st in states():
                if it.get("only_if_step") and it["only_if_step"] not in CONFIG["state_info"][st]["addresses_steps"]:
                    continue
                d = json.loads(json.dumps(it).replace("{state}", st))
                d["state"] = st
                out.append(d)
        else:
            out.append(dict(it))
    return out


def item_files(it: dict) -> list[Path]:
    """The files an item covers (sorted), relative paths under its folder for a dir item."""
    base = REPO / it["path"]
    if it["kind"] == "file":
        return [base] if base.is_file() else []
    if not base.is_dir():
        return []
    files = sorted(p for p in base.rglob("*") if p.is_file())
    if it.get("include"):
        files = [p for p in files if any(fnmatch.fnmatchcase(p.relative_to(base).as_posix(), pat)
                                         for pat in it["include"])]
    return files


def item_identity(it: dict) -> tuple[str, int] | None:
    """SHA-256 over the item's files (name, size, SHA-256 of each) and their total size; None if empty."""
    files = item_files(it)
    if not files:
        return None
    base = REPO / it["path"]
    h, total = hashlib.sha256(), 0
    for p in files:
        name = p.name if it["kind"] == "file" else p.relative_to(base).as_posix()
        n = p.stat().st_size
        total += n
        h.update(f"{name}\t{n}\t{sha256_file(p)}\n".encode())
    return h.hexdigest(), total


def pack_item(it: dict, dest: Path) -> None:
    """A dir item as one tar (gzip when the item says so), deterministic: sorted, no owners, mtime 0."""
    base = REPO / it["path"]
    mode = "w:gz" if it.get("compress") else "w"
    with tarfile.open(dest, mode, format=tarfile.USTAR_FORMAT) as t:
        for p in item_files(it):
            ti = tarfile.TarInfo(p.relative_to(base).as_posix())
            ti.size, ti.mtime, ti.mode, ti.uid, ti.gid, ti.uname, ti.gname = p.stat().st_size, 0, 0o644, 0, 0, "", ""
            with open(p, "rb") as f:
                t.addfile(ti, f)


def unpack_item(it: dict, src: Path) -> None:
    base = REPO / it["path"]
    base.mkdir(parents=True, exist_ok=True)
    with tarfile.open(src, "r:*") as t:
        for m in t.getmembers():
            target = (base / m.name).resolve()
            if not m.isfile() or not str(target).startswith(str(base.resolve()) + os.sep):
                raise SystemExit(f"input {it['name']}: unsafe entry {m.name!r}")
        t.extractall(base, filter="data")


def restore_inputs(run: dict, store: Store) -> dict:
    """Every input item the store holds and data/ lacks is restored. Returns the store's index."""
    idx = store.index()
    names = store.names()
    restored = []
    for it in items():
        if item_identity(it) is not None or it["name"] not in names or it["name"] not in idx["assets"]:
            continue
        if it["kind"] == "file":
            store.get(it["name"], REPO / it["path"])
        else:
            tmp = DATA / "store-tmp" / it["name"]
            store.get(it["name"], tmp)
            unpack_item(it, tmp)
            tmp.unlink()
        restored.append(it["name"])
    run["inputs_restored"] = restored
    print(f"== inputs restored from the store: {len(restored)} of {len(items())} items", flush=True)
    return idx


def head_last_modified(url: str) -> str:
    req = urllib.request.Request(url, headers={**UA, "Range": "bytes=0-0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.headers.get("Last-Modified", "")


def refresh_inputs(run: dict, idx: dict) -> None:
    """Upstream items: fetched again when the publisher has a newer file and the kept one is old enough.
    max_age items: fetched again when older than max_age_days (the kept copy stays if that fails)."""
    manifest_p = REPO / "data/map/work/data/sources/manifest.json"
    manifest = json.loads(manifest_p.read_text(encoding="utf-8")) if manifest_p.exists() else {}
    refreshed, kept = [], []
    for it in items():
        pol = it["policy"]
        if pol == "upstream" and it["name"] in manifest and (REPO / it["path"]).exists():
            rec = manifest[it["name"]]
            age = (now_utc() - datetime.strptime(rec["downloaded_utc"], "%Y-%m-%dT%H:%M:%SZ")
                   .replace(tzinfo=timezone.utc)).days
            try:
                lm = head_last_modified(rec["url"])
            except OSError as e:
                kept.append(f"{it['name']} (publisher not reached: {type(e).__name__})")
                continue
            if lm and lm != rec.get("last_modified") and age >= it.get("min_age_days", 0):
                rc = step(run, f"refresh-{it['name']}", [py("map"), "packbuilder/map/fetch_sources.py", "--only",
                                                          it["name"], "--force"], check=False)
                (refreshed if rc == 0 else kept).append(it["name"])
        elif pol == "max_age" and it["name"] in idx["assets"] and item_identity(it) is not None:
            got = idx["assets"][it["name"]].get("fetched_utc", "1970-01-01T00:00:00Z")
            age = (now_utc() - datetime.strptime(got, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)).days
            if age >= it["max_age_days"]:
                base = REPO / it["path"]
                backup = DATA / "store-tmp" / ("old-" + it["name"])
                pack_item(it, backup)
                for p in item_files(it):
                    p.unlink()
                st = it["state"]
                rc = step(run, f"refresh-{it['name']}", [py("addresses"), "packbuilder/addresses/run.py", "--only",
                                                          it["only_if_step"]], env=addr_env(st), check=False)
                if rc == 0:
                    refreshed.append(it["name"])
                else:
                    unpack_item(it, backup)
                    kept.append(f"{it['name']} (refresh failed, the kept copy is used)")
                backup.unlink(missing_ok=True)
                _ = base
    run["inputs_refreshed"], run["inputs_kept_on_failure"] = refreshed, kept
    print(f"== inputs refreshed: {refreshed or 'none'}; kept: {kept or 'none'}", flush=True)


def save_inputs(store: Store) -> list[str]:
    """Items whose content differs from the store's (or that it lacks) are uploaded; the index last."""
    idx = store.index()
    manifest_p = REPO / "data/map/work/data/sources/manifest.json"
    manifest = json.loads(manifest_p.read_text(encoding="utf-8")) if manifest_p.exists() else {}
    saved = []
    tmpdir = DATA / "store-tmp"
    tmpdir.mkdir(parents=True, exist_ok=True)
    for it in items():
        ident = item_identity(it)
        if ident is None:
            continue
        old = idx["assets"].get(it["name"], {})
        if old.get("identity") == ident[0]:
            continue
        if it["kind"] == "file":
            src = REPO / it["path"]
        else:
            src = tmpdir / it["name"]
            pack_item(it, src)
        store.put(it["name"], src)
        rec = {"path": it["path"], "kind": it["kind"], "identity": ident[0], "bytes": ident[1],
               "asset_bytes": src.stat().st_size, "saved_utc": iso(now_utc()), "fetched_utc": iso(now_utc())}
        if it["name"] in manifest:
            rec.update(url=manifest[it["name"]].get("url"), last_modified=manifest[it["name"]].get("last_modified"),
                       fetched_utc=manifest[it["name"]].get("downloaded_utc", rec["fetched_utc"]))
        idx["assets"][it["name"]] = rec
        if src.parent == tmpdir:
            src.unlink()
        saved.append(it["name"])
    if saved:
        p = tmpdir / Store.INDEX
        p.write_text(json.dumps(idx, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        store.put(Store.INDEX, p)
    print(f"== inputs saved to the store: {saved or 'none (unchanged)'}", flush=True)
    return saved


# ------------------------------------------------------------------ setup
def java_major() -> int:
    """The major version of the `java` on PATH (the one Maven and the map build run); 0 when there is none."""
    try:
        out = subprocess.run(["java", "-version"], capture_output=True, text=True).stderr
    except OSError:
        return 0
    m = re.search(r'version "(\d+)', out)
    return int(m.group(1)) if m else 0


def cmd_setup(_a) -> None:
    if java_major() < 21:
        raise SystemExit(f"Java 21 or newer is needed on PATH (found {java_major() or 'none'}; JAVA_HOME="
                         f"{os.environ.get('JAVA_HOME', '')}): build/setup_ubuntu.sh installs one")
    run = load_run()
    VENV.mkdir(exist_ok=True)
    py312 = shutil.which("python3.12") or sys.executable
    step(run, "setup-routing-env", [py312, "tools/fetch_routing_env.py"])
    for b in ("map", "search", "addresses"):
        if not (VENV / b / "bin" / "python").exists():
            step(run, f"setup-venv-{b}", [py312, "-m", "venv", str(VENV / b)])
        step(run, f"setup-pip-{b}", [py(b), "-m", "pip", "install", "--disable-pip-version-check", "-q", "-r",
                                     f"packbuilder/{b}/requirements.txt"])
    step(run, "setup-maven", [py312, "tools/fetch_map_tools.py", "maven"])
    step(run, "setup-profile-jar", [py("map"), "packbuilder/map/build_profile.py"])


# ------------------------------------------------------------------ the OSM extracts
def osm_timestamp(pbf: Path) -> str:
    code = ("import osmium,sys; r=osmium.io.Reader(sys.argv[1], osmium.osm.osm_entity_bits.NOTHING); "
            "print(r.header().get('osmosis_replication_timestamp')); r.close()")
    out = subprocess.run([py("search"), "-c", code, str(pbf)], capture_output=True, text=True, check=True).stdout
    ts = out.strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}T", ts):
        raise SystemExit(f"{pbf.name}: no replication timestamp in its header ({ts!r})")
    return ts


def download(url: str, dest: Path, tries: int = 5) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    for attempt in range(1, tries + 1):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=120) as r, \
                    open(tmp, "wb") as f:
                shutil.copyfileobj(r, f, 1 << 20)
            tmp.replace(dest)
            return
        except OSError as e:
            print(f"   {dest.name}: attempt {attempt} failed ({type(e).__name__}); retrying", flush=True)
            time.sleep(10 * attempt)
    raise SystemExit(f"download failed: {url}")


def geofabrik_dir(name: str) -> str:
    """The folder of a state's files. Geofabrik moved the US states into one folder each
    (north-america/us/<state>/...; the flat north-america/us/<state>-latest... links answer 404 since
    November 2026); the flat layout is tried second in case it comes back."""
    base = CONFIG["geofabrik_base"]
    for d in (f"{base}/{name}", base):
        try:
            req = urllib.request.Request(f"{d}/{name}-latest.osm.pbf.md5", headers=UA, method="HEAD")
            with urllib.request.urlopen(req, timeout=60):
                return d
        except OSError:
            continue
    raise SystemExit(f"Geofabrik: no {name}-latest.osm.pbf.md5 under {base}/{name}/ or {base}/")


def fetch_extracts(run: dict) -> str:
    """This week's Geofabrik extract of every state (MD5 checked) and its .poly. Returns the snapshot
    YYMMDD (the date of the extracts' replication timestamp); every state must share it."""
    osm = DATA / "osm"
    t0 = time.time()
    got = {}
    for st in states():
        name = CONFIG["state_info"][st]["geofabrik"]
        base = geofabrik_dir(name)
        latest = osm / f"{name}-latest.osm.pbf"
        for attempt in range(3):
            download(f"{base}/{name}-latest.osm.pbf.md5", osm / f"{name}-latest.osm.pbf.md5")
            want = (osm / f"{name}-latest.osm.pbf.md5").read_text().split()[0].lower()
            if not (latest.exists() and md5_file(latest) == want):
                download(f"{base}/{name}-latest.osm.pbf", latest)
            if md5_file(latest) == want:
                break
            print(f"   {name}: MD5 differs (the file changed upstream?); again", flush=True)
        else:
            raise SystemExit(f"{name}: MD5 still differs after 3 tries")
        download(f"{base}/{name}.poly", osm / f"{name}.poly")
        ts = osm_timestamp(latest)
        got[st] = (name, ts, want)
    days = {ts[:10] for _, ts, _ in got.values()}
    if len(days) != 1:
        raise SystemExit(f"the extracts are of different days {sorted(days)}: Geofabrik is updating; run again later")
    day = days.pop()
    snap = day[2:4] + day[5:7] + day[8:10]
    for st, (name, ts, md5) in got.items():
        dated = osm / f"{name}-{snap}.osm.pbf"
        dated.unlink(missing_ok=True)
        os.replace(osm / f"{name}-latest.osm.pbf", dated)
        (osm / f"{name}-{snap}.osm.pbf.md5").write_text(f"{md5}  {dated.name}\n")
        (osm / f"{name}-latest.osm.pbf.md5").unlink(missing_ok=True)
        run.setdefault("extracts", {})[st] = {"file": dated.name, "md5": md5, "bytes": dated.stat().st_size,
                                              "replication_timestamp": ts}
    run["snapshot"], run["osm_snapshot"] = snap, day
    run.setdefault("steps", {})["fetch-extracts"] = {"wall_s": round(time.time() - t0, 1)}
    save_run(run)
    print(f"== extracts: {', '.join(f'{v[0]} {v[1]}' for v in got.values())} -> snapshot {snap}", flush=True)
    return snap


# ------------------------------------------------------------------ build
def base_env(run: dict) -> dict:
    return {"BEAMLINE_SNAPSHOT": run["snapshot"], "BEAMLINE_THREADS": str(CONFIG["threads"]),
            "BEAMLINE_JAVA_HEAP": CONFIG["java_heap"], "BEAMLINE_ROUTING_STATES": ",".join(states()),
            "PYTHONUTF8": "1"}


def addr_env(st: str, run: dict | None = None) -> dict:
    env = {"BEAMLINE_ADDR_STATE": st, "BEAMLINE_ADDR_DATA": f"data/addresses-{st}", "PYTHONUTF8": "1"}
    if run is not None:
        env.update(base_env(run))
    return env


def remove(*paths: Path) -> None:
    for p in paths:
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        elif p.exists():
            p.unlink()


def build_parts(run: dict, keep_work: bool) -> dict:
    env = base_env(run)
    snap = run["snapshot"]
    parts = {}
    step(run, "map-sources", [py("map"), "packbuilder/map/fetch_sources.py"], env)
    for st in states():
        step(run, f"map-{st}", [py("map"), "packbuilder/map/build_pack.py", st], env)
        if not keep_work:
            remove(DATA / "map/work/tmp", DATA / f"map/work/raw-v2/{st}-{snap}-z14.pmtiles")
    step(run, "routing", [py("routing"), "packbuilder/routing/build.py", "all"], env)
    if not keep_work:
        remove(DATA / "routing/build/valhalla_tiles", DATA / "routing/build/valhalla_tiles.tar",
               DATA / f"routing/osm/{'-'.join(states())}-{snap}.osm.pbf")
    for st in states():
        step(run, f"search-{st}", [py("search"), "-I", "packbuilder/search/build.py", "--state", st,
                                   "--out-dir", "data/search", "--work-dir", "data/search/work"], env)
        if not keep_work:
            remove(DATA / f"search/work/{st}-extract.pkl")
        steps_ = CONFIG["state_info"][st]["addresses_steps"]
        # osm, tiger and build are this week's work: the address steps skip a step whose metrics file
        # exists, so an earlier run's (left in a reused folder, e.g. a local or self-hosted run) would
        # be taken as done. The kept inputs (download, nad, extra) come with their own metrics.
        for m in ("osm", "tiger", "build"):
            remove(DATA / f"addresses-{st}" / "metrics" / f"{m}.json")
        step(run, f"addresses-{st}", [py("addresses"), "packbuilder/addresses/run.py", "--only", *steps_],
             addr_env(st, run))
        if not keep_work:
            remove(DATA / f"addresses-{st}/work/osm_{st}.sqlite", DATA / f"addresses-{st}/work/tiger_{st}.sqlite",
                   DATA / f"addresses-{st}/{st}-addresses.sqlite")
        parts[st] = {"map": f"data/map/packs-v2/{st}-{snap}-z14.pmtiles", "routing": f"data/routing/packs/{st}",
                     "search": f"data/search/{st}-search.sqlite",
                     "addresses": f"data/addresses-{st}/{st}-addresses-slim.sqlite"}
    return parts


def assemble(run: dict, parts: dict, keep_work: bool) -> None:
    created = datetime.strptime(run["started_utc"], "%Y-%m-%dT%H:%M:%SZ")
    created = created.replace(second=0)
    remove(OUT / "packs")
    for st, p in parts.items():
        step(run, f"assemble-{st}", [sys.executable, "packbuilder/pack/assemble.py", st, "--map", p["map"],
                                     "--routing", p["routing"], "--search", p["search"], "--addresses",
                                     p["addresses"], "--out", "data/out/packs", "--no-archive",
                                     "--created-utc", iso(created)])
        man = json.loads((OUT / "packs" / f"us-{st}" / "manifest.json").read_text(encoding="utf-8"))
        run.setdefault("packs", {})[st] = {
            "pack_id": man["pack_id"], "pack_version": man["pack_version"], "osm_snapshot": man["osm_snapshot"],
            "graph_build": man["parts"]["routing"]["graph_build"],
            "parts": {k: v["bytes"] for k, v in man["parts"].items()},
            "total_bytes": sum(v["bytes"] for v in man["parts"].values())}
        save_run(run)
        if not keep_work:  # the pack folder holds copies; the builders' outputs are not needed any more
            remove(REPO / p["map"], REPO / p["search"], REPO / p["addresses"])
            for f in (REPO / p["routing"]).glob("*.tar"):
                f.unlink()


# ------------------------------------------------------------------ checks
def search_summary(path: Path) -> dict:
    d = json.loads(path.read_text(encoding="utf-8"))
    n = sum(v["n"] for v in d["summary"].values())
    top5 = sum(v["top5"] for v in d["summary"].values())
    gaps = sum(v["gap"] for v in d["summary"].values())
    return {"cases": n, "top5": round(top5 / n, 4) if n else 0.0, "gaps": gaps,
            "top5_excl_gaps": round(top5 / (n - gaps), 4) if n > gaps else 0.0}


def previous_catalog(a) -> dict | None:
    if a.previous_catalog:
        p = Path(a.previous_catalog).expanduser()
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
    if a.store_release:  # on GitHub: the published catalog, if there is one
        # Only "there is no catalog yet" skips the size comparison; any other failure (network,
        # sign-in) stops the run, so an error never turns the safeguard off.
        r = subprocess.run(["gh", "release", "view", "catalog", "--json", "assets"], capture_output=True, text=True)
        if r.returncode != 0:
            if "release not found" in r.stderr.lower():
                return None
            raise SystemExit(f"gh release view catalog failed: {r.stderr.strip()[:300]}")
        if not any(x["name"] == "catalog.json" for x in json.loads(r.stdout)["assets"]):
            return None
        tmp = DATA / "store-tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        gh("release", "download", "catalog", "--pattern", "catalog.json", "--dir", str(tmp), "--clobber",
           capture=True)
        return json.loads((tmp / "catalog.json").read_text(encoding="utf-8"))
    return None


def checks(run: dict, a) -> None:
    results = {}
    ok = True
    prev = previous_catalog(a)
    for st in states():
        pack = OUT / "packs" / f"us-{st}"
        r = {}
        r["read_pack_verify"] = step(run, f"check-read-pack-{st}", [sys.executable, "tools/read_pack.py", str(pack),
                                                                    "--verify"], check=False) == 0
        for cases in CONFIG["state_info"][st]["search_cases"]:
            out = OUT / "checks" / f"search-{st}-{cases}"
            out.parent.mkdir(parents=True, exist_ok=True)
            step(run, f"check-search-{st}-{cases}", [sys.executable, "packbuilder/search/tests/run_tests.py",
                                                     "--db", str(pack / "search.sqlite"), "--addresses",
                                                     str(pack / "addresses.sqlite"), "--cases", cases,
                                                     "--json", str(out)], check=False)
            s = search_summary(out) if out.exists() else {"error": "no result"}
            lim = CONFIG["search_check"][cases]
            s["ok"] = s.get("top5", 0) >= lim["min_top5"] and s.get("top5_excl_gaps", 0) >= lim["min_top5_excl_gaps"]
            r[f"search {cases}"] = s
        r["search_inputs"] = step(run, f"check-search-inputs-{st}", [sys.executable,
                                                                    "packbuilder/search/tests/check_inputs.py",
                                                                    "--db", str(pack / "search.sqlite")],
                                  check=False) == 0
        out = OUT / "checks" / f"routes-{st}.json"
        step(run, f"check-routes-{st}", [py("routing"), "build/check_routes.py", str(pack), st, "--out", str(out)],
             check=False)
        r["routes"] = json.loads(out.read_text(encoding="utf-8"))["summary"] if out.exists() else {"ok": False}
        # not much smaller than the published pack of this state
        shrink = {"ok": True, "note": "no published catalog: skipped"}
        entry = next((p for p in (prev or {}).get("packs", []) if p.get("id") == f"us-{st}"), None)
        if entry:
            new = run["packs"][st]["parts"]
            old = {p["name"]: p["bytes"] for p in entry["parts"]}
            lim = CONFIG["shrink_check"]
            ratios = {k: round(new.get(k, 0) / v, 4) for k, v in old.items() if v}
            total = round(sum(new.values()) / max(1, sum(old.values())), 4)
            low = {k: v for k, v in ratios.items() if v < lim["min_part_ratio"]}
            shrink = {"ok": total >= lim["min_total_ratio"] and not low, "against": entry["pack_version"],
                      "total_ratio": total, "part_ratios": ratios}
            if a.allow_smaller and not shrink["ok"]:
                shrink.update(ok=True, note="smaller than the published pack, allowed by --allow-smaller")
        r["not_much_smaller"] = shrink
        st_ok = (r["read_pack_verify"] and r["search_inputs"] and r["routes"].get("ok")
                 and shrink["ok"] and all(v["ok"] for k, v in r.items() if k.startswith("search ")))
        r["ok"] = bool(st_ok)
        ok &= r["ok"]
        results[st] = r
    run["checks"] = results
    run["checks_passed"] = bool(ok)
    save_run(run)
    print(json.dumps(results, indent=1))
    if not ok:
        raise SystemExit("a check failed: nothing will be published")


def build_site(run: dict) -> None:
    site = OUT / "site"
    remove(site)
    args = [sys.executable, "packbuilder/catalog/build_catalog.py", "--out", "data/out/site", "--check"]
    for st in states():
        args += ["--pack", f"data/out/packs/us-{st}"]
    step(run, "catalog", args)
    cat = json.loads((site / "catalog" / "catalog.json").read_text(encoding="utf-8"))
    run["catalog"] = {"serial": cat["serial"], "packs": [
        {"id": p["id"], "pack_version": p["pack_version"], "osm_snapshot": p["osm_snapshot"],
         "transfer_bytes": sum(q["transfer"]["bytes"] for q in p["parts"]) + p["manifest"]["bytes"],
         "files": [p["manifest"]["path"]] + [q["transfer"]["path"] for q in p["parts"]]} for p in cat["packs"]]}
    save_run(run)


def write_summary(run: dict) -> None:
    lines = [f"## Weekly pack build {run.get('started_utc', '')}", ""]
    lines.append(f"OSM snapshot {run.get('osm_snapshot', '?')}; states {', '.join(states())}; "
                 f"checks {'passed' if run.get('checks_passed') else 'FAILED or not run'}.")
    lines += ["", "| Step | Wall (s) | Peak memory (MiB) | Extra disk at peak (MiB) |", "|---|---:|---:|---:|"]
    for k, v in run.get("steps", {}).items():
        lines.append(f"| {k} | {v.get('wall_s', '')} | {v.get('peak_rss_mb', '')} | {v.get('disk_peak_extra_mb', '')} |")
    for st, p in run.get("packs", {}).items():
        lines += ["", f"**{p['pack_id']}-{p['pack_version']}** ({p['osm_snapshot']}, routing {p['graph_build']}): "
                  + ", ".join(f"{k} {v / 1e6:.1f} MB" for k, v in p["parts"].items())]
        chk = run.get("checks", {}).get(st, {}).get("not_much_smaller", {})
        if chk.get("total_ratio"):
            lines.append(f"Size against the published build {chk['against']}: x{chk['total_ratio']} "
                         f"(parts {chk['part_ratios']}).")
    text = "\n".join(lines) + "\n"
    (OUT / "summary.md").write_text(text, encoding="utf-8")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(text)


def cmd_run(a) -> None:
    store = Store(a.store, a.store_release)
    remove(OUT)
    started = now_utc()
    run = {"format": "beamline-weekly-run/1", "started_utc": iso(started), "states": states(),
           "checks_passed": False}
    save_run(run)
    idx = restore_inputs(run, store)
    fetch_extracts(run)
    refresh_inputs(run, idx)
    parts = build_parts(run, a.keep_work)
    assemble(run, parts, a.keep_work)
    try:
        checks(run, a)
        build_site(run)
    finally:
        run["finished_utc"] = iso(now_utc())
        run["wall_s"] = round(time.time() - started.timestamp())
        run["data_mb_end"] = round(tree_bytes(DATA) / 2**20)
        save_run(run)
        write_summary(run)
    print(f"== done in {run['wall_s']} s; site data/out/site", flush=True)


# ------------------------------------------------------------------ publish
LICENCE_NOTES = """Map data © OpenStreetMap contributors, available under the Open Database License (ODbL 1.0): \
https://www.openstreetmap.org/copyright. House numbers also from the U.S. Census Bureau TIGER/Line files and the \
USDOT National Address Database (public domain), and the City of Scranton GIS (PDDL 1.0). See ATTRIBUTION.md in \
this repository."""


def release_notes(entry: dict) -> str:
    sizes = ", ".join(f"{p['name']} {p['bytes'] / 1e6:.1f} MB" for p in entry["parts"])
    return (f"{entry['name']} pack {entry['pack_version']}, OpenStreetMap data of {entry['osm_snapshot']}.\n\n"
            f"Parts as installed: {sizes}. The app reads them through the catalog (release 'catalog'); "
            f"file formats: README.\n\n{LICENCE_NOTES}\n")


def gh(*args: str, capture: bool = False) -> str:
    r = subprocess.run(["gh", *args], capture_output=capture, text=True)
    if r.returncode != 0:
        raise SystemExit(f"gh {' '.join(args[:3])} failed" + (f": {r.stderr.strip()[:300]}" if capture else ""))
    return r.stdout if capture else ""


def release_files(entry: dict) -> list[Path]:
    site = OUT / "site" / "catalog"
    return [(site / p).resolve() for p in [entry["manifest"]["path"]] + [q["transfer"]["path"] for q in entry["parts"]]]


def cmd_publish(a) -> None:
    run = load_run()
    if not run.get("checks_passed"):
        raise SystemExit("the last run's checks did not all pass (data/out/run.json): nothing is published")
    cat_path = OUT / "site" / "catalog" / "catalog.json"
    cat = json.loads(cat_path.read_text(encoding="utf-8"))
    keep = int(CONFIG["keep_builds"])
    published = []
    if a.github:
        existing = {r["tagName"]: r for r in json.loads(gh("release", "list", "--limit", "500", "--json",
                                                           "tagName,isDraft", capture=True))}
        for e in cat["packs"]:
            tag = f"{e['id']}-{e['pack_version']}"
            files = release_files(e)
            if tag in existing and not existing[tag]["isDraft"]:
                raise SystemExit(f"release {tag} exists already: a published build is never rebuilt")
            if tag in existing:
                gh("release", "delete", tag, "--yes")
            notes = OUT / f"notes-{tag}.md"
            notes.write_text(release_notes(e), encoding="utf-8")
            gh("release", "create", tag, "--draft", "--title", f"{e['name']} {e['pack_version']}",
               "--notes-file", str(notes), "--target", os.environ.get("GITHUB_SHA", "main"))
            gh("release", "upload", tag, *map(str, files))
            assets = {x["name"]: x["size"] for x in json.loads(gh("release", "view", tag, "--json", "assets",
                                                                  capture=True))["assets"]}
            for f in files:
                if assets.get(f.name) != f.stat().st_size:
                    raise SystemExit(f"{tag}: {f.name} uploaded as {assets.get(f.name)} bytes, expected "
                                     f"{f.stat().st_size}")
            gh("release", "edit", tag, "--draft=false")
            published.append(tag)
        # the catalog last: a phone never sees a catalog naming files not yet uploaded
        try:
            if subprocess.run(["gh", "release", "view", "catalog"], capture_output=True).returncode != 0:
                gh("release", "create", "catalog", "--title", "Catalog", "--notes",
                   "catalog.json: the list of the newest pack of every state, read by the app "
                   "(format beamline-catalog/1, README). " + LICENCE_NOTES, "--target",
                   os.environ.get("GITHUB_SHA", "main"))
            gh("release", "upload", "catalog", str(cat_path), "--clobber")
        except SystemExit:
            # No catalog names this run's releases: back to drafts, which cleanup-drafts removes, so
            # they never count among the newest builds that pruning keeps.
            for tag in published:
                subprocess.run(["gh", "release", "edit", tag, "--draft=true"])
            raise
        # builds older than the newest `keep` of each state
        tags = [r["tagName"] for r in json.loads(gh("release", "list", "--limit", "500", "--json",
                                                     "tagName,isDraft", capture=True)) if not r["isDraft"]]
        deleted = prune_list(tags, keep, cat)
        for t in deleted:
            gh("release", "delete", t, "--yes", "--cleanup-tag")
        store = Store(a.store, a.store_release)
    else:
        dest = Path(a.local).expanduser().resolve()
        for e in cat["packs"]:
            tag = f"{e['id']}-{e['pack_version']}"
            d = dest / tag
            if d.exists():
                raise SystemExit(f"{d} exists already: a published build is never rebuilt")
            tmp = dest / f".{tag}.part"
            remove(tmp)
            tmp.mkdir(parents=True)
            for f in release_files(e):
                shutil.copyfile(f, tmp / f.name)
            (tmp / "RELEASE-NOTES.md").write_text(release_notes(e), encoding="utf-8")
            tmp.rename(d)
            published.append(tag)
        (dest / "catalog").mkdir(parents=True, exist_ok=True)
        tmp = dest / "catalog" / "catalog.json.part"
        shutil.copyfile(cat_path, tmp)
        tmp.replace(dest / "catalog" / "catalog.json")
        deleted = prune_list([p.name for p in dest.iterdir() if p.is_dir()], keep, cat)
        for t in deleted:
            shutil.rmtree(dest / t)
        store = Store(a.store, a.store_release)
    saved = save_inputs(store)
    run["published"] = {"releases": published, "catalog_serial": cat["serial"], "deleted": deleted,
                        "inputs_saved": saved, "utc": iso(now_utc())}
    save_run(run)
    print(json.dumps(run["published"], indent=1))


def prune_list(tags: list[str], keep: int, cat: dict) -> list[str]:
    """Pack release tags beyond the newest `keep` of each pack id; never one the catalog names."""
    named = {f"{p['id']}-{p['pack_version']}" for p in cat["packs"]}
    by_id: dict[str, list[tuple[int, str]]] = {}
    for t in tags:
        m = PACK_TAG.match(t)
        if m:
            by_id.setdefault(m.group(1), []).append((int(m.group(2)), t))
    out = []
    for lst in by_id.values():
        lst.sort(reverse=True)
        out += [t for _, t in lst[keep:] if t not in named]
    return sorted(out)


def cmd_cleanup_drafts(_a) -> None:
    rels = json.loads(gh("release", "list", "--limit", "500", "--json", "tagName,isDraft", capture=True))
    for r in rels:
        if r["isDraft"] and PACK_TAG.match(r["tagName"]):
            gh("release", "delete", r["tagName"], "--yes")
            print(f"deleted the draft {r['tagName']}")


def main() -> None:
    if sys.platform != "linux":
        raise SystemExit("the weekly build runs on Linux (Ubuntu 24.04)")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup")
    r = sub.add_parser("run")
    p = sub.add_parser("publish")
    for x in (r, p):
        g = x.add_mutually_exclusive_group(required=True)
        g.add_argument("--store", help="a folder holding the kept inputs")
        g.add_argument("--store-release", help="a GitHub release holding the kept inputs (e.g. inputs)")
    r.add_argument("--previous-catalog", help="the published catalog.json to compare sizes with (a file)")
    r.add_argument("--allow-smaller", action="store_true", help="pass the 'not much smaller' check")
    r.add_argument("--keep-work", action="store_true", help="keep the builders' work files (more disk)")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--github", action="store_true")
    g.add_argument("--local", help="a folder laid out like GitHub Releases")
    sub.add_parser("cleanup-drafts")
    a = ap.parse_args()
    {"setup": cmd_setup, "run": cmd_run, "publish": cmd_publish, "cleanup-drafts": cmd_cleanup_drafts}[a.cmd](a)


if __name__ == "__main__":
    main()
