"""comma.json: the comma catalog (docs/comma-pack-format.md section 2), beside the phone's catalog.json.

    python packbuilder/comma/catalog.py --site data/out/site --parts data/out/comma [--check]
    python packbuilder/comma/catalog.py --site data/out/site --check-only

Run after packbuilder/catalog/build_catalog.py has written the site: reads its catalog/catalog.json
(which this never changes) and, for each pack there, the pack's manifest.json from its release
folder and the three comma-only parts from <parts>/<pack id>/ (search-code.tar, roads.tar, mapd.tar,
each checked with its builder's verify before use; Mapd's admission is check.py's). Then:

- the comma parts' transfer files go into the pack's release folder with the phone builder's rules
  (docs/catalog-format.md 2: `<file>.gz` by gzip -6 with no name and no time when the part is at least
  65,536 bytes and shrinks to 90 % or less, else `<file>` itself), recorded in comma-transfer.json there
  so a re-run reuses them;
- `routing`, `search` and `addresses` name the very same release files as catalog.json (the comma
  downloads the phone's assets);
- `engines` is the list of pins.json, each url built from its PyPI digest;
- catalog/comma.json is written (UTF-8, LF, through a temporary file): format
  beamline-comma-catalog/1, `serial` and `generated_utc` the build time (SOURCE_DATE_EPOCH when set,
  the clock otherwise; the serial always above that of a comma.json already in the site, as the second
  after it when needed, so it stays a real UTC time YYYYMMDDhhmmss; a serial that is not such a time
  or lies more than two days ahead of this clock is refused, never written), the licence of
  catalog.json, one entry per pack whose `credit` joins the credits of the manifest and of the parts it
  names. A run that changes nothing leaves the file as it is.

A pack of catalog.json without comma parts keeps the entry an earlier comma.json in the site has
for the same build; with none, the run is refused. --check re-reads every file comma.json names
(size and SHA-256) and checks the format again (the serial's rule included), --check-only does only
that. Standard library only.
"""
from __future__ import annotations

import argparse
import datetime
import gzip
import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "catalog"))
import build_catalog as bc  # noqa: E402  (the phone catalog's writer: its transfer rule and constants)
import catalog_format as cf  # noqa: E402
import common  # noqa: E402
import mapd as mapd_part  # noqa: E402
import roads as roads_part  # noqa: E402
import search_code as search_code_part  # noqa: E402
from common import Refused  # noqa: E402

FORMAT = "beamline-comma-catalog/1"
FORMAT_PREFIX = "beamline-comma-catalog/"
SHARED_PARTS = ("routing", "search", "addresses")                  # the phone's own release files
COMMA_PARTS = {"search_code": "search-code.tar", "roads": "roads.tar", "mapd": "mapd.tar"}
PART_ORDER = ("routing", "search", "addresses", "search_code", "roads", "mapd")
OPTIONAL = {"addresses", "mapd"}
REQUIRED = {"routing", "search", "search_code", "roads"}
TRANSFER_TABLE = "comma-transfer.json"
PYPI_FILES = "https://files.pythonhosted.org/packages/"
WHEEL = re.compile(r"^pyvalhalla-[0-9][0-9a-z.]*-cp3[0-9]+-[a-z0-9_]+-[a-z0-9_]+\.whl$")
SERIAL_FORMAT = "%Y%m%d%H%M%S"
SERIAL_AHEAD_MAX_S = 2 * 86400        # a comma refuses a serial more than two days ahead of its clock


# ------------------------------------------------------------------ the serial (comma-pack-format.md 2)
def serial_time(serial) -> datetime.datetime:
    """The UTC time a serial names: the 14 digits YYYYMMDDhhmmss of a real date and time. ValueError
    otherwise (a number such as 20261122101560, one second "after" 20261122101559, is not a time)."""
    s = str(serial) if cf._int(serial) else ""
    if not re.fullmatch(r"[1-9][0-9]{13}", s):
        raise ValueError(f"serial {serial!r} is not 14 digits YYYYMMDDhhmmss")
    try:
        return datetime.datetime(int(s[0:4]), int(s[4:6]), int(s[6:8]), int(s[8:10]), int(s[10:12]), int(s[12:14]),
                                 tzinfo=datetime.timezone.utc)
    except ValueError:
        raise ValueError(f"serial {serial} is not a real UTC time YYYYMMDDhhmmss") from None


