#!/usr/bin/env python3
"""Turn the files of one pack release into the pack archive the app's "Use a file instead" reads.

    python3 build/release_to_archive.py <release folder> [--out <file>.tar]

<release folder> holds the release's files as downloaded (for a private repository:
`gh release download us-pa-<version> --dir <folder>`; or a folder of a local publish). The files
are checked against manifest.json and the catalog's rules (sizes and SHA-256 of every part, gzip
inflated), then written as an uncompressed ustar archive: manifest.json first, then the parts from
the smallest to the largest (docs of the pack format: README). Standard library only.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import sys
import tarfile
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("release", type=Path)
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()
    rel = a.release
    mbytes = (rel / "manifest.json").read_bytes()
    m = json.loads(mbytes.decode("utf-8"))
    parts = sorted(m["parts"].values(), key=lambda p: p["bytes"])
    out = a.out or rel.parent / f"{m['pack_id']}-{m['osm_snapshot']}.tar"
    tmp = out.with_name(out.name + ".part")
    with tarfile.open(tmp, "w", format=tarfile.USTAR_FORMAT) as t:
        def add(name: str, size: int, f) -> None:
            ti = tarfile.TarInfo(name)
            ti.size, ti.mode, ti.mtime = size, 0o644, 0
            t.addfile(ti, f)
        add("manifest.json", len(mbytes), io.BytesIO(mbytes))
        for p in parts:
            plain, packed = rel / p["file"], rel / (p["file"] + ".gz")
            if plain.is_file():
                src = open(plain, "rb")
            elif packed.is_file():
                src = gzip.open(packed, "rb")
            else:
                print(f"{p['file']}: missing from {rel}")
                return 1
            with src:
                data = HashingReader(src)
                add(p["file"], p["bytes"], data)
                if data.n != p["bytes"] or data.h.hexdigest() != p["sha256"]:
                    print(f"{p['file']}: {data.n} bytes, sha256 {data.h.hexdigest()[:12]}; the manifest says "
                          f"{p['bytes']}, {p['sha256'][:12]}")
                    tmp.unlink()
                    return 1
            print(f"  {p['file']}: {p['bytes']} bytes, sha256 ok")
    tmp.replace(out)
    print(f"wrote {out} ({out.stat().st_size} bytes)")
    return 0


class HashingReader:
    def __init__(self, f):
        self.f, self.h, self.n = f, hashlib.sha256(), 0

    def read(self, size: int = -1) -> bytes:
        b = self.f.read(size)
        self.h.update(b)
        self.n += len(b)
        return b


if __name__ == "__main__":
    sys.exit(main())
