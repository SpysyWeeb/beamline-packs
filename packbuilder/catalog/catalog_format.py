"""The catalog format beamline-catalog/1 (docs/catalog-format.md, docs/architecture-downloads.md 2.3).

Shared by build_catalog.py (--check), catalog_client.py and the tests. Standard library only.
A reader refuses the whole list for a bad format, size, licence or path (CatalogRefused) and lists
an entry it cannot use as unusable (BROKEN, OLD_FORMAT or NEEDS_NEWER_APP) with a fixed sentence
that names a field, never a value the server sent.
"""
from __future__ import annotations

import datetime
import json
import posixpath
import re
import urllib.parse

FORMAT = "beamline-catalog/1"
FORMAT_PREFIX = "beamline-catalog/"
ENCODINGS = ("identity", "gzip")
CATALOG_MAX_BYTES = 2 * 1024 * 1024
MAX_FILE_BYTES = 8 * 1024 ** 3 - 1
MAX_NAME, MAX_CREDIT, MAX_LICENCE_NAME, MAX_LICENCE_URL, MAX_NOTICE, MAX_GENERATED_UTC = 64, 300, 100, 200, 300, 32

PATH_RULE = re.compile(r"^(\.\./)?[A-Za-z0-9][A-Za-z0-9._-]*(/[A-Za-z0-9][A-Za-z0-9._-]*)*$")
PACK_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
STATE = re.compile(r"^[A-Z]{2}$")
PART_NAME = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
RESERVED_FILES = {"manifest.json", "install-record.json", "job.json"}
OPTIONAL_PART = "addresses"

LICENCE = {
    "id": "ODbL-1.0",
    "name": "Open Database License 1.0",
    "url": "https://opendatacommons.org/licenses/odbl/1-0/",
    "notice": "Map data © OpenStreetMap contributors, available under the Open Database License.",
}


class CatalogRefused(Exception):
    """The whole list is refused: reason is NEWER_FORMAT, NOT_A_CATALOG, TOO_LARGE or OUTSIDE_SCOPE."""

    def __init__(self, reason: str, detail: str):
        super().__init__(f"{reason}: {detail}")
        self.reason, self.detail = reason, detail


class EntryUnusable(Exception):
    def __init__(self, reason: str, detail: str):
        super().__init__(detail)
        self.reason, self.detail = reason, detail


def plain_file_name(name) -> bool:
    """The pack format's plain-file-name rule (docs/pack-format.md)."""
    return (isinstance(name, str) and 0 < len(name) <= 128 and not name.startswith(".")
            and not any(c in "/\\:" or ord(c) < 0x20 for c in name) and name not in RESERVED_FILES)


def scope_of(catalog_url: str) -> str:
    """The folder above the catalog's folder: every file the catalog names must stay inside it."""
    u = urllib.parse.urlsplit(catalog_url)
    folder = posixpath.dirname(u.path)            # /catalog
    parent = posixpath.dirname(folder) if folder not in ("", "/") else "/"
    if not parent.endswith("/"):
        parent += "/"
    return urllib.parse.urlunsplit((u.scheme.lower(), u.netloc.lower(), parent, "", ""))


def resolve(catalog_url: str, path: str) -> str:
    """The absolute URL of a catalog-relative path; ValueError when it breaks the path rule or leaves the scope."""
    if not isinstance(path, str) or not PATH_RULE.match(path):
        raise ValueError("path breaks the path rule")
    u = urllib.parse.urlsplit(catalog_url)
    base = urllib.parse.urlunsplit((u.scheme.lower(), u.netloc.lower(), u.path, "", ""))
    absolute = urllib.parse.urljoin(base, path)
    if not absolute.startswith(scope_of(catalog_url)):
        raise ValueError("path leaves the catalog's scope")
    return absolute


def _int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _paths(entry) -> list:
    out = []
    if isinstance(entry, dict):
        m = entry.get("manifest")
        if isinstance(m, dict) and isinstance(m.get("path"), str):
            out.append(m["path"])
        for p in entry.get("parts") or [] if isinstance(entry.get("parts"), list) else []:
            t = p.get("transfer") if isinstance(p, dict) else None
            if isinstance(t, dict) and isinstance(t.get("path"), str):
                out.append(t["path"])
    return out