def check_serial(serial, now: datetime.datetime | None = None) -> None:
    """ValueError unless the serial is a real UTC time at most two days ahead of now (this clock)."""
    t = serial_time(serial)
    now = now or datetime.datetime.now(datetime.timezone.utc)
    if (t - now).total_seconds() > SERIAL_AHEAD_MAX_S:
        raise ValueError(f"serial {serial} is more than two days ahead of this clock "
                         f"({now.strftime('%Y-%m-%dT%H:%M:%SZ')}): a comma refuses it")


def next_serial(t: datetime.datetime, previous) -> int:
    """The serial of a catalog written at t: t itself, or the second after the previous serial when that
    is not lower (a serial only grows, and stays a real time)."""
    serial = int(t.strftime(SERIAL_FORMAT))
    if previous is not None:
        last = serial_time(previous)
        if serial <= previous:
            serial = int((last + datetime.timedelta(seconds=1)).strftime(SERIAL_FORMAT))
    return serial


# ------------------------------------------------------------------ engines (pins.json)
def pypi_url(blake2b_256: str, file: str) -> str:
    """PyPI's address of a file: packages/<2>/<2>/<60>/<file>, the folders being the file's BLAKE2b-256
    (the "url" PyPI's JSON gives). pins.json keeps the digest, not the url: the packs repository's
    privacy scan refuses the url's 60-digit hex run."""
    if not re.fullmatch(r"[0-9a-f]{64}", blake2b_256 or ""):
        raise Refused(f"pins.json: {file}: blake2b_256 is not 64 hex digits")
    return f"{PYPI_FILES}{blake2b_256[:2]}/{blake2b_256[2:4]}/{blake2b_256[4:]}/{file}"


def engines() -> list[dict]:
    """The engines of pins.json as comma.json lists them."""
    out = []
    for e in common.load_pins()["engines"]:
        out.append({"valhalla_version": e["valhalla_version"], "machine": e["machine"], "python": e["python"],
                    "file": e["file"], "url": pypi_url(e.get("blake2b_256", ""), e["file"]),
                    "bytes": e["bytes"], "sha256": e["sha256"]})
    for e in out:
        check_engine(e)
    return out


def check_engine(e) -> None:
    ok = (isinstance(e, dict) and all(isinstance(e.get(k), str) and e[k] for k in ("valhalla_version", "machine", "python"))
          and isinstance(e.get("file"), str) and WHEEL.match(e["file"])
          and isinstance(e.get("url"), str) and e["url"].startswith("https://") and e["url"].endswith("/" + e["file"])
          and not any(c.isspace() for c in e["url"]) and len(e["url"]) <= 512
          and cf._int(e.get("bytes")) and 0 < e["bytes"] <= cf.MAX_FILE_BYTES
          and isinstance(e.get("sha256"), str) and cf.SHA256.match(e["sha256"]))
    if not ok:
        raise ValueError("an engine entry breaks the format")


# ------------------------------------------------------------------ transfer files
def make_transfer(src: Path, release: Path, table: dict) -> tuple[dict, str, int, bool]:
    """(transfer.json-style entry, part sha256, part bytes, reused) of one comma part file, written into
    the release folder by the phone builder's rule (build_catalog.make_transfer, for a plain file)."""
    size = src.stat().st_size
    sha = common.sha256_file(src)
    old = table.get(src.name)
    if isinstance(old, dict) and old.get("part_sha256") == sha:
        p = release / str(old.get("path"))
        if p.is_file() and p.stat().st_size == old.get("bytes") and common.sha256_file(p) == old.get("sha256"):
            return old, sha, size, True
    gz_tmp = release / (src.name + ".gz.tmp")
    entry = None
    if size >= bc.GZIP_MIN_BYTES:
        with open(src, "rb") as f, open(gz_tmp, "wb") as raw:
            w = bc.HashingWriter(raw)
            with gzip.GzipFile(filename="", mode="wb", compresslevel=6, mtime=0, fileobj=w) as gz:
                for block in iter(lambda: f.read(bc.CHUNK), b""):
                    gz.write(block)
        if w.n <= bc.GZIP_MAX_RATIO * size:
            out = release / (src.name + ".gz")
            os.replace(gz_tmp, out)
            entry = {"path": out.name, "encoding": "gzip", "bytes": w.n, "sha256": w.h.hexdigest()}
        gz_tmp.unlink(missing_ok=True)
    if entry is None:
        out = release / src.name
        out.unlink(missing_ok=True)
        try:
            os.link(src, out)
        except OSError:
            shutil.copyfile(src, out)
        entry = {"path": out.name, "encoding": "identity", "bytes": size, "sha256": sha}
    stale = release / (src.name if entry["encoding"] == "gzip" else src.name + ".gz")
    stale.unlink(missing_ok=True)
    if common.sha256_file(src) != sha:
        raise Refused(f"{src}: changed while its transfer file was written")
    entry["part_sha256"] = sha
    if entry["bytes"] >= bc.ASSET_LIMIT:
        raise Refused(f"{entry['path']} is too large for one release asset ({entry['bytes']} bytes)")
    return entry, sha, size, False


