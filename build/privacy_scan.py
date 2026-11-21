#!/usr/bin/env python3
"""Privacy scan of this repository: generic patterns only, standard library only.

    python3 build/privacy_scan.py [--root DIR] [FILE ...]

Fails (exit 1) on any of these in a file of the repository (git ls-files with the untracked files git
does not ignore; without git, every file outside .git, data/ and .venv/), or in the FILEs given:
  - a home folder path: a drive letter with Users, /home/<name>/, /Users/<name>/, /mnt/<drive>/Users;
  - a private network address: IPv4 in 10/8, 172.16/12, 192.168/16, 100.64/10 (an address followed
    by /prefix is a network and passes; 10.0.2.2, the Android emulator's fixed name for its host, passes),
    an IPv6 unique-local or link-local address, an address with an adb port (:5555);
  - an e-mail address, other than GitHub's noreply addresses (...@users.noreply.github.com) and the
    example domains of RFC 2606;
  - a secret or token: GitHub token prefixes (ghp_, gho_, ghu_, ghs_, ghr_, github_pat_), AWS key ids,
    private key blocks, JSON web tokens, an assignment of a long literal to a name such as password,
    secret, token or api_key, a hex string of 24 or more digits whose length is not that of a
    common digest (32, 40, 64 or 128: MD5, SHA-1/git, SHA-256, SHA-512), a mixed-case letters-and-digits
    run of 32 or more characters;
  - a device identifier: a MAC address, an assignment to serial, serialno, imei or android_id,
    adb -s <serial>;
  - a binary file, or a file over 2 MB (the repository holds code and text only, never data).
No specific name, place or address is written here: the patterns are generic on purpose.
Prints the file, line and kind of each finding, never the matched text. Exit 0 when clean.
"""
from __future__ import annotations

import argparse
import ipaddress
import os
import re
import subprocess
import sys
from pathlib import Path

MAX_BYTES = 2 * 1024 * 1024
SKIP_DIRS = {".git", "data", ".venv", "__pycache__", "tools/bin", "tools/cache"}

PATTERNS: list[tuple[str, re.Pattern]] = [
    ("home folder path", re.compile(r"(?i)\b[a-z]:[\\/]{1,2}users[\\/]{1,2}[^\\/\s\"'<>]+")),
    ("home folder path", re.compile(r"(?<![\w.])/home/(?!runner\b)[a-z_][a-z0-9_.-]*")),
    ("home folder path", re.compile(r"(?<![\w.])/Users/[A-Za-z][\w.-]*")),
    ("home folder path", re.compile(r"(?i)/mnt/[a-z]/users\b")),
    ("adb address", re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}:5555\b")),
    ("secret", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}")),
    ("secret", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}")),
    ("secret", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("secret", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("secret", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.")),
    ("secret", re.compile(r"(?i)\b(?:password|passwd|secret|token|api[_-]?key|client[_-]?secret)\b[\"']?\s*[:=]\s*"
                          r"[\"'][^\"'\s$]{12,}[\"']")),
    ("device identifier", re.compile(r"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b")),
    ("device identifier", re.compile(r"(?i)\b(?:serial|serialno|serial_number|imei|android_id)\b[\"']?\s*[:=]\s*"
                                     r"[\"'](?=[A-Za-z0-9]*\d)[A-Za-z0-9]{6,}[\"']")),
    ("device identifier", re.compile(r"\bANDROID_SERIAL=(?!\$)\S")),
    ("device identifier", re.compile(r"\badb\s+-s\s+(?!\$|<|\{)\S+")),
]
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
EMAIL_OK = re.compile(r"(?i)@(?:users\.noreply\.github\.com|example\.(?:com|org|net)|[a-z0-9-]+\.(?:example|invalid|test))$")
HEX = re.compile(r"(?<![0-9A-Za-z])[0-9a-fA-F]{24,}(?![0-9A-Za-z])")
DIGEST_LENGTHS = {32, 40, 64, 128}
MIXED = re.compile(r"(?<![A-Za-z0-9/+_.-])(?=[A-Za-z0-9]*[a-z])(?=[A-Za-z0-9]*[A-Z])(?=[A-Za-z0-9]*[0-9])[A-Za-z0-9]{32,}"
                   r"(?![A-Za-z0-9])")
IPV4 = re.compile(r"(?<![0-9.])((?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(?:\.(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3})"
                  r"(?![0-9])(/\d{1,2})?(?!\.\d)")
IPV6 = re.compile(r"(?i)(?<![0-9a-f:])(f[cd][0-9a-f]{2}|fe80):[0-9a-f:]{2,}")
PRIVATE4 = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10")]
ALLOWED4 = {"10.0.2.2"}


def files(root: Path) -> list[Path]:
    try:
        out = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=root,
                             capture_output=True, check=True).stdout
        names = [n for n in out.decode("utf-8", "replace").split("\0") if n]
    except (OSError, subprocess.CalledProcessError):
        names = []
        for dirpath, dirnames, filenames in os.walk(root):
            reldir = Path(dirpath).relative_to(root).as_posix()
            dirnames[:] = [d for d in dirnames if (f"{reldir}/{d}" if reldir != "." else d) not in SKIP_DIRS
                           and d not in (".git", "__pycache__")]
            names += [(f"{reldir}/{f}" if reldir != "." else f) for f in filenames]
    return [root / n for n in sorted(names) if not any(n == d or n.startswith(d + "/") for d in SKIP_DIRS)]


def scan_text(text: str) -> list[tuple[int, str]]:
    hits = []
    for n, line in enumerate(text.splitlines(), 1):
        for kind, pat in PATTERNS:
            if pat.search(line):
                hits.append((n, kind))
        for m in EMAIL.finditer(line):
            if not EMAIL_OK.search(m.group(0)):
                hits.append((n, "e-mail address"))
        for m in HEX.finditer(line):
            if len(m.group(0)) not in DIGEST_LENGTHS:
                hits.append((n, "long hex string"))
        if MIXED.search(line):
            hits.append((n, "token-like string"))
        for m in IPV4.finditer(line):
            ip, prefix = m.group(1), m.group(2)
            if prefix or ip in ALLOWED4:
                continue
            if any(ipaddress.ip_address(ip) in net for net in PRIVATE4):
                hits.append((n, "private network address"))
        if IPV6.search(line):
            hits.append((n, "private network address (IPv6)"))
    return sorted(set(hits))


def scan_file(p: Path, label: str) -> list[str]:
    try:
        data = p.read_bytes()
    except OSError as e:
        return [f"{label}: unreadable ({type(e).__name__})"]
    out = []
    if len(data) > MAX_BYTES:
        out.append(f"{label}: over 2 MB ({len(data)} bytes); the repository holds no data")
    if b"\0" in data[:8192]:
        return out + [f"{label}: a binary file; the repository holds code and text only"]
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return out + [f"{label}: not UTF-8 text"]
    return out + [f"{label}:{n}: {kind}" for n, kind in scan_text(text)]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    ap.add_argument("files", nargs="*", type=Path)
    a = ap.parse_args(argv)
    root = a.root.resolve()
    targets = [(p, str(p)) for p in a.files] if a.files else [(p, p.relative_to(root).as_posix()) for p in files(root)]
    hits = [h for p, label in targets if p.is_file() for h in scan_file(p, label)]
    for h in hits:
        print(h)
    print(f"privacy scan: {len(targets)} files, {len(hits)} finding(s)")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
