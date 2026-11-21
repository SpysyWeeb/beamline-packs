"""The street-key fingerprint and the LF-normalised file hash of the pack builders (track P).

docs/architecture.md 2.11 and decision 6.11:

    fingerprint = sha256_hex( for each spelling s in addrlib/fingerprint_spellings.json "spellings",
                              in order: utf8(s + "\\t" + street_key(s) + "\\n") )

The search builder writes it to search meta `addr_key_fingerprint`, the address builder to
addresses meta `key_fingerprint`; packbuilder/pack/assemble.py copies both into the manifest and
refuses a pair that differs. The app's port (track S, :core:search) computes the same over its copy
of the list. Every hash the builders record of a text file (normaliser sources, rule files,
`ranking_sha256`) is `lf_sha256`: SHA-256 of the bytes with every CRLF read as LF, so a Windows
checkout and a Linux checkout give the same value.

Standard library only. Run from the repository root:

    python packbuilder/addresses/key_fingerprint.py            # prints the fingerprint
    python packbuilder/addresses/key_fingerprint.py --dump F   # also writes "spelling<TAB>key" lines to F
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SPELLINGS_FILE = HERE / "addrlib" / "fingerprint_spellings.json"


def lf_bytes(data: bytes) -> bytes:
    """The bytes with every CR LF pair read as LF (a lone CR stays)."""
    return data.replace(b"\r\n", b"\n")


def lf_sha256(path: str | Path) -> str:
    """SHA-256 of a text file over LF-normalised bytes."""
    return hashlib.sha256(lf_bytes(Path(path).read_bytes())).hexdigest()


def spellings(path: str | Path = SPELLINGS_FILE) -> list[str]:
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    out = doc["spellings"]
    if not isinstance(out, list) or not all(isinstance(s, str) for s in out):
        raise ValueError(f"{path}: 'spellings' must be a list of strings")
    return out


def _street_key():
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    from addrlib.normalize import street_key   # noqa: E402  (packbuilder/addresses/addrlib)
    return street_key


def key_lines(street_key=None, path: str | Path = SPELLINGS_FILE) -> list[str]:
    """The hashed lines, "spelling<TAB>key<LF>" each."""
    sk = street_key or _street_key()
    return [f"{s}\t{sk(s)}\n" for s in spellings(path)]


def key_fingerprint(street_key=None, path: str | Path = SPELLINGS_FILE) -> str:
    h = hashlib.sha256()
    for line in key_lines(street_key, path):
        h.update(line.encode("utf-8"))
    return h.hexdigest()


def main(argv: list[str]) -> int:
    lines = key_lines()
    fp = hashlib.sha256("".join(lines).encode("utf-8")).hexdigest()
    if "--dump" in argv:
        out = Path(argv[argv.index("--dump") + 1])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes("".join(lines).encode("utf-8"))
        print(f"{len(lines)} lines -> {out}")
    print(f"spellings: {len(lines)} from {SPELLINGS_FILE.relative_to(HERE.parents[1]).as_posix()}")
    print(f"key_fingerprint: {fp}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