# ------------------------------------------------------------------ one pack
def credit_of(manifest: dict, names) -> str:
    items = []
    for text in [manifest.get("attribution", "")] + [manifest["parts"][n].get("attribution", "") for n in names]:
        for item in str(text).split(";"):
            item = item.strip()
            if item and item not in items:
                items.append(item)
    credit = "; ".join(items)
    if not credit or len(credit) > cf.MAX_CREDIT:
        raise Refused(f"{manifest.get('pack_id')}: the joined credit is empty or longer than {cf.MAX_CREDIT}")
    return credit


def pack_entry(phone: dict, site: Path, parts_dir: Path) -> dict:
    release_name = phone["manifest"]["path"].split("/")[1]
    release = site / release_name
    mbytes = (release / "manifest.json").read_bytes()
    if hashlib.sha256(mbytes).hexdigest() != phone["manifest"]["sha256"]:
        raise Refused(f"{release_name}/manifest.json differs from catalog.json's")
    m = json.loads(mbytes.decode("utf-8").lstrip("﻿"))
    for k_cat, k_man in (("id", "pack_id"), ("state", "state"), ("name", "name"), ("osm_snapshot", "osm_snapshot")):
        if phone[k_cat] != m[k_man]:
            raise Refused(f"{release_name}: catalog.json's {k_cat} differs from the manifest's")
    by_name = {p["name"]: p for p in phone["parts"]}
    parts = []
    for name in SHARED_PARTS:
        if name not in by_name:
            if name in REQUIRED:
                raise Refused(f"{release_name}: catalog.json has no {name} part")
            continue
        p = by_name[name]
        parts.append({"name": name, "file": p["file"], "optional": name in OPTIONAL, "bytes": p["bytes"],
                      "sha256": p["sha256"], "transfer": dict(p["transfer"])})
    box = common.mapd_box(m["bounds"])
    files = {n: parts_dir / f for n, f in COMMA_PARTS.items()}
    missing = [str(p) for p in files.values() if not p.is_file()]
    if missing:
        raise Refused(f"{m['pack_id']}: comma part missing: {', '.join(missing)}")
    roads_part.verify(files["roads"], m["bounds"])
    search_code_part.verify(files["search_code"], release / "manifest.json", None)   # its ranking.json is the pack's
    mapd_part.verify(files["mapd"], box, None)
    info_path = files["mapd"].with_suffix(".json")
    if info_path.is_file():
        info = json.loads(info_path.read_text(encoding="utf-8"))
        if any(info.get(k) != v for k, v in box.items()) or info.get("source_date") != m["osm_snapshot"]:
            raise Refused(f"{info_path}: its box or source date is not the pack's")
        if info.get("sha256") not in (None, common.sha256_file(files["mapd"])):
            raise Refused(f"{info_path}: names another mapd.tar")
    table_path = release / TRANSFER_TABLE
    try:
        table = json.loads(table_path.read_text(encoding="utf-8")) if table_path.is_file() else {}
    except ValueError:
        table = {}
    new_table = {}
    for name in ("search_code", "roads", "mapd"):
        t, sha, size, reused = make_transfer(files[name], release, table)
        new_table[files[name].name] = t
        part = {"name": name, "file": files[name].name, "optional": name in OPTIONAL, "bytes": size, "sha256": sha,
                "transfer": {"path": f"../{release_name}/{t['path']}", "encoding": t["encoding"],
                             "bytes": t["bytes"], "sha256": t["sha256"]}}
        if name == "mapd":
            part["mapd"] = {**box, "source_date": m["osm_snapshot"]}
        parts.append(part)
        print(f"  {name}: {t['encoding']} {t['bytes']:,} bytes{' (reused)' if reused else ''}")
    tmp = table_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(new_table, indent=1, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    os.replace(tmp, table_path)
    parts.sort(key=lambda p: PART_ORDER.index(p["name"]))
    return {"id": m["pack_id"], "state": m["state"], "name": m["name"], "pack_version": phone["pack_version"],
            "osm_snapshot": m["osm_snapshot"], "bounds": m["bounds"],
            "valhalla_version": m["parts"]["routing"]["valhalla_version"],
            "credit": credit_of(m, [p["name"] for p in parts if p["name"] in SHARED_PARTS]),
            "parts": parts}


# ------------------------------------------------------------------ the format, as a reader checks it
def check_mapd_object(o) -> bool:
    if not isinstance(o, dict):
        return False
    nums = [o.get(k) for k in ("min_lat", "min_lon", "max_lat", "max_lon")]
    if not all(cf._int(v) and v % 2 == 0 for v in nums):
        return False
    try:
        datetime.date.fromisoformat(o.get("source_date", ""))
    except (TypeError, ValueError):
        return False
    return -90 <= nums[0] < nums[2] <= 90 and -180 <= nums[1] < nums[3] <= 180


def parse(data: bytes, catalog_url: str, now: datetime.datetime | None = None) -> dict:
    """The catalog, or ValueError naming the first field that breaks comma-pack-format.md 2 (a stricter check
    than a reader's: the writer must never publish what a reader could refuse). The serial must be a real
    UTC time at most two days ahead of now (this clock, unless given)."""
    if len(data) > cf.CATALOG_MAX_BYTES:
        raise ValueError("larger than 2 MiB")
    root = json.loads(data.decode("utf-8"))
    if not isinstance(root, dict) or root.get("format") != FORMAT:
        raise ValueError("format")
    if not cf._int(root.get("serial")) or root["serial"] <= 0:
        raise ValueError("serial")
    check_serial(root["serial"], now)
    if not (isinstance(root.get("generated_utc"), str) and len(root["generated_utc"]) <= cf.MAX_GENERATED_UTC):
        raise ValueError("generated_utc")
    lic = root.get("licence")
    if lic != cf.LICENCE:
        raise ValueError("licence (not the phone catalog's)")
    if not isinstance(root.get("engines"), list) or not root["engines"]:
        raise ValueError("engines")
    for e in root["engines"]:
        check_engine(e)
    packs = root.get("packs")
    if not isinstance(packs, list):
        raise ValueError("packs")
    ids = set()
    for e in packs:
        if not (isinstance(e, dict) and isinstance(e.get("id"), str) and cf.PACK_ID.match(e["id"])
                and isinstance(e.get("state"), str) and cf.STATE.match(e["state"]) and e["id"] == "us-" + e["state"].lower()):
            raise ValueError("pack id or state")
        if e["id"] in ids:
            raise ValueError("pack id twice")
        ids.add(e["id"])
        if not (isinstance(e.get("name"), str) and e["name"].strip() and len(e["name"]) <= cf.MAX_NAME):
            raise ValueError(f"{e['id']}: name")
        if not (cf._int(e.get("pack_version")) and e["pack_version"] > 0):
            raise ValueError(f"{e['id']}: pack_version")
        if not (isinstance(e.get("osm_snapshot"), str) and cf.DATE.match(e["osm_snapshot"])):
            raise ValueError(f"{e['id']}: osm_snapshot")
        b = e.get("bounds")
        if not (isinstance(b, list) and len(b) == 4 and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in b)
                and -180 <= b[0] < b[2] <= 180 and -90 <= b[1] < b[3] <= 90):
            raise ValueError(f"{e['id']}: bounds")
        if not (isinstance(e.get("valhalla_version"), str) and e["valhalla_version"]):
            raise ValueError(f"{e['id']}: valhalla_version")
        if not (isinstance(e.get("credit"), str) and e["credit"] and len(e["credit"]) <= cf.MAX_CREDIT):
            raise ValueError(f"{e['id']}: credit")
        if not isinstance(e.get("parts"), list):
            raise ValueError(f"{e['id']}: parts")
        names, files = set(), set()
        for p in e["parts"]:
            if not (isinstance(p, dict) and isinstance(p.get("name"), str) and cf.PART_NAME.match(p["name"])
                    and cf.plain_file_name(p.get("file")) and isinstance(p.get("optional"), bool)
                    and cf._int(p.get("bytes")) and 0 <= p["bytes"] <= cf.MAX_FILE_BYTES
                    and isinstance(p.get("sha256"), str) and cf.SHA256.match(p["sha256"])):
                raise ValueError(f"{e['id']}: a part")
            t = cf._transfer(p.get("transfer"), cf.MAX_FILE_BYTES)
            if t is None:
                raise ValueError(f"{e['id']}: {p['name']} transfer")
            cf.resolve(catalog_url, t["path"])
            if t["encoding"] == "identity" and (t["bytes"] != p["bytes"] or t["sha256"] != p["sha256"]):
                raise ValueError(f"{e['id']}: {p['name']} identity transfer differs from its part")
            if p["name"] in names or p["file"] in files:
                raise ValueError(f"{e['id']}: part name or file twice")
            names.add(p["name"])
            files.add(p["file"])
            if p["name"] in PART_ORDER and p["optional"] != (p["name"] in OPTIONAL):
                raise ValueError(f"{e['id']}: {p['name']} optional")
            if p["name"] == "mapd" and not check_mapd_object(p.get("mapd")):
                raise ValueError(f"{e['id']}: mapd object")
        if not REQUIRED <= names:
            raise ValueError(f"{e['id']}: parts {sorted(REQUIRED - names)} missing")
        if not any(x["valhalla_version"] == e["valhalla_version"] and x["python"] == "cp312" for x in root["engines"]):
            raise ValueError(f"{e['id']}: no engine for Valhalla {e['valhalla_version']}")
    return root


