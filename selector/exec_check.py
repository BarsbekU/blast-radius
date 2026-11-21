"""Can the TPC executor tests notice this change? (decision-time half of
tools/mapbuild/exec_profile.py)

For the affected queries of test_execute_tpch / test_execute_tpcds (and the
duckdb-only test_optimized_tpch), recompute every pipeline output with the
patched package - parse, pickle round trip, tables, optimize() exactly as
sqlglot.executor.execute calls it, the plan, the code generated for each
recorded step expression (replayed in the recorded order, with the same
CTEs in effect), the duckdb SQL - and compare with the pristine
fingerprints. Identical outputs and untouched data-phase code mean the
query's result is unchanged. The selector only asks when no changed line
runs in the data phase.

    python exec_check.py < request.json
request:  {"root", "overrides", "deleted", "profile",
           "queries": {"tpch": [i, ...], "tpcds": [...]}, "optimized_tpch": bool}
response: {"ok": true, "changed": {"tpch": [...], "tpcds": [...]},
           "optimized_tpch_changed": bool} | {"ok": false, "error": ...}
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import pickle
import sys


def h(value) -> str:
    return hashlib.blake2b(json.dumps(value, sort_keys=True, default=repr).encode(), digest_size=10).hexdigest()


def step_fingerprint(plan, serde) -> str:
    # Must stay identical to tools/mapbuild/exec_profile.py:step_fingerprint.
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
        if hasattr(value, "dependencies"):
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
    request = json.load(sys.stdin)
    if request.get("mode") == "rerun_only":
        return rerun_only(request)
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, here)
    from fingerprint import _Finder  # noqa: E402

    sys.path.insert(0, request["root"])
    overrides, deleted = request.get("overrides", {}), set(request.get("deleted", []))
    if overrides or deleted:
        sys.meta_path.insert(0, _Finder(overrides, deleted))
    try:
        from sqlglot import find_tables, parse_one, serde, transpile
        from sqlglot.executor import python as pyex
        from sqlglot.optimizer import optimize
        from sqlglot.planner import Plan
        from sqlglot.schema import ensure_schema
        # Everything tests/test_executor.py imports: a module that no longer
        # imports there fails every test in the file.
        from sqlglot import exp  # noqa: F401
        from sqlglot.errors import ExecuteError  # noqa: F401
        from sqlglot.executor import env, execute  # noqa: F401
        from sqlglot.executor.python import Python, PythonExecutor  # noqa: F401
        from sqlglot.executor.table import Table, ensure_tables  # noqa: F401
    except BaseException as error:  # noqa: BLE001
        json.dump({"ok": False, "error": f"import: {type(error).__name__}: {error}"[:300]}, sys.stdout)
        return 0

    with gzip.open(request["profile"], "rt") as handle:
        profile = json.load(handle)
    store = profile.get("store", {})
    parsed: dict = {}
    loaded: dict = {}

    def resolve(ref):
        if ref is None:
            return None
        if ref not in parsed:
            parsed[ref] = json.loads(store[ref]) if isinstance(ref, str) and ref in store else ref
        return parsed[ref]

    def expression(ref):
        # Code generation copies before it changes anything, so one loaded
        # expression per distinct input can be shared by every replay.
        if ref not in loaded:
            loaded[ref] = serde.load(resolve(ref))
        return loaded[ref]

    subquery_nodes = getattr(pyex, "SUBQUERY_NODES", None)

    def same(rec, schema_dict) -> bool:
        e1 = parse_one(rec["sql"])
        if h(serde.dump(e1)) != rec["parse"]:
            return False
        e2 = pickle.loads(pickle.dumps(e1))
        if h(serde.dump(e2)) != rec["pickle"]:
            return False
        if rec["execute"]:
            if sorted(t.name for t in find_tables(e2)) != rec["tables"]:
                return False
            schema = ensure_schema(schema_dict, dialect=None)
            optimized = optimize(e2, schema, leave_tables_isolated=True, dialect=None)
            if h(serde.dump(optimized)) != rec["opt"]:
                return False
            if step_fingerprint(Plan(optimized), serde) != rec["plan"]:
                return False
            executor = pyex.PythonExecutor(tables={})
            generator = executor.generator
            inner = generator.generate
            produced = []
            ctes_objects: dict = {}
            for dump_ref, ctes_ref in rec["codegen_inputs"]:
                dump = resolve(dump_ref)
                expr = expression(dump_ref)
                captured = []

                def capture(e, *a, **k):
                    text = inner(e, *a, **k)
                    captured.append(text)
                    return text

                # The CTEs in effect only matter when a subquery gets compiled;
                # within one execution they were one shared object.
                ctes = None
                if ctes_ref is not None and (subquery_nodes is None or expr.find(*subquery_nodes)):
                    if ctes_ref not in ctes_objects:
                        ctes_objects[ctes_ref] = serde.load(resolve(ctes_ref))
                    ctes = ctes_objects[ctes_ref]
                executor._ctes = ctes
                generator.generate = capture
                try:
                    executor.generate(expr)
                finally:
                    del generator.generate
                produced.append(h([dump, captured[-1] if captured else None]))
            if sorted(produced) != rec["codegen"]:
                return False
        if "duck" in rec and h(transpile(rec["sql"], write="duckdb")[0]) != rec["duck"]:
            return False
        return True

    rerun = request.get("rerun") or {}
    data_queries = request.get("data_queries") or {}
    changed = {"tpch": [], "tpcds": []}
    rerun_note = {}
    try:
        for test in ("tpch", "tpcds"):
            records = profile[test]
            schema_dict = profile["schemas"][test]
            cfg = rerun.get(test)
            costs = {int(k): v for k, v in cfg.get("costs", {}).items()} if cfg else {}
            for i, rec in enumerate(records):
                if not rec.get("execute"):
                    costs.setdefault(i, 0.02)  # parsed and pickled only
            sample = bool(cfg and cfg.get("sample"))
            limit = (max(cfg["max_queries"], cfg.get("sample_max", 0)) if sample else cfg["max_queries"]) if cfg else 0
            # Queries running changed data-phase lines must be re-run; the
            # others ran changed pipeline lines only, and are re-run only if
            # a pipeline output differs.
            dq = sorted(data_queries.get(test, []))
            qs = list(request.get("queries", {}).get(test, []))
            if sample:
                qs.sort(key=lambda i: (costs.get(i, 99.0), i))
            def probe(i) -> bool:
                """Run one query now: a difference keeps the test, and then
                nothing else needs checking."""
                hit = rerun_queries(dict(cfg, extra_seeds=[]), records, [i], schema_dict, transpile, h)
                if hit:
                    changed[test] = hit
                    rerun_note[test] = {"tried": [i], "still": hit, "mode": "probe"}
                return bool(hit)

            if cfg and dq:
                first = [i for i in cfg.get("must", []) if i in dq] or sorted(
                    dq, key=lambda i: (not records[i].get("execute"), costs.get(i, 99.0), i)
                )
                if probe(first[0]):
                    continue
            pipe_changed, truncated, probed = [], False, False
            for i in qs:
                try:
                    if not same(records[i], schema_dict):
                        pipe_changed.append(i)
                except BaseException:  # noqa: BLE001 - an error is a change
                    pipe_changed.append(i)
                if cfg and pipe_changed and not probed:
                    probed = True
                    if probe(pipe_changed[0]):
                        break
                if len(pipe_changed) > limit:
                    truncated = True  # more changed queries than we will run
                    break
            if changed[test]:
                continue  # a probe found a difference
            need = sorted(set(dq) | set(pipe_changed))
            if not need:
                continue
            if not cfg:
                changed[test] = need
                continue
            est = sum(costs.get(i, 99.0) for i in need)
            if not truncated and len(need) <= cfg["max_queries"] and est <= cfg["max_secs"]:
                # Every affected query whose output may differ is run for
                # real with the patched package and compared with the
                # pristine result; identical results and duckdb SQL under
                # every seed tried mean the comparisons the test makes are
                # unchanged.
                still = rerun_queries(cfg, records, need, schema_dict, transpile, h)
                rerun_note[test] = {"tried": need, "still": still, "mode": "all"}
                changed[test] = still
            elif sample:
                executed = {i for i, rec in enumerate(records) if rec.get("execute")}
                pick = sample_queries(need, costs, cfg, executed)
                still = rerun_queries(dict(cfg, extra_seeds=[]), records, pick, schema_dict, transpile, h)
                rerun_note[test] = {"tried": pick, "still": still, "mode": "sample",
                                    "of": f"{len(need)}{'+' if truncated else ''}"}
                changed[test] = still
            else:
                changed[test] = need
        optimized_changed = False
        if request.get("optimized_tpch"):
            for rec in profile["tpch"]:
                try:
                    if h(transpile(rec["sql"], write="duckdb")[0]) != rec["duck"] or (
                        h(transpile(rec["optimized_sql"], write="duckdb")[0]) != rec["duck_opt"]
                    ):
                        optimized_changed = True
                        break
                except BaseException:  # noqa: BLE001
                    optimized_changed = True
                    break
    except BaseException as error:  # noqa: BLE001
        json.dump({"ok": False, "error": f"{type(error).__name__}: {error}"[:300]}, sys.stdout)
        return 0
    json.dump({"ok": True, "changed": changed, "optimized_tpch_changed": optimized_changed,
               "rerun": rerun_note}, sys.stdout)
    return 0


def load_table(zf, member, columns, Table):
    """tests/test_executor.py setUpClass's loader, reading from data.zip."""
    import ast
    import csv
    import gzip
    import io

    with zf.open(member) as raw, gzip.open(io.BytesIO(raw.read()), "rt", encoding="utf-8", newline="") as handle:
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


