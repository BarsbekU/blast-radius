#!/usr/bin/env python3
"""Compare selector versions on labeled data sets from eval dumps.

    python tools/bench.py --base selV10 --new selN sample c1eval corpus2 corpus3 corpus4 corpus5 corpus6

Reads work/<prefix>_<dataset>.json (tools/eval.py --dump output) and prints,
per data set: commits, misses, simulated seconds (misses excluded), the
oracle (failing tests only, packed optimally) and the excess over it, for
both versions, and the share of the base version's excess the new one
removes.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load(prefix: str, ds: str):
    path = ROOT / "work" / f"{prefix}_{ds}.json"
    if not path.exists():
        return None
    rows = json.loads(path.read_text("utf-8"))
    sim = sum(r["cost"] for r in rows) / 1000
    oracle = sum(r["oracle"] for r in rows) / 1000
    misses = sum(1 for r in rows if r["missed"])
    return {"n": len(rows), "sim": sim, "oracle": oracle, "excess": sim - oracle, "misses": misses}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--new", required=True)
    parser.add_argument("datasets", nargs="+")
    args = parser.parse_args()
    print(f"{'data set':10s} {'n':>4s} | {'base sim':>9s} {'miss':>4s} {'excess':>8s} | {'new sim':>9s} {'miss':>4s} {'excess':>8s} | {'oracle':>8s} {'excess cut':>10s}")
    tb = tn = to = 0.0
    for ds in args.datasets:
        b, n = load(args.base, ds), load(args.new, ds)
        if b is None or n is None:
            print(f"{ds:10s} missing dump ({'base' if b is None else 'new'})")
            continue
        cut = (1 - n["excess"] / b["excess"]) * 100 if b["excess"] else 0.0
        print(f"{ds:10s} {n['n']:4d} | {b['sim']:9.1f} {b['misses']:4d} {b['excess']:8.1f} | "
              f"{n['sim']:9.1f} {n['misses']:4d} {n['excess']:8.1f} | {n['oracle']:8.1f} {cut:9.1f}%")
        tb += b["excess"]
        tn += n["excess"]
        to += n["oracle"]
    if tb:
        print(f"{'total':10s}      | {'':9s} {'':4s} {tb:8.1f} | {'':9s} {'':4s} {tn:8.1f} | {to:8.1f} {(1 - tn / tb) * 100:9.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