def _transfer(t, max_file_bytes):
    if not isinstance(t, dict):
        return None
    ok = (isinstance(t.get("path"), str) and t.get("encoding") in ENCODINGS and _int(t.get("bytes"))
          and 0 <= t["bytes"] <= max_file_bytes and isinstance(t.get("sha256"), str) and SHA256.match(t["sha256"]))
    return t if ok else None


def _entry(e: dict, min_version: int, max_version: int, max_file_bytes: int) -> dict:
    def broken(why):
        raise EntryUnusable("BROKEN", why)

    mv = e.get("manifest_version")
    if not _int(mv) or mv < 0:
        broken("manifest_version")
    if mv > max_version:
        raise EntryUnusable("NEEDS_NEWER_APP", "manifest_version above this reader's maximum")
    if mv < min_version:
        raise EntryUnusable("OLD_FORMAT", "manifest_version below this reader's minimum")
    encodings = []
    if isinstance(e.get("manifest"), dict):
        encodings.append(e["manifest"].get("encoding"))
    for p in e.get("parts") if isinstance(e.get("parts"), list) else []:
        if isinstance(p, dict) and isinstance(p.get("transfer"), dict):
            encodings.append(p["transfer"].get("encoding"))
    if any(isinstance(x, str) and x not in ENCODINGS for x in encodings):
        raise EntryUnusable("NEEDS_NEWER_APP", "an encoding this reader does not know")
    if not (isinstance(e.get("id"), str) and PACK_ID.match(e["id"])):
        broken("id")
    if not (isinstance(e.get("state"), str) and STATE.match(e["state"])):
        broken("state")
    if e["id"] != "us-" + e["state"].lower():
        broken("id is not us- and the state")
    if not (isinstance(e.get("name"), str) and e["name"].strip() and len(e["name"]) <= MAX_NAME):
        broken("name")
    if not (_int(e.get("pack_version")) and e["pack_version"] > 0):
        broken("pack_version")
    snap = e.get("osm_snapshot")
    try:
        ok = isinstance(snap, str) and DATE.match(snap) and datetime.date.fromisoformat(snap)
    except ValueError:
        ok = False
    if not ok:
        broken("osm_snapshot")
    if not (isinstance(e.get("credit"), str) and len(e["credit"]) <= MAX_CREDIT):
        broken("credit")
    b = e.get("bounds")
    if not (isinstance(b, list) and len(b) == 4 and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in b)
            and -180 <= b[0] < b[2] <= 180 and -90 <= b[1] < b[3] <= 90):
        broken("bounds")
    m = _transfer(e.get("manifest"), max_file_bytes)
    if m is None or m["encoding"] != "identity":
        broken("manifest")
    parts = e.get("parts")
    if not isinstance(parts, list) or not parts:
        broken("parts")
    names, files = set(), set()
    for p in parts:
        if not isinstance(p, dict):
            broken("part is not an object")
        if not (isinstance(p.get("name"), str) and PART_NAME.match(p["name"])):
            broken("part name")
        if not plain_file_name(p.get("file")):
            broken("part file")
        if not isinstance(p.get("optional"), bool):
            broken("part optional")
        if p["optional"] and p["name"] != OPTIONAL_PART:
            broken("only the addresses part may be optional")
        if not (_int(p.get("bytes")) and 0 <= p["bytes"] <= max_file_bytes):
            broken("part bytes")
        if not (isinstance(p.get("sha256"), str) and SHA256.match(p["sha256"])):
            broken("part sha256")
        if "credit" in p and not (isinstance(p["credit"], str) and len(p["credit"]) <= MAX_CREDIT):
            broken("part credit")
        t = _transfer(p.get("transfer"), max_file_bytes)
        if t is None:
            broken("part transfer")
        if t["encoding"] == "identity" and (t["bytes"] != p["bytes"] or t["sha256"] != p["sha256"]):
            broken("identity transfer differs from its part")
        if p["name"] in names:
            broken("duplicate part name")
        if p["file"] in files:
            broken("duplicate part file")
        names.add(p["name"])
        files.add(p["file"])
    return e


