"""Development server for the download site, with real Range and ETag support.

    python packbuilder/catalog/serve_packs.py --root data/catalog/site [--bind 127.0.0.1] [--port 8765]
        [--log-headers] [--log-clients] [--throttle-kbps N] [--cut-after BYTES] [--ignore-range] [--fail-every N]

docs/architecture-downloads.md 7.2. Standard library only (Python's SimpleHTTPRequestHandler
ignores Range). GET and HEAD only; a URL path maps to a file under --root only when every segment
matches [A-Za-z0-9][A-Za-z0-9._-]*. Strong ETag "<size hex>-<mtime_ns hex>"; one byte range ->
206, a start at or past the end -> 416 with "bytes */<size>", several ranges -> 200 with the whole
file; If-Match mismatch -> 412, If-None-Match match -> 304, If-Range honoured.

One log line per request: time, method, path, Range, status, bytes sent, User-Agent; the client's
address only with --log-clients, so a pasted log carries no home-network address. --log-headers
prints every request header. Faults for tests: --cut-after N closes the connection after N body
bytes, once per path; --ignore-range answers 200 always; --throttle-kbps; --fail-every N answers
every Nth request 503.

The default bind is 127.0.0.1: the emulator reaches it as 10.0.2.2, a USB phone through
`adb reverse tcp:8765 tcp:8765` as 127.0.0.1. --bind 0.0.0.0 serves the home network.
"""
from __future__ import annotations

import argparse
import email.utils
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
CHUNK = 64 * 1024


class Options:
    def __init__(self, root: Path, log_headers=False, log_clients=False, throttle_kbps=0, cut_after=None,
                 ignore_range=False, fail_every=0, out=None):
        self.root = root.resolve()
        self.log_headers, self.log_clients = log_headers, log_clients
        self.throttle_kbps, self.cut_after, self.ignore_range, self.fail_every = throttle_kbps, cut_after, ignore_range, fail_every
        self.out = out or sys.stdout
        self.lock = threading.Lock()
        self.count = 0
        self.cut_done: set[str] = set()
        self.redirects: dict[str, str] = {}      # tests: path -> Location (301)
        self.log: list[dict] = []                 # tests: every request as a dict


def etag_of(st: os.stat_result) -> str:
    return f'"{st.st_size:x}-{st.st_mtime_ns:x}"'


def parse_range(value: str, size: int):
    """('one', start, end) | ('many',) | ('none',) | ('unsatisfiable',)."""
    m = re.fullmatch(r"\s*bytes\s*=\s*(.+)", value or "")
    if not m:
        return ("none",)
    specs = [s.strip() for s in m.group(1).split(",") if s.strip()]
    if len(specs) != 1:
        return ("many",) if specs else ("none",)
    r = re.fullmatch(r"(\d+)-(\d*)", specs[0])
    if not r:
        return ("none",)                          # suffix ranges and odd forms: the whole file
    start = int(r.group(1))
    end = int(r.group(2)) if r.group(2) else size - 1
    if start >= size:
        return ("unsatisfiable",)
    if end < start:
        return ("none",)
    return ("one", start, min(end, size - 1))


