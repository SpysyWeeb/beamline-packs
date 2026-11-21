"""Fetch the portable build tools used by the map track (packbuilder/map).

Usage (from the repo root, any Python 3.12):
    python tools/fetch_map_tools.py [--force]

Downloads each archive into tools/cache/, checks its pinned SHA-256, and
unpacks it into tools/bin/<TOOL>/. Nothing is installed system-wide and PATH is
not changed: the map scripts call the binaries by their path.

Pinned tools:
  maven    Apache Maven 3.9.16 (Apache-2.0) - only to compile the Protomaps
           basemap profile (a Planetiler application) into a jar. Build time only.
  pmtiles  go-pmtiles 1.31.2 (BSD-3-Clause) - show/verify/extract/cluster PMTiles.
           Build time only.
  planetiler  Planetiler 0.10.2 (Apache-2.0), the release jar, which includes
           "generate-custom" (YAML schemas). Builds the US overview pack from
           Natural Earth and Census data. Build time only. The SHA-256 is the
           digest GitHub lists for the release asset.

Java is not fetched: Temurin 25 is already installed on the build machine.
"""
import argparse
import hashlib
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "tools" / "cache"
BIN = ROOT / "tools" / "bin"

TOOLS = {
    "maven": {
        "version": "3.9.16",
        "url": "https://dlcdn.apache.org/maven/maven-3/3.9.16/binaries/apache-maven-3.9.16-bin.zip",
        # dlcdn only keeps current releases; archive.apache.org keeps every release forever
        "fallback_url": "https://archive.apache.org/dist/maven/maven-3/3.9.16/binaries/apache-maven-3.9.16-bin.zip",
        "sha256": "5af3b743dd8b876b5c45da33b676251e5f1687712644abb4ee519ca56e1d89ce",
        "check": "apache-maven-3.9.16/bin/mvn.cmd",
        "licence": "Apache-2.0",
    },
    "pmtiles": {
        "version": "1.31.2",
        "url": "https://github.com/protomaps/go-pmtiles/releases/download/v1.31.2/go-pmtiles_1.31.2_Windows_x86_64.zip",
        "sha256": "a658baa4d7e55020aef6ca17bd9ff9faa1582671266b36f58c52db0ac8e785a1",
        "check": "pmtiles.exe",
        "licence": "BSD-3-Clause",
    },
    "planetiler": {
        "version": "0.10.2",
        "url": "https://github.com/onthegomap/planetiler/releases/download/v0.10.2/planetiler.jar",
        "sha256": "f310bd0413e2e4512b27f4046d418664e8e1d3bf31603c2a70e23de06c167e4d",
        "check": "planetiler.jar",
        "licence": "Apache-2.0",
        "single_file": True,
    },
}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch(name: str, spec: dict, force: bool) -> None:
    dest = BIN / name
    if (dest / spec["check"]).exists() and (dest / "VERSION").exists() and not force:
        if (dest / "VERSION").read_text().strip() == spec["version"]:
            print(f"skip {name} {spec['version']} (present)")
            return
    CACHE.mkdir(parents=True, exist_ok=True)
    archive = CACHE / Path(spec["url"]).name
    if not archive.exists() or sha256(archive) != spec["sha256"]:
        for url in (spec["url"], spec.get("fallback_url")):
            if not url:
                continue
            try:
                print(f"download {url}")
                req = urllib.request.Request(url, headers={"User-Agent": "beamline-tools"})
                with urllib.request.urlopen(req, timeout=120) as r, open(archive, "wb") as f:
                    shutil.copyfileobj(r, f)
                break
            except OSError as e:
                print(f"  failed: {e}")
    got = sha256(archive)
    if got != spec["sha256"]:
        archive.unlink(missing_ok=True)
        raise SystemExit(f"SHA-256 mismatch for {name}: got {got}, want {spec['sha256']}")
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    if spec.get("single_file"):  # a jar is used as it is, not unpacked
        shutil.copyfile(archive, dest / spec["check"])
        (dest / "VERSION").write_text(spec["version"] + "\n")
        print(f"ok {name} {spec['version']} -> tools/bin/{name}/{spec['check']}")
        return
    with zipfile.ZipFile(archive) as z:
        for member in z.namelist():
            target = (dest / member).resolve()
            if not str(target).startswith(str(dest.resolve())):
                raise SystemExit(f"unsafe path in archive: {member}")
        z.extractall(dest)
    (dest / "VERSION").write_text(spec["version"] + "\n")
    print(f"ok {name} {spec['version']} -> tools/bin/{name}/{spec['check']}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("tools", nargs="*", default=list(TOOLS), help="subset of: " + ", ".join(TOOLS))
    a = ap.parse_args()
    for name in a.tools:
        fetch(name, TOOLS[name], a.force)
    return 0


if __name__ == "__main__":
    sys.exit(main())
