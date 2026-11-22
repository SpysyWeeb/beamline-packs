#!/usr/bin/env python3
"""The weekly build of Beamline's state packs: one entry point for the workflow and for a local run.

    python3 build/weekly.py setup                    the Python environments and the map tools (idempotent)
    python3 build/weekly.py run   --store DIR | --store-release TAG   [--previous-catalog FILE]
                                  [--allow-smaller] [--keep-work]
    python3 build/weekly.py publish --github | --local DIR   (--store DIR | --store-release TAG)
    python3 build/weekly.py cleanup-drafts            delete unpublished (draft) pack releases (after a failed publish)
    python3 build/weekly.py comma-status              exit 1 when the last run's comma side failed

run      Restores the slower-changing inputs from the store, fetches this week's Geofabrik extract of
         every state of build/config.json "states" (checked against its MD5; when the latest extract
         cannot be downloaded or never matches its .md5, the newest dated extract of the same folder
         that matches its own .md5 and whose map date is at most config "extract_fallback"
         "max_age_days" old, said in the log and in run.json "extracts"), refreshes an input whose
         publisher has a newer file (config "inputs"), builds the map, routing, search and house-number
         parts, assembles each state's pack (format 3), checks it (read_pack.py --verify, the search
         tests, the test routes, not much smaller than the published pack) and writes the catalog site
         (packbuilder/catalog/build_catalog.py --check) into data/out/site. Nothing is published.
         The comma side, kept apart: the pack's three comma-only parts (packbuilder/comma:
         search-code.tar, the frozen release of the search code with this week's ranking.json and
         abbreviations.json; roads.tar; mapd.tar; into data/out/comma/us-<state>), their check (the
         search code answering known queries from this week's search file in a separate Python,
         Mapd's own admission) and comma.json (packbuilder/comma/catalog.py --check). Every comma step
         (Mapd's setup included) has a time limit, config "comma" "step_time_limit_s" (30 minutes):
         past it the step's whole process group is killed and the step counts as failed. Its result
         is run.json "comma" (ok, or the stage that failed and why; comma.json's size and SHA-256). A
         failure there never stops the phone's side: the later comma steps are skipped, comma.json
         is not left in the site, and run ends normally with a plain message.
publish  Only after a run whose checks all passed (data/out/run.json "checks_passed"), and only when
         data/out/site/catalog/catalog.json is the file the run checked (its size and SHA-256 in
         run.json "catalog") and every file it names has the size and SHA-256 it gives: checked before
         anything is uploaded, else nothing is published. --github: one
         release per pack build (created as a draft, every file uploaded and compared, then
         published), then catalog.json and comma.json of the release tagged "catalog" replaced, then
         pack releases older than the newest config "keep_builds" of each state deleted (never one
         that catalog.json or comma.json names), then the changed inputs saved to the store. Uses the
         GitHub CLI with the workflow's GITHUB_TOKEN. --local DIR: the same into a folder laid out like
         GitHub Releases (DIR/<tag>/..., DIR/catalog/catalog.json and comma.json).
         The comma files and comma.json go with them only when run.json records the comma side as
         passed, comma.json is the file the run checked, its serial is a real UTC time at most two
         days ahead of this clock and every file it names has the size and SHA-256 it gives.
         Otherwise the phone's side is published exactly as if there were no comma
         side, the published comma.json stays untouched, no build it names is deleted, and publish
         ends as failed (exit 1) after publishing.

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
import signal
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
COMMA = OUT / "comma"                        # the comma-only parts of each pack: data/out/comma/us-<state>/
COMMA_DATA = DATA / "comma"
MAPD_TOOL = COMMA_DATA / "mapd-tool"         # Mapd's program, built by setup from StarPilot's pinned source
COMMA_SETUP = COMMA_DATA / "setup.json"      # setup's record of Mapd's build (run clears data/out, not this)
COMMA_FORMAT = "beamline-comma-catalog/1"
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
        try:
            entries = os.listdir("/proc")
        except OSError:  # no /proc (a test on another system): nothing to measure
            return [self.pid]
        for d in entries:
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


TIMED_OUT = 124   # a step's exit code when it ran past its time limit (as coreutils' timeout gives)


def kill_group(proc: subprocess.Popen) -> None:
    """The step's whole process group (its own session: step's start_new_session), so nothing it started
    (git, go, a compiler) is left running; only the process itself where there are no process groups."""
    if hasattr(os, "killpg"):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    else:
        proc.kill()


def step(run: dict, name: str, cmd: list, env: dict | None = None, check: bool = True,
         time_limit_s: float | None = None) -> int:
    """Run one build step: output to data/out/logs/<name>.log, wall time, peak memory and disk recorded.
    With time_limit_s the step runs in a process group of its own; past the limit the whole group is
    killed and the step counts as failed (exit TIMED_OUT, "timed_out" in run.json, a line in its log)."""
    LOGS.mkdir(parents=True, exist_ok=True)
    log = LOGS / f"{name}.log"
    free0 = shutil.disk_usage(REPO).free
    t0 = time.time()
    timed_out = False
    print(f"== {name}" + (f" (time limit {time_limit_s:g} s)" if time_limit_s else ""), flush=True)
    with open(log, "w", encoding="utf-8", errors="replace") as lf:
        lf.write("$ " + " ".join(rel(Path(c)) if os.path.isabs(str(c)) else str(c) for c in cmd) + "\n")
        lf.flush()
        proc = subprocess.Popen([str(c) for c in cmd], stdout=lf, stderr=subprocess.STDOUT, cwd=REPO,
                                env={**os.environ, **(env or {})}, start_new_session=time_limit_s is not None)
        s = Sampler(proc.pid)
        try:
            rc = proc.wait(timeout=time_limit_s)
        except subprocess.TimeoutExpired:
            kill_group(proc)
            proc.wait()
            rc, timed_out = TIMED_OUT, True
            lf.write(f"\n== {name} ran past its time limit of {time_limit_s:g} s: its processes were killed\n")
        except BaseException:  # interrupted: a step in a session of its own gets no signal of the terminal
            if time_limit_s is not None:
                kill_group(proc)
                proc.wait()
            raise
        finally:
            s.done()
    rec = {"wall_s": round(time.time() - t0, 1), "peak_rss_mb": round(s.peak_rss / 2**20),
           "disk_peak_extra_mb": round(max(0, free0 - s.min_free) / 2**20),
           "data_mb_after": round(tree_bytes(DATA) / 2**20), "returncode": rc}
    if timed_out:
        rec["timed_out"] = True
        rec["time_limit_s"] = time_limit_s
    run.setdefault("steps", {})[name] = rec
    save_run(run)
    print(f"   {rec['wall_s']} s, peak {rec['peak_rss_mb']} MiB, data/ {rec['data_mb_after']} MiB, exit {rc}"
          + (f" (KILLED: past its time limit of {time_limit_s:g} s)" if timed_out else ""), flush=True)
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


def store_delete(store: Store, name: str) -> None:
    """One asset out of the store (an older kept extract)."""
    if store.folder:
        (store.folder / name).unlink(missing_ok=True)
        return
    subprocess.run(["gh", "release", "delete-asset", store.release, name, "--yes"], check=True)


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
    setup_comma(run)


def setup_comma(run: dict) -> bool:
    """Mapd's generator (the comma's speed-limit map): Go and StarPilot's source at the commit
    packbuilder/comma/pins.json pins (a C compiler, pkg-config and zlib's developer files:
    setup_ubuntu.sh). A failure here never stops the setup: it is recorded in data/comma/setup.json
    (with the step's log beside it, as run clears data/out), and run then leaves the comma side out.
    Like every comma step it has the comma time limit (a stalled fetch is killed and counts as failed)."""
    rec = {"utc": iso(now_utc()), "ok": False}
    try:
        why = comma_step(run, "setup-mapd", [py("map"), "packbuilder/comma/mapd.py", "setup", "--dest", str(MAPD_TOOL)])
        rec["ok"] = why is None and (MAPD_TOOL / "mapd").is_file()
        if not rec["ok"]:
            rec["error"] = f"packbuilder/comma/mapd.py setup: {why or 'it built no mapd program'}"
    except (Exception, SystemExit) as e:  # noqa: BLE001 - nothing on the comma side may stop the phone's packs
        rec["error"] = f"{type(e).__name__}: {e}"
    COMMA_DATA.mkdir(parents=True, exist_ok=True)
    log = LOGS / "setup-mapd.log"
    if not rec["ok"] and log.is_file():
        shutil.copyfile(log, COMMA_DATA / "setup-mapd.log")
        rec["log"] = "data/comma/setup-mapd.log"
    tmp = COMMA_SETUP.with_suffix(".tmp")
    tmp.write_text(json.dumps(rec, indent=1) + "\n", encoding="utf-8")
    tmp.replace(COMMA_SETUP)
    if not rec["ok"]:
        if log.is_file():  # the log itself is not among a failed run's kept files unless the build got that far
            tail = log.read_text(encoding="utf-8", errors="replace").splitlines()[-40:]
            print("   the end of setup-mapd.log:", flush=True)
            for line in tail:
                print("   | " + line[:300], flush=True)
        print(f"== COMMA SIDE: the setup of Mapd's generator failed ({rec['error']}). The phone's packs are built "
              f"and published as usual; this week's comma files and comma.json are not (the published comma.json "
              f"stays), and the run ends as failed after publishing.", flush=True)
    return rec["ok"]


# ------------------------------------------------------------------ the comma side, kept apart from the phone's
# Every comma step (Mapd's setup, the three parts, their check, comma.json) runs through comma_stage:
# its failure is recorded in run.json "comma" and the later comma steps are skipped, while the phone's
# packs and catalog.json are built, checked and published exactly as if there were no comma side.
# publish then leaves the published comma.json as it is and ends as failed (after publishing).
def comma_state(run: dict) -> dict:
    return run.setdefault("comma", {"ok": None})


def comma_time_limit() -> int:
    """The time limit of every comma step (config "comma" "step_time_limit_s"; 30 minutes): a stalled git
    fetch or Go module download must not use up the job's time and so hold back the phone's packs."""
    return int(CONFIG.get("comma", {}).get("step_time_limit_s", 1800))


def comma_step(run: dict, name: str, cmd: list) -> str | None:
    """One comma command through step() under the comma time limit: None when it passed, else why not
    (its exit, or that it ran past the limit and was killed with everything it started)."""
    limit = comma_time_limit()
    rc = step(run, name, cmd, check=False, time_limit_s=limit)
    if rc == 0:
        return None
    if (run.get("steps", {}).get(name) or {}).get("timed_out"):
        return f"{name} ran past its time limit of {limit} s and was stopped; log data/out/logs/{name}.log"
    return f"{name} failed (exit {rc}); log data/out/logs/{name}.log"


def comma_fail(run: dict, stage: str, why: str) -> None:
    c = comma_state(run)
    c["ok"] = False
    c.setdefault("failed", {"stage": stage, "why": why})
    save_run(run)
    print(f"== COMMA SIDE FAILED ({stage}): {why}\n   The phone's packs and catalog.json go on as if there were no "
          f"comma side; this week's comma.json is not published (the published one stays) and the run ends as "
          f"failed after publishing.", flush=True)


def comma_stage(run: dict, stage: str, fn) -> bool:
    """One comma step: fn() returns None when it passed, else why not. Skipped once the comma side has
    failed; no error of it ever reaches the phone's side. True when it ran and passed."""
    c = comma_state(run)
    if c.get("ok") is False:
        c.setdefault("skipped", []).append(stage)
        save_run(run)
        print(f"== comma {stage}: skipped (the comma side failed: {(c.get('failed') or {}).get('stage', '?')})",
              flush=True)
        return False
    try:
        why = fn()
    except (Exception, SystemExit) as e:  # noqa: BLE001 - nothing on the comma side may stop the phone's packs
        why = f"{type(e).__name__}: {e}"
    if why:
        comma_fail(run, stage, str(why))
        # A failed comma step leaves nothing behind that the phone's steps after it must share the disk
        # with (Mapd's work folder is gigabytes; a hosted runner has little room). The logs stay.
        try:
            remove(COMMA_DATA / "work", COMMA)
        except OSError as e:
            print(f"== comma {stage}: could not clear the comma work folders ({e})", flush=True)
        return False
    return True


def comma_setup_problem() -> str | None:
    """Why Mapd's generator is not ready (setup's record, data/comma/setup.json); None when it is."""
    if not COMMA_SETUP.is_file():
        return "Mapd's generator was not set up (weekly.py setup records it in data/comma/setup.json)"
    rec = json.loads(COMMA_SETUP.read_text(encoding="utf-8"))
    if not rec.get("ok"):
        saved = COMMA_DATA / "setup-mapd.log"
        if saved.is_file():  # into this run's logs, which a failed workflow run keeps
            LOGS.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(saved, LOGS / "setup-mapd.log")
        return f"the setup of Mapd's generator failed: {rec.get('error', 'no reason recorded')}"
    if not (MAPD_TOOL / "mapd").is_file():
        return "Mapd's program is missing from data/comma/mapd-tool"
    return None


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


GEOFABRIK_PAUSES_S = (30, 90, 240)   # between the tries to find a state's folder


def url_exists(url: str) -> bool:
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA, method="HEAD"), timeout=60):
            return True
    except OSError as e:
        if getattr(e, "code", None) != 404:  # "not there" is an answer; anything else is worth a line in the log
            print(f"   {url}: {type(e).__name__}: {e}", flush=True)
        return False


