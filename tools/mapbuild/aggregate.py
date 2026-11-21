"""Turn per-test traces into the compact maps the selector loads at run time.

Runs inside the mapbuild image:

    python /tools/aggregate.py --traces /traces/seed0 /traces/seed1 --out /out

Writes to --out:
  cov/<path with / as __>.json.gz   one shard per traced source file:
      sets  distinct test sets (list of test numbers, or a hex bitset)
      f     line -> set index: tests that ran the line in function-like code
            (functions, lambdas, comprehensions) - run time
      d     line -> set index: tests that ran the line in a module or class
            body - definition (import) time
      xf/xd lines that have bytecode of each kind (static, from compile())
      imp   set index: tests that imported the module (ran any d line)
      rd    set index: tests that read the file as data (not via import)
  files.json.gz   non-traced files read as data: path -> set encoding
  dirs.json.gz    directories listed: path -> set encoding
  outcomes.json   each test's exit code and seconds when run alone
  src.zip         the pinned sqlglot checkout's text files
"""

from __future__ import annotations

import argparse
import glob
import gzip
import json
import os
import types
import zipfile
from collections import defaultdict

SRC = "/src/"


def rel(path: str) -> str | None:
    return path[len(SRC):] if path.startswith(SRC) else None


def encode(bits: int):
    count = bin(bits).count("1")
    if count <= 64:
        out, i = [], 0
        b = bits
        while b:
            low = b & -b
            out.append(low.bit_length() - 1)
            b ^= low
        return out
    return format(bits, "x")


