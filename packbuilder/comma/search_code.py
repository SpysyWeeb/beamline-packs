"""search-code.tar: Beamline's search code for the comma (docs/comma-pack-format.md 2, part `search_code`).

    python packbuilder/comma/search_code.py --out search-code.tar [--manifest <pack>/manifest.json] [--mtime SECONDS]
                                            [--rev COMMIT]
    python packbuilder/comma/search_code.py --verify search-code.tar [--manifest <pack>/manifest.json] [--rev COMMIT]
    python packbuilder/comma/search_code.py --answers search-code.tar --db <pack>/search.sqlite
                                            [--addresses <pack>/addresses.sqlite] --state PA
    python packbuilder/comma/search_code.py --status

An uncompressed USTAR tar of the five files query.py reads at run time, at the top level (no folders;
owner 0, mode 0644, one fixed time):

- query.py, lines.py, normalize.py: the frozen release of the code a comma runs, from
  packbuilder/comma/search-release, never from packbuilder/search. Each must have the size and SHA-256
  that pins.json "search_code" pins (the bytes git stores, LF line ends: a checkout with CRLF line ends
  gives the same bytes); StarPilot's source pins the same SHA-256, so a pack can never bring code it has
  not named. A new release is a deliberate act (pins.json's comment): --status says when
  packbuilder/search has moved ahead of the release.
- ranking.json, abbreviations.json: the pack's own, from packbuilder/search of this checkout (LF line
  ends), or as committed at COMMIT with --rev (`git show`, read only) for a pack built from an older
  commit. With --manifest the pack's search part must have been built with this ranking.json (its
  `ranking_sha256`, the SHA-256 of the file with LF line ends).

--verify checks the member rule, the three .py members against the pins and, against this checkout (or
COMMIT), the two data files. --answers proves the code still answers from a pack's search file: the
tar (checked first) is unpacked into a temporary folder and a separate Python (`-I -S`: nothing of
this checkout or of site-packages on its path) imports query from there the way StarPilot does, opens
the search file (and the house numbers) and asks the known queries of search_probes.json for the state
(a state without any there: its own most important place, by name); each must find its place among the
first results. Standard library and git only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import common  # noqa: E402
from common import SEARCH_CODE_FILES, Refused  # noqa: E402

SEARCH = common.REPO / "packbuilder" / "search"
RELEASE = HERE / "search-release"                    # the frozen release (pins.json "search_code")
CODE_FILES = ("query.py", "lines.py", "normalize.py")            # from RELEASE, pinned
DATA_FILES = ("ranking.json", "abbreviations.json")              # the pack's own, from packbuilder/search
PROBES = HERE / "search_probes.json"
ANSWER_TIME_LIMIT_S = 300
ANSWER_LIMIT = 8                                     # StarPilot asks for 8 results (beamline_places.MAX_RESULTS)

assert sorted(CODE_FILES + DATA_FILES) == sorted(SEARCH_CODE_FILES)


# ------------------------------------------------------------------ the frozen release
def pins() -> dict:
    """pins.json "search_code": {"release", "folder", "files": {name: {"bytes", "sha256"}}}."""
    p = common.load_pins().get("search_code")
    if not (isinstance(p, dict) and isinstance(p.get("files"), dict) and sorted(p["files"]) == sorted(CODE_FILES)):
        raise Refused(f"pins.json: \"search_code\" must pin exactly {', '.join(CODE_FILES)}")
    if p.get("folder") != RELEASE.relative_to(common.REPO).as_posix():
        raise Refused(f"pins.json: \"search_code\" names the folder {p.get('folder')!r}, the release is in "
                      f"{RELEASE.relative_to(common.REPO).as_posix()}")
    return p


def check_pinned(name: str, data: bytes, where: str) -> None:
    want = pins()["files"][name]
    have = hashlib.sha256(data).hexdigest()
    if len(data) != want["bytes"] or have != want["sha256"]:
        raise Refused(f"{where}: {name} is {len(data)} bytes with SHA-256 {have[:12]}, not the pinned release "
                      f"{pins()['release']} ({want['bytes']} bytes, {want['sha256'][:12]}): a comma runs only pinned "
                      f"code (pins.json \"search_code\")")


def release_files(folder: Path = RELEASE) -> dict[str, bytes]:
    """The three .py of the frozen release as git stores them (CRLF read as LF), each refused unless it
    has its pinned size and SHA-256."""
    out = {}
    for name in CODE_FILES:
        p = Path(folder) / name
        if not p.is_file():
            raise Refused(f"{p} is missing")
        out[name] = common.text_bytes(p)
        check_pinned(name, out[name], f"{Path(folder).name}/{name}")
    return out


def moved_ahead(search_dir: Path = SEARCH, folder: Path = RELEASE) -> list[str]:
    """The code files whose copy in packbuilder/search differs from the frozen release (a report, not a
    failure: the comma keeps running the release until a new one is made on purpose)."""
    out = []
    for name in CODE_FILES:
        a, b = Path(search_dir) / name, Path(folder) / name
        if not (a.is_file() and b.is_file()) or common.text_bytes(a) != common.text_bytes(b):
            out.append(name)
    return out


def data_files(search_dir: Path = SEARCH, rev: str | None = None) -> dict[str, bytes]:
    """ranking.json and abbreviations.json with LF line ends: from search_dir, or as committed at rev."""
    out = {}
    for name in DATA_FILES:
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


def files(search_dir: Path = SEARCH, rev: str | None = None, release: Path = RELEASE) -> dict[str, bytes]:
    """The five members: the frozen release's code and the pack's two data files."""
    return {**release_files(release), **data_files(search_dir, rev)}


# ------------------------------------------------------------------ build and verify
def ranking_of_manifest(manifest_path: Path | None) -> str | None:
    if manifest_path is None:
        return None
    m = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    sha = m.get("parts", {}).get("search", {}).get("ranking_sha256")
    if not sha:
        raise Refused(f"{manifest_path}: the search part has no ranking_sha256")
    return sha


def build(out: Path, mtime: int = 0, manifest: Path | None = None, search_dir: Path = SEARCH,
          rev: str | None = None, release: Path = RELEASE) -> dict:
    data = files(search_dir, rev, release)
    want = ranking_of_manifest(manifest)
    have = hashlib.sha256(data["ranking.json"]).hexdigest()
    if want is not None and want != have:
        raise Refused(f"ranking.json here ({have[:12]}) is not the one the pack's search file was built with "
                      f"({want[:12]}): build search-code.tar from the commit that built the pack (--rev)")
    with common.TarOut(Path(out), mtime=mtime) as tar:
        for name in sorted(data):
            tar.add_bytes(name, data[name])
    return {"file": Path(out).name, "bytes": Path(out).stat().st_size, "sha256": common.sha256_file(out),
            "release": pins()["release"], "members": {n: hashlib.sha256(data[n]).hexdigest() for n in sorted(data)}}


def verify(tar_path: Path, manifest: Path | None = None, search_dir: Path | None = SEARCH,
           rev: str | None = None) -> dict:
    """The member rule; query.py, lines.py and normalize.py are the pinned release (always); ranking.json
    is the pack's (with manifest); the two data files equal packbuilder/search here (search_dir) or at
    rev; the JSON files parse and the code compiles. Returns each member's SHA-256."""
    members = common.checked_members(tar_path, "search_code")
    names = {m.name for m in members}
    if names != set(SEARCH_CODE_FILES):
        raise Refused(f"{Path(tar_path).name}: members {sorted(names)}, the contract's five are {sorted(SEARCH_CODE_FILES)}")
    with tarfile.open(tar_path, "r:") as t:
        got = {m.name: t.extractfile(m).read() for m in members}
    for n in CODE_FILES:
        check_pinned(n, got[n], Path(tar_path).name)
    want = ranking_of_manifest(manifest)
    if want is not None and hashlib.sha256(got["ranking.json"]).hexdigest() != want:
        raise Refused(f"{Path(tar_path).name}: ranking.json is not the one the pack's search file was built with")
    if search_dir is not None or rev:
        here = data_files(search_dir if search_dir is not None else SEARCH, rev)
        differ = [n for n in DATA_FILES if here[n] != got[n]]
        if differ:
            where = f"commit {rev}" if rev else "this checkout"
            raise Refused(f"{Path(tar_path).name}: {', '.join(differ)} differ from packbuilder/search of {where}")
    for n in DATA_FILES:
        try:
            json.loads(got[n].decode("utf-8"))
        except ValueError:
            raise Refused(f"{Path(tar_path).name}: {n} is not JSON") from None
    for n in CODE_FILES:
        try:
            compile(got[n].decode("utf-8"), n, "exec")
        except (SyntaxError, UnicodeDecodeError):
            raise Refused(f"{Path(tar_path).name}: {n} is not Python 3 source") from None
    return {n: hashlib.sha256(got[n]).hexdigest() for n in sorted(got)}


