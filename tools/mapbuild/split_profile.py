"""Split exec_profile.py's output into the two files the selector ships.

    python tools/mapbuild/split_profile.py work/exec_profile.json.gz selector/maps

exec_lines.json.gz    per test, per query: pipeline (P) / data (D) lines,
                      read by the selector to decide whether to ask.
exec_profile.json.gz  per query fingerprints, with the recorded code
                      generation inputs moved to a content-addressed store
                      (4,400 calls share ~2,900 distinct expressions), read
                      by selector/exec_check.py.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import sys


def main(src: str, out_dir: str) -> int:
    with gzip.open(src, "rt") as handle:
        profile = json.load(handle)
    store: dict = {}

    def ref(part):
        if part is None:
            return None
        text = json.dumps(part, sort_keys=True, separators=(",", ":"))
        key = hashlib.blake2b(text.encode(), digest_size=10).hexdigest()
        store[key] = text
        return key

    lines: dict = {}
    compact: dict = {"schemas": profile["schemas"]}
    for test in ("tpch", "tpcds"):
        lines[test] = []
        compact[test] = []
        for rec in profile[test]:
            lines[test].append({"P": rec.pop("P"), "D": rec.pop("D")})
            if "codegen_inputs" in rec:
                rec["codegen_inputs"] = [[ref(a), ref(b)] for a, b in rec["codegen_inputs"]]
            compact[test].append(rec)
    compact["store"] = store
    with gzip.open(os.path.join(out_dir, "exec_lines.json.gz"), "wt", compresslevel=9) as handle:
        json.dump(lines, handle, separators=(",", ":"))
    with gzip.open(os.path.join(out_dir, "exec_profile.json.gz"), "wt", compresslevel=9) as handle:
        json.dump(compact, handle, separators=(",", ":"))
    for name in ("exec_lines.json.gz", "exec_profile.json.gz"):
        print(name, os.path.getsize(os.path.join(out_dir, name)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
