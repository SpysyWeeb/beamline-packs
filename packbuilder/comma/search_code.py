"""search-code.tar: Beamline's search code for the comma (docs/comma-pack-format.md 2, part `search_code`).

    python packbuilder/comma/search_code.py --out search-code.tar [--manifest <pack>/manifest.json] [--mtime SECONDS]
                                            [--rev COMMIT]
    python packbuilder/comma/search_code.py --verify search-code.tar [--manifest <pack>/manifest.json] [--rev COMMIT]

An uncompressed USTAR tar of the five files query.py reads at run time, at the top level (no
folders), from packbuilder/search of this checkout: query.py, lines.py, normalize.py, ranking.json,
abbreviations.json (line ends LF, owner 0, mode 0644, one fixed time). With --manifest the pack's
search part must have been built with this ranking.json (its `ranking_sha256`, the SHA-256 of the
file with LF line ends): the contract asks for the code of the commit that built the search file.
--rev takes the five files as committed at COMMIT instead (`git show`, read only), for a pack built
from an older commit than the checkout. --verify checks the member rule and, against this checkout
(or COMMIT), every file's bytes. Standard library and git only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tarfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import common  # noqa: E402
from common import SEARCH_CODE_FILES, Refused  # noqa: E402

SEARCH = common.REPO / "packbuilder" / "search"


def files(search_dir: Path = SEARCH, rev: str | None = None) -> dict[str, bytes]:
    """The five files with LF line ends: from search_dir, or as committed at rev (git show)."""
    out = {}
    for name in SEARCH_CODE_FILES:
        if rev:
            r = subprocess.run(["git", "-C", str(common.REPO), "show", f"{rev}:packbuilder/search/{name}"],
                               capture_output=True)
            if r.returncode != 0:
                raise Refused(f"git show {rev}:packbuilder/search/{name} failed: "
                              f"{r.stderr.decode('utf-8', 'replace').strip()[:200]}")
            out[name] = r.stdout.replace(b"\r\n", b"\n")
            continue
        p = Path(search_dir) / name
        if not p.is_file():
            raise Refused(f"{p} is missing")
        out[name] = common.text_bytes(p)
    return out


def ranking_of_manifest(manifest_path: Path | None) -> str | None:
    if manifest_path is None:
        return None
    m = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    sha = m.get("parts", {}).get("search", {}).get("ranking_sha256")
    if not sha:
        raise Refused(f"{manifest_path}: the search part has no ranking_sha256")
    return sha


def build(out: Path, mtime: int = 0, manifest: Path | None = None, search_dir: Path = SEARCH,
          rev: str | None = None) -> dict:
    data = files(search_dir, rev)
    want = ranking_of_manifest(manifest)
    have = hashlib.sha256(data["ranking.json"]).hexdigest()
    if want is not None and want != have:
        raise Refused(f"ranking.json here ({have[:12]}) is not the one the pack's search file was built with "
                      f"({want[:12]}): build search-code.tar from the commit that built the pack (--rev)")
    with common.TarOut(Path(out), mtime=mtime) as tar:
        for name in sorted(data):
            tar.add_bytes(name, data[name])
    return {"file": Path(out).name, "bytes": Path(out).stat().st_size, "sha256": common.sha256_file(out),
            "members": {n: hashlib.sha256(data[n]).hexdigest() for n in sorted(data)}}


def verify(tar_path: Path, manifest: Path | None = None, search_dir: Path | None = SEARCH,
           rev: str | None = None) -> dict:
    members = common.checked_members(tar_path, "search_code")
    names = {m.name for m in members}
    if names != set(SEARCH_CODE_FILES):
        raise Refused(f"{Path(tar_path).name}: members {sorted(names)}, the contract's five are {sorted(SEARCH_CODE_FILES)}")
    with tarfile.open(tar_path, "r:") as t:
        got = {m.name: t.extractfile(m).read() for m in members}
    want = ranking_of_manifest(manifest)
    if want is not None and hashlib.sha256(got["ranking.json"]).hexdigest() != want:
        raise Refused(f"{Path(tar_path).name}: ranking.json is not the one the pack's search file was built with")
    if search_dir is not None or rev:
        here = files(search_dir if search_dir is not None else SEARCH, rev)
        differ = [n for n in SEARCH_CODE_FILES if here[n] != got[n]]
        if differ:
            where = f"commit {rev}" if rev else "this checkout"
            raise Refused(f"{Path(tar_path).name}: {', '.join(differ)} differ from packbuilder/search of {where}")
    for n in ("ranking.json", "abbreviations.json"):
        try:
            json.loads(got[n].decode("utf-8"))
        except ValueError:
            raise Refused(f"{Path(tar_path).name}: {n} is not JSON") from None
    for n in ("query.py", "lines.py", "normalize.py"):
        try:
            compile(got[n].decode("utf-8"), n, "exec")
        except (SyntaxError, UnicodeDecodeError):
            raise Refused(f"{Path(tar_path).name}: {n} is not Python 3 source") from None
    return {n: hashlib.sha256(got[n]).hexdigest() for n in sorted(got)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--manifest", type=Path, help="the pack's manifest.json (its search part's ranking_sha256)")
    ap.add_argument("--mtime", type=int, default=0, help="the members' time (seconds; the pack's created time)")
    ap.add_argument("--rev", help="take the five files as committed at this commit (git show) instead of the checkout")
    ap.add_argument("--verify", type=Path, metavar="TAR")
    a = ap.parse_args(argv)
    try:
        if a.verify:
            print(json.dumps(verify(a.verify, a.manifest, SEARCH, a.rev), indent=1))
            print(f"{a.verify.name}: the five files, equal to packbuilder/search {'at ' + a.rev if a.rev else 'here'}")
            return 0
        if not a.out:
            ap.error("--out (or --verify) is needed")
        print(json.dumps(build(a.out, a.mtime, a.manifest, SEARCH, a.rev), indent=1))
    except Refused as e:
        print(f"refused: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
