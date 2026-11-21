"""Download the auxiliary (non-OSM) inputs the Protomaps basemap profile needs.

Usage (from the repo root):
    .venv/map/Scripts/python packbuilder/map/fetch_sources.py [--force]

Files land in data/map/work/data/sources/ (the Protomaps profile looks for
qrank.csv.gz and pgf-encoding.zip under ./data/sources relative to its working
directory, so the build runs with data/map/work as its working directory).

These files are updated upstream (daily for the osmdata.openstreetmap.de ones),
so they cannot be pinned by hash in git. Instead every download is recorded
with its SHA-256, size, Last-Modified header and download time in
data/map/work/data/sources/manifest.json, and build_pack.py copies that
manifest into each pack's build record.

Re-running skips files that are already present unless --force is given.

The US Census files (TIGER/Line 2026 state outlines and primary roads, a
cartographic boundary file for comparison) are versioned by year and pinned
by SHA-256 in PINNED; they go to data/map/work/data/census/.
--pinned-only fetches only those.
daylight-landcover.gpkg (CC-BY 4.0) is only fetched when build_config.json has
"landcover": true; by default the build feeds the profile an empty layer.
"""
import argparse
import hashlib
import json
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SOURCES = REPO / "data" / "map" / "work" / "data" / "sources"
LANDCOVER_ON = json.loads((Path(__file__).resolve().parent / "build_config.json").read_text())["build"]["landcover"]

# name -> (url, licence, used for)
FILES = {
    "natural_earth_vector.gpkg.zip": (
        "https://naciscdn.org/naturalearth/packages/natural_earth_vector.gpkg.zip",
        "Public domain (Natural Earth terms of use)",
        "low-zoom (z0-z5) earth, water, boundaries, places",
    ),
    "water-polygons-split-3857.zip": (
        "https://osmdata.openstreetmap.de/download/water-polygons-split-3857.zip",
        "ODbL 1.0 (derived from OpenStreetMap)",
        "ocean / Great Lakes water polygons",
    ),
    "land-polygons-split-3857.zip": (
        "https://osmdata.openstreetmap.de/download/land-polygons-split-3857.zip",
        "ODbL 1.0 (derived from OpenStreetMap)",
        "earth (land) polygons",
    ),
    "daylight-landcover.gpkg": (
        "https://r2-public.protomaps.com/datasets/daylight-landcover.gpkg",
        "CC-BY 4.0 (derived from ESA WorldCover); see report about the extra credit",
        "low-zoom landcover layer",
    ),
    "qrank.csv.gz": (
        "https://qrank.toolforge.org/download/qrank.csv.gz",
        "CC0 1.0 (Wikidata QRank)",
        "ranking of places/POIs by Wikidata page views (build time only)",
    ),
    "pgf-encoding.zip": (
        "https://wipfli.github.io/pgf-encoding/pgf-encoding.zip",
        "MIT (wipfli/pgf-encoding), font data from Noto (OFL-1.1); build time only, not shipped",
        "encodes Devanagari names; irrelevant for US data but required by the profile",
    ),
}

