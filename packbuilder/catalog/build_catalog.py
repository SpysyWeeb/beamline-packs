"""Build the download site: one release folder per pack build and catalog/catalog.json.

    python packbuilder/catalog/build_catalog.py --out data/catalog/site \\
        --pack data/r3-packs/us-pa [--pack data/r3-packs/us-oh] \\
        [--archive data/android/archives-z14/us-pa-2026-11-14.tar] [--manifest-versions 3] [--prune] [--check]

docs/architecture-downloads.md 2.1-2.3 and 7.1; the format is docs/catalog-format.md. Standard
library only; reads its inputs, writes only under --out, uploads nothing.

Each input is a pack folder (manifest.json beside the part files) or a pack archive (an
uncompressed tar, manifest.json first). Every part's size and SHA-256 are verified in one read
pass, which also writes the transfer file: `<file>.gz` (gzip -6, no name, no time) when the part is
at least 65,536 bytes and gzip makes it 90 % or less of its size, else `<file>` itself (a hard link
to a folder input's file when possible, else a copy). The release folder `<pack_id>-<pack_version>`
gets manifest.json (the same bytes), the transfer files and transfer.json, so a re-run reuses
finished transfer files instead of compressing again. A release folder that catalog.json already
names is never written again: a changed pack is a new build with a new pack_version.

SOURCE_DATE_EPOCH (seconds), when set, is the build time for `serial` and `generated_utc`, so two
builds of the same inputs give the same bytes; without it the clock is used. A run that changes no
entry leaves catalog.json as it is.
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
import tarfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import catalog_format as cf  # noqa: E402

GZIP_MIN_BYTES = 65536
GZIP_MAX_RATIO = 0.90
ASSET_LIMIT = 2 * 1024 ** 3            # a GitHub release asset must be smaller than 2 GiB
NAME_RULE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
CHUNK = 1 << 20


class Refused(Exception):
    pass


def entry_name(e: tarfile.TarInfo) -> str:
    return e.name[2:] if e.name.startswith("./") else e.name


class HashingWriter:
    """A file object that counts and hashes what is written through it."""

    def __init__(self, f):
        self.f, self.h, self.n = f, hashlib.sha256(), 0

    def write(self, b):
        self.h.update(b)
        self.n += len(b)
        return self.f.write(b)

    def flush(self):
        self.f.flush()


class Source:
    """One input pack: its manifest bytes and a way to open each part file."""

    def __init__(self, path: Path):
        self.path = path
        self.is_dir = path.is_dir()
        if self.is_dir:
            self.manifest_bytes = (path / "manifest.json").read_bytes()
            self._members = None
        else:
            with tarfile.open(path) as t:
                first = t.next()
                if first is None or not first.isfile() or entry_name(first) != "manifest.json":
                    raise Refused(f"{path.name}: the first entry must be manifest.json")
                self.manifest_bytes = t.extractfile(first).read()
                self._members = {}
                for e in t:
                    if e.isfile() and entry_name(e) not in self._members:
                        self._members[entry_name(e)] = (e.offset_data, e.size)
        self.manifest = json.loads(self.manifest_bytes.decode("utf-8").lstrip("﻿"))

    def file_path(self, name: str):
        return self.path / name if self.is_dir else None

    def open_part(self, name: str):
        """(stream, size) of one part file."""
        if self.is_dir:
            p = self.path / name
            if not p.is_file():
                raise Refused(f"{self.path.name}: part file {name} is missing")
            return open(p, "rb"), p.stat().st_size
        if name not in self._members:
            raise Refused(f"{self.path.name}: part file {name} is not in the archive")
        offset, size = self._members[name]
        f = open(self.path, "rb")
        f.seek(offset)
        return _Limited(f, size), size


class _Limited:
    def __init__(self, f, size):
        self.f, self.left = f, size

    def read(self, n=-1):
        if self.left <= 0:
            return b""
        n = self.left if n < 0 else min(n, self.left)
        b = self.f.read(n)
        self.left -= len(b)
        return b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.f.close()


def pack_version(m: dict) -> int:
    v = m.get("pack_version")
    if isinstance(v, int) and not isinstance(v, bool) and v > 0:
        return v
    created = m.get("created_utc")
    if not isinstance(created, str):
        raise Refused(f"{m.get('pack_id')}: no pack_version and no created_utc to derive it from")
    t = datetime.datetime.strptime(created.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S%z").astimezone(datetime.timezone.utc)
    return int(t.strftime("%Y%m%d%H%M"))


def check_manifest(m: dict, versions: set[int], where: str) -> None:
    mv = m.get("manifest_version")
    if mv not in versions:
        raise Refused(f"{where}: manifest_version {mv} is not one of --manifest-versions {sorted(versions)}")
    pid, state = m.get("pack_id"), m.get("state")
    if not (isinstance(pid, str) and cf.PACK_ID.match(pid)):
        raise Refused(f"{where}: pack_id does not match {cf.PACK_ID.pattern}")
    if not (isinstance(state, str) and cf.STATE.match(state)):
        raise Refused(f"{where}: state is not a USPS code")
    if pid != "us-" + state.lower():
        raise Refused(f"{where}: pack_id {pid} is not us- and its state in lower case")
    if not isinstance(m.get("name"), str) or not m["name"].strip() or len(m["name"]) > cf.MAX_NAME:
        raise Refused(f"{where}: name missing or longer than {cf.MAX_NAME}")
    if not isinstance(m.get("attribution"), str) or len(m["attribution"]) > cf.MAX_CREDIT:
        raise Refused(f"{where}: attribution missing or longer than {cf.MAX_CREDIT}")
    if not isinstance(m.get("parts"), dict) or not m["parts"]:
        raise Refused(f"{where}: no parts")
    for name, p in m["parts"].items():
        if not cf.PART_NAME.match(name):
            raise Refused(f"{where}: part name {name!r} breaks {cf.PART_NAME.pattern}")
        if not cf.plain_file_name(p.get("file")) or not NAME_RULE.match(p["file"]):
            raise Refused(f"{where}: part {name} has a file name the site cannot serve")
        if "attribution" in p and (not isinstance(p["attribution"], str) or len(p["attribution"]) > cf.MAX_CREDIT):
            raise Refused(f"{where}: part {name} attribution longer than {cf.MAX_CREDIT}")


def make_transfer(src: Source, part: dict, release: Path, reuse: dict | None) -> dict:
    """Verifies one part (one read pass) and writes or reuses its transfer file; returns its transfer.json entry."""
    file = part["file"]
    stream, size = src.open_part(file)
    if size != part["bytes"]:
        stream.__exit__(None, None, None)
        raise Refused(f"{src.path.name}: {file} has {size} bytes, the manifest says {part['bytes']}")
    raw_hash = hashlib.sha256()
    want_gzip = size >= GZIP_MIN_BYTES and reuse is None
    gz_tmp = release / (file + ".gz.tmp")
    raw_tmp = release / (file + ".tmp")
    copy_raw = reuse is None and src.file_path(file) is None     # an archive input: the raw file may be needed
    with stream:
        gzw = gzf = rawf = None
        try:
            if want_gzip:
                gzf = open(gz_tmp, "wb")
                gzw = HashingWriter(gzf)
                gz = gzip.GzipFile(filename="", mode="wb", compresslevel=6, mtime=0, fileobj=gzw)
            if copy_raw:
                rawf = open(raw_tmp, "wb")
            for chunk in iter(lambda: stream.read(CHUNK), b""):
                raw_hash.update(chunk)
                if want_gzip:
                    gz.write(chunk)
                if rawf:
                    rawf.write(chunk)
            if want_gzip:
                gz.close()
        finally:
            if gzf:
                gzf.close()
            if rawf:
                rawf.close()
    if raw_hash.hexdigest() != part["sha256"]:
        for t in (gz_tmp, raw_tmp):
            t.unlink(missing_ok=True)
        raise Refused(f"{src.path.name}: {file} SHA-256 differs from its manifest")
    if reuse is not None:
        return reuse
    if want_gzip and gzw.n <= GZIP_MAX_RATIO * size:
        out = release / (file + ".gz")
        os.replace(gz_tmp, out)
        raw_tmp.unlink(missing_ok=True)
        entry = {"path": out.name, "encoding": "gzip", "bytes": gzw.n, "sha256": gzw.h.hexdigest()}
    else:
        gz_tmp.unlink(missing_ok=True)
        out = release / file
        out.unlink(missing_ok=True)
        if copy_raw:
            os.replace(raw_tmp, out)
        else:
            try:
                os.link(src.file_path(file), out)
            except OSError:
                shutil.copyfile(src.file_path(file), out)
        entry = {"path": out.name, "encoding": "identity", "bytes": size, "sha256": part["sha256"]}
    entry["part_sha256"] = part["sha256"]
    return entry


def file_sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(CHUNK), b""):
            h.update(b)
    return h.hexdigest()


def reusable(release: Path, table: dict, part: dict) -> dict | None:
    e = table.get(part["file"])
    if not isinstance(e, dict) or e.get("part_sha256") != part["sha256"]:
        return None
    p = release / str(e.get("path"))
    if not p.is_file() or p.stat().st_size != e.get("bytes") or file_sha256(p) != e.get("sha256"):
        return None
    return e


def build_release(src: Source, out: Path, versions: set[int], published: set[str]) -> dict:
    m = src.manifest
    check_manifest(m, versions, src.path.name)
    version = pack_version(m)
    rel_name = f"{m['pack_id']}-{version}"
    if not NAME_RULE.match(rel_name):
        raise Refused(f"{rel_name}: release folder name breaks {NAME_RULE.pattern}")
    release = out / rel_name
    if rel_name in published:
        print(f"{m['state']}: {rel_name} is already published; left as it is")
        return None
    release.mkdir(parents=True, exist_ok=True)
    table_path = release / "transfer.json"
    try:
        table = json.loads(table_path.read_text(encoding="utf-8")) if table_path.is_file() else {}
    except ValueError:
        table = {}
    new_table, parts = {}, []
    for name, part in m["parts"].items():
        reuse = reusable(release, table, part)
        t = make_transfer(src, part, release, reuse)
        if t["bytes"] >= ASSET_LIMIT:
            raise Refused(f"{rel_name}: {t['path']} is too large for one release asset ({t['bytes']} bytes)")
        new_table[part["file"]] = t
        p = {"name": name, "file": part["file"], "optional": name == cf.OPTIONAL_PART,
             "bytes": part["bytes"], "sha256": part["sha256"]}
        if isinstance(part.get("attribution"), str):
            p["credit"] = part["attribution"]
        p["transfer"] = {"path": f"../{rel_name}/{t['path']}", "encoding": t["encoding"], "bytes": t["bytes"], "sha256": t["sha256"]}
        parts.append(p)
        print(f"  {name}: {t['encoding']} {t['bytes']:,} bytes{' (reused)' if reuse else ''}")
    (release / "manifest.json").write_bytes(src.manifest_bytes)
    tmp = table_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(new_table, indent=1, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    os.replace(tmp, table_path)
    parts.sort(key=lambda p: (p["transfer"]["bytes"], p["name"]))
    entry = {
        "id": m["pack_id"], "state": m["state"], "name": m["name"], "pack_version": version,
        "manifest_version": m["manifest_version"], "osm_snapshot": m["osm_snapshot"], "credit": m["attribution"],
        "bounds": m["bounds"],
        "manifest": {"path": f"../{rel_name}/manifest.json", "encoding": "identity", "bytes": len(src.manifest_bytes),
                     "sha256": hashlib.sha256(src.manifest_bytes).hexdigest()},
        "parts": parts,
    }
    return entry


def build_time() -> datetime.datetime:
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    if epoch:
        return datetime.datetime.fromtimestamp(int(epoch), datetime.timezone.utc)
    return datetime.datetime.now(datetime.timezone.utc)


def write_catalog(out: Path, packs: list, previous: dict | None) -> Path:
    t = build_time()
    serial = int(t.strftime("%Y%m%d%H%M%S"))
    if previous and isinstance(previous.get("serial"), int) and serial <= previous["serial"]:
        serial = previous["serial"] + 1
    cat = {"format": cf.FORMAT, "serial": serial, "generated_utc": t.strftime("%Y-%m-%dT%H:%M:%SZ"),
           "licence": cf.LICENCE, "packs": packs}
    path = out / "catalog" / "catalog.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_bytes((json.dumps(cat, indent=1, ensure_ascii=False) + "\n").encode("utf-8"))
    os.replace(tmp, path)
    return path


def site_url(out: Path) -> str:
    """A stand-in catalog address for the path checks: the paths are relative, so any scope works."""
    return "https://site.invalid/catalog/catalog.json"


def check_site(out: Path, versions: set[int]) -> bool:
    path = out / "catalog" / "catalog.json"
    data = path.read_bytes()
    _, usable, unusable = cf.parse(data, site_url(out), min(versions), max(versions))
    ok = True
    for u in unusable:
        print(f"unusable entry {u[0] or '?'}: {u[3]} ({u[4]})")
        ok = False
    for e in usable:
        files = [e["manifest"]] + [p["transfer"] for p in e["parts"]]
        for t in files:
            p = (out / "catalog" / t["path"]).resolve()
            good = p.is_file() and p.stat().st_size == t["bytes"] and file_sha256(p) == t["sha256"]
            ok &= good
            if not good:
                print(f"  {t['path']}: MISSING or MISMATCH")
    return ok


def summary(entry: dict) -> str:
    dl = entry["manifest"]["bytes"] + sum(p["transfer"]["bytes"] for p in entry["parts"])
    raw = entry["manifest"]["bytes"] + sum(p["bytes"] for p in entry["parts"])
    return f"{entry['state']} {entry['pack_version']}: download {dl / 1e6:,.1f} MB, unpacked {raw / 1e6:,.1f} MB"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--pack", action="append", default=[], type=Path, help="a pack folder")
    ap.add_argument("--archive", action="append", default=[], type=Path, help="a pack archive (.tar)")
    ap.add_argument("--manifest-versions", default="3", help="comma-separated manifest versions to accept (default 3)")
    ap.add_argument("--prune", action="store_true", help="delete release folders no catalog entry names")
    ap.add_argument("--check", action="store_true", help="re-read every file the catalog names")
    a = ap.parse_args(argv)
    versions = {int(v) for v in a.manifest_versions.split(",") if v.strip()}
    out: Path = a.out
    cat_path = out / "catalog" / "catalog.json"
    previous = None
    if cat_path.is_file():
        previous = json.loads(cat_path.read_text(encoding="utf-8"))
        if previous.get("format") != cf.FORMAT:
            print(f"error: {cat_path} is not {cf.FORMAT}", file=sys.stderr)
            return 1
    old_entries = previous.get("packs", []) if previous else []
    published = {e["manifest"]["path"].split("/")[1] for e in old_entries
                 if isinstance(e, dict) and isinstance(e.get("manifest"), dict) and str(e["manifest"].get("path", "")).startswith("../")}
    try:
        sources = [Source(p) for p in a.pack + a.archive]
        states = [s.manifest.get("state") for s in sources]
        dup = {x for x in states if states.count(x) > 1}
        if dup:
            raise Refused(f"two packs for the same state: {', '.join(sorted(map(str, dup)))}")
        out.mkdir(parents=True, exist_ok=True)
        new_entries = []
        for s in sources:
            print(f"{s.manifest.get('state')}: {s.path.name}")
            e = build_release(s, out, versions, published)
            if e is not None:
                new_entries.append(e)
        by_id = {e["id"]: e for e in old_entries if isinstance(e, dict)}
        changed = False
        for e in new_entries:
            if by_id.get(e["id"]) != e:
                by_id[e["id"]] = e
                changed = True
        packs = sorted(by_id.values(), key=lambda e: (e.get("state", ""), e.get("id", "")))
        # Every path must keep to the reader's rules before anything is published.
        for e in packs:
            for p in [e["manifest"]["path"]] + [x["transfer"]["path"] for x in e["parts"]]:
                cf.resolve(site_url(out), p)
        if changed or previous is None:
            write_catalog(out, packs, previous)
            print(f"wrote {cat_path.as_posix()}")
        else:
            print(f"{cat_path.as_posix()}: no entry changed; left as it is")
        for e in packs:
            print(summary(e))
        if a.prune:
            named = {e["manifest"]["path"].split("/")[1] for e in packs}
            for d in out.iterdir():
                if d.is_dir() and d.name != "catalog" and d.name not in named and NAME_RULE.match(d.name):
                    shutil.rmtree(d)
                    print(f"pruned {d.name}")
        if a.check:
            if not check_site(out, versions):
                print("check FAILED")
                return 1
            print("check ok: every file the catalog names has its size and SHA-256")
    except Refused as r:
        print(f"refused: {r}", file=sys.stderr)
        return 1
    except ValueError as v:
        print(f"refused: {v}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
