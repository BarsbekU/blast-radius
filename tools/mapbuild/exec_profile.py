"""Profile the TPC executor tests query by query (used by selector/exec_check.py).

test_execute_tpcds / test_execute_tpch run every query of the fixture
through sqlglot's Python executor and compare with duckdb. Per query, the
result is a deterministic function of:
  pipeline ("P"): parse, pickle round trip, find_tables, optimize(),
      Plan(), the Python code generated for each step, the duckdb SQL;
  data ("D"): everything else inside sqlglot.executor.execute (table and
      schema setup, the steps' row processing, env functions).
This records, per query, the sqlglot lines each phase runs and fingerprints
of every pipeline output, plus the inputs of each code generation call so
it can be replayed. If a change only touches pipeline lines and every
pipeline output of the affected queries is unchanged, the query's result
cannot change.

    python /tools/exec_profile.py --out /out
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import pickle
import sys

sys.path.insert(0, "/src")
os.environ["SKIP_INTEGRATION"] = "0"

SRC = "/src/"


def h(value) -> str:
    return hashlib.blake2b(json.dumps(value, sort_keys=True, default=repr).encode(), digest_size=10).hexdigest()


def step_fingerprint(plan, serde) -> str:
    """Every step reachable from the plan root, with its attributes."""
    from sqlglot import exp

    seen, order, stack = set(), [], [plan.root]
    while stack:
        step = stack.pop()
        if id(step) in seen:
            continue
        seen.add(id(step))
        order.append(step)
        stack.extend(step.dependencies)

    def norm(value):
        if isinstance(value, exp.Expr):
            return serde.dump(value)
        if isinstance(value, (list, tuple)):
            return [norm(v) for v in value]
        if isinstance(value, dict):
            return {str(k): norm(v) for k, v in value.items()}
        if isinstance(value, set):
            return sorted(repr(norm(v)) for v in value)
        if hasattr(value, "dependencies"):  # another step
            return f"<step {getattr(value, 'name', '?')}>"
        return repr(value)

    rows = []
    for step in order:
        attrs = {}
        for key, value in vars(step).items():
            if key in ("dependencies", "dependents"):
                attrs[key] = sorted(getattr(d, "name", "?") for d in value)
            else:
                attrs[key] = norm(value)
        rows.append([type(step).__name__, attrs])
    rows.sort(key=lambda r: json.dumps(r, sort_keys=True, default=repr))
    return h(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    import tests.test_executor as te
    from tests.helpers import TPCDS_SCHEMA, TPCH_SCHEMA
    from sqlglot import find_tables, parse_one, serde, transpile
    import sqlglot.executor as ex
    from sqlglot.executor import python as pyex

    te.TestExecutor.setUpClass()
    cls = te.TestExecutor

    mon = sys.monitoring
    tool = 3
    mon.use_tool_id(tool, "exec-profile")
    state = {"phase": None, "lines": None, "codegen": None, "opt": None, "plan": None}

    def on_line(code, line):
        name = code.co_filename
        phase = state["phase"]
        if phase is None or not name.startswith(SRC + "sqlglot/"):
            return mon.DISABLE
        state["lines"][phase].setdefault(name[len(SRC):], set()).add(line)
        return mon.DISABLE

    mon.register_callback(tool, mon.events.LINE, on_line)
    mon.set_events(tool, mon.events.LINE)

    def set_phase(phase):
        previous = state["phase"]
        if previous != phase:
            state["phase"] = phase
            mon.restart_events()  # lines already seen report again in the new phase
        return previous

    original_execute = ex.execute
    original_optimize = ex.optimize
    original_plan = ex.Plan
    original_generate = pyex.PythonExecutor.generate

    def w_execute(*a, **k):
        previous = set_phase("D")
        try:
            return original_execute(*a, **k)
        finally:
            set_phase(previous)

    def w_optimize(*a, **k):
        previous = set_phase("P")
        try:
            result = original_optimize(*a, **k)
            state["opt"] = h(serde.dump(result))
            return result
        finally:
            set_phase(previous)

    def w_plan(*a, **k):
        previous = set_phase("P")
        try:
            plan = original_plan(*a, **k)
            state["plan"] = step_fingerprint(plan, serde)
            return plan
        finally:
            set_phase(previous)

    def w_generate(self, expression):
        previous = set_phase("P")
        try:
            if not expression:
                return original_generate(self, expression)
            dump = serde.dump(expression)
            generator = self.generator
            inner = generator.generate
            captured = []

            def capture(e, *a, **k):
                text = inner(e, *a, **k)
                captured.append(text)
                return text

            # Subqueries are registered with the CTEs of the plan being run.
            ctes = serde.dump(self._ctes) if self._ctes is not None else None
            generator.generate = capture
            try:
                code = original_generate(self, expression)
            finally:
                del generator.generate
            state["codegen"].append([dump, captured[-1] if captured else None, ctes])
            return code
        finally:
            set_phase(previous)

    te.execute = w_execute
    ex.optimize = w_optimize
    ex.Plan = w_plan
    pyex.PythonExecutor.generate = w_generate

    def profile(sqls, schema, tables, with_optimized):
        te.initializer(schema, tables)
        records = []
        for i, (meta, sql, optimized) in enumerate(sqls):
            state.update(lines={"P": {}, "D": {}}, codegen=[], opt=None, plan=None)
            rec = {"i": i, "meta": meta, "sql": sql, "execute": bool(meta.get("execute"))}
            set_phase("P")
            e1 = parse_one(sql)
            rec["parse"] = h(serde.dump(e1))
            e2 = pickle.loads(pickle.dumps(e1))
            rec["pickle"] = h(serde.dump(e2))
            if rec["execute"]:
                rec["tables"] = sorted(t.name for t in find_tables(e2))
                result = te.mp_execute(e2, meta)
                rec["opt"], rec["plan"] = state["opt"], state["plan"]
                rec["codegen_inputs"] = [[c[0], c[2]] for c in state["codegen"]]
                rec["codegen"] = sorted(h([c[0], c[1]]) for c in state["codegen"])
                rec["result"] = h([list(result.columns), [[repr(v) for v in row] for row in result.rows]])
                rec["duck"] = h(transpile(sql, write="duckdb")[0])
            if with_optimized:
                rec["optimized_sql"] = optimized
                rec["duck_opt"] = h(transpile(optimized, write="duckdb")[0])
                rec["duck"] = rec.get("duck") or h(transpile(sql, write="duckdb")[0])
            set_phase(None)
            rec["P"] = {f: sorted(l) for f, l in state["lines"]["P"].items()}
            rec["D"] = {f: sorted(l) for f, l in state["lines"]["D"].items()}
            records.append(rec)
            print(f"query {i}: execute={rec['execute']} P={sum(map(len, rec['P'].values()))} "
                  f"D={sum(map(len, rec['D'].values()))} codegen={len(state['codegen'])}", flush=True)
        return records

    out = {
        "schemas": {"tpch": TPCH_SCHEMA, "tpcds": TPCDS_SCHEMA},
        "tpch": profile(cls.tpch_sqls, TPCH_SCHEMA, cls.tpch_tables, True),
        "tpcds": profile(cls.tpcds_sqls, TPCDS_SCHEMA, cls.tpcds_tables, False),
    }
    os.makedirs(args.out, exist_ok=True)
    with gzip.open(os.path.join(args.out, "exec_profile.json.gz"), "wt", compresslevel=9) as handle:
        json.dump(out, handle, separators=(",", ":"), default=repr)
    print("written", os.path.getsize(os.path.join(args.out, "exec_profile.json.gz")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
