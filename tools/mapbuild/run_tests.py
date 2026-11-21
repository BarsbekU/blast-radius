"""Run tests one per pytest process, optionally traced (see blast_trace.py).

Runs inside the mapbuild image, where sqlglot at the pinned commit is /src.

    python /tools/run_tests.py --out /traces/seed0 --seed 0 --jobs 14
    python /tools/run_tests.py --out /traces/check --only 12,845 --no-trace

Writes <out>/<number>/<pid>.json.gz traces and <out>/results.jsonl with each
test's exit code (0 passed, 1 failed, 5 nothing collected) and duration.
Resumable: tests already in results.jsonl are skipped.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tests", default="/data/tests.json")
    parser.add_argument("--out", required=True)
    parser.add_argument("--seed", default="0")
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 4)
    parser.add_argument("--only", default="", help="comma separated test numbers")
    parser.add_argument("--timeout", type=int, default=1200)
    parser.add_argument("--no-trace", action="store_true")
    parser.add_argument("--cmp", action="store_true", help="comparison trace (cmp_trace.py) instead of line trace")
    parser.add_argument("--cmp-cache", default="", help="shared cache of rewritten module code")
    parser.add_argument("--src", default="/src")
    args = parser.parse_args()

    tests = json.load(open(args.tests))["tests"]
    if args.only:
        keep = {int(x) for x in args.only.split(",") if x}
        tests = [t for t in tests if t["number"] in keep]
    # Longest first keeps the pool busy until the end.
    tests.sort(key=lambda t: -t["seconds"])

    os.makedirs(args.out, exist_ok=True)
    results_path = os.path.join(args.out, "results.jsonl")
    done: set[int] = set()
    if os.path.exists(results_path):
        with open(results_path) as handle:
            for line in handle:
                if line.strip():
                    done.add(json.loads(line)["number"])
    todo = [t for t in tests if t["number"] not in done]
    print(f"{len(todo)} to run ({len(done)} already done), jobs={args.jobs}", flush=True)

    lock = threading.Lock()
    started = time.monotonic()
    finished = [0]
    failures = [0]

    def run(test: dict) -> None:
        trace_dir = os.path.join(args.out, str(test["number"]))
        shutil.rmtree(trace_dir, ignore_errors=True)
        os.makedirs(trace_dir, exist_ok=True)
        env = dict(os.environ)
        env["PYTHONHASHSEED"] = str(args.seed)
        env["SKIP_INTEGRATION"] = "0"
        env.pop("BLAST_TRACE_DIR", None)
        env.pop("BLAST_CMP_DIR", None)
        if args.cmp:
            env["BLAST_CMP_DIR"] = trace_dir
            env["BLAST_TRACE_ROOT"] = args.src.rstrip("/") + "/"
            if args.cmp_cache:
                env["BLAST_CMP_CACHE"] = args.cmp_cache
        elif not args.no_trace:
            env["BLAST_TRACE_DIR"] = trace_dir
            env["BLAST_TRACE_ROOT"] = args.src.rstrip("/") + "/"
        t0 = time.monotonic()
        proc = subprocess.run(
            [
                sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q",
                "--no-header", "-o", "addopts=", f"--timeout={args.timeout}", test["id"],
            ],
            cwd=args.src, env=env, capture_output=True, text=True,
        )
        record = {
            "number": test["number"],
            "id": test["id"],
            "rc": proc.returncode,
            "secs": round(time.monotonic() - t0, 3),
        }
        if proc.returncode != 0:
            record["tail"] = (proc.stdout[-3000:] + proc.stderr[-2000:])
        with lock:
            with open(results_path, "a") as handle:
                handle.write(json.dumps(record) + "\n")
            finished[0] += 1
            if proc.returncode != 0:
                failures[0] += 1
                print(f"  rc={proc.returncode} {test['id']}", flush=True)
            if finished[0] % 100 == 0 or finished[0] == len(todo):
                elapsed = time.monotonic() - started
                print(f"{finished[0]}/{len(todo)} done, {failures[0]} nonzero, {elapsed:.0f}s", flush=True)

    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        list(pool.map(run, todo))
    return 0


if __name__ == "__main__":
    sys.exit(main())