# ------------------------------------------------------------------ does the code answer from the pack's search file
# Run as `python -I -S -c CHILD <code folder> <search.sqlite> <addresses.sqlite or ""> <queries JSON>`: the
# code folder alone is put on the path (as StarPilot's beamline_places.load_pack_code does), query is
# refused unless it is the folder's, the engine is opened the way StarPilot opens it and each query is
# asked with StarPilot's limit. Prints one JSON line: per query, the results' title, subtitle, place, kind.
CHILD = r"""
import json, os, sys
code, db, houses, queries = sys.argv[1], sys.argv[2], sys.argv[3], json.loads(sys.argv[4])
sys.path.insert(0, code)
import query
if os.path.dirname(os.path.abspath(query.__file__)) != os.path.abspath(code):
    raise SystemExit("the query module imported is not the pack's: " + str(query.__file__))
engine = query.SearchEngine(db, addresses_path=houses or None)
out = []
for q in queries:
    near = q.get("near") or (None, None)
    found = engine.search(q["query"], near[0], near[1], limit=q["limit"])
    rows = found.get("results") if isinstance(found, dict) else None
    out.append([{k: r.get(k) for k in ("title", "subtitle", "lat", "lon", "kind")} for r in (rows or [])])
print(json.dumps(out))
"""