def fetch_text(url: str) -> str:
    """A small text page (a folder listing): at most 4 MiB."""
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r:
        return r.read(4 << 20).decode("utf-8", "replace")


def dated_extracts(folder: str, name: str) -> list[str]:
    """The dated extracts "<name>-YYMMDD.osm.pbf" Geofabrik's listing of the folder links to, newest
    first (the listing's own links are only read for their file names; every download is made from
    the folder's address). [] when the listing cannot be read."""
    try:
        page = fetch_text(folder + "/")
    except OSError as e:
        print(f"   {folder}/: the listing cannot be read ({type(e).__name__}: {e})", flush=True)
        return []
    rule = re.compile(rf"^{re.escape(name)}-(\d{{6}})\.osm\.pbf$")
    found = set()
    for href in re.findall(r"""href\s*=\s*["']([^"'<>]+)["']""", page):
        last = href.split("?", 1)[0].split("#", 1)[0].rstrip("/").rsplit("/", 1)[-1]
        if rule.match(last):
            found.add(last)
    return sorted(found, key=lambda f: f[len(name) + 1:len(name) + 7], reverse=True)


def geofabrik_dir(name: str) -> str:
    """The folder of a state's files. Geofabrik moved the US states into one folder each
    (north-america/us/<state>/...; the flat north-america/us/<state>-latest... links answer 404 since
    November 2026); the flat layout is tried second in case it comes back. A folder without the
    latest extract's .md5 is still taken when its listing has dated extracts (fetch_extracts' fallback)."""
    base = CONFIG["geofabrik_base"]
    folders = (f"{base}/{name}", base)
    # Neither answer may only mean that the server did not answer for a moment (it happened): asked again
    # after each pause of GEOFABRIK_PAUSES_S before the run gives up.
    for pause in (*GEOFABRIK_PAUSES_S, None):
        for d in folders:
            if url_exists(f"{d}/{name}-latest.osm.pbf.md5"):
                return d
        for d in folders:
            if dated_extracts(d, name):
                print(f"   Geofabrik: no {name}-latest.osm.pbf.md5 under {d}/, but dated extracts are listed there",
                      flush=True)
                return d
        if pause is not None:
            print(f"   Geofabrik: no answer for {name} under {base}/; asking again in {pause} s", flush=True)
            time.sleep(pause)
    raise SystemExit(f"Geofabrik: no {name}-latest.osm.pbf.md5 and no dated extract under {base}/{name}/ or {base}/")