def static_lines(path: str):
    with open(path, "rb") as handle:
        source = handle.read()
    try:
        top = compile(source, path, "exec", dont_inherit=True)
    except SyntaxError:
        return set(), set()
    func, defn = set(), set()
    stack = [top]
    while stack:
        code = stack.pop()
        target = func if code.co_flags & 0x1 else defn
        for _, _, line in code.co_lines():
            if line is not None:
                target.add(line)
        for const in code.co_consts:
            if isinstance(const, types.CodeType):
                stack.append(const)
    return func, defn


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--traces", nargs="+", required=True)
    parser.add_argument("--tests", default="/data/tests.json")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    tests = json.load(open(args.tests))["tests"]
    numbers = [t["number"] for t in tests]

    # (relfile, kind, line) -> bitset of tests
    cov: dict = defaultdict(int)
    data_reads: dict = defaultdict(int)
    cache_reads: dict = defaultdict(int)
    listed: dict = defaultdict(int)
    imported: dict = defaultdict(int)
    attr_readers: dict = defaultdict(int)
    key_readers: dict = defaultdict(lambda: defaultdict(int))  # "mod|Qual|attr" -> key -> bits
    keyed_tests: dict = defaultdict(int)  # tests traced with table-key recording
    outcomes: dict = {}

    for trace_root in args.traces:
        results = os.path.join(trace_root, "results.jsonl")
        if os.path.exists(results):
            for line in open(results):
                if line.strip():
                    rec = json.loads(line)
                    prev = outcomes.get(rec["number"])
                    rc = rec["rc"] if prev is None else max(prev["rc"], rec["rc"])
                    outcomes[rec["number"]] = {"rc": rc, "secs": rec["secs"]}

    for number in numbers:
        bit = 1 << number
        keys = set()
        reads_d, reads_l, dirs, mods, attrs = set(), set(), set(), set(), set()
        found = 0
        for trace_root in args.traces:
            for path in glob.glob(os.path.join(trace_root, str(number), "*.json.gz")):
                found += 1
                with gzip.open(path, "rt") as handle:
                    data = json.load(handle)
                for filename, _qual, _first, is_func, lines in data["code"]:
                    r = rel(filename)
                    if r is None:
                        continue
                    kind = "f" if is_func else "d"
                    for line in lines:
                        keys.add((r, kind, line))
                for path_read, kinds in data["reads"].items():
                    r = rel(path_read)
                    if r is None:
                        continue
                    if "data" in kinds:
                        reads_d.add(r)
                    if "linecache" in kinds:
                        reads_l.add(r)
                for d in data["listed"]:
                    r = rel(d)
                    if r is not None:
                        dirs.add(r)
                for module in data.get("imported", ()):
                    if module.split(".")[0] in ("sqlglot", "sqlglotc", "tests"):
                        mods.add(module)
                attrs.update(data.get("attrs", ()))
        if not found:
            print(f"WARNING: no traces for test {number}")
        for key in keys:
            cov[key] |= bit
        for r in reads_d:
            data_reads[r] |= bit
        for r in reads_l:
            cache_reads[r] |= bit
        for r in dirs:
            listed[r] |= bit
        for module in mods:
            imported[module] |= bit
        for key in attrs:
            parts = key.split("|", 3)
            if len(parts) == 4:
                key_readers["|".join(parts[:3])][parts[3]] |= bit
                attr_readers["|".join(parts[:3])] |= bit
            else:
                attr_readers[key] |= bit
        if number % 200 == 0:
            print(f"loaded test {number}, {len(cov)} keys", flush=True)

    os.makedirs(os.path.join(args.out, "cov"), exist_ok=True)
    by_file: dict = defaultdict(lambda: {"f": {}, "d": {}})
    for (r, kind, line), bits in cov.items():
        by_file[r][kind][line] = bits

    traced_files = set(by_file)
    for root, _dirs, files in os.walk(SRC):
        for name in files:
            if name.endswith(".py"):
                r = rel(os.path.join(root, name))
                traced_files.add(r)

    total_bytes = 0
    for r in sorted(traced_files):
        entry = by_file.get(r, {"f": {}, "d": {}})
        sets: list = []
        index: dict = {}

        def idx(bits: int) -> int:
            if bits not in index:
                index[bits] = len(sets)
                sets.append(encode(bits))
            return index[bits]

        shard = {"f": {}, "d": {}}
        importers = 0
        for kind in ("f", "d"):
            for line, bits in sorted(entry[kind].items()):
                shard[kind][str(line)] = idx(bits)
                if kind == "d":
                    importers |= bits
        shard["imp"] = idx(importers)
        shard["rd"] = idx(data_reads.get(r, 0))
        shard["rl"] = idx(cache_reads.get(r, 0))
        path = os.path.join(SRC, r)
        if r.endswith(".py") and os.path.exists(path):
            xf, xd = static_lines(path)
            shard["xf"] = sorted(xf)
            shard["xd"] = sorted(xd)
        shard["sets"] = sets
        out_path = os.path.join(args.out, "cov", r.replace("/", "__") + ".json.gz")
        blob = json.dumps(shard, separators=(",", ":")).encode()
        with gzip.open(out_path, "wb", compresslevel=9) as handle:
            handle.write(blob)
        total_bytes += os.path.getsize(out_path)

    with gzip.open(os.path.join(args.out, "files.json.gz"), "wt", compresslevel=9) as handle:
        json.dump(
            {
                "data": {r: encode(b) for r, b in sorted(data_reads.items())},
                "linecache": {r: encode(b) for r, b in sorted(cache_reads.items())},
            },
            handle,
            separators=(",", ":"),
        )
    with gzip.open(os.path.join(args.out, "dirs.json.gz"), "wt", compresslevel=9) as handle:
        json.dump({r: encode(b) for r, b in sorted(listed.items())}, handle, separators=(",", ":"))
    if attr_readers:
        with gzip.open(os.path.join(args.out, "attrs.json.gz"), "wt", compresslevel=9) as handle:
            json.dump({k: encode(b) for k, b in sorted(attr_readers.items())}, handle, separators=(",", ":"))
    if key_readers:
        # Per table: which tests looked up which key ("*": the whole table).
        # Only valid for traces made with BLAST_TRACE_KEYS=1; the selector
        # uses it only when every reader of the table was traced that way.
        with gzip.open(os.path.join(args.out, "akeys.json.gz"), "wt", compresslevel=9) as handle:
            json.dump(
                {tag: {k: encode(b) for k, b in sorted(keys.items())} for tag, keys in sorted(key_readers.items())},
                handle, separators=(",", ":"),
            )
    with gzip.open(os.path.join(args.out, "imports.json.gz"), "wt", compresslevel=9) as handle:
        json.dump({m: encode(b) for m, b in sorted(imported.items())}, handle, separators=(",", ":"))
    with open(os.path.join(args.out, "outcomes.json"), "w") as handle:
        json.dump({str(k): v for k, v in sorted(outcomes.items())}, handle, indent=0)

    zpath = os.path.join(args.out, "src.zip")
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for root, dirs, files in os.walk(SRC):
            dirs[:] = [d for d in dirs if d not in (".git", "__pycache__")]
            for name in sorted(files):
                full = os.path.join(root, name)
                if name.endswith((".gz", ".png", ".jpg", ".so", ".pyc")):
                    continue
                if os.path.getsize(full) > 2_000_000:
                    continue
                zf.write(full, rel(full))

    print(f"shards: {len(traced_files)}, {total_bytes/1e6:.2f} MB; src.zip {os.path.getsize(zpath)/1e6:.2f} MB")
    nonzero = {k: v for k, v in outcomes.items() if v["rc"] != 0}
    print(f"tests with nonzero exit when run alone: {len(nonzero)} {sorted(nonzero)[:20]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
