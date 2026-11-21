"""Time each TPC query in sqlglot's Python executor, loading only the tables
it needs the way tests/test_executor.py does. Verifies the result hash
matches exec_profile's (proves the replicated loader is faithful).

    PYTHONHASHSEED=0 python /tools/exec_times.py --profile /out/exec_profile.json.gz --out /out/exec_times.json
"""

from __future__ import annotations

import argparse
import ast
import csv
import gzip
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/src")
os.environ["SKIP_INTEGRATION"] = "0"


def h(value) -> str:
    return hashlib.blake2b(json.dumps(value, sort_keys=True, default=repr).encode(), digest_size=10).hexdigest()


def load_table(path, columns):
    from sqlglot.executor.table import Table

    with gzip.open(path, "rt", newline="") as handle:
        reader = csv.reader(handle, delimiter="|")
        next(reader)
        rows, ctypes = [], []
        for row in reader:
            if not ctypes:
                for v in row:
                    try:
                        ctypes.append(type(ast.literal_eval(v)))
                    except (ValueError, SyntaxError):
                        ctypes.append(str)
            rows.append(tuple(None if (t is not str and v == "") else t(v) for t, v in zip(ctypes, row)))
    return Table(columns=columns, rows=rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    from sqlglot import parse_one
    from sqlglot.executor import execute
    from tests.helpers import TPCDS_SCHEMA, TPCH_SCHEMA

    with gzip.open(args.profile, "rt") as f:
        profile = json.load(f)
    out = {}
    for test, schema, d in (("tpch", TPCH_SCHEMA, "/src/tests/fixtures/optimizer/tpc-h/"),
                            ("tpcds", TPCDS_SCHEMA, "/src/tests/fixtures/optimizer/tpc-ds/")):
        cache, load_secs, rows = {}, {}, []
        for rec in profile[test]:
            if not rec.get("execute"):
                rows.append(None)
                continue
            t0 = time.process_time()
            tables = {}
            for name in rec["tables"]:
                if name not in cache:
                    s = time.process_time()
                    cache[name] = load_table(d + name + ".csv.gz", schema[name])
                    load_secs[name] = round(time.process_time() - s, 3)
                tables[name] = cache[name]
            t1 = time.process_time()
            result = execute(parse_one(rec["sql"]), schema=schema, tables=tables)
            t2 = time.process_time()
            got = h([list(result.columns), [[repr(v) for v in row] for row in result.rows]])
            ok = got == rec["result"]
            rows.append({"exec": round(t2 - t1, 3), "match": ok})
            print(test, rec["i"], round(t2 - t1, 2), "match" if ok else "MISMATCH", flush=True)
        out[test] = {"queries": rows, "load": load_secs}
    with open(args.out, "w") as f:
        json.dump(out, f)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
