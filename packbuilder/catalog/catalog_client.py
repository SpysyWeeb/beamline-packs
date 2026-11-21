"""Reference client of the download site (the normative example of docs/catalog-format.md).

    python packbuilder/catalog/catalog_client.py list <catalog URL> [--manifest-versions 3]
    python packbuilder/catalog/catalog_client.py get <catalog URL> <state or id> --out <folder> [--skip-optional]

docs/architecture-downloads.md 2.4, 2.5, 3.1 and 7.3. Standard library only. Sends GET requests
with the fixed headers of rule N3 and nothing else; follows at most 5 redirects, each checked
(https only to https on public addresses; from a debug server's plain http only to the same scheme,
host and port); every address a host resolves to must be public, except a loopback or private
catalog host itself, which only then allows plain http. The result of `get` is a pack folder
(manifest.json and the part files) that android/scripts/read_pack.py --verify passes. A run cut
short keeps its bytes and resumes with Range and If-Match when started again.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import ipaddress
import json
import os
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import catalog_format as cf  # noqa: E402

USER_AGENT = "Beamline-Packs/1"
MAX_REDIRECTS = 5
HASH_RETRIES = 1
CHUNK = 1 << 20
STATE_FILE = ".client-state.json"


class Refused(Exception):
    """The network rule or the server's rules were broken; nothing more is fetched."""


class Damaged(Exception):
    """A file's SHA-256 was wrong twice."""


def is_private_or_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        a = ipaddress.ip_address(host)
    except ValueError:
        return False
    return a.version == 4 and (a.is_private or a.is_loopback) and not a.is_link_local


class Rules:
    """Where requests may go, for one catalog address."""

    def __init__(self, catalog_url: str):
        u = urllib.parse.urlsplit(catalog_url)
        if u.username or u.password or u.query or u.fragment:
            raise Refused("the catalog address must not hold a user, a query or a fragment")
        if not u.path.endswith(".json"):
            raise Refused("the catalog address must end in .json")
        self.local_host = u.hostname if is_private_or_loopback(u.hostname or "") else None
        if u.scheme == "http" and self.local_host is None:
            raise Refused("plain http is allowed only to a loopback or private IPv4 address")
        if u.scheme not in ("http", "https"):
            raise Refused("the catalog address must be https")
        self.url = catalog_url

    def check_host(self, host: str, port: int):
        """Every address of [host] must be public, except the local catalog host itself."""
        if self.local_host is not None and host == self.local_host:
            return
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except socket.gaierror as e:
            raise OSError("name not resolved") from e
        for info in infos:
            if not ipaddress.ip_address(info[4][0].split("%")[0]).is_global:
                raise Refused("a host address is not public")

    def redirect_allowed(self, frm: str, to: str) -> bool:
        f, t = urllib.parse.urlsplit(frm), urllib.parse.urlsplit(to)
        if t.username or t.password:
            return False
        if f.scheme == "https":
            if t.scheme != "https" or not t.hostname:
                return False
            try:
                a = ipaddress.ip_address(t.hostname)
                return a.version == 4 and a.is_global
            except ValueError:
                return not is_private_or_loopback(t.hostname)
        if f.scheme == "http":
            return t.scheme == "http" and t.hostname == f.hostname and (t.port or 80) == (f.port or 80)
        return False