def md5_of(md5_path: Path) -> str:
    """The MD5 a Geofabrik .md5 file gives ("<32 hex>  <file name>")."""
    words = md5_path.read_text(encoding="utf-8", errors="replace").split()
    if not words or not re.fullmatch(r"[0-9a-fA-F]{32}", words[0]):
        raise SystemExit(f"{md5_path.name} holds no MD5")
    return words[0].lower()


def fetch_checked(url: str, dest: Path, tries: int = 3) -> str:
    """url's file into dest, matching its own url.md5 (fetched again when it differs: the file may
    have changed upstream). Returns the MD5; SystemExit when it cannot be downloaded or never matches."""
    md5p = dest.with_name(dest.name + ".md5")
    for _ in range(tries):
        download(url + ".md5", md5p)
        want = md5_of(md5p)
        if not (dest.exists() and md5_file(dest) == want):
            download(url, dest)
        if md5_file(dest) == want:
            return want
        print(f"   {dest.name}: MD5 differs from its .md5 (the file changed upstream?); again", flush=True)
    raise SystemExit(f"{url.rsplit('/', 1)[-1]}: its MD5 still differs from its .md5 after {tries} tries")


def header_time(ts: str) -> datetime:
    return datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)


def fetch_dated(osm: Path, base: str, name: str) -> tuple[Path, str, str, list[str]]:
    """The fallback when the latest extract cannot be used: the newest dated extract of the folder that
    downloads, matches its own .md5 and whose map date (its header) is at most config
    "extract_fallback" "max_age_days" old (default 10). Returns (file, md5, header time, what was
    tried before it); SystemExit naming every file tried when none qualifies."""
    limit = float(CONFIG.get("extract_fallback", {}).get("max_age_days", 10))
    now = now_utc()
    tried = []
    for fname in dated_extracts(base, name):
        named = datetime.strptime(fname[len(name) + 1:len(name) + 7], "%y%m%d").replace(tzinfo=timezone.utc)
        if (now - named).total_seconds() > (limit + 1) * 86400:  # by its name already too old: so are the rest
            tried.append(f"{fname} and older (named more than {limit:g} days ago)")
            break
        dest = osm / fname
        try:
            md5 = fetch_checked(f"{base}/{fname}", dest)
            ts = osm_timestamp(dest)
        except SystemExit as e:
            tried.append(f"{fname} ({e})")
            remove(dest, dest.with_name(dest.name + ".part"))
            continue
        age = (now - header_time(ts)).total_seconds() / 86400
        if age > limit:
            tried.append(f"{fname} (map date {ts}, {age:.1f} days old, over the limit of {limit:g})")
            remove(dest)
            continue
        return dest, md5, ts, tried
    raise SystemExit(f"{name}: no dated extract of {base}/ qualifies either (tried: {'; '.join(tried) or 'none listed'})")


def fetch_from_geofabrik(osm: Path, name: str) -> tuple[Path, str, dict]:
    """One state's extract and .poly from Geofabrik: the latest, else the newest dated one that qualifies.
    Returns (file, md5, what run.json records about its source); SystemExit when Geofabrik gives neither."""
    base = geofabrik_dir(name)
    latest = osm / f"{name}-latest.osm.pbf"
    try:
        md5 = fetch_checked(f"{base}/{name}-latest.osm.pbf", latest)
        try:
            osm_timestamp(latest)  # a file that matches its .md5 but has no readable map date is no use either
        except subprocess.CalledProcessError as e:
            raise SystemExit(f"{latest.name}: its header cannot be read ({e})") from None
        path, rec = latest, {"source": "latest", "source_file": latest.name}
    except SystemExit as e:
        reason = str(e)
        print(f"== {name}: the latest extract cannot be used ({reason}); looking for the newest dated extract "
              f"of {base}/", flush=True)
        remove(latest, latest.with_name(latest.name + ".part"), latest.with_name(latest.name + ".md5"))
        try:
            path, md5, ts, tried = fetch_dated(osm, base, name)
        except SystemExit as e2:
            raise SystemExit(f"{name}: the latest extract cannot be used ({reason}), and {e2}") from None
        rec = {"source": "dated", "source_file": path.name, "fallback_reason": reason}
        if tried:
            rec["fallback_skipped"] = tried
        print(f"== {name}: USING {path.name} (map date {ts}) instead of {latest.name}, because: {reason}"
              + (f"; skipped first: {'; '.join(tried)}" if tried else ""), flush=True)
    download(f"{base}/{name}.poly", osm / f"{name}.poly")
    return path, md5, rec


def kept_names(names: set[str], name: str) -> list[str]:
    """The extracts of a state the store keeps ("extract-<name>-YYMMDD.osm.pbf"), newest first."""
    rule = re.compile(rf"^extract-{re.escape(name)}-(\d{{6}})\.osm\.pbf$")
    return sorted((n for n in names if rule.match(n)), reverse=True)