def ask(code_dir: Path, db: Path, addresses: Path | None, queries: list[dict], python: str = sys.executable,
        time_limit_s: float = ANSWER_TIME_LIMIT_S) -> list[list[dict]]:
    """The results of each query (with "query", optional "near" [lat, lon]) from the code in code_dir,
    run in a separate Python. Refused when it fails, prints no answer or runs past the time limit."""
    qs = [{"query": q["query"], "near": q.get("near"), "limit": ANSWER_LIMIT} for q in queries]
    # absolute paths: the separate Python runs in the code's folder
    cmd = [python, "-I", "-S", "-c", CHILD, str(Path(code_dir).resolve()), str(Path(db).resolve()),
           str(Path(addresses).resolve()) if addresses else "", json.dumps(qs)]
    try:
        r = subprocess.run(cmd, capture_output=True, cwd=str(code_dir), timeout=time_limit_s)
    except subprocess.TimeoutExpired:
        raise Refused(f"the search code gave no answer within {time_limit_s:g} s") from None
    err = r.stderr.decode("utf-8", "replace").strip()
    if r.returncode != 0:
        raise Refused(f"the search code failed in a separate Python (exit {r.returncode}): {err[-600:]}")
    try:
        out = json.loads(r.stdout.decode("utf-8").strip().splitlines()[-1])
    except (ValueError, IndexError, UnicodeDecodeError):
        raise Refused(f"the search code printed no answer ({err[-300:]})") from None
    if not (isinstance(out, list) and len(out) == len(qs) and all(isinstance(x, list) for x in out)):
        raise Refused("the search code's answer is not one list of results per query")
    return out


def km(a, b) -> float:
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(b[1] - a[1]) / 2) ** 2
    return 2 * 6371.0088 * math.asin(math.sqrt(min(1.0, h)))


def judge(probe: dict, results: list[dict]) -> tuple[dict | None, str]:
    """(the result that answers the probe, "") or (None, why not): a result among the first probe["top"]
    whose title contains probe["title"] (case ignored) and lies within probe["within_km"] of probe["at"]."""
    top = results[:int(probe.get("top", 3))]
    for r in top:
        try:
            title, where = str(r.get("title") or ""), (float(r["lat"]), float(r["lon"]))
        except (KeyError, TypeError, ValueError):
            continue
        if probe["title"].lower() in title.lower() and km(where, probe["at"]) <= float(probe["within_km"]):
            return {"title": title, "subtitle": r.get("subtitle"), "km": round(km(where, probe["at"]), 2)}, ""
    seen = "; ".join(f"{r.get('title')} ({r.get('subtitle')})" for r in top) or "no results"
    return None, (f"{probe['query']!r}: no {probe['title']!r} within {probe['within_km']} km of {probe['at']} "
                  f"among the first {len(top) or probe.get('top', 3)} (got: {seen})")


