#!/usr/bin/env python3
"""Run-time coverage for function lines that also run while modules import.

Input: traces made with BLAST_TRACE_RUNTIME=1 (blast_trace.py records only
lines executed outside module loading), and the regular coverage shards in
<maps>/cov. Output <maps>/rtcov.json.gz:

    {"sets": [encoded bitsets...], "files": {rel: {line: set index}}}

with an entry for every function line whose run-time testers differ from its
regular testers, i.e. lines that some test runs only during an import
(metaclasses, __init_subclass__, helpers called from class bodies...). For
those lines the selector takes the run-time testers and leaves what the line
did during imports to the import-time fingerprint.

    python rt_aggregate.py --traces /traces/rt0 --maps /out
"""

from __future__ import annotations

import argparse
import glob
import gzip
import json
import os
from collections import defaultdict

SRC = "/src/"


def rel(path: str) -> str | None:
    return path[len(SRC):] if path.startswith(SRC) else None


def encode(bits: int):
    count = bin(bits).count("1")
    if count <= 64:
        out, b = [], bits
        while b:
            low = b & -b
            out.append(low.bit_length() - 1)
            b ^= low
        return out
    return format(bits, "x")


def decode(enc) -> int:
    if isinstance(enc, str):
        return int(enc, 16) if enc else 0
    bits = 0
    for n in enc:
        bits |= 1 << n
    return bits


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--traces", nargs="+", required=True)
    parser.add_argument("--maps", required=True)
    parser.add_argument("--tests", default="/data/tests.json")
    args = parser.parse_args()

    numbers = [t["number"] for t in json.load(open(args.tests))["tests"]]
    rt: dict = defaultdict(int)
    missing = []
    for number in numbers:
        bit = 1 << number
        found = 0
        keys = set()
        for root in args.traces:
            for path in glob.glob(os.path.join(root, str(number), "*.json.gz")):
                found += 1
                with gzip.open(path, "rt") as handle:
                    data = json.load(handle)
                for filename, _qual, _first, is_func, lines in data["code"]:
                    r = rel(filename)
                    if r is None or not is_func:
                        continue
                    for line in lines:
                        keys.add((r, line))
        if not found:
            missing.append(number)
        for key in keys:
            rt[key] |= bit
    if missing:
        print(f"WARNING: no traces for {len(missing)} tests: {missing[:20]}")
        if len(missing) > 5:
            raise SystemExit("too many tests without traces; refusing to write a partial map")

    files: dict = {}
    sets: list = []
    index: dict = {}
    diff_lines = 0
    only_import = 0
    # Shard names are rel.replace("/", "__"), which is not reversible
    # ("__init__.py"): map them back from the source tree.
    by_name = {}
    for dirpath, _dirs, names in os.walk(SRC):
        for name in names:
            r = os.path.relpath(os.path.join(dirpath, name), SRC).replace(os.sep, "/")
            by_name[r.replace("/", "__") + ".json.gz"] = r
    for shard_path in sorted(glob.glob(os.path.join(args.maps, "cov", "*.json.gz"))):
        r = by_name.get(os.path.basename(shard_path))
        if r is None:
            print(f"WARNING: no source for shard {shard_path}")
            continue
        with gzip.open(shard_path, "rt") as handle:
            shard = json.load(handle)
        shard_sets = shard["sets"]
        for line, idx in shard["f"].items():
            full = decode(shard_sets[idx])
            run = rt.get((r, int(line)), 0)
            if run == full:
                continue
            # Run-time testers the regular trace missed (other seeds) stay in.
            enc = json.dumps(encode(run))
            k = index.get(enc)
            if k is None:
                k = index[enc] = len(sets)
                sets.append(encode(run))
            files.setdefault(r, {})[line] = k
            diff_lines += 1
            only_import += run == 0
    out = os.path.join(args.maps, "rtcov.json.gz")
    blob = json.dumps({"sets": sets, "files": files}, separators=(",", ":")).encode()
    with gzip.open(out, "wb", compresslevel=9) as handle:
        handle.write(blob)
    print(f"rtcov: {diff_lines} lines in {len(files)} files ({only_import} only at import), "
          f"{len(sets)} sets, {os.path.getsize(out) / 1e6:.2f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
