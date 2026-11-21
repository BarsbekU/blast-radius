"""Which tests read each per-key-tracked table in the key-tracking trace run.

akeys.json.gz says which tests looked up which entry of an UPPER_CASE class
table (BLAST_TRACE_KEYS=1 run). A reader of the table in that same run with
no record for an entry never looked that entry up and never iterated the
table; it used only the table object itself (truth value, length, identity),
which a change to an existing entry's value cannot alter. This map holds,
per table, the tests that read the table in the key-tracking run.

    python /tools/seen_tables.py --traces /traces/seed4 --akeys /out/akeys.json.gz --out /out
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
    ap.add_argument("--akeys", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    with gzip.open(args.akeys, "rt") as handle:
        tables = set(json.load(handle))
    ok = set()
    with open(os.path.join(args.traces, "results.jsonl")) as handle:
        for line in handle:
            if line.strip():
                rec = json.loads(line)
                if rec["rc"] == 0:
                    ok.add(rec["number"])
    seen: dict = {}
    for n in sorted(ok):
        tags = set()
        for path in glob.glob(os.path.join(args.traces, str(n), "*.json.gz")):
            if os.path.basename(path).startswith("cmp-"):
                continue
            try:
                with gzip.open(path, "rt") as handle:
                    data = json.load(handle)
            except (OSError, EOFError, ValueError):
                continue
            for key in data.get("attrs", ()):
                tag = "|".join(key.split("|", 3)[:3])
                if tag in tables:
                    tags.add(tag)
        for tag in tags:
            seen[tag] = seen.get(tag, 0) | (1 << n)
    index: dict = {}
    sets: list = []

    def ref(bits: int) -> int:
        enc = format(bits, "x")
        if enc not in index:
            index[enc] = len(sets)
            sets.append(enc)
        return index[enc]

    out = {"sets": sets, "tables": {tag: ref(b) for tag, b in sorted(seen.items())}}
    path = os.path.join(args.out, "akeys_seen.json.gz")
    with gzip.open(path, "wt", compresslevel=9) as handle:
        json.dump(out, handle, separators=(",", ":"))
    print(f"{len(seen)} tables, {len(sets)} distinct sets, {len(ok)} tests traced, {os.path.getsize(path)} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