def kept_extract(store: Store | None, osm: Path, name: str) -> tuple[Path, str, str]:
    """The newest extract of the state the store keeps (save_extracts, after a published run), with its
    .md5 and the state's .poly: it must match that .md5 and its map date (its header) must be at most
    config "extract_fallback" "max_age_days" old. Returns (file, md5, header time)."""
    if store is None:
        raise SystemExit(f"{name}: there is no store to look in")
    limit = float(CONFIG.get("extract_fallback", {}).get("max_age_days", 10))
    names = store.names()
    poly = f"extract-{name}.poly"
    tried = []
    for asset in kept_names(names, name):
        if asset + ".md5" not in names or poly not in names:
            tried.append(f"{asset} (its .md5 or {poly} is not kept)")
            continue
        dest = osm / asset[len("extract-"):]
        md5p = dest.with_name(dest.name + ".md5")
        store.get(asset + ".md5", md5p)
        md5 = md5_of(md5p)
        if not (dest.exists() and md5_file(dest) == md5):
            store.get(asset, dest)
        try:
            if md5_file(dest) != md5:
                raise SystemExit("its MD5 differs from its .md5")
            ts = osm_timestamp(dest)
        except (SystemExit, subprocess.CalledProcessError) as e:
            tried.append(f"{asset} ({e})")
            remove(dest, md5p)
            continue
        age = (now_utc() - header_time(ts)).total_seconds() / 86400
        if age > limit:
            tried.append(f"{asset} (map date {ts}, {age:.1f} days old, over the limit of {limit:g})")
            remove(dest, md5p)
            continue
        store.get(poly, osm / f"{name}.poly")
        return dest, md5, ts
    raise SystemExit(f"{name}: the store keeps no usable extract (tried: {'; '.join(tried) or 'none kept'})")


def save_extracts(store: Store, run: dict) -> list[str]:
    """After a published run: each state's extract, its .md5 and its .poly are kept in the store (one
    extract per state: older ones are dropped), for a week when Geofabrik cannot be reached (kept_extract).
    An extract that came from the store is there already. Returns the names saved."""
    osm = DATA / "osm"
    names = store.names()
    saved = []
    for st, e in sorted((run.get("extracts") or {}).items()):
        name = CONFIG["state_info"][st]["geofabrik"]
        asset = "extract-" + e["file"]
        src, poly = osm / e["file"], osm / f"{name}.poly"
        if e.get("source") == "store" or (asset in names and asset + ".md5" in names):
            continue
        if not (src.is_file() and poly.is_file()):
            print(f"== {name}: the extract is not kept in the store ({src.name} or {poly.name} is gone)", flush=True)
            continue
        tmp = DATA / "store-tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        md5p = tmp / (asset + ".md5")
        md5p.write_text(f"{e['md5']}  {e['file']}\n", encoding="utf-8")
        store.put(asset, src)
        store.put(asset + ".md5", md5p)
        store.put(f"extract-{name}.poly", poly)
        md5p.unlink(missing_ok=True)
        for old in kept_names(names, name):
            if old != asset:
                for n in (old, old + ".md5"):
                    if n in names:
                        store_delete(store, n)
        saved.append(asset)
    if saved:
        print(f"== extracts kept in the store: {saved}", flush=True)
    return saved


def fetch_extracts(run: dict, store: Store | None = None) -> str:
    """This week's Geofabrik extract of every state (MD5 checked) and its .poly. When the latest extract
    cannot be downloaded (after download's retries) or never matches its .md5, the newest dated extract
    of the same folder that qualifies is used instead (fetch_dated); when Geofabrik gives nothing usable
    at all, the extract the store keeps from the last published run (kept_extract). Which one, and why,
    is said plainly in the log and in run.json ("extracts" of the state: "source", "source_file",
    "fallback_reason"). Returns the snapshot YYMMDD (the date of the extracts' replication timestamp,
    from each file's header); every state must share it."""
    osm = DATA / "osm"
    t0 = time.time()
    got = {}
    for st in states():
        name = CONFIG["state_info"][st]["geofabrik"]
        try:
            path, md5, rec = fetch_from_geofabrik(osm, name)
        except SystemExit as e:
            # Geofabrik gave nothing usable (in November 2026 its server could not be reached from GitHub's
            # runners for hours): the extract the store keeps from the last published run, under the same
            # age limit. A run from the same extract builds the same map with this week's builders.
            reason = str(e)
            print(f"== {name}: nothing usable from Geofabrik ({reason}); looking in the store", flush=True)
            try:
                path, md5, ts = kept_extract(store, osm, name)
            except SystemExit as e2:
                raise SystemExit(f"{reason}; and {e2}") from None
            rec = {"source": "store", "source_file": "extract-" + path.name, "fallback_reason": reason}
            print(f"== {name}: USING the extract kept in the store, {path.name} (map date {ts}), because: {reason}",
                  flush=True)
        ts = osm_timestamp(path)
        got[st] = (name, ts, md5, path, rec)
    days = {v[1][:10] for v in got.values()}
    if len(days) != 1:
        raise SystemExit(f"the extracts are of different days {sorted(days)}: Geofabrik is updating; run again later")
    day = days.pop()
    snap = day[2:4] + day[5:7] + day[8:10]
    for st, (name, ts, md5, path, rec) in got.items():
        dated = osm / f"{name}-{snap}.osm.pbf"
        if path.resolve() != dated.resolve():  # a fallback file may already have the snapshot's name
            dated.unlink(missing_ok=True)
            os.replace(path, dated)
            path.with_name(path.name + ".md5").unlink(missing_ok=True)
        (osm / f"{name}-{snap}.osm.pbf.md5").write_text(f"{md5}  {dated.name}\n")
        run.setdefault("extracts", {})[st] = {"file": dated.name, "md5": md5, "bytes": dated.stat().st_size,
                                              "replication_timestamp": ts, **rec}
    run["snapshot"], run["osm_snapshot"] = snap, day
    run.setdefault("steps", {})["fetch-extracts"] = {"wall_s": round(time.time() - t0, 1)}
    save_run(run)
    used = ", ".join(f"{v[0]} {v[1]} ({v[4]['source_file']})" for v in got.values())
    print(f"== extracts: {used} -> snapshot {snap}", flush=True)
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


