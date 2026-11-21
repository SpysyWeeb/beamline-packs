"""Create the routing track's Python environment (.venv/routing) with pinned, hash-checked Valhalla.

The Valhalla build tools (valhalla_build_tiles, valhalla_build_admins, ...) come
from the pyvalhalla wheel, so the wheel *is* the portable tool. This script
downloads the wheel for this platform from PyPI, checks its SHA-256 against the
pins below, installs it, then installs the rest of
packbuilder/routing/requirements.txt (exact versions).

usage (repo root):  python tools/fetch_routing_env.py      (Python 3.12, Windows or Linux)
"""
from __future__ import annotations

import hashlib
import platform
import subprocess
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
VENV = REPO / ".venv" / "routing"
CACHE = REPO / "tools" / "cache"
REQS = REPO / "packbuilder" / "routing" / "requirements.txt"

PYVALHALLA = "3.9.1"  # must equal the Valhalla bundled by valhalla-mobile (0.6.4 -> valhalla 3.9.1)
WHEELS = {  # platform tag -> sha256, from https://pypi.org/pypi/pyvalhalla/3.9.1/json
    "win_amd64": "542ea3e11da281c63f2043fa69a2f426c5b309cb1e3b0032028f7034847d1c13",
    "manylinux_2_28_x86_64": "7b49c5e5f86cefd555b473aaaf34df3305889d30aa889b8abd3b4c2acc622271",
    "manylinux_2_28_aarch64": "6ebe5f9dd6ce3b16413f939a271d68e64a4addd44df8a802a8eda0b9ca0bf16f",
}


def platform_tag() -> str:
    m = platform.machine().lower()
    if sys.platform == "win32" and m in ("amd64", "x86_64"):
        return "win_amd64"
    if sys.platform.startswith("linux") and m in ("x86_64", "amd64"):
        return "manylinux_2_28_x86_64"
    if sys.platform.startswith("linux") and m in ("aarch64", "arm64"):
        return "manylinux_2_28_aarch64"
    sys.exit(f"no pinned pyvalhalla wheel for {sys.platform}/{m}")


def main() -> None:
    if sys.version_info[:2] != (3, 12):
        sys.exit("use Python 3.12")
    tag = platform_tag()
    name = f"pyvalhalla-{PYVALHALLA}-cp312-abi3-{tag}.whl"
    CACHE.mkdir(parents=True, exist_ok=True)
    whl = CACHE / name
    if not whl.exists() or hashlib.sha256(whl.read_bytes()).hexdigest() != WHEELS[tag]:
        url = f"https://files.pythonhosted.org/packages/cp312/p/pyvalhalla/{name}"
        print(f"downloading {url}")
        urllib.request.urlretrieve(url, whl)
    got = hashlib.sha256(whl.read_bytes()).hexdigest()
    if got != WHEELS[tag]:
        whl.unlink()
        sys.exit(f"sha256 mismatch for {name}: {got}")
    if not VENV.exists():
        subprocess.run([sys.executable, "-m", "venv", str(VENV)], check=True)
    py = VENV / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    subprocess.run([str(py), "-m", "pip", "install", "--disable-pip-version-check", str(whl)], check=True)
    subprocess.run([str(py), "-m", "pip", "install", "--disable-pip-version-check", "-r", str(REQS)], check=True)
    subprocess.run([str(py), "-c", "import valhalla; print('pyvalhalla', valhalla.__version__)"], check=True)


if __name__ == "__main__":
    main()
