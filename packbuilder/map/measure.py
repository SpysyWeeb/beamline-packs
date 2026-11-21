"""Run a command and record its wall time and peak memory.

Peak memory is measured by polling (every 0.25 s, with psutil) the resident set
size (Windows: working set) of the process and all its children, summed, and
keeping the maximum. Polling can miss a spike shorter than the interval, so the
value is a lower bound on the true peak; for a JVM the working set includes the
heap, off-heap buffers and memory-mapped file pages that are resident.

Can be used as a module (run_measured) or from the command line:
    python packbuilder/map/measure.py OUT.json -- command args...
"""
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil


def _tree_rss(p: psutil.Process) -> int:
    total = 0
    try:
        procs = [p] + p.children(recursive=True)
    except psutil.Error:
        return 0
    for q in procs:
        try:
            total += q.memory_info().rss
        except psutil.Error:
            pass
    return total


def run_measured(cmd, log_path: Path, cwd=None, env=None) -> dict:
    """Run cmd, stream its output to log_path, return measurements."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    peak = 0
    with open(log_path, "w", encoding="utf-8", errors="replace") as log:
        proc = subprocess.Popen([str(c) for c in cmd], cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT)
        ps = psutil.Process(proc.pid)
        stop = threading.Event()

        def poll():
            nonlocal peak
            while not stop.is_set():
                peak = max(peak, _tree_rss(ps))
                stop.wait(0.25)

        th = threading.Thread(target=poll, daemon=True)
        th.start()
        rc = proc.wait()
        stop.set()
        th.join()
    return {
        "command": [str(c) for c in cmd],
        "returncode": rc,
        "wall_seconds": round(time.time() - t0, 1),
        "peak_rss_bytes": peak,
        "peak_rss_gib": round(peak / 1024**3, 2),
        "peak_method": "psutil RSS (working set) of process tree, polled every 0.25 s",
        "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t0)),
    }


if __name__ == "__main__":
    out = Path(sys.argv[1])
    cmd = sys.argv[sys.argv.index("--") + 1:]
    res = run_measured(cmd, out.with_suffix(".log"))
    out.write_text(json.dumps(res, indent=2))
    print(json.dumps(res, indent=2))
    sys.exit(res["returncode"])