def build_comma(run: dict, keep_work: bool) -> str | None:
    """The three comma-only parts of every state's pack (packbuilder/comma,
    docs/comma-pack-format.md): search-code.tar from the frozen release of the search code
    (packbuilder/comma/search-release, pinned) with packbuilder/search's ranking.json and
    abbreviations.json, roads.tar from the pack's map, mapd.tar from the state's extract with Mapd's own
    generator. Each step under the comma time limit. Returns None when every step passed, else why not
    (the first failed step; the steps after it are not run)."""
    remove(COMMA)
    for st in states():
        pack = OUT / "packs" / f"us-{st}"
        out = COMMA / f"us-{st}"
        out.mkdir(parents=True, exist_ok=True)
        man = json.loads((pack / "manifest.json").read_text(encoding="utf-8"))
        mtime = str(int(datetime.strptime(man["created_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                        .timestamp()))
        extract = DATA / "osm" / f"{CONFIG['state_info'][st]['geofabrik']}-{run['snapshot']}.osm.pbf"
        todo = [
            (f"comma-search-code-{st}", [sys.executable, "packbuilder/comma/search_code.py", "--out",
                                         str(out / "search-code.tar"), "--manifest", str(pack / "manifest.json"),
                                         "--mtime", mtime]),
            (f"comma-roads-{st}", [py("map"), "packbuilder/comma/roads.py", "--map", str(pack / "map.pmtiles"),
                                   "--out", str(out / "roads.tar"), "--mtime", mtime, "--workers",
                                   str(CONFIG["threads"]), "--report", str(OUT / "checks" / f"roads-{st}.json")]),
            # Mapd's admission is the comma check's (check.py --mapd), not run twice
            (f"comma-mapd-{st}", [py("map"), "packbuilder/comma/mapd.py", "build", "--extract", str(extract),
                                  "--manifest", str(pack / "manifest.json"), "--mapd", str(MAPD_TOOL / "mapd"),
                                  "--out", str(out / "mapd.tar"), "--work", str(COMMA_DATA / "work"),
                                  "--no-admit"] + ([] if keep_work else ["--clean"])),
        ]
        for name, cmd in todo:
            why = comma_step(run, name, cmd)
            if why:
                return why
        comma_state(run).setdefault("parts", {})[st] = {f.name: f.stat().st_size for f in sorted(out.glob("*.tar"))}
        save_run(run)
    return None


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


def check_comma(run: dict) -> str | None:
    """The comma parts of every state's pack: each present, by the contract's rules, the search code the
    pinned release and answering known queries from this week's search file in a separate Python, every
    road tile through StarPilot decoder's checks, mapd.tar admitted by Mapd itself
    (packbuilder/comma/check.py), under the comma time limit. Returns None when every state passed, else
    why not. Kept apart from the phone's checks (checks())."""
    bad = []
    for st in states():
        why = comma_step(run, f"check-comma-{st}", [sys.executable, "packbuilder/comma/check.py", "--pack",
                                                    str(OUT / "packs" / f"us-{st}"), "--parts", str(COMMA / f"us-{st}"),
                                                    "--mapd", str(MAPD_TOOL / "mapd"), "--json",
                                                    str(OUT / "checks" / f"comma-{st}.json")])
        comma_state(run).setdefault("checks", {})[st] = why is None
        save_run(run)
        if why:
            bad.append(f"{st} ({why})" if "time limit" in why else st)
    if bad:
        return (f"the comma parts of {', '.join(bad)} failed their check (data/out/checks/comma-<state>.json, "
                f"log data/out/logs/check-comma-<state>.log)")
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
        # the phone's checks only: the comma parts are checked apart (check_comma), and their failure
        # never holds back the phone's pack
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
    """The phone's catalog site (catalog.json and the release folders); comma.json is build_comma_catalog's."""
    site = OUT / "site"
    remove(site)
    args = [sys.executable, "packbuilder/catalog/build_catalog.py", "--out", str(site), "--check"]
    for st in states():
        args += ["--pack", str(OUT / "packs" / f"us-{st}")]
    step(run, "catalog", args)
    data = (site / "catalog" / "catalog.json").read_bytes()
    cat = json.loads(data.decode("utf-8"))
    # publish requires this very file (phone_to_publish), as it requires comma.json's record for the comma side
    run["catalog"] = {"catalog_json": {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()},
                      "serial": cat["serial"], "packs": [
        {"id": p["id"], "pack_version": p["pack_version"], "osm_snapshot": p["osm_snapshot"],
         "transfer_bytes": sum(q["transfer"]["bytes"] for q in p["parts"]) + p["manifest"]["bytes"],
         "files": [p["manifest"]["path"]] + [q["transfer"]["path"] for q in p["parts"]]} for p in cat["packs"]]}
    save_run(run)


def build_comma_catalog(run: dict) -> str | None:
    """comma.json beside catalog.json (which it never changes): the comma parts' transfer files go into
    each pack's release folder; every file it names is checked again (catalog.py --check), then once
    more here the way publish checks it (comma_problem). On success the comma side is recorded as passed
    in run.json with comma.json's size and SHA-256, which publish requires; on failure comma.json is
    removed from the site, so it can never be published. Returns None when it passed, else why not."""
    site = OUT / "site"
    path = site / "catalog" / "comma.json"
    why = comma_step(run, "comma-catalog", [sys.executable, "packbuilder/comma/catalog.py", "--site", str(site),
                                            "--parts", str(COMMA), "--check"])
    if why:
        why = f"packbuilder/comma/catalog.py --check: {why}"
    if why is None:
        if not path.is_file():
            why = "packbuilder/comma/catalog.py wrote no comma.json"
        else:
            cat = json.loads((site / "catalog" / "catalog.json").read_text(encoding="utf-8"))
            why = comma_problem(cat, path.read_bytes())
    if why:
        path.unlink(missing_ok=True)
        return why
    data = path.read_bytes()
    comma = json.loads(data.decode("utf-8"))
    c = comma_state(run)
    c["comma_json"] = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "serial": comma["serial"]}
    c["catalog"] = {"serial": comma["serial"], "packs": [
        {"id": p["id"], "pack_version": p["pack_version"],
         "files": [q["transfer"]["path"] for q in p["parts"]]} for p in comma["packs"]]}
    c["ok"] = True
    save_run(run)
    return None


def write_summary(run: dict) -> None:
    lines = [f"## Weekly pack build {run.get('started_utc', '')}", ""]
    lines.append(f"OSM snapshot {run.get('osm_snapshot', '?')}; states {', '.join(states())}; "
                 f"checks {'passed' if run.get('checks_passed') else 'FAILED or not run'}.")
    comma = run.get("comma") or {}
    if comma.get("ok") is True:
        lines.append("Comma side (comma files and comma.json): passed.")
    elif comma.get("ok") is False:
        f = comma.get("failed") or {}
        lines.append(f"**Comma side FAILED** ({f.get('stage', '?')}): {f.get('why', '')}. The phone's packs and "
                     f"catalog.json are published without it; the published comma.json stays as it was.")
    for st, e in run.get("extracts", {}).items():
        if e.get("source") == "dated":
            lines.append(f"Extract of {st}: **{e['source_file']}** (map date {e.get('replication_timestamp')}) was "
                         f"used instead of the latest, because: {e.get('fallback_reason')}.")
    lines += ["", "| Step | Wall (s) | Peak memory (MiB) | Extra disk at peak (MiB) |", "|---|---:|---:|---:|"]
    for k, v in run.get("steps", {}).items():
        lines.append(f"| {k} | {v.get('wall_s', '')} | {v.get('peak_rss_mb', '')} | {v.get('disk_peak_extra_mb', '')} |")
    for st, p in run.get("packs", {}).items():
        lines += ["", f"**{p['pack_id']}-{p['pack_version']}** ({p['osm_snapshot']}, routing {p['graph_build']}): "
                  + ", ".join(f"{k} {v / 1e6:.1f} MB" for k, v in p["parts"].items())]
        cparts = comma.get("parts", {}).get(st)
        if cparts:
            lines.append("Comma parts: " + ", ".join(f"{k} {v / 1e6:.1f} MB" for k, v in cparts.items()) + ".")
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
           "checks_passed": False, "comma": {"ok": None}}
    save_run(run)
    idx = restore_inputs(run, store)
    fetch_extracts(run, store)
    refresh_inputs(run, idx)
    parts = build_parts(run, a.keep_work)
    assemble(run, parts, a.keep_work)
    # the comma side: each stage isolated (comma_stage), none of them can stop the phone's packs
    # (stages: "setup" of Mapd's generator, "parts", their "check", "catalog" = comma.json)
    comma_stage(run, "setup", comma_setup_problem)
    comma_stage(run, "parts", lambda: build_comma(run, a.keep_work))
    try:
        checks(run, a)
        comma_stage(run, "check", lambda: check_comma(run))
        build_site(run)
        comma_stage(run, "catalog", lambda: build_comma_catalog(run))
    finally:
        run["finished_utc"] = iso(now_utc())
        run["wall_s"] = round(time.time() - started.timestamp())
        run["data_mb_end"] = round(tree_bytes(DATA) / 2**20)
        save_run(run)
        write_summary(run)
    print(f"== done in {run['wall_s']} s; site data/out/site", flush=True)
    if run["comma"].get("ok") is not True:
        f = run["comma"].get("failed") or {}
        print(f"== THE COMMA SIDE FAILED ({f.get('stage', '?')}): {f.get('why', 'not run')}. The phone's packs "
              f"passed and are published as usual; publish leaves the published comma.json as it is and ends as "
              f"failed after publishing.", flush=True)


# ------------------------------------------------------------------ publish
LICENCE_NOTES = """Map data © OpenStreetMap contributors, available under the Open Database License (ODbL 1.0): \
https://www.openstreetmap.org/copyright. House numbers also from the U.S. Census Bureau TIGER/Line files and the \
USDOT National Address Database (public domain), and the City of Scranton GIS (PDDL 1.0). See ATTRIBUTION.md in \
this repository."""


def release_notes(entry: dict, with_comma: bool = True) -> str:
    sizes = ", ".join(f"{p['name']} {p['bytes'] / 1e6:.1f} MB" for p in entry["parts"])
    comma = ("The comma parts (search-code.tar, roads.tar, mapd.tar) are read through comma.json of the same "
             "release." if with_comma else "This build has no comma parts (its comma side failed).")
    return (f"{entry['name']} pack {entry['pack_version']}, OpenStreetMap data of {entry['osm_snapshot']}.\n\n"
            f"Parts as installed: {sizes}. The app reads them through the catalog (release 'catalog'); "
            f"file formats: README. {comma}\n\n{LICENCE_NOTES}\n")


def gh(*args: str, capture: bool = False) -> str:
    r = subprocess.run(["gh", *args], capture_output=capture, text=True)
    if r.returncode != 0:
        raise SystemExit(f"gh {' '.join(args[:3])} failed" + (f": {r.stderr.strip()[:300]}" if capture else ""))
    return r.stdout if capture else ""


RELEASE_FILE_PATH = re.compile(r"^\.\./(us-[a-z]{2}-\d{12})/[A-Za-z0-9][A-Za-z0-9._-]*$")   # ../<build>/<file>
SERIAL_AHEAD_MAX_S = 2 * 86400   # a comma refuses a comma.json serial more than two days ahead of its clock


def serial_problem(serial) -> str | None:
    """Why a comma.json serial may not be published (docs/comma-pack-format.md 2), None when it may: it
    must be the 14 digits YYYYMMDDhhmmss of a real UTC time, at most two days ahead of this clock."""
    s = str(serial) if isinstance(serial, int) and not isinstance(serial, bool) else ""
    if not re.fullmatch(r"[1-9]\d{13}", s):
        return f"its serial {serial!r} is not 14 digits YYYYMMDDhhmmss"
    try:
        t = datetime(int(s[0:4]), int(s[4:6]), int(s[6:8]), int(s[8:10]), int(s[10:12]), int(s[12:14]),
                     tzinfo=timezone.utc)
    except ValueError:
        return f"its serial {serial} is not a real UTC time"
    if (t - now_utc()).total_seconds() > SERIAL_AHEAD_MAX_S:
        return f"its serial {serial} is more than two days ahead of this clock ({iso(now_utc())})"
    return None


def site_file_problem(p: str, size, sha: str, tag: str) -> str | None:
    """Why the file a catalog names (../<tag>/<file>, its size and SHA-256) may not be published from
    data/out/site; None when it is there, inside its own build's release folder, with that size and SHA-256."""
    m = RELEASE_FILE_PATH.match(p) if isinstance(p, str) else None
    if not m or m.group(1) != tag:
        return f"{p!r} for {tag} is outside its release"
    f = OUT / "site" / "catalog" / p
    if not f.is_file():
        return f"{p[3:]} is missing from data/out/site"
    if f.stat().st_size != size or sha256_file(f) != sha:
        return f"{p[3:]}: its size or SHA-256 is not the one the catalog gives"
    return None


def phone_to_publish(run: dict) -> tuple[dict | None, str]:
    """(catalog.json, "") when the phone's side may be published, else (None, why not). It may only when
    data/out/site/catalog/catalog.json is the very file the run checked (its size and SHA-256 in run.json
    "catalog", written by build_site) and every file it names (each build's manifest.json and parts) is in
    its own build's release folder with the size and SHA-256 catalog.json gives. Checked before anything
    is uploaded or copied; the files themselves are published exactly as they are."""
    rec = (run.get("catalog") or {}).get("catalog_json") or {}
    path = OUT / "site" / "catalog" / "catalog.json"
    if not rec:
        return None, "run.json records no checked catalog.json (run build/weekly.py run first)"
    if not path.is_file():
        return None, "data/out/site/catalog/catalog.json is missing"
    data = path.read_bytes()
    if len(data) != rec.get("bytes") or hashlib.sha256(data).hexdigest() != rec.get("sha256"):
        return None, "data/out/site/catalog/catalog.json is not the catalog.json the run checked (run.json)"
    try:
        cat = json.loads(data.decode("utf-8"))
        packs = cat["packs"]
        for e in packs:
            tag = f"{e['id']}-{e['pack_version']}"
            for q in [e["manifest"]] + [p["transfer"] for p in e["parts"]]:
                why = site_file_problem(q["path"], q["bytes"], q["sha256"], tag)
                if why:
                    return None, f"catalog.json: {why}"
    except (ValueError, KeyError, TypeError) as e:
        return None, f"catalog.json cannot be read as the phone catalog ({type(e).__name__}: {e})"
    if not packs:
        return None, "catalog.json names no pack"
    return cat, ""


def comma_problem(cat: dict, data: bytes) -> str | None:
    """Why this comma.json (its bytes) may not be published beside this catalog.json; None when it may:
    its format, its serial a real UTC time at most two days ahead of this clock (serial_problem), exactly
    one entry per pack build of catalog.json, and every file it names inside its own build's release
    folder of data/out/site with the size and SHA-256 comma.json gives."""
    try:
        comma = json.loads(data.decode("utf-8"))
    except ValueError as e:
        return f"comma.json is not JSON ({e})"
    if not (isinstance(comma, dict) and comma.get("format") == COMMA_FORMAT and isinstance(comma.get("packs"), list)
            and isinstance(comma.get("serial"), int)):
        return f"comma.json is not {COMMA_FORMAT}"
    why = serial_problem(comma["serial"])
    if why:
        return f"comma.json: {why}"
    want = sorted(f"{e['id']}-{e['pack_version']}" for e in cat["packs"])
    have = []
    for e in comma["packs"]:
        if not (isinstance(e, dict) and isinstance(e.get("parts"), list) and e["parts"]):
            return "comma.json has a pack without parts"
        tag = f"{e.get('id')}-{e.get('pack_version')}"
        have.append(tag)
        for q in e["parts"]:
            t = q.get("transfer") if isinstance(q, dict) else None
            t = t if isinstance(t, dict) else {}
            why = site_file_problem(t.get("path"), t.get("bytes"), t.get("sha256"), tag)
            if why:
                return f"comma.json: {why}"
    if sorted(have) != want:
        return f"comma.json names the builds {sorted(have)}, catalog.json {want}"
    return None


def comma_to_publish(run: dict, cat: dict) -> tuple[dict | None, str]:
    """(comma.json, "") when this run's comma side may be published, else (None, why not). It may only
    when run.json records the comma side as passed, data/out/site/catalog/comma.json is the very file
    the run checked (its size and SHA-256 in run.json) and every file it names still has the size and
    SHA-256 it gives (comma_problem): a later manual publish never publishes what failed its check."""
    c = run.get("comma") or {}
    if c.get("ok") is not True:
        f = c.get("failed") or {}
        return None, (f"the run's comma side failed ({f.get('stage', '?')}): {f.get('why', '')}" if f
                      else "run.json records no passed comma side")
    path = OUT / "site" / "catalog" / "comma.json"
    if not path.is_file():
        return None, "data/out/site/catalog/comma.json is missing"
    data = path.read_bytes()
    rec = c.get("comma_json") or {}
    if len(data) != rec.get("bytes") or hashlib.sha256(data).hexdigest() != rec.get("sha256"):
        return None, "data/out/site/catalog/comma.json is not the comma.json the run checked (run.json)"
    why = comma_problem(cat, data)
    if why:
        return None, why
    return json.loads(data.decode("utf-8")), ""


def comma_packs_of(path: Path) -> list | None:
    """The packs a published comma.json names ([] when there is no such file); None when it cannot be read."""
    if not path.exists():
        return []
    try:
        packs = json.loads(path.read_text(encoding="utf-8")).get("packs")
    except (OSError, ValueError, AttributeError):
        return None
    return [p for p in packs if isinstance(p, dict)] if isinstance(packs, list) else None


def published_comma_github() -> list | None:
    """The packs of the comma.json the "catalog" release holds now ([] when it holds none); None when
    it cannot be read."""
    r = subprocess.run(["gh", "release", "view", "catalog", "--json", "assets"], capture_output=True, text=True)
    if r.returncode != 0:
        return [] if "release not found" in (r.stderr or "").lower() else None
    try:
        if not any(x.get("name") == "comma.json" for x in json.loads(r.stdout)["assets"]):
            return []
    except (ValueError, KeyError, TypeError, AttributeError):
        return None
    tmp = DATA / "store-tmp" / "published-comma"
    remove(tmp)
    tmp.mkdir(parents=True)
    d = subprocess.run(["gh", "release", "download", "catalog", "--pattern", "comma.json", "--dir", str(tmp),
                        "--clobber"], capture_output=True, text=True)
    if d.returncode != 0 or not (tmp / "comma.json").is_file():
        return None
    return comma_packs_of(tmp / "comma.json")


def release_files(entry: dict, comma: dict | None) -> list[Path]:
    """The files of one pack build's release: the phone catalog's (manifest.json and the parts) and,
    when this run's comma side is published (comma_to_publish), the comma catalog's parts of the same
    build (the parts the two share are the very same files)."""
    site = OUT / "site" / "catalog"
    tag = f"{entry['id']}-{entry['pack_version']}"
    paths = [entry["manifest"]["path"]] + [q["transfer"]["path"] for q in entry["parts"]]
    if comma is not None:  # comma_to_publish has checked these already; kept as a second guard
        ce = next((c for c in comma.get("packs", []) if c.get("id") == entry["id"]
                   and c.get("pack_version") == entry["pack_version"]), None)
        if ce is None:
            raise SystemExit(f"comma.json has no entry for {tag}: nothing is published")
        for q in ce["parts"]:
            p = q["transfer"]["path"]
            if not p.startswith(f"../{tag}/"):
                raise SystemExit(f"comma.json names {p} for {tag}, outside its release: nothing is published")
            if p not in paths:
                paths.append(p)
    files = [(site / p).resolve() for p in paths]
    missing = [f.name for f in files if not f.is_file()]
    if missing:
        raise SystemExit(f"{tag}: {', '.join(missing)} missing from data/out/site: nothing is published")
    return files


def cmd_publish(a) -> None:
    run = load_run()
    if not run.get("checks_passed"):
        raise SystemExit("the last run's checks did not all pass (data/out/run.json): nothing is published")
    cat_path = OUT / "site" / "catalog" / "catalog.json"
    comma_path = OUT / "site" / "catalog" / "comma.json"
    # The phone's side, before anything is uploaded or copied: catalog.json must be the file the run
    # checked and every file it names must still have the size and SHA-256 it gives (as for comma.json).
    cat, phone_why = phone_to_publish(run)
    if cat is None:
        raise SystemExit(f"{phone_why}: nothing is published")
    # The comma side is published only when the run recorded it as passed and its files are still the
    # ones comma.json gives; otherwise the phone's side is published exactly as if there were no comma
    # side, the published comma.json stays with every build it names, and publish ends as failed.
    comma, comma_why = comma_to_publish(run, cat)
    if comma is None:
        print(f"== the comma side is NOT published: {comma_why}\n   The phone's packs and catalog.json are "
              f"published as usual; the published comma.json stays as it is and no build it names is deleted.",
              flush=True)
    files_of = {f"{e['id']}-{e['pack_version']}": release_files(e, comma) for e in cat["packs"]}
    keep = int(CONFIG["keep_builds"])
    published = []
    if a.github:
        # the builds kept besides catalog.json's: this run's comma.json's, or, when it is not published,
        # those of the comma.json comma devices read now (None: not known, so nothing is deleted)
        protect = comma["packs"] if comma is not None else published_comma_github()
        existing = {r["tagName"]: r for r in json.loads(gh("release", "list", "--limit", "500", "--json",
                                                           "tagName,isDraft", capture=True))}
        for e in cat["packs"]:
            tag = f"{e['id']}-{e['pack_version']}"
            files = files_of[tag]
            if tag in existing and not existing[tag]["isDraft"]:
                raise SystemExit(f"release {tag} exists already: a published build is never rebuilt")
            if tag in existing:
                gh("release", "delete", tag, "--yes")
            notes = OUT / f"notes-{tag}.md"
            notes.write_text(release_notes(e, comma is not None), encoding="utf-8")
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
                   "(format beamline-catalog/1, README); comma.json: the same packs for a comma device "
                   "(format beamline-comma-catalog/1, README). " + LICENCE_NOTES, "--target",
                   os.environ.get("GITHUB_SHA", "main"))
            gh("release", "upload", "catalog", str(cat_path), "--clobber")
        except SystemExit:
            # No catalog names this run's releases: back to drafts, which cleanup-drafts removes, so
            # they never count among the newest builds that pruning keeps.
            for tag in published:
                subprocess.run(["gh", "release", "edit", tag, "--draft=true"])
            raise
        # comma.json after catalog.json, in a call of its own: if it fails, the phone's catalog stays
        # published and no build is deleted (what the catalog release holds as comma.json is not known)
        if comma is not None:
            try:
                gh("release", "upload", "catalog", str(comma_path), "--clobber")
            except SystemExit as e:
                comma, comma_why, protect = None, f"the upload of comma.json failed ({e})", None
        # builds older than the newest `keep` of each state
        tags = [r["tagName"] for r in json.loads(gh("release", "list", "--limit", "500", "--json",
                                                     "tagName,isDraft", capture=True)) if not r["isDraft"]]
        deleted = prune(tags, keep, cat, protect)
        for t in deleted:
            gh("release", "delete", t, "--yes", "--cleanup-tag")
        store = Store(a.store, a.store_release)
    else:
        dest = Path(a.local).expanduser().resolve()
        protect = comma["packs"] if comma is not None else comma_packs_of(dest / "catalog" / "comma.json")
        for e in cat["packs"]:
            tag = f"{e['id']}-{e['pack_version']}"
            d = dest / tag
            if d.exists():
                raise SystemExit(f"{d} exists already: a published build is never rebuilt")
            tmp = dest / f".{tag}.part"
            remove(tmp)
            tmp.mkdir(parents=True)
            for f in files_of[tag]:
                shutil.copyfile(f, tmp / f.name)
            (tmp / "RELEASE-NOTES.md").write_text(release_notes(e, comma is not None), encoding="utf-8")
            tmp.rename(d)
            published.append(tag)
        (dest / "catalog").mkdir(parents=True, exist_ok=True)
        for src in [cat_path] + ([comma_path] if comma is not None else []):
            tmp = dest / "catalog" / (src.name + ".part")
            shutil.copyfile(src, tmp)
            tmp.replace(dest / "catalog" / src.name)
        deleted = prune([p.name for p in dest.iterdir() if p.is_dir()], keep, cat, protect)
        for t in deleted:
            shutil.rmtree(dest / t)
        store = Store(a.store, a.store_release)
    saved = save_inputs(store)
    try:
        saved += save_extracts(store, run)
    except (OSError, subprocess.CalledProcessError) as e:  # the packs are published; next week's safety net is not worth a failed run
        print(f"== the extracts could not be kept in the store ({type(e).__name__}: {e})", flush=True)
    run["published"] = {"releases": published, "catalog_serial": cat["serial"], "comma_published": comma is not None,
                        "comma_serial": comma["serial"] if comma is not None else None,
                        "deleted": deleted, "inputs_saved": saved, "utc": iso(now_utc())}
    if comma is None:
        run["published"]["comma_not_published"] = comma_why
    save_run(run)
    print(json.dumps(run["published"], indent=1))
    if comma is None:
        raise SystemExit(f"THE COMMA SIDE FAILED: {comma_why}. The phone's packs and catalog.json were published; "
                         f"comma.json was not, and the one published before stays with every build it names. "
                         f"Logs: data/out/logs, data/out/run.json.")


def prune(tags: list[str], keep: int, cat: dict, protect: list | None) -> list[str]:
    """prune_list over catalog.json's builds and the protected comma.json's; nothing when the latter is
    not known (None)."""
    if protect is None:
        print("== no build is deleted this time: the published comma.json could not be read, and nothing it "
              "names may be deleted", flush=True)
        return []
    return prune_list(tags, keep, {"packs": cat["packs"] + protect})


def named_builds(cat: dict) -> set[str]:
    """The pack builds a catalog (catalog.json or comma.json) names: each entry's id-version and the
    release folder of every file it names (../<tag>/<file>)."""
    out = set()
    for p in cat.get("packs", []):
        if not isinstance(p, dict):
            continue
        if "id" in p and "pack_version" in p:
            out.add(f"{p['id']}-{p['pack_version']}")
        for q in [p.get("manifest")] + list(p.get("parts") or []):
            t = q.get("transfer", q) if isinstance(q, dict) else None
            path = t.get("path") if isinstance(t, dict) else None
            m = re.match(r"^\.\./([^/]+)/", path) if isinstance(path, str) else None
            if m:
                out.add(m.group(1))
    return out


def prune_list(tags: list[str], keep: int, cat: dict) -> list[str]:
    """Pack release tags beyond the newest `keep` of each pack id; never one the catalog names."""
    named = named_builds(cat)
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


def cmd_comma_status(_a) -> None:
    """Exit 1 with a plain message when the last run's comma side did not pass: the workflow's last
    step, so a failed comma side fails the run also when nothing is published."""
    c = load_run().get("comma") or {}
    if c.get("ok") is True:
        print("the comma side of the last run passed")
        return
    f = c.get("failed") or {}
    raise SystemExit(f"THE COMMA SIDE FAILED ({f.get('stage', 'not run')}): {f.get('why', 'the run did not reach it')}. "
                     f"The phone's packs are not held back by it; the published comma.json stays as it was.")


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
    sub.add_parser("comma-status")
    a = ap.parse_args()
    {"setup": cmd_setup, "run": cmd_run, "publish": cmd_publish, "cleanup-drafts": cmd_cleanup_drafts,
     "comma-status": cmd_comma_status}[a.cmd](a)


if __name__ == "__main__":
    main()