def run_one(sql, schema_dict, zf, prefix, cache):
    """mp_execute as the test runs it: parse in the parent, pickle to the
    worker, find_tables there, execute with the schema and loaded tables."""
    from sqlglot import find_tables, parse_one
    from sqlglot.executor import execute
    from sqlglot.executor.table import Table

    expression = pickle.loads(pickle.dumps(parse_one(sql)))
    tables = {}
    for t in find_tables(expression):
        name = t.name
        if name not in cache:
            cache[name] = load_table(zf, f"{prefix}{name}.csv.gz", schema_dict[name], Table)
        tables[name] = cache[name]
    result = execute(expression, schema=schema_dict, tables=tables)
    return h([list(result.columns), [[repr(v) for v in row] for row in result.rows]])


def sample_queries(need, costs, cfg, executed=None) -> list:
    """Every parse-only query (cheap and exact), then among executed ones:
    the cheapest query exercising each changed data-phase line, the
    cheapest others and some spread over the cost range, within budget."""
    parse_only = []
    if executed is not None:
        parse_only = [i for i in need if i not in executed]
        need = [i for i in need if i in executed]
    budget = cfg.get("sample_secs", 4.0)
    lo, hi = cfg.get("sample_min", 8), cfg.get("sample_max", 16)
    must = [i for i in cfg.get("must", []) if i in need]
    rest = sorted((i for i in need if i not in must), key=lambda i: (costs.get(i, 99.0), i))
    pick, spent = list(must), sum(costs.get(i, 99.0) for i in must)
    # Half the budget on the cheapest queries...
    for i in rest:
        c = costs.get(i, 99.0)
        if len(pick) >= hi or (len(pick) >= lo // 2 and spent + c > budget / 2):
            break
        pick.append(i)
        spent += c
    # ...the rest spread over the remaining cost range (bigger queries
    # touch more rows and more edge cases).
    remaining = [i for i in rest if i not in pick]
    slots = hi - len(pick)
    if remaining and slots > 0:
        step = max(1, len(remaining) // slots)
        for i in remaining[step - 1::step] + remaining:
            if len(pick) >= hi:
                break
            c = costs.get(i, 99.0)
            if i not in pick and (spent + c <= budget or len(pick) < lo):
                pick.append(i)
                spent += c
    # Executed queries first: they exercise everything a parse does and more,
    # so a difference shows up (and stops the run) sooner.
    return pick + parse_only


def rerun_queries(cfg, records, indices, schema_dict, transpile, hasher) -> list:
    """Indices whose result (or duckdb SQL) differs, or that error; checked
    in this process (PYTHONHASHSEED=0) and again in a child per extra seed."""
    import zipfile

    still = []
    with zipfile.ZipFile(cfg["data"]) as zf:
        cache: dict = {}
        for i in indices:
            rec = records[i]
            try:
                if not rec.get("execute"):
                    # The test only parses these in the parent and pickles them
                    # to a worker that returns None: only an exception counts.
                    from sqlglot import parse_one

                    pickle.loads(pickle.dumps(parse_one(rec["sql"])))
                elif "result" not in rec:
                    still.append(i)
                elif hasher(transpile(rec["sql"], write="duckdb")[0]) != rec.get("duck"):
                    still.append(i)
                elif run_one(rec["sql"], schema_dict, zf, cfg["prefix"], cache) != rec["result"]:
                    still.append(i)
            except BaseException:  # noqa: BLE001 - an error is a change
                still.append(i)
            if still:
                # One differing query keeps the test; the rest would only
                # cost CPU time.
                return sorted(still)
    ok = [i for i in indices if i not in still]
    for seed in cfg.get("extra_seeds", []):
        if not ok:
            break
        import subprocess

        child = {"mode": "rerun_only", "root": cfg["root"], "overrides": cfg["overrides"],
                 "deleted": cfg["deleted"], "data": cfg["data"], "prefix": cfg["prefix"],
                 "schema": schema_dict, "queries": [[i, records[i]["sql"], records[i]["result"]] for i in ok if records[i].get("execute")]}
        env = dict(os.environ, PYTHONHASHSEED=str(seed))
        try:
            proc = subprocess.run([sys.executable, "-P", "-s", os.path.abspath(__file__)],
                                  input=json.dumps(child), capture_output=True, text=True,
                                  timeout=cfg.get("timeout", 30), env=env)
            bad = json.loads(proc.stdout)["bad"]
        except Exception:  # noqa: BLE001
            bad = ok
        still.extend(i for i in ok if i in bad)
        ok = [i for i in ok if i not in bad]
    return sorted(still)


def rerun_only(request) -> int:
    import zipfile

    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, here)
    from fingerprint import _Finder  # noqa: E402

    sys.path.insert(0, request["root"])
    if request.get("overrides") or request.get("deleted"):
        sys.meta_path.insert(0, _Finder(request.get("overrides", {}), set(request.get("deleted", []))))
    bad = []
    with zipfile.ZipFile(request["data"]) as zf:
        cache: dict = {}
        for i, sql, expected in request["queries"]:
            try:
                if run_one(sql, request["schema"], zf, request["prefix"], cache) != expected:
                    bad.append(i)
            except BaseException:  # noqa: BLE001
                bad.append(i)
            if bad:
                break
    json.dump({"bad": bad}, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
