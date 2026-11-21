#!/usr/bin/env python3
"""Simulate "verify the cheap tests, skip the heavy ones" on labeled data.

    python tools/proxy_stats.py work/corpus1_sel.json work/corpus1/tests.json

For each commit whose selection S contains heavy tests H (>= --heavy s):
run the cheap part C = S - H at decision time (its result equals the labels,
since tests are deterministic). If some test in C fails, output the failing
cheap tests plus H; otherwise output nothing. Reports how often a heavy test
fails while every cheap one passes (a miss) and the net effect on cost,
charging --overhead s of CPU plus C's own seconds for each verification.
"""

from __future__ import annotations

import argparse
import heapq
import json
from pathlib import Path


def pack_cost(numbers, ms) -> int:
    if not numbers:
        return 0
    items = sorted(numbers, key=lambda n: -ms[n])
    total = sum(ms[n] for n in items)
    best = None
    for w in range(1, min(8, len(items)) + 1):
        heap = [(0, k) for k in range(w)]
        loads = [0] * w
        for n in items:
            load, k = heapq.heappop(heap)
            loads[k] = load + ms[n]
            heapq.heappush(heap, (loads[k], k))
        cost = max(loads) + 2000 * w
        best = cost if best is None or cost < best else best
    return best


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("dump", type=Path)
    parser.add_argument("tests", type=Path)
    parser.add_argument("--heavy", type=float, default=2.5)
    parser.add_argument("--overhead", type=float, default=2.0)
    parser.add_argument("--min-cheap", type=int, default=1)
    args = parser.parse_args()

    rows = json.loads(args.dump.read_text())
    tests = json.loads(args.tests.read_text())["tests"]
    ms = {t["number"]: int(t["seconds"] * 1000 + 0.5) for t in tests}
    name = {t["number"]: t["id"].split("::")[-1] for t in tests}
    heavy_ms = args.heavy * 1000

    base_total = policy_total = 0
    misses = []
    affected = 0
    for row in rows:
        chosen = set(row["chosen"])
        failing = set(row["failing"])
        base = pack_cost(chosen, ms)
        base_total += base
        heavy = {n for n in chosen if ms[n] >= heavy_ms}
        cheap = chosen - heavy
        if not heavy or len(cheap) < args.min_cheap:
            policy_total += base
            continue
        affected += 1
        verify_cost = int(args.overhead * 1000) + sum(ms[n] for n in cheap)
        cheap_fail = cheap & failing
        if cheap_fail:
            out = cheap_fail | heavy
        else:
            out = set()
            if heavy & failing:
                misses.append((row["commit"], row["kind"], sorted(name[n] for n in heavy & failing), len(cheap)))
        cost = pack_cost(out, ms) + verify_cost
        policy_total += cost
    print(f"commits {len(rows)}, with heavy tests selected: {affected}")
    print(f"base {base_total/1000:.1f}s  policy {policy_total/1000:.1f}s (+{300*len(misses)}s misses) "
          f"-> net {(base_total - policy_total)/1000 - 300*len(misses):.1f}s saved")
    for miss in misses:
        print("  would miss:", miss)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
