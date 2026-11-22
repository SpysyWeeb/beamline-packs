"""The weekly build's check of one pack's three comma-only parts (docs/comma-pack-format.md 2, 4 and 5).

    python packbuilder/comma/check.py --pack data/out/packs/us-pa --parts data/out/comma/us-pa
                                      [--mapd data/comma/mapd-tool/mapd] [--json result.json] [--rev COMMIT]

- search-code.tar: the five files and nothing else; query.py, lines.py and normalize.py the frozen
  release pins.json pins; ranking.json and abbreviations.json equal to packbuilder/search of this
  checkout (or of --rev COMMIT, for a pack built from an older commit), its ranking.json the one the
  pack's search file was built with (the result also says, without failing, when packbuilder/search
  has moved ahead of the release);
- the search code answers: unpacked and imported in a separate Python, it finds the known places of
  search_probes.json in the pack's own search.sqlite (search_code.py --answers);
- roads.tar: the member rule, every tile against StarPilot decoder's checks (size, header, zlib,
  classes, line lengths), tiles of each of the zooms 8, 11 and 14, every tile touching the pack's bounds;
- mapd.tar: the member rule, exactly the two-degree groups covering the pack's bounds, all 64 cells of
  each group, mapd.json agreeing; with --mapd, Mapd's own admission of the unpacked archives.
Exit 1 when a part is missing or a check fails. Standard library only (the admission runs `mapd`, Linux).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import common  # noqa: E402
import mapd as mapd_part  # noqa: E402
import roads as roads_part  # noqa: E402
import search_code as search_code_part  # noqa: E402
from common import Refused  # noqa: E402


def check(pack: Path, parts: Path, mapd_bin: Path | None, rev: str | None = None) -> dict:
    manifest = json.loads((pack / "manifest.json").read_text(encoding="utf-8"))
    box, date = common.mapd_box(manifest["bounds"]), manifest["osm_snapshot"]
    results = {}

    def run(name, fn):
        try:
            results[name] = {"ok": True, **fn()}
        except (Refused, OSError, ValueError, KeyError) as e:
            results[name] = {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def search_code():
        return {"members": search_code_part.verify(parts / "search-code.tar", pack / "manifest.json",
                                                   search_code_part.SEARCH, rev),
                "release": search_code_part.pins()["release"],
                # a report, not a failure: the comma keeps running the release until a new one is made
                "packbuilder_search_moved_ahead": search_code_part.moved_ahead()}

    def search_answers():
        addresses = pack / "addresses.sqlite"
        return search_code_part.answers(parts / "search-code.tar", pack / "search.sqlite",
                                        addresses if addresses.is_file() else None, manifest["state"])

    def roads():
        return {"zooms": roads_part.verify(parts / "roads.tar", manifest["bounds"])}

    def mapd():
        tar = parts / "mapd.tar"
        rep = mapd_part.verify(tar, box, mapd_bin, None, date)
        info_path = tar.with_suffix(".json")
        if not info_path.is_file():
            raise Refused(f"{info_path.name} is missing")
        info = json.loads(info_path.read_text(encoding="utf-8"))
        if any(info.get(k) != v for k, v in box.items()) or info.get("source_date") != date:
            raise Refused(f"{info_path.name}: its box or source date is not the pack's")
        if info.get("sha256") != common.sha256_file(tar):
            raise Refused(f"{info_path.name} names another mapd.tar")
        if mapd_bin is not None and not rep.get("admitted"):
            raise Refused("Mapd did not admit the archives")
        return {**rep, "box": box, "source_date": date}

    for name, fn in (("search_code", search_code), ("search_answers", search_answers), ("roads", roads),
                     ("mapd", mapd)):
        run(name, fn)
    results["ok"] = all(v["ok"] for v in results.values())
    return results


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pack", required=True, type=Path, help="the pack folder (manifest.json)")
    ap.add_argument("--parts", required=True, type=Path, help="the folder of search-code.tar, roads.tar, mapd.tar")
    ap.add_argument("--mapd", type=Path, help="Mapd's program: run its admission of the archives too")
    ap.add_argument("--json", type=Path, help="write the result here too")
    ap.add_argument("--rev", help="compare search-code.tar with packbuilder/search as committed at this commit")
    a = ap.parse_args(argv)
    r = check(a.pack, a.parts, a.mapd, a.rev)
    text = json.dumps(r, indent=1)
    print(text)
    if a.json:
        a.json.parent.mkdir(parents=True, exist_ok=True)
        a.json.write_text(text + "\n", encoding="utf-8")
    print("comma parts: " + ("every check passed" if r["ok"] else "a check FAILED"))
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
