"""Shared by the comma part builders (docs/comma-pack-format.md): hashing, deterministic tars, the
member rules of the three comma-only parts and the strict unpack the contract asks of an unpacker.
Standard library only.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import math
import os
import re
import tarfile
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
CHUNK = 1 << 20

# The member names of the three parts (comma-pack-format.md 2, "What the three comma-only parts are").
SEARCH_CODE_FILES = ("query.py", "lines.py", "normalize.py", "ranking.json", "abbreviations.json")
ROAD_ZOOMS = (8, 11, 14)
ROADS_MEMBER = re.compile(r"^(0|[1-9][0-9]{0,1})/(0|[1-9][0-9]{0,5})/(0|[1-9][0-9]{0,5})\.rt$")
MAPD_MEMBER = re.compile(r"^(-?(?:0|[1-9][0-9]{0,1}))/(-?(?:0|[1-9][0-9]{0,2}))\.tar\.gz$")


class Refused(Exception):
    """A part that breaks the contract: nothing is written, or the check fails."""


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for block in iter(lambda: f.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def load_pins() -> dict:
    return json.loads((HERE / "pins.json").read_text(encoding="utf-8"))


def utc_epoch(text: str) -> int:
    """'2026-11-21T18:59:00Z' or '2026-11-21' -> seconds since 1970 (UTC)."""
    fmt = "%Y-%m-%dT%H:%M:%SZ" if "T" in text else "%Y-%m-%d"
    return int(datetime.strptime(text, fmt).replace(tzinfo=timezone.utc).timestamp())


def text_bytes(p: Path) -> bytes:
    """A text file's bytes with LF line ends, so a checkout on any platform gives the same part."""
    return p.read_bytes().replace(b"\r\n", b"\n")


# ------------------------------------------------------------------ Mapd's two-degree groups
def mapd_box(bounds) -> dict:
    """The whole two-degree groups covering a pack's bounds [west, south, east, north] (comma-pack-format.md 5)."""
    west, south, east, north = (float(v) for v in bounds)
    if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        raise Refused(f"bounds {bounds} are not [west, south, east, north]")
    box = {"min_lat": math.floor(south / 2) * 2, "min_lon": math.floor(west / 2) * 2,
           "max_lat": math.ceil(north / 2) * 2, "max_lon": math.ceil(east / 2) * 2}
    return box


def mapd_groups(box: dict) -> list[tuple[int, int]]:
    """(lat, lon) of every group of a box, in Mapd's own order (latitude, then longitude)."""
    return [(lat, lon) for lat in range(box["min_lat"], box["max_lat"], 2)
            for lon in range(box["min_lon"], box["max_lon"], 2)]


def mapd_cells(lat: int, lon: int) -> list[str]:
    """The 64 quarter-degree cell file names of one group, as Mapd's GenerateBoundsFileName writes them."""
    out = []
    for a in range(lat * 4, lat * 4 + 8):
        for b in range(lon * 4, lon * 4 + 8):
            out.append(f"{a / 4:f}_{b / 4:f}_{(a + 1) / 4:f}_{(b + 1) / 4:f}")
    return out


