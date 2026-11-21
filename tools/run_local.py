#!/usr/bin/env python3
"""Build the submission image and run it the way the scorer does, then score it.

Unlike `make sample`, this also measures the container's CPU time (the board
adds it) and can run against any data set laid out like sample/:

    <data>/tests.json  <data>/commits/*.patch  <data>/answers.json

usage:
    python tools/run_local.py                      # build, 1 run on sample/
    python tools/run_local.py --runs 3             # board-style middle of 3
    python tools/run_local.py --data work/corpus   # another labeled set
    python tools/run_local.py --no-build --show-misses
"""

from __future__ import annotations

import argparse
import json
import shlex
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import score  # noqa: E402  (the kit's scorer, same rules as the server)


def docker_run_flags(data: Path) -> list[str]:
    # Mirrors RUN_FLAGS in the kit's Makefile.
    return [
        "--rm", "--platform", "linux/amd64", "--network", "none", "--cpus", "2",
        "--memory", "4096m", "--memory-swap", "4096m", "--pids-limit", "256",
        "--read-only", "--tmpfs", "/tmp:rw,size=64m,mode=1777", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges", "--user", "65534:65534",
        "--env", "HOME=/tmp",
        "--volume", f"{(data / 'commits').resolve()}:/data/commits:ro",
        "--volume", f"{(data / 'tests.json').resolve()}:/data/tests.json:ro",
    ]


def image_command(image: str) -> list[str]:
    out = subprocess.run(
        ["docker", "image", "inspect", "-f", "{{json .Config.Entrypoint}}|{{json .Config.Cmd}}", image],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    entrypoint, cmd = (json.loads(part) or [] for part in out.split("|", 1))
    return list(entrypoint) + list(cmd)


def run_once(image: str, data: Path) -> dict:
    # Wrap the image's own command so the container reports its cgroup CPU
    # usage (every process in it, user + system) on stderr after it exits.
    command = shlex.join(image_command(image))
    wrapper = (
        f"{command}; status=$?; "
        "echo \"__CPU_STAT__ $(grep usage_usec /sys/fs/cgroup/cpu.stat)\" >&2; exit $status"
    )
    started = time.monotonic()
    proc = subprocess.run(
        ["docker", "run", *docker_run_flags(data), "--entrypoint", "sh", image, "-c", wrapper],
        capture_output=True,
    )
    wall = time.monotonic() - started
    stderr = proc.stderr.decode("utf-8", "replace")
    cpu_usec = None
    log_lines = []
    for line in stderr.splitlines():
        if line.startswith("__CPU_STAT__"):
            parts = line.split()
            if len(parts) >= 3:
                cpu_usec = int(parts[2])
        else:
            log_lines.append(line)
    return {
        "status": proc.returncode,
        "stdout": proc.stdout,
        "log": "\n".join(log_lines),
        "wall": wall,
        "cpu_usec": cpu_usec,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, default=ROOT / "sample")
    parser.add_argument("--image", default="blast-radius-entry")
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--no-build", action="store_true")
    parser.add_argument("--show-misses", action="store_true")
    parser.add_argument("--quiet-log", action="store_true")
    parser.add_argument("--save", type=Path, default=ROOT / "output.jsonl")
    args = parser.parse_args()

    if not args.no_build:
        subprocess.run(
            ["docker", "build", "--platform", "linux/amd64", "--tag", args.image, str(ROOT)],
            check=True,
        )

    tests = json.loads((args.data / "tests.json").read_text("utf-8"))["tests"]
    answers = json.loads((args.data / "answers.json").read_text("utf-8"))["commits"]

    totals = []
    for run_index in range(args.runs):
        result = run_once(args.image, args.data)
        if not args.quiet_log and result["log"]:
            print(result["log"], file=sys.stderr)
        args.save.write_bytes(result["stdout"])
        if result["status"] != 0:
            print(f"run {run_index + 1}: container exited {result['status']} -> submission rejected")
            return 1
        evaluation = score.evaluate_run(result["stdout"], tests, answers)
        if not evaluation["ok"]:
            print(f"run {run_index + 1}: Rejected: {evaluation['code']} line {evaluation['line']}")
            return 1
        simulated_ms = evaluation["simulated_milliseconds"]
        cpu_usec = result["cpu_usec"] or 0
        total_usec = score.run_total_microseconds(simulated_ms, cpu_usec)
        totals.append(total_usec)
        misses = evaluation["missed_commits"]
        print(
            f"run {run_index + 1}: simulated {simulated_ms / 1000:.3f} s | misses {len(misses)} "
            f"({300 * len(misses)} s) | cpu {cpu_usec / 1e6:.3f} s | total {total_usec / 1e6:.3f} s "
            f"| wall {result['wall']:.1f} s"
        )
        if args.show_misses:
            for commit in misses:
                print(f"   missed {commit}: failing {answers[commit]['failing']}")

    middle = statistics.median_low(sorted(totals)) if len(totals) % 2 == 0 else statistics.median(totals)
    score_value = (middle + 50_000) // 100_000 / 10
    print(f"score (middle of {len(totals)}): {score_value:.1f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