def probes_for(state: str, db: Path) -> tuple[list[dict], str]:
    """(the probes of search_probes.json for the state, "known") or, for a state without any, (the
    state's own most important place asked by its name, "own place")."""
    table = json.loads(PROBES.read_text(encoding="utf-8"))
    known = table.get(str(state).upper())
    if known:
        return known, "known"
    con = None
    try:
        con = sqlite3.connect(f"{Path(db).resolve().as_uri()}?mode=ro", uri=True)
        row = con.execute("SELECT name, lat, lon FROM entry WHERE kind = 1 ORDER BY importance DESC LIMIT 1").fetchone()
    except sqlite3.Error as e:
        raise Refused(f"{Path(db).name}: its most important place cannot be read ({e})") from None
    finally:
        if con is not None:
            con.close()
    if not row:
        raise Refused(f"{Path(db).name}: no place to ask for")
    return [{"query": row[0], "near": None, "title": row[0], "at": [row[1], row[2]], "within_km": 2, "top": 3}], "own place"


def answers(tar_path: Path, db: Path, addresses: Path | None, state: str, python: str = sys.executable,
            time_limit_s: float = ANSWER_TIME_LIMIT_S) -> dict:
    """The tar (verify first: only pinned code is ever run) unpacked into a temporary folder; its code asked
    the probes of the state from the search file db (with the house numbers when given). Refused unless
    every probe finds its place."""
    verify(tar_path, None, None)
    if not Path(db).is_file():
        raise Refused(f"{db} is missing")
    probes, kind = probes_for(state, db)
    with tempfile.TemporaryDirectory(prefix="search-code-") as d:
        code = Path(d) / "search"
        common.unpack(tar_path, code, "search_code")
        got = ask(code, Path(db), Path(addresses) if addresses and Path(addresses).is_file() else None, probes,
                  python, time_limit_s)
    report, bad = [], []
    for probe, results in zip(probes, got):
        hit, why = judge(probe, results)
        if hit is None:
            bad.append(why)
        else:
            report.append({"query": probe["query"], **hit})
    if bad:
        raise Refused("the search code does not answer from this search file: " + " | ".join(bad))
    return {"probes": kind, "answered": report}


# ------------------------------------------------------------------ main
def status() -> dict:
    p = pins()
    out = {"release": p["release"], "folder": p["folder"]}
    try:
        release_files()
        out["release_matches_pins"] = True
    except Refused as e:
        out["release_matches_pins"] = False
        out["problem"] = str(e)
    ahead = moved_ahead()
    out["packbuilder_search_moved_ahead"] = ahead
    now = {}
    for n in ahead:                      # what the pins of a new release would be
        if (SEARCH / n).is_file():
            b = common.text_bytes(SEARCH / n)
            now[n] = {"bytes": len(b), "sha256": hashlib.sha256(b).hexdigest()}
    if now:
        out["packbuilder_search_now"] = now
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--manifest", type=Path, help="the pack's manifest.json (its search part's ranking_sha256)")
    ap.add_argument("--mtime", type=int, default=0, help="the members' time (seconds; the pack's created time)")
    ap.add_argument("--rev", help="take ranking.json and abbreviations.json as committed at this commit")
    ap.add_argument("--verify", type=Path, metavar="TAR")
    ap.add_argument("--answers", type=Path, metavar="TAR", help="ask the tar's code the known queries (--db, --state)")
    ap.add_argument("--db", type=Path, help="with --answers: the pack's search.sqlite")
    ap.add_argument("--addresses", type=Path, help="with --answers: the pack's addresses.sqlite")
    ap.add_argument("--state", help="with --answers: the pack's state (PA)")
    ap.add_argument("--status", action="store_true", help="the frozen release against its pins and packbuilder/search")
    a = ap.parse_args(argv)
    try:
        if a.status:
            s = status()
            print(json.dumps(s, indent=1))
            if s["packbuilder_search_moved_ahead"]:
                print(f"packbuilder/search has moved ahead of release {s['release']} "
                      f"({', '.join(s['packbuilder_search_moved_ahead'])}): the comma keeps running the release; a new "
                      f"one is made on purpose (pins.json's comment)")
            return 0 if s["release_matches_pins"] else 1
        if a.answers:
            if not (a.db and a.state):
                ap.error("--answers needs --db and --state")
            print(json.dumps(answers(a.answers, a.db, a.addresses, a.state), indent=1))
            print(f"{a.answers.name}: the search code answers every known query from {a.db.name}")
            return 0
        if a.verify:
            print(json.dumps(verify(a.verify, a.manifest, SEARCH, a.rev), indent=1))
            print(f"{a.verify.name}: the pinned release {pins()['release']} and the data files of packbuilder/search "
                  f"{'at ' + a.rev if a.rev else 'here'}")
            return 0
        if not a.out:
            ap.error("--out (or --verify, --answers, --status) is needed")
        print(json.dumps(build(a.out, a.mtime, a.manifest, SEARCH, a.rev), indent=1))
    except Refused as e:
        print(f"refused: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
