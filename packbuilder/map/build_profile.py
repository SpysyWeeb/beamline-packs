"""Compile the Protomaps basemap profile (a Planetiler application) into one jar.

Usage (from the repo root):
    .venv/map/Scripts/python packbuilder/map/build_profile.py            # the Beamline jar (used by build_pack.py)
    .venv/map/Scripts/python packbuilder/map/build_profile.py --stock    # the unpatched jar of the first build
    options: --force

Steps:
  1. git clone https://github.com/protomaps/basemaps into data/map/src/basemaps
     and check out the commit pinned in versions.json (tiles 4.15.2, which
     depends on Planetiler 0.10.2).
  2. Beamline jar only: a fresh local clone of that checkout in
     data/map/src/basemaps-beamline, with packbuilder/map/profile/beamline-profile.patch
     applied (git apply). The patch adds lane tags on motorway/trunk/primary
     roads, an optional clamp of z15-only features (POIs, house numbers,
     private roads) into a lower max zoom, and a list of POI kinds to leave out.
     Every option is off unless build_pack.py passes it, so the patched jar
     writes the stock tiles when called with no --beamline_* argument.
  3. mvn package with the portable Maven from tools/fetch_map_tools.py, with its
     local repository in data/map/m2 (so nothing is written to ~/.m2).
  4. Copy the fat jar to data/map/bin/protomaps-basemap-<tiles version>[-beamline<N>].jar.
     For the Beamline jar, <jar>.json records the patch's SHA-256; a changed
     patch makes the next run rebuild.

Re-running skips all of this if the jar exists (and, for the Beamline jar, the
patch is unchanged), unless --force.
"""
import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
VERSIONS = json.loads((HERE / "versions.json").read_text())
PATCH = HERE / "profile" / "beamline-profile.patch"


def jar_path(stock: bool = False) -> Path:
    v = VERSIONS["protomaps_basemaps"]["tiles_version"]
    if stock:
        return REPO / "data" / "map" / "bin" / f"protomaps-basemap-{v}.jar"
    n = VERSIONS["beamline_profile_patch"]["version"]
    return REPO / "data" / "map" / "bin" / f"protomaps-basemap-{v}-beamline{n}.jar"


def patch_sha256() -> str:
    return hashlib.sha256(PATCH.read_bytes()).hexdigest()


def run(cmd, cwd=None):
    print("+", " ".join(str(c) for c in cmd), flush=True)
    subprocess.run([str(c) for c in cmd], cwd=cwd, check=True)


def checkout(src: Path, url: str, commit: str):
    if not (src / ".git").exists():
        src.parent.mkdir(parents=True, exist_ok=True)
        run(["git", "clone", "--filter=blob:none", url, src])
    run(["git", "-C", src, "fetch", "--quiet", "origin", commit])
    run(["git", "-C", src, "checkout", "--quiet", "--detach", commit])
    head = subprocess.run(["git", "-C", str(src), "rev-parse", "HEAD"], capture_output=True, text=True,
                          check=True).stdout.strip()
    assert head == commit, head


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stock", action="store_true", help="build the unpatched profile jar")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    jar = jar_path(a.stock)
    stamp = jar.with_name(jar.name + ".json")
    if jar.exists() and not a.force:
        if a.stock or (stamp.exists() and json.loads(stamp.read_text()).get("patch_sha256") == patch_sha256()):
            print(f"skip: {jar.relative_to(REPO)} exists")
            return 0
        print("the patch changed since the jar was built: rebuilding")
    pin = VERSIONS["protomaps_basemaps"]
    src = REPO / "data" / "map" / "src" / "basemaps"
    checkout(src, pin["repo"], pin["commit"])
    if not a.stock:
        work = REPO / "data" / "map" / "src" / "basemaps-beamline"
        if work.exists():
            shutil.rmtree(work, onerror=lambda f, p, e: (Path(p).chmod(0o666), f(p)))
        run(["git", "clone", "--quiet", "--no-hardlinks", src, work])
        run(["git", "-C", work, "checkout", "--quiet", "--detach", pin["commit"]])
        run(["git", "-C", work, "apply", "--whitespace=nowarn", PATCH])
        src = work
    mvn_bin = REPO / "tools" / "bin" / "maven" / f"apache-maven-{VERSIONS['maven']['version']}" / "bin"
    # Windows runs mvn.cmd; elsewhere the POSIX script, through sh (an unzipped file has no execute bit)
    mvn = [mvn_bin / "mvn.cmd"] if sys.platform == "win32" else ["sh", mvn_bin / "mvn"]
    if not Path(mvn[-1]).exists():
        raise SystemExit("Maven missing: run  python tools/fetch_map_tools.py  first")
    m2 = REPO / "data" / "map" / "m2"
    run([*mvn, "-B", "-q", f"-Dmaven.repo.local={m2}", "-DskipTests", "-Dspotless.check.skip=true",
         "clean", "package"], cwd=src / "tiles")
    built = list((src / "tiles" / "target").glob("*-with-deps.jar"))
    if len(built) != 1:
        raise SystemExit(f"expected one *-with-deps.jar, found {built}")
    jar.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(built[0], jar)
    if not a.stock:
        stamp.write_text(json.dumps({"patch": PATCH.relative_to(REPO).as_posix(), "patch_sha256": patch_sha256(),
                                     "base_commit": pin["commit"]}, indent=1))
    print(f"ok {jar.relative_to(REPO)} ({jar.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