def check_site(site: Path) -> bool:
    path = site / "catalog" / "comma.json"
    cat = parse(path.read_bytes(), bc.site_url(site))
    ok = True
    for e in cat["packs"]:
        for p in e["parts"]:
            t = p["transfer"]
            f = (site / "catalog" / t["path"]).resolve()
            good = f.is_file() and f.stat().st_size == t["bytes"] and common.sha256_file(f) == t["sha256"]
            ok &= good
            if not good:
                print(f"  {t['path']}: MISSING or MISMATCH")
    return ok


# ------------------------------------------------------------------ main
def write(site: Path, parts_root: Path) -> Path:
    phone = json.loads((site / "catalog" / "catalog.json").read_text(encoding="utf-8"))
    if phone.get("format") != cf.FORMAT:
        raise Refused("catalog/catalog.json is not the phone catalog")
    path = site / "catalog" / "comma.json"
    previous = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
    if previous is not None and previous.get("format") != FORMAT:
        raise Refused(f"{path} is not {FORMAT}")
    old = {(e.get("id"), e.get("pack_version")): e for e in (previous or {}).get("packs", []) if isinstance(e, dict)}
    packs = []
    for pe in phone["packs"]:
        folder = parts_root / pe["id"]
        if folder.is_dir():
            print(f"{pe['state']}: {pe['id']}-{pe['pack_version']}")
            packs.append(pack_entry(pe, site, folder))
        elif (pe["id"], pe["pack_version"]) in old:
            print(f"{pe['state']}: no comma parts given; the entry of the comma.json here is kept")
            packs.append(old[(pe["id"], pe["pack_version"])])
        else:
            raise Refused(f"{pe['id']}: no comma parts in {folder} and no earlier comma.json entry for "
                          f"{pe['id']}-{pe['pack_version']}")
    eng = engines()
    body = {"licence": cf.LICENCE, "engines": eng, "packs": packs}
    if previous is not None and {k: previous.get(k) for k in body} == body:
        print(f"{path.as_posix()}: nothing changed; left as it is")
        return path
    t = bc.build_time()
    try:
        serial = next_serial(t, previous.get("serial") if previous is not None else None)
    except ValueError as e:
        raise Refused(f"{path} (the comma.json here): {e}; a serial only grows, so no comma.json can follow it") from None
    cat = {"format": FORMAT, "serial": serial, "generated_utc": t.strftime("%Y-%m-%dT%H:%M:%SZ"), **body}
    data = (json.dumps(cat, indent=1, ensure_ascii=False) + "\n").encode("utf-8")
    parse(data, bc.site_url(site))
    tmp = path.with_suffix(".json.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    print(f"wrote {path.as_posix()}: serial {serial}, {len(packs)} pack(s), {len(eng)} engine(s)")
    return path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--site", required=True, type=Path, help="the site build_catalog.py wrote (catalog/catalog.json)")
    ap.add_argument("--parts", type=Path, help="a folder with one <pack id>/ folder of comma parts per pack")
    ap.add_argument("--check", action="store_true", help="re-read every file comma.json names")
    ap.add_argument("--check-only", action="store_true", help="only the --check")
    a = ap.parse_args(argv)
    try:
        if not a.check_only:
            if not a.parts:
                ap.error("--parts is needed (or --check-only)")
            write(a.site, a.parts)
        if a.check or a.check_only:
            if not check_site(a.site):
                print("check FAILED")
                return 1
            print("check ok: every file comma.json names has its size and SHA-256")
    except (Refused, ValueError, KeyError, OSError) as e:
        print(f"refused: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
