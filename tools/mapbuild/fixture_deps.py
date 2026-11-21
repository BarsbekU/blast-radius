"""Measure how heavy tests depend on the SQL fixture files they read.

Some fixtures are read by a whole test class in setUpClass (e.g. every
TestExecutor test reads tpc-ds.sql and tpc-h.sql), so "reads the file" says
little about which tests use which part. For each heavy test and each SQL
pair fixture it reads, run it twice on a copy of the checkout:

  1. every statement of the fixture replaced by garbage -> still passes?
     then the test does not depend on the file's content at all ("none");
  2. only the expected-output statements (the 2nd of each pair, as
     tests/helpers.py:load_sql_fixture_pairs reads them) replaced -> passes?
     then it depends only on the input statements and metadata ("inputs").
  Otherwise "all".

    python /tools/fixture_deps.py --out /out --min-seconds 2.5
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import shutil
import subprocess
import sys

GARBAGE = "SELECT )( FROM WHERE garbage__"


def split_pairs(text: str):
    """Same grouping as tests/helpers.py (comments dropped, split on ';')."""
    lines = [line for line in text.splitlines() if line and not line.startswith("--")]
    return "\n".join(lines).split(";")


def corrupt(text: str, which: str) -> str:
    statements = split_pairs(text)
    out = []
    for i, stmt in enumerate(statements):
        is_expected = i % 2 == 1
        if which == "all" or (which == "expected" and is_expected):
            # Keep metadata lines (# key: value) so pairing stays intact.
            meta = [l for l in stmt.strip().split("\n") if l.startswith("#")] if not is_expected else []
            stmt = "\n" + "\n".join(meta + [GARBAGE]) if stmt.strip() else stmt
        out.append(stmt)
    return ";".join(out)


def run_test(tree: str, test_id: str, timeout: int) -> bool:
    env = {k: v for k, v in os.environ.items() if not k.startswith("BLAST_TRACE")}
    env.update(PYTHONPATH=tree, SKIP_INTEGRATION="0", PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", "--no-header",
         "-o", "addopts=", f"--timeout={timeout}", test_id],
        cwd=tree, env=env, capture_output=True, text=True,
    )
    return proc.returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--src", default="/src")
    parser.add_argument("--maps", default="/out")
    parser.add_argument("--out", default="/out")
    parser.add_argument("--tests", default="/data/tests.json")
    parser.add_argument("--min-seconds", type=float, default=2.5)
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()

    tests = json.load(open(args.tests))["tests"]
    by_number = {t["number"]: t for t in tests}
    with gzip.open(os.path.join(args.maps, "files.json.gz"), "rt") as handle:
        files = json.load(handle)

    def decode(enc):
        if isinstance(enc, str):
            bits = int(enc, 16)
            return [n for n in range(bits.bit_length()) if bits >> n & 1]
        return list(enc)

    tree = "/tmp/fxtree"
    shutil.rmtree(tree, ignore_errors=True)
    shutil.copytree(args.src, tree, symlinks=True, ignore=shutil.ignore_patterns(".git"))

    deps: dict = {}
    for rel, enc in sorted(files["data"].items()):
        if not rel.endswith(".sql") or not rel.startswith("tests/fixtures/"):
            continue
        heavy = [n for n in decode(enc) if by_number[n]["seconds"] >= args.min_seconds]
        if not heavy:
            continue
        path = os.path.join(tree, rel)
        original = open(path, encoding="utf-8").read()
        for n in heavy:
            test_id = by_number[n]["id"]
            result = "all"
            for which, verdict in (("all", "none"), ("expected", "inputs")):
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(corrupt(original, which))
                passed = run_test(tree, test_id, args.timeout)
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(original)
                print(f"{rel} | {test_id} | corrupt {which}: {'pass' if passed else 'FAIL'}", flush=True)
                if passed:
                    result = verdict
                    break
            deps.setdefault(rel, {})[str(n)] = result
    with open(os.path.join(args.out, "fixture_deps.json"), "w") as handle:
        json.dump(deps, handle, indent=1, sort_keys=True)
    print(json.dumps(deps, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
