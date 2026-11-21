"""Aggregate comparison traces (cmp_trace.py) into selector/maps/cmp.json.gz.

    python /tools/cmp_aggregate.py --traces /traces/cmp0 --out /out

Output:
  traced: test numbers whose traced run passed (their records are complete)
  sets:   deduplicated test sets (hex bitsets), referenced by index below
  sites:  site key -> {"s": {signature: set index}, "v": {value repr: set index}}
          signature = "<op>|<rel chars>" (see cmp_trace.RELS); "v" only for
          comparisons against a literal ('*' = a value that was not plain, or
          too many distinct values)
  ran:    "<rel>:<line>" -> set index: tests that ran a line holding sites
"""

from __future__ import annotations

import argparse
import glob
import gzip
import json
import os


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--traces", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    traced = []
    with open(os.path.join(args.traces, "results.jsonl")) as handle:
        for line in handle:
            if line.strip():
                rec = json.loads(line)
                if rec["rc"] == 0:
                    traced.append(rec["number"])
    traced_set = set(traced)

    site_sig: dict = {}   # site -> sig -> bits
    site_val: dict = {}   # site -> repr -> bits
    ran: dict = {}        # rel:line -> bits
    for n in sorted(traced_set):
        bit = 1 << n
        sigs: dict = {}
        vals: dict = {}
        lines: set = set()
        for path in glob.glob(os.path.join(args.traces, str(n), "cmp-*.json.gz")):
            try:
                with gzip.open(path, "rt") as handle:
                    data = json.load(handle)
            except (OSError, EOFError, ValueError):
                continue
            for site, ss in data["sigs"].items():
                sigs.setdefault(site, set()).update(ss)
            for site, vs in data["vals"].items():
                cur = vals.get(site)
                if vs == "*" or cur == "*":
                    vals[site] = "*"
                else:
                    vals.setdefault(site, set()).update(vs)
            for rel, ls in data["ran"].items():
                lines.update(f"{rel}:{ln}" for ln in ls)
        for site, ss in sigs.items():
            d = site_sig.setdefault(site, {})
            for s in ss:
                d[s] = d.get(s, 0) | bit
        for site, vs in vals.items():
            d = site_val.setdefault(site, {})
            for v in (["*"] if vs == "*" else vs):
                d[v] = d.get(v, 0) | bit
        for key in lines:
            ran[key] = ran.get(key, 0) | bit

    index: dict = {}
    sets: list = []

    def ref(bits: int) -> int:
        enc = format(bits, "x")
        i = index.get(enc)
        if i is None:
            i = index[enc] = len(sets)
            sets.append(enc)
        return i

    sites_out = {}
    for site, d in site_sig.items():
        entry = {"s": {s: ref(b) for s, b in d.items()}}
        if site in site_val:
            entry["v"] = {v: ref(b) for v, b in site_val[site].items()}
        sites_out[site] = entry
    out = {
        "traced": sorted(traced_set),
        "sets": sets,
        "sites": sites_out,
        "ran": {k: ref(b) for k, b in ran.items()},
    }
    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, "cmp.json.gz")
    with gzip.open(path, "wt", compresslevel=9) as handle:
        json.dump(out, handle, separators=(",", ":"))
    print(f"traced {len(traced_set)} tests, {len(sites_out)} sites, {len(ran)} lines, "
          f"{len(sets)} distinct sets, {os.path.getsize(path)} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