# Versioned public-domain files, pinned by SHA-256 (computed at the first download on 2026-11-15;
# the Census Bureau publishes no checksums). name -> (url, sha256, licence, used for)
CENSUS = REPO / "data" / "map" / "work" / "data" / "census"
PINNED = {
    "tl_2026_us_state.zip": (
        "https://www2.census.gov/geo/tiger/TIGER2026/STATE/tl_2026_us_state.zip",
        "2e17ea40dff3e00163eec87a26efa0b0ff7e8fe364a191bf1a7754dcb49ea326",
        "Public domain (US Census Bureau TIGER/Line, a US government work)",
        "state outlines for clipping state packs at the state line (census_clip.py)",
    ),
    "tl_2026_us_primaryroads.zip": (
        "https://www2.census.gov/geo/tiger/TIGER2026/PRIMARYROADS/tl_2026_us_primaryroads.zip",
        "ed7c8e47344d45ecf73694e92d5d105fe6955983733e3a57317c318f80a159ef",
        "Public domain (US Census Bureau TIGER/Line, a US government work)",
        "interstates and other limited-access highways of the US overview pack",
    ),
    "2026_Gaz_place_national.zip": (
        "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2026_Gazetteer/2026_Gaz_place_national.zip",
        "af678e2d990827c89ee39b98c82de6e90b693c7361ff0e559ae3076670dd2863",
        "Public domain (US Census Bureau Gazetteer file)",
        "town names and points of the US overview pack",
    ),
    "sub-est2025.csv": (
        "https://www2.census.gov/programs-surveys/popest/datasets/2020-2025/cities/totals/sub-est2025.csv",
        "e3508f5201465913476d4ddf91740f191f7977c9f2614a2a7e30b5fd22027934",
        "Public domain (US Census Bureau population estimates, Vintage 2025)",
        "town populations (ranking and zoom levels) of the US overview pack",
    ),
    "cb_2025_us_state_500k.zip": (
        "https://www2.census.gov/geo/tiger/GENZ2025/shp/cb_2025_us_state_500k.zip",
        "9cbfe171dad1555e11770c981d8f4db9e687a65c86f5bdae684eeb487e2e9b80",
        "Public domain (US Census Bureau cartographic boundary file)",
        "comparison only: cut at the shoreline, so not used for clipping",
    ),
}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch_pinned(force: bool) -> None:
    CENSUS.mkdir(parents=True, exist_ok=True)
    for name, (url, want, licence, use) in PINNED.items():
        dest = CENSUS / name
        if dest.exists() and not force and sha256_file(dest) == want:
            print(f"skip {name} (present, sha256 ok)")
            continue
        print(f"download {name} from {url}", flush=True)
        download(name, url, dest)
        got = sha256_file(dest)
        if got != want:
            dest.unlink()
            raise SystemExit(f"{name}: SHA-256 {got} does not match the pinned {want}")
        print(f"  ok {name} ({licence}; {use})")