# ------------------------------------------------------------------ deterministic tars
class TarOut:
    """A USTAR tar written member by member through a temporary file: regular files only, owner 0,
    mode 0644, one fixed time; with gzip_level, one gzip member with no name and no time. The caller
    adds the members in their final order."""

    def __init__(self, path: Path, mtime: int = 0, gzip_level: int | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.tmp = self.path.with_name(self.path.name + ".tmp")
        self.mtime = int(mtime)
        self.raw = open(self.tmp, "wb")
        self.gz = None
        target = self.raw
        if gzip_level is not None:
            self.gz = gzip.GzipFile(filename="", mode="wb", compresslevel=gzip_level, mtime=0, fileobj=self.raw)
            target = self.gz
        self.tar = tarfile.open(fileobj=target, mode="w", format=tarfile.USTAR_FORMAT)
        self.names: set[str] = set()

    def _info(self, name: str, size: int) -> tarfile.TarInfo:
        if name in self.names:
            raise Refused(f"{self.path.name}: member {name} twice")
        self.names.add(name)
        ti = tarfile.TarInfo(name)
        ti.size, ti.mtime, ti.mode, ti.uid, ti.gid, ti.uname, ti.gname = size, self.mtime, 0o644, 0, 0, "", ""
        return ti

    def add_bytes(self, name: str, data: bytes) -> None:
        import io
        self.tar.addfile(self._info(name, len(data)), io.BytesIO(data))

    def add_file(self, name: str, src: Path) -> None:
        with open(src, "rb") as f:
            self.tar.addfile(self._info(name, os.fstat(f.fileno()).st_size), f)

    def close(self) -> None:
        self.tar.close()
        if self.gz is not None:
            self.gz.close()
        self.raw.close()
        os.replace(self.tmp, self.path)

    def abort(self) -> None:
        for c in (self.tar, self.gz, self.raw):
            try:
                if c is not None:
                    c.close()
            except Exception:  # noqa: BLE001 - cleaning up after the real error
                pass
        self.tmp.unlink(missing_ok=True)

    def __enter__(self):
        return self

    def __exit__(self, kind, value, tb):
        if kind is None:
            self.close()
        else:
            self.abort()
        return False


# ------------------------------------------------------------------ the strict member rule
def roads_name_ok(name: str) -> bool:
    m = ROADS_MEMBER.match(name)
    if not m:
        return False
    z, x, y = (int(v) for v in m.groups())
    return z in ROAD_ZOOMS and x < (1 << z) and y < (1 << z)


def mapd_name_ok(name: str) -> bool:
    m = MAPD_MEMBER.match(name)
    if not m:
        return False
    lat, lon = (int(v) for v in m.groups())
    return lat % 2 == 0 and lon % 2 == 0 and -90 <= lat < 90 and -180 <= lon < 180


def search_code_name_ok(name: str) -> bool:
    return name in SEARCH_CODE_FILES


NAME_RULES = {"roads": roads_name_ok, "mapd": mapd_name_ok, "search_code": search_code_name_ok}


def checked_members(tar_path: Path, part: str) -> list[tarfile.TarInfo]:
    """The members of an uncompressed comma part tar, refused (Refused) for an absolute member, a '..'
    component, a link, a device, a folder, a member twice or a name outside the part's rule."""
    rule = NAME_RULES[part]
    with open(tar_path, "rb") as f:
        head = f.read(512)
    if head[:2] == b"\x1f\x8b" or len(head) < 512 or head[257:262] != b"ustar":
        raise Refused(f"{Path(tar_path).name}: not an uncompressed POSIX tar")
    out, seen = [], set()
    with tarfile.open(tar_path, "r:") as t:
        for m in t:
            name = m.name
            if name.startswith("/") or "\\" in name or any(c in ("..", "") for c in name.split("/")):
                raise Refused(f"{Path(tar_path).name}: unsafe member name {name!r}")
            if not m.isreg():
                raise Refused(f"{Path(tar_path).name}: {name!r} is not a regular file (links, folders and devices are refused)")
            if not rule(name):
                raise Refused(f"{Path(tar_path).name}: {name!r} is not a {part} member")
            if name in seen:
                raise Refused(f"{Path(tar_path).name}: {name!r} twice")
            seen.add(name)
            out.append(m)
    if not out:
        raise Refused(f"{Path(tar_path).name}: no members")
    return out


def unpack(tar_path: Path, dest: Path, part: str) -> list[str]:
    """Every member checked first, then written under dest (never through a link). Returns the names."""
    members = checked_members(tar_path, part)
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    names = []
    with tarfile.open(tar_path, "r:") as t:
        for m in members:
            target = dest.joinpath(*m.name.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_symlink():
                raise Refused(f"{target}: a link where a member goes")
            src = t.extractfile(m)
            with open(target, "wb") as out:
                left = m.size
                while left:
                    block = src.read(min(CHUNK, left))
                    if not block:
                        raise Refused(f"{Path(tar_path).name}: {m.name} is cut short")
                    out.write(block)
                    left -= len(block)
            names.append(m.name)
    return names