def etag_matches(header: str, etag: str) -> bool:
    tags = [t.strip() for t in header.split(",")]
    return "*" in tags or etag in tags


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "serve_packs"
    sys_version = ""
    opts: Options = None  # set per server

    def log_message(self, format, *args):        # our own log line only
        pass

    def _log(self, status: int, sent: int):
        o = self.opts
        line = (f"{time.strftime('%H:%M:%S')} {self.command} {self.path.split('?')[0]} "
                f"Range={self.headers.get('Range', '-')} {status} {sent} UA={self.headers.get('User-Agent', '-')}")
        if o.log_clients:
            line += f" client={self.client_address[0]}"
        with o.lock:
            o.log.append({"method": self.command, "path": self.path.split("?")[0], "range": self.headers.get("Range"),
                          "if_match": self.headers.get("If-Match"), "status": status, "sent": sent,
                          "headers": dict(self.headers.items())})
            print(line, file=o.out, flush=True)
            if o.log_headers:
                for k, v in self.headers.items():
                    print(f"    {k}: {v}", file=o.out, flush=True)

    def _simple(self, status: int, extra: dict | None = None, head=False):
        self.send_response(status)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", "0")
        self.end_headers()
        self._log(status, 0)

    def do_POST(self):
        self._method_not_allowed()

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_POST

    def _method_not_allowed(self):
        self.send_response(405)
        self.send_header("Allow", "GET, HEAD")
        self.send_header("Content-Length", "0")
        self.end_headers()
        self._log(405, 0)

    def do_HEAD(self):
        self._serve(head=True)

    def do_GET(self):
        self._serve(head=False)

    def _file(self):
        path = self.path.split("?", 1)[0].split("#", 1)[0]
        segs = path.split("/")[1:]
        if not path.startswith("/") or not segs or not all(SEGMENT.match(s) for s in segs):
            return None
        p = self.opts.root.joinpath(*segs)
        try:
            p.resolve().relative_to(self.opts.root)
        except ValueError:
            return None
        return p if p.is_file() else None

    def _serve(self, head: bool):
        o = self.opts
        with o.lock:
            o.count += 1
            n = o.count
        if o.fail_every and n % o.fail_every == 0:
            return self._simple(503, {"Retry-After": "1"})
        path = self.path.split("?", 1)[0]
        if path in o.redirects:
            return self._simple(301, {"Location": o.redirects[path]})
        f = self._file()
        if f is None:
            return self._simple(404)
        st = f.stat()
        size, etag = st.st_size, etag_of(st)
        base = {"Accept-Ranges": "bytes", "ETag": etag, "Last-Modified": email.utils.formatdate(st.st_mtime, usegmt=True),
                "Cache-Control": "no-cache"}
        im = self.headers.get("If-Match")
        if im is not None and not etag_matches(im, etag):
            return self._simple(412, base)
        inm = self.headers.get("If-None-Match")
        if inm is not None and etag_matches(inm, etag):
            return self._simple(304, base)
        rng = ("none",) if o.ignore_range else parse_range(self.headers.get("Range"), size)
        if_range = self.headers.get("If-Range")
        if if_range is not None and rng[0] != "none":
            if if_range.strip() != etag and if_range.strip() != base["Last-Modified"]:
                rng = ("none",)
        if rng[0] == "unsatisfiable":
            return self._simple(416, dict(base, **{"Content-Range": f"bytes */{size}"}))
        if rng[0] == "one":
            status, start, end = 206, rng[1], rng[2]
        else:
            status, start, end = 200, 0, size - 1
        length = max(0, end - start + 1)
        self.send_response(status)
        ctype = "application/json" if f.suffix == ".json" else "application/octet-stream"
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        for k, v in base.items():
            self.send_header(k, v)
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        sent = 0
        if head:
            return self._log(status, 0)
        cut = None
        if o.cut_after is not None:
            with o.lock:
                if path not in o.cut_done:
                    o.cut_done.add(path)
                    cut = o.cut_after
        try:
            with open(f, "rb") as fh:
                fh.seek(start)
                left = length
                t0 = time.monotonic()
                while left > 0:
                    want = min(CHUNK, left)
                    if cut is not None:
                        want = min(want, cut - sent)
                        if want <= 0:
                            self.close_connection = True
                            self.wfile.flush()
                            return
                    b = fh.read(want)
                    if not b:
                        break
                    self.wfile.write(b)
                    sent += len(b)
                    left -= len(b)
                    if o.throttle_kbps:
                        ahead = sent / (o.throttle_kbps * 1000 / 8) - (time.monotonic() - t0)
                        if ahead > 0:
                            time.sleep(ahead)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True
        finally:
            self._log(status, sent)


def make_server(opts: Options, bind: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    handler = type("PackHandler", (Handler,), {"opts": opts})
    server = ThreadingHTTPServer((bind, port), handler)
    server.daemon_threads = True
    return server


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", required=True, type=Path)
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--log-headers", action="store_true")
    ap.add_argument("--log-clients", action="store_true")
    ap.add_argument("--throttle-kbps", type=int, default=0)
    ap.add_argument("--cut-after", type=int, default=None)
    ap.add_argument("--ignore-range", action="store_true")
    ap.add_argument("--fail-every", type=int, default=0)
    a = ap.parse_args(argv)
    if not a.root.is_dir():
        print(f"error: {a.root} is not a folder", file=sys.stderr)
        return 2
    opts = Options(a.root, a.log_headers, a.log_clients, a.throttle_kbps, a.cut_after, a.ignore_range, a.fail_every)
    server = make_server(opts, a.bind, a.port)
    if a.bind == "0.0.0.0":
        print(f"Serving {a.root.as_posix()} to your home network on port {a.port}. Anyone on it can download these files.")
    else:
        print(f"Serving {a.root.as_posix()} on {a.bind}:{a.port}")
    sys.stdout.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