def parse(data: bytes, catalog_url: str, min_version: int = 3, max_version: int = 3,
          max_catalog_bytes: int = CATALOG_MAX_BYTES, max_file_bytes: int = MAX_FILE_BYTES):
    """(catalog dict, usable entries, [(id, state, name, reason, detail)]); raises CatalogRefused."""
    if len(data) > max_catalog_bytes:
        raise CatalogRefused("TOO_LARGE", f"larger than {max_catalog_bytes} bytes")
    try:
        root = json.loads(data.decode("utf-8").lstrip("﻿"))
    except (UnicodeDecodeError, ValueError):
        raise CatalogRefused("NOT_A_CATALOG", "not UTF-8 JSON") from None
    if not isinstance(root, dict):
        raise CatalogRefused("NOT_A_CATALOG", "not a JSON object")
    fmt = root.get("format")
    if not isinstance(fmt, str) or not fmt.startswith(FORMAT_PREFIX):
        raise CatalogRefused("NOT_A_CATALOG", "no or another format")
    major = fmt[len(FORMAT_PREFIX):]
    if not re.fullmatch(r"[0-9]{1,6}", major):
        raise CatalogRefused("NOT_A_CATALOG", "format without a major version")
    if int(major) > 1:
        raise CatalogRefused("NEWER_FORMAT", f"format major {int(major)}")
    if int(major) < 1:
        raise CatalogRefused("NOT_A_CATALOG", "format major 0")
    if not _int(root.get("serial")):
        raise CatalogRefused("NOT_A_CATALOG", "serial")
    if not (isinstance(root.get("generated_utc"), str) and len(root["generated_utc"]) <= MAX_GENERATED_UTC):
        raise CatalogRefused("NOT_A_CATALOG", "generated_utc")
    lic = root.get("licence")
    if not (isinstance(lic, dict)
            and isinstance(lic.get("id"), str) and lic["id"].strip() and len(lic["id"]) <= 64
            and isinstance(lic.get("name"), str) and lic["name"].strip() and len(lic["name"]) <= MAX_LICENCE_NAME
            and isinstance(lic.get("url"), str) and lic["url"].startswith("https://") and len(lic["url"]) <= MAX_LICENCE_URL
            and not any(c.isspace() for c in lic["url"])
            and isinstance(lic.get("notice"), str) and lic["notice"].strip() and len(lic["notice"]) <= MAX_NOTICE
            and "\n" not in lic["notice"] and "\r" not in lic["notice"]):
        raise CatalogRefused("NOT_A_CATALOG", "licence")
    packs = root.get("packs")
    if not isinstance(packs, list):
        raise CatalogRefused("NOT_A_CATALOG", "packs")
    for entry in packs:                      # rule 2: one bad path refuses everything
        for path in _paths(entry):
            try:
                resolve(catalog_url, path)
            except ValueError:
                raise CatalogRefused("OUTSIDE_SCOPE", "a path breaks the path rule or leaves the scope") from None
    usable, unusable, ids, states = [], [], set(), set()
    for entry in packs:
        raw_id = entry.get("id") if isinstance(entry, dict) else None
        raw_state = entry.get("state") if isinstance(entry, dict) else None
        dup = (isinstance(raw_id, str) and raw_id in ids) or (isinstance(raw_state, str) and raw_state in states)
        if isinstance(raw_id, str):
            ids.add(raw_id)
        if isinstance(raw_state, str):
            states.add(raw_state)
        try:
            if not isinstance(entry, dict):
                raise EntryUnusable("BROKEN", "entry is not an object")
            if dup:
                raise EntryUnusable("BROKEN", "duplicate id or state")
            usable.append(_entry(entry, min_version, max_version, max_file_bytes))
        except EntryUnusable as u:
            name = entry.get("name") if isinstance(entry, dict) else None
            unusable.append((raw_id if isinstance(raw_id, str) else "", raw_state if isinstance(raw_state, str) else None,
                             name if isinstance(name, str) else None, u.reason, u.detail))
    return root, usable, unusable
