#!/usr/bin/env python3
"""Run the selector on a labeled data set and explain its score per commit.

    python tools/eval.py                       # sample/
    python tools/eval.py --data work/corpus    # any dir with tests.json, commits/, answers.json
    python tools/eval.py --top 15 --kinds

For every commit: selected cost, oracle cost (the failing tests alone,
packed optimally), excess, and misses with the failing tests left out.
The selector runs in-process on this machine's Python, so CPU is not the
container's; use tools/run_local.py for the exact board number.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import subprocess
import sys
from pathlib import Path

import importlib.util

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("blast_select", ROOT / "selector" / "blast.py")


def load_selector(data: Path, maps: Path):
    os.environ["BLAST_DATA"] = str(data)
    os.environ["BLAST_MAPS"] = str(maps)
    os.environ.setdefault("BLAST_TIME_BUDGET", "1000000")  # no wall-clock throttling in evaluation
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, default=ROOT / "sample")
    parser.add_argument("--maps", type=Path, default=ROOT / "selector" / "maps")
    parser.add_argument("--top", type=int, default=12, help="show the N commits with most excess")
    parser.add_argument("--kinds", action="store_true", help="break totals down by commit kind")
    parser.add_argument("--only", default="", help="comma separated commit ids")
    parser.add_argument("--reasons", action="store_true")
    parser.add_argument("--dump", type=Path, help="write per-commit selections as JSON")
    args = parser.parse_args()

    sel = load_selector(args.data, args.maps)
    tests = json.loads((args.data / "tests.json").read_text("utf-8"))["tests"]
    answers = json.loads((args.data / "answers.json").read_text("utf-8"))["commits"]
    ms = {t["number"]: int(t["seconds"] * 1000 + 0.5) for t in tests}
    names = {t["number"]: t["id"] for t in tests}

    maps = sel.Maps(args.maps)
    selector = sel.Selector(maps, tests)

    def cost_of(workers):
        if not workers:
            return 0
        return max(sum(ms[n] for n in w) for w in workers) + 2000 * len(workers)

    rows = []
    only = set(filter(None, args.only.split(",")))
    for patch in sorted((args.data / "commits").glob("*.patch")):
        commit = patch.stem
        if only and commit not in only:
            continue
        if commit not in answers:
            continue  # unlabeled (e.g. the suite hung on it)
        reasons: list = []
        bits = selector.commit(patch.read_text("utf-8", "replace"), reasons)
        chosen = [n for n in ms if bits >> n & 1]
        workers = sel.pack(chosen, ms)
        cost = cost_of(workers)
        failing = answers[commit]["failing"]
        oracle = cost_of(sel.pack(failing, ms))
        missed = [n for n in failing if not bits >> n & 1]
        rows.append({
            "commit": commit,
            "kind": answers[commit].get("kind", "?"),
            "chosen": chosen,
            "failing": failing,
            "n": len(chosen),
            "cost": cost,
            "oracle": oracle,
            "missed": missed,
            "reasons": reasons,
            "heavy": [names[n].split("::")[-1] for n in chosen if ms[n] > 2500],
        })

    if args.dump:
        args.dump.write_text(json.dumps(rows, indent=0))
    total = sum(r["cost"] for r in rows)
    oracle = sum(r["oracle"] for r in rows)
    misses = [r for r in rows if r["missed"]]
    print(f"commits {len(rows)} | simulated {total/1000:.1f}s + misses {len(misses)}x300 "
          f"= {total/1000 + 300*len(misses):.1f}s | oracle {oracle/1000:.1f}s | "
          f"excess {(total-oracle)/1000:.1f}s")
    for r in misses:
        print(f"  MISS {r['commit']} ({r['kind']}): left out {[names[n] for n in r['missed']]}")
        for reason in r["reasons"][:8]:
            print(f"       - {reason}")
    if args.kinds:
        by = collections.defaultdict(lambda: [0, 0, 0, 0])
        for r in rows:
            b = by[r["kind"]]
            b[0] += 1
            b[1] += r["cost"]
            b[2] += r["oracle"]
            b[3] += bool(r["missed"])
        for kind, (count, cost, orc, miss) in sorted(by.items()):
            print(f"  {kind:10s} n={count:3d} cost {cost/1000:7.1f}s oracle {orc/1000:7.1f}s "
                  f"excess {(cost-orc)/1000:7.1f}s misses {miss}")
    print("most excess:")
    for r in sorted(rows, key=lambda r: r["oracle"] - r["cost"])[: args.top]:
        print(f"  {r['commit']} {r['kind']:8s} {r['n']:4d} tests {r['cost']/1000:6.2f}s "
              f"(oracle {r['oracle']/1000:5.2f}s) heavy={r['heavy']}")
        if args.reasons:
            for reason in r["reasons"][:6]:
                print(f"       - {reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
