"""Run the whole address pipeline for Pennsylvania, one step after another.

    python packbuilder/addresses/run.py            # skip finished steps
    python packbuilder/addresses/run.py --force tiger build   # re-run named steps (and nothing else forced)
    python packbuilder/addresses/run.py --list

Each step runs in its own process so that its wall time and peak memory are
measured separately (data/addresses/metrics/<step>.json). A step whose metrics
file exists is skipped unless named after --force.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
STEPS = [
    ("download", ["step_download.py", "--skip-nad"]),
    ("nad", ["step_nad.py"]),
    ("extra", ["step_extra.py"]),
    ("osm", ["step_osm.py"]),
    ("tiger", ["step_tiger.py"]),
    ("coverage", ["step_coverage.py"]),
    ("build", ["step_build.py"]),
    ("eval_accuracy", ["step_eval_accuracy.py"]),
    ("eval_parser", ["step_eval_parser.py"]),
    ("estimate", ["step_estimate.py"]),
    ("variants", ["step_variants.py"]),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", nargs="*", default=[], help="step names to re-run")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--only", nargs="*", default=None, help="run only these steps")
    a = ap.parse_args()
    if a.list:
        for name, cmd in STEPS:
            print(f"{name:14} python packbuilder/addresses/{' '.join(cmd)}")
        return
    t0 = time.time()
    for name, cmd in STEPS:
        if a.only is not None and name not in a.only:
            continue
        args = [sys.executable, str(HERE / cmd[0])] + cmd[1:] + (["--force"] if name in a.force else [])
        print(f"== {name}: {' '.join(args[1:])}", flush=True)
        r = subprocess.run(args, cwd=HERE.parents[1])
        if r.returncode != 0:
            raise SystemExit(f"step {name} failed with exit code {r.returncode}")
    print(f"== all done in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
