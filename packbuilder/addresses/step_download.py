"""Step 1: download inputs (Census tables and shapes, TIGER ADDRFEAT, optionally the NAD).

    python packbuilder/addresses/step_download.py [--force] [--skip-nad] [--only nad]

Files land in data/addresses/download/. Each file's size and SHA-256 are written
to packbuilder/addresses/sources.lock.json; when a lock entry exists, a
re-download that does not match it fails loudly (upstream changed the file).
The NAD text file (8.5 GB) is fetched with up to 4 parallel HTTP range
requests into part files, so an interrupted download resumes.
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import os
import shutil
import time
import urllib.request
from pathlib import Path

from common import DL, HERE, MAX_THREADS, SOURCES, STATE_INFO, USER_AGENT, Step, dl_file, file_mb, rel, sha256_file

LOCK = HERE / "sources.lock.json"
UA = USER_AGENT


def load_lock() -> dict:
    if LOCK.exists():
        return json.loads(LOCK.read_text(encoding="utf-8"))
    return {}


def save_lock(lock: dict) -> None:
    LOCK.write_text(json.dumps(dict(sorted(lock.items())), indent=2) + "\n", encoding="utf-8")


def http_size(url: str) -> int:
    req = urllib.request.Request(url, headers={**UA, "Range": "bytes=0-0"})
    with urllib.request.urlopen(req, timeout=120) as r:
        cr = r.headers.get("Content-Range")
        if cr and "/" in cr:
            return int(cr.split("/")[-1])
        return int(r.headers.get("Content-Length", "0"))


def fetch_simple(url: str, dest: Path, retries: int = 4) -> None:
    tmp = dest.with_name(dest.name + ".part")
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=300) as r, open(tmp, "wb") as f:
                shutil.copyfileobj(r, f, 1 << 20)
            tmp.replace(dest)
            return
        except Exception as e:  # network hiccups: retry
            print(f"  retry {attempt + 1} for {dest.name}: {e}", flush=True)
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"failed to download {url}")


def fetch_ranged(url: str, dest: Path, parallel: int, chunk_mb: int = 256) -> None:
    """Resumable parallel range download into dest.parts/NNNNN files, then concatenate."""
    total = http_size(url)
    parts_dir = dest.with_name(dest.name + ".parts")
    parts_dir.mkdir(exist_ok=True)
    chunk = chunk_mb << 20
    ranges = [(i, s, min(s + chunk, total) - 1) for i, s in enumerate(range(0, total, chunk))]
    print(f"  {dest.name}: {total / 1e9:.2f} GB in {len(ranges)} chunks, {parallel} connections", flush=True)

    def get(rng):
        i, s, e = rng
        p = parts_dir / f"{i:05d}"
        want = e - s + 1
        if p.exists() and p.stat().st_size == want:
            return 0
        for attempt in range(8):
            try:
                have = p.stat().st_size if p.exists() else 0
                if have > want:
                    p.unlink()
                    have = 0
                req = urllib.request.Request(url, headers={**UA, "Range": f"bytes={s + have}-{e}"})
                with urllib.request.urlopen(req, timeout=300) as r, open(p, "ab") as f:
                    shutil.copyfileobj(r, f, 1 << 20)
                if p.stat().st_size == want:
                    return want - have
            except Exception as ex:
                print(f"  chunk {i} retry {attempt + 1}: {ex}", flush=True)
                time.sleep(10 * (attempt + 1))
        raise RuntimeError(f"chunk {i} failed")

    t0 = time.time()
    done = 0
    with cf.ThreadPoolExecutor(max_workers=min(parallel, MAX_THREADS)) as ex:
        for n, got in enumerate(ex.map(get, ranges), 1):
            done += got
            if n % 4 == 0 or n == len(ranges):
                el = time.time() - t0
                print(f"  {n}/{len(ranges)} chunks, {done / 1e6:.0f} MB new in {el:.0f} s"
                      f" ({done / 1e6 / max(el, 1):.2f} MB/s)", flush=True)
    tmp = dest.with_name(dest.name + ".part")
    with open(tmp, "wb") as out:
        for i, _, _ in ranges:
            with open(parts_dir / f"{i:05d}", "rb") as f:
                shutil.copyfileobj(f, out, 1 << 22)
    if tmp.stat().st_size != total:
        raise RuntimeError(f"size mismatch {tmp.stat().st_size} != {total}")
    tmp.replace(dest)
    shutil.rmtree(parts_dir)


def jobs(skip_nad: bool, only: str | None):
    out = []
    files = {k: f for k, f in SOURCES["files"].items() if k in STATE_INFO["files"]}
    for key, f in files.items():
        if key == "nad_txt" and skip_nad:
            continue
        name = "nad_r24_TXT.zip" if key == "nad_txt" else f["url"].split("/")[-1]
        out.append((key, f["url"], dl_file(name), f.get("parallel", 1)))
    for c in STATE_INFO["county_fips3"]:
        county5 = SOURCES["state"]["fips"] + c
        url = SOURCES["tiger_addrfeat_url"].format(county5=county5)
        out.append((f"addrfeat_{county5}", url, dl_file("addrfeat/" + url.split("/")[-1]), 1))
    if only:
        out = [j for j in out if j[0].startswith(only)]
    return out


def main():
    st = Step("download", extra_args=[(("--skip-nad",), {"action": "store_true"}),
                                      (("--only",), {"default": None})])
    if st.args.only:  # separate bookkeeping, e.g. --only nad
        st.name = f"download_{st.args.only}"
        st.metrics_path = st.metrics_path.with_name(st.name + ".json")
    with st:
        lock = load_lock()
        (DL / "addrfeat").mkdir(parents=True, exist_ok=True)
        todo = jobs(st.args.skip_nad, st.args.only)
        t0 = time.time()
        small = [j for j in todo if j[3] == 1 and not j[2].exists()]

        def run_small(j):
            key, url, dest, _ = j
            fetch_simple(url, dest)
            return key

        with cf.ThreadPoolExecutor(max_workers=MAX_THREADS) as ex:
            for key in ex.map(run_small, small):
                pass
        for key, url, dest, par in todo:
            if par > 1 and not dest.exists():
                fetch_ranged(url, dest, par)
        dl_s = time.time() - t0

        sizes = {}
        lock = load_lock()  # re-read: another download run may have updated it meanwhile
        for key, url, dest, _ in todo:
            entry = {"url": url, "file": rel(dest), "bytes": dest.stat().st_size}
            if key in lock and lock[key].get("sha256") and lock[key]["bytes"] == entry["bytes"] \
                    and not st.args.force:
                entry["sha256"] = lock[key]["sha256"]
            else:
                entry["sha256"] = sha256_file(dest)
            if key in lock and lock[key]["sha256"] != entry["sha256"]:
                raise SystemExit(f"{key}: SHA-256 differs from sources.lock.json; upstream changed. "
                                 f"Delete the lock entry to accept the new file.")
            lock[key] = entry
            sizes[key] = entry["bytes"]
        save_lock(lock)
        tiger = sum(v for k, v in sizes.items() if k.startswith("addrfeat_"))
        st.info.update({
            "download_wall_s": round(dl_s, 1),
            "files": len(sizes),
            "state": SOURCES["state"]["usps"],
            "tiger_addrfeat_mb": round(tiger / 1e6, 1),
            "tiger_addrfeat_files": sum(1 for k in sizes if k.startswith("addrfeat_")),
            "nad_txt_mb": round(sizes.get("nad_txt", 0) / 1e6, 1),
            "other_mb": {k: round(v / 1e6, 2) for k, v in sizes.items()
                         if not k.startswith("addrfeat_") and k != "nad_txt"},
        })


if __name__ == "__main__":
    main()