def probe(url: str):
    """(total bytes, Last-Modified, server accepts ranges)"""
    req = urllib.request.Request(url, headers={"User-Agent": "beamline-packbuilder", "Range": "bytes=0-0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        cr = r.headers.get("Content-Range", "")
        if r.status == 206 and "/" in cr:
            return int(cr.rsplit("/", 1)[1]), r.headers.get("Last-Modified", ""), True
        return int(r.headers.get("Content-Length", "0") or 0), r.headers.get("Last-Modified", ""), False


def fetch_segment(url, path: Path, start: int, end: int, last_modified: str, label: str, attempts=30):
    """Download bytes start..end (inclusive) into path, resuming what path already holds."""
    want = end - start + 1
    for attempt in range(1, attempts + 1):
        have = path.stat().st_size if path.exists() else 0
        if have >= want:
            return
        headers = {"User-Agent": "beamline-packbuilder", "Range": f"bytes={start + have}-{end}"}
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=120) as r:
                if r.status != 206:
                    raise OSError(f"server answered {r.status} to a range request")
                if last_modified and r.headers.get("Last-Modified", last_modified) != last_modified:
                    raise SystemExit(f"{label}: file changed upstream during download; delete *.seg* and retry")
                with open(path, "ab") as f:
                    while True:
                        chunk = r.read(1 << 20)
                        if not chunk:
                            break
                        f.write(chunk)
        except OSError as e:
            print(f"  {label} attempt {attempt}: {e}; retrying in 10 s", flush=True)
            time.sleep(10)
    if not path.exists() or path.stat().st_size != want:
        raise SystemExit(f"{label}: incomplete after {attempts} attempts")


def download_segmented(name: str, url: str, dest: Path, segments: int) -> dict:
    """Large files: several parallel range requests (osmdata.openstreetmap.de serves ~0.3-1 MB/s per connection)."""
    import threading
    t0 = time.time()
    total, last_modified, ranges_ok = probe(url)
    if not ranges_ok or total < 64 * 1024 * 1024:
        return download(name, url, dest)
    bounds = [(i * total // segments, (i + 1) * total // segments - 1) for i in range(segments)]
    parts = [dest.with_suffix(dest.suffix + f".seg{i}") for i in range(segments)]
    old = dest.with_suffix(dest.suffix + ".part")  # a previous single-stream attempt: reuse as segment 0
    if old.exists() and not parts[0].exists() and old.stat().st_size <= bounds[0][1] + 1:
        old.replace(parts[0])
    errors = []

    def run(i):
        try:
            fetch_segment(url, parts[i], bounds[i][0], bounds[i][1], last_modified, f"{name}#{i}")
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(segments)]
    for t in threads:
        t.start()
    while any(t.is_alive() for t in threads):
        time.sleep(15)
        done = sum(p.stat().st_size for p in parts if p.exists())
        print(f"  {name}: {done / 1e6:.0f} / {total / 1e6:.0f} MB ({segments} connections)", flush=True)
    if errors:
        raise SystemExit(f"{name}: {errors[0]}")
    h = hashlib.sha256()
    tmp = dest.with_suffix(dest.suffix + ".part")
    with open(tmp, "wb") as out:
        for p in parts:
            with open(p, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
                    out.write(chunk)
    if tmp.stat().st_size != total:
        raise SystemExit(f"{name}: assembled {tmp.stat().st_size} bytes, expected {total}")
    tmp.replace(dest)
    for p in parts:
        p.unlink()
    return {
        "url": url, "sha256": h.hexdigest(), "bytes": total, "last_modified": last_modified,
        "downloaded_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "seconds": round(time.time() - t0, 1),
    }


def download(name: str, url: str, dest: Path, attempts: int = 20) -> dict:
    """Download with resume (HTTP Range) and a size check against Content-Length.

    A dropped connection can end the response early without an error, so the
    byte count is compared with the expected total and the transfer resumed.
    """
    tmp = dest.with_suffix(dest.suffix + ".part")
    t0 = time.time()
    total = None
    last_modified = ""
    for attempt in range(1, attempts + 1):
        have = tmp.stat().st_size if tmp.exists() else 0
        headers = {"User-Agent": "beamline-packbuilder"}
        if have:
            headers["Range"] = f"bytes={have}-"
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=120) as r:
                if have and r.status != 206:
                    have = 0  # server ignored Range: start over
                if not have:
                    total = int(r.headers.get("Content-Length", "0") or 0) or None
                    last_modified = r.headers.get("Last-Modified", "")
                elif total is None:
                    cr = r.headers.get("Content-Range", "")  # bytes a-b/total
                    total = int(cr.rsplit("/", 1)[1]) if "/" in cr else None
                    last_modified = r.headers.get("Last-Modified", "")
                with open(tmp, "ab" if have else "wb") as f:
                    done = have
                    next_report = time.time() + 15
                    while True:
                        chunk = r.read(1 << 20)
                        if not chunk:
                            break
                        f.write(chunk)
                        done += len(chunk)
                        if time.time() > next_report:
                            print(f"  {name}: {done / 1e6:.0f} / {(total or 0) / 1e6:.0f} MB", flush=True)
                            next_report = time.time() + 15
        except OSError as e:
            print(f"  attempt {attempt}: {e}; retrying in 10 s", flush=True)
            time.sleep(10)
            continue
        size = tmp.stat().st_size
        if total is None or size == total:
            break
        print(f"  attempt {attempt}: got {size} of {total} bytes; resuming", flush=True)
        time.sleep(5)
    else:
        raise SystemExit(f"{name}: download did not complete after {attempts} attempts")
    h = hashlib.sha256()
    with open(tmp, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    tmp.replace(dest)
    return {
        "url": url,
        "sha256": h.hexdigest(),
        "bytes": dest.stat().st_size,
        "last_modified": last_modified,
        "downloaded_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "seconds": round(time.time() - t0, 1),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--force", action="store_true", help="download again even if present")
    ap.add_argument("--only", nargs="*", help="only these file names")
    ap.add_argument("--segments", type=int, default=4,
                    help="parallel range requests for files over 64 MB (1 = single stream)")
    ap.add_argument("--pinned-only", action="store_true", help="only the SHA-256-pinned Census files")
    a = ap.parse_args()
    fetch_pinned(a.force)
    if a.pinned_only:
        return 0
    SOURCES.mkdir(parents=True, exist_ok=True)
    manifest_path = SOURCES / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    for name, (url, licence, use) in FILES.items():
        if a.only and name not in a.only:
            continue
        if name == "daylight-landcover.gpkg" and not LANDCOVER_ON:
            print(f"skip {name} (landcover is off in build_config.json)")
            continue
        dest = SOURCES / name
        if dest.exists() and not a.force and name in manifest:
            print(f"skip {name} (present, sha256 {manifest[name]['sha256'][:12]}...)")
            continue
        print(f"download {name} from {url}", flush=True)
        rec = download_segmented(name, url, dest, a.segments) if a.segments > 1 else download(name, url, dest)
        rec["licence"] = licence
        rec["use"] = use
        # re-read before writing so parallel runs with --only do not drop each other's entries
        manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
        manifest[name] = rec
        manifest_path.write_text(json.dumps(manifest, indent=2))
        print(f"  done {rec['bytes'] / 1e6:.1f} MB in {rec['seconds']} s, sha256 {rec['sha256']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