class NoRedirects(urllib.request.HTTPRedirectHandler):
    """Redirects are followed by get() itself, so each hop can be checked before it is made."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Http:
    def __init__(self, rules: Rules, log=None):
        self.rules = rules
        # No proxy from the environment (the address rule applies to the connection actually made), no cookies.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirects())
        self.log = log

    def get(self, url: str, range_from: int = 0, if_match: str | None = None, if_none_match: str | None = None):
        """The response after checked redirects (status, headers, body), or urllib.error.HTTPError for 4xx/5xx."""
        headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}
        if range_from > 0:
            headers["Range"] = f"bytes={range_from}-"
            if if_match:
                headers["If-Match"] = if_match
        if if_none_match:
            headers["If-None-Match"] = if_none_match
        hops = 0
        while True:
            u = urllib.parse.urlsplit(url)
            self.rules.check_host(u.hostname, u.port or (443 if u.scheme == "https" else 80))
            req = urllib.request.Request(url, headers=headers, method="GET")
            try:
                r = self.opener.open(req, timeout=30)
            except urllib.error.HTTPError as e:
                if e.code in (301, 302, 303, 307, 308):
                    location = e.headers.get("Location")
                    e.close()
                    if not location:
                        raise Refused("a redirect without a Location")
                    nxt = urllib.parse.urljoin(url, location)
                    hops += 1
                    if hops > MAX_REDIRECTS:
                        raise Refused("too many redirects")
                    if not self.rules.redirect_allowed(url, nxt):
                        raise Refused("a redirect the network rule refuses")
                    url = nxt
                    continue
                raise
            ce = r.headers.get("Content-Encoding")
            if ce and ce.strip().lower() != "identity":
                r.close()
                raise Refused("a Content-Encoding")
            return r


def fetch_catalog(http: Http, url: str, min_version: int, max_version: int):
    r = http.get(url)
    with r:
        data = r.read(cf.CATALOG_MAX_BYTES + 1)
    return cf.parse(data, url, min_version, max_version)


def strong_etag(headers) -> str | None:
    e = headers.get("ETag")
    return e if e and not e.startswith("W/") else None


def fetch_file(http: Http, url: str, dest: Path, size: int, sha256: str, etag: str | None, progress=None, on_etag=None) -> str | None:
    """One transfer file (2.4): resumes a partial file with Range and If-Match; returns the ETag to keep.

    [on_etag] is called with each new ETag as soon as the headers arrive, so a transfer cut short
    can still resume with If-Match the next time.
    """
    restarts = 0
    while True:
        have = dest.stat().st_size if dest.exists() else 0
        if have > size:
            dest.unlink()
            have = 0
        h = hashlib.sha256()
        if have:
            with open(dest, "rb") as f:
                for b in iter(lambda: f.read(CHUNK), b""):
                    h.update(b)
        start_over = False
        try:
            r = http.get(url, range_from=have, if_match=etag if have else None)
        except urllib.error.HTTPError as e:
            cr = e.headers.get("Content-Range") if e.headers else None
            e.close()
            if e.code == 416 and have == size and cr == f"bytes */{size}":
                r = None                                   # already complete
            elif e.code in (412, 416):                     # the file changed: start again at 0
                r, start_over = None, True
            else:
                raise
        if r is not None:
            with r:
                new_etag = strong_etag(r.headers)
                if r.status == 206:
                    if not r.headers.get("Content-Range", "").startswith(f"bytes {have}-"):
                        raise Refused("a 206 that does not start where asked")
                    if etag and new_etag and new_etag != etag:
                        start_over = True
                elif r.status == 200:
                    have, h = 0, hashlib.sha256()          # the range was ignored: take the whole body
                else:
                    raise Refused(f"HTTP {r.status}")
                if not start_over:
                    etag = new_etag
                    if on_etag:
                        on_etag(etag)
                    cl = r.headers.get("Content-Length")
                    if cl is not None and int(cl) > size - have:
                        raise Refused("a body longer than the catalog says")
                    with open(dest, "r+b" if have else "wb") as out:
                        out.seek(have)
                        out.truncate()
                        for b in iter(lambda: r.read(CHUNK), b""):
                            if have + len(b) > size:
                                raise Refused("a body longer than the catalog says")
                            out.write(b)
                            h.update(b)
                            have += len(b)
                            if progress:
                                progress(len(b))
        if start_over:
            restarts += 1
            if restarts > 3:
                raise Refused("the file keeps changing on the server")
            dest.unlink(missing_ok=True)
            etag = None
            continue
        if have < size:
            raise ConnectionError("cut short; run again to resume")   # bytes kept
        if h.hexdigest() != sha256:
            dest.unlink()
            raise ValueError("damaged")
        return etag


def inflate(src: Path, dest: Path, size: int, sha256: str) -> bool:
    """gzip transfer file -> part file, at most [size] bytes, hashed on the way; False on a mismatch."""
    h, n = hashlib.sha256(), 0
    try:
        with gzip.open(src, "rb") as g, open(dest, "wb") as out:
            for b in iter(lambda: g.read(CHUNK), b""):
                n += len(b)
                if n > size:
                    return False
                out.write(b)
                h.update(b)
    except (OSError, EOFError, zlib.error):
        return False
    return n == size and h.hexdigest() == sha256


def manifest_agrees(m: dict, entry: dict, parts: list) -> bool:
    """Rule 4 of 2.3."""
    if m.get("pack_id") != entry["id"] or m.get("state") != entry["state"] or m.get("osm_snapshot") != entry["osm_snapshot"]:
        return False
    if "pack_version" in m and m["pack_version"] != entry["pack_version"]:
        return False
    mp = m.get("parts") or {}
    for p in parts:
        q = mp.get(p["name"]) or {}
        if q.get("file") != p["file"] or q.get("bytes") != p["bytes"] or str(q.get("sha256", "")).lower() != p["sha256"]:
            return False
    return True


def get_pack(catalog_url: str, key: str, out: Path, skip_optional: bool, min_version: int, max_version: int,
             quiet: bool = False) -> Path:
    rules = Rules(catalog_url)
    http = Http(rules)
    _, usable, unusable = fetch_catalog(http, catalog_url, min_version, max_version)
    entry = next((e for e in usable if e["state"] == key.upper() or e["id"] == key.lower()), None)
    if entry is None:
        why = next((u for u in unusable if u[1] == key.upper() or u[0] == key.lower()), None)
        raise SystemExit(f"no usable entry for {key}" + (f" ({why[3]})" if why else ""))
    out.mkdir(parents=True, exist_ok=True)
    state_path = out / STATE_FILE
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
    if state.get("pack") != f"{entry['id']}-{entry['pack_version']}":
        state = {"pack": f"{entry['id']}-{entry['pack_version']}", "parts": {}}

    def save():
        state_path.write_text(json.dumps(state), encoding="utf-8")

    parts = [p for p in entry["parts"] if not (skip_optional and p["optional"])]
    total = entry["manifest"]["bytes"] + sum(p["transfer"]["bytes"] for p in parts)
    done = [0]

    def progress(n):
        done[0] += n
        if not quiet:
            print(f"\r{entry['name']}: {done[0] / 1e6:,.1f} of {total / 1e6:,.1f} MB", end="", flush=True)

    # 1. The manifest: small, no resume, size and hash from the catalog, then rule 4.
    mt = entry["manifest"]
    r = http.get(cf.resolve(catalog_url, mt["path"]))
    with r:
        mbytes = r.read(mt["bytes"] + 1)
    if len(mbytes) != mt["bytes"] or hashlib.sha256(mbytes).hexdigest() != mt["sha256"]:
        raise SystemExit("LIST_MISMATCH: manifest.json differs from the catalog")
    manifest = json.loads(mbytes.decode("utf-8"))
    if not manifest_agrees(manifest, entry, parts):
        raise SystemExit("LIST_MISMATCH: the manifest and the catalog disagree")
    (out / "manifest.json").write_bytes(mbytes)
    done[0] += len(mbytes)

    # 2. Each part in catalog order.
    for p in parts:
        final = out / p["file"]
        rec = state["parts"].setdefault(p["name"], {"etag": None, "verified": False, "failures": 0})
        if rec["verified"] and final.is_file() and final.stat().st_size == p["bytes"]:
            done[0] += p["transfer"]["bytes"]
            continue
        t = p["transfer"]
        tmp = out / f".transfer-{p['name']}"
        while True:
            try:
                before = tmp.stat().st_size if tmp.exists() else 0
                done[0] += min(before, t["bytes"])
                def keep_etag(e):
                    rec["etag"] = e
                    save()
                rec["etag"] = fetch_file(http, cf.resolve(catalog_url, t["path"]), tmp, t["bytes"], t["sha256"], rec["etag"],
                                         progress, keep_etag)
                save()
            except ValueError:
                rec["failures"] += 1
                rec["etag"] = None
                save()
                if rec["failures"] > HASH_RETRIES:
                    raise Damaged(f"part {p['name']} arrived damaged twice") from None
                done[0] -= t["bytes"]
                continue
            except ConnectionError:
                save()
                raise
            if t["encoding"] == "identity":
                os.replace(tmp, final)
                break
            if inflate(tmp, final, p["bytes"], p["sha256"]):
                tmp.unlink()
                break
            final.unlink(missing_ok=True)
            tmp.unlink(missing_ok=True)
            rec["failures"] += 1
            save()
            if rec["failures"] > HASH_RETRIES:
                raise Damaged(f"part {p['name']} arrived damaged twice")
        rec["verified"] = True
        save()
    if not quiet:
        print()
    state_path.unlink(missing_ok=True)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    ls = sub.add_parser("list")
    ls.add_argument("url")
    ls.add_argument("--manifest-versions", default="3")
    g = sub.add_parser("get")
    g.add_argument("url")
    g.add_argument("key", help="USPS code (PA) or pack id (us-pa)")
    g.add_argument("--out", required=True, type=Path)
    g.add_argument("--skip-optional", action="store_true")
    g.add_argument("--manifest-versions", default="3")
    a = ap.parse_args(argv)
    versions = [int(v) for v in a.manifest_versions.split(",") if v.strip()]
    try:
        if a.cmd == "list":
            rules = Rules(a.url)
            cat, usable, unusable = fetch_catalog(Http(rules), a.url, min(versions), max(versions))
            print(f"serial {cat['serial']}, generated {cat['generated_utc']}")
            for e in usable:
                dl = e["manifest"]["bytes"] + sum(p["transfer"]["bytes"] for p in e["parts"])
                print(f"{e['state']}  {e['name']}: build {e['pack_version']}, data of {e['osm_snapshot']}, download {dl / 1e6:,.1f} MB")
            for u in unusable:
                print(f"{u[1] or '?'}  {u[2] or u[0] or '?'}: {u[3]}")
            return 0
        get_pack(a.url, a.key, a.out, a.skip_optional, min(versions), max(versions))
        print(f"done: {a.out.as_posix()}")
        return 0
    except cf.CatalogRefused as e:
        print(f"the list is refused: {e.reason}", file=sys.stderr)
    except Refused as e:
        print(f"refused: {e}", file=sys.stderr)
    except Damaged as e:
        print(f"damaged: {e}", file=sys.stderr)
    except (urllib.error.URLError, OSError, ConnectionError) as e:
        print(f"network: {type(e).__name__}; run again to resume", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
