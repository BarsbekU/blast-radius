"""Comparison tracer for building the comparison map (selector/maps/cmp.json.gz).

Loaded from blast_trace.py when BLAST_CMP_DIR is set. sqlglot modules under
BLAST_TRACE_ROOT are compiled from a rewritten syntax tree in which every
single comparison `a OP b` becomes `_blast_cmp_(site, op, a, b)`: the call
evaluates exactly `a OP b` (same operands, same order, same exceptions) and
returns its result, and records how the two operands relate:

  signature: one character per relation in RELS (identity, ==, <, <=, >, >=):
      '1' true, '0' false, 'E' raises, '?' not evaluated (operands that are
      not plain values: only identity, the original operator, and == against
      None / enum members / plain values are evaluated)
  values:    when the other side is a literal, the repr of this side (plain
      values only, at most VALUE_CAP distinct per site, else '*')

With these, the selector can tell whether a changed operator (or a changed
literal) in a comparison gives the same result for every evaluation a test
made; if so, that test runs exactly as before.

It also records which lines holding comparison sites ran (sys.monitoring,
once per line), so "the comparison never ran in this test" can be told apart
from "this test did not reach the line in this run".

Each process writes BLAST_CMP_DIR/cmp-<pid>.json.gz at exit.
"""

from __future__ import annotations

import ast
import atexit
import builtins
import enum
import gzip
import importlib.abc
import importlib.machinery
import importlib.util
import json
import marshal
import operator
import os
import sys

RELS = ("is", "==", "<", "<=", ">", ">=")
VALUE_CAP = 16
OPS = {
    ast.Eq: "==", ast.NotEq: "!=", ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">=",
    ast.Is: "is", ast.IsNot: "is not", ast.In: "in", ast.NotIn: "not in",
}
FUNCS = {
    "==": operator.eq, "!=": operator.ne, "<": operator.lt, "<=": operator.le,
    ">": operator.gt, ">=": operator.ge, "is": operator.is_, "is not": operator.is_not,
    "in": lambda a, b: a in b, "not in": lambda a, b: a not in b,
}
PLAIN = (type(None), bool, int, float, str, bytes)


def _plain(v) -> bool:
    t = type(v)
    if t in PLAIN:
        return True
    if t is tuple and len(v) <= 8:
        return all(type(x) in PLAIN for x in v)
    return isinstance(v, enum.Enum)


def _literal(node) -> bool:
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        return isinstance(node.operand, ast.Constant)
    if isinstance(node, ast.Tuple):
        return all(_literal(e) for e in node.elts)
    return False


class _Rewriter(ast.NodeTransformer):
    def __init__(self, rel: str):
        self.rel = rel
        self.sites: list = []

    def visit_Compare(self, node):
        self.generic_visit(node)
        if len(node.ops) != 1 or type(node.ops[0]) not in OPS:
            return node
        op = OPS[type(node.ops[0])]
        right = node.comparators[0]
        # which side is a literal: 'L' left, 'R' right, '' neither
        lit = "R" if _literal(right) else ("L" if _literal(node.left) else "")
        key = f"{self.rel}:{node.lineno}:{node.col_offset}:{node.end_lineno}:{node.end_col_offset}"
        self.sites.append([key, node.lineno])
        call = ast.Call(
            func=ast.Name(id="_blast_cmp_", ctx=ast.Load()),
            args=[ast.Constant(key), ast.Constant(op), ast.Constant(lit), node.left, right],
            keywords=[],
        )
        return ast.copy_location(call, node)


def install(out_dir: str) -> None:
    root = os.environ.get("BLAST_TRACE_ROOT", "/src/")
    pkg_dir = os.path.join(root, "sqlglot")
    cache_dir = os.environ.get("BLAST_CMP_CACHE", "")
    sigs: dict = {}      # site -> set(signature)
    vals: dict = {}      # site -> set(repr) | '*'
    site_lines: dict = {}  # rel -> set(lines holding sites)
    ran: dict = {}       # rel -> set(lines run)

    def record(site, op, lit, a, b, result, error):
        if error:
            sig = "E" + "?" * (len(RELS) - 1)
            s = sigs.get(site)
            if s is None:
                s = sigs[site] = set()
            s.add(op + "|" + sig)
            return
        pa, pb = _plain(a), _plain(b)
        if not pa and not pb:
            # Objects on both sides (expressions, tokens, classes...): only
            # identity is evaluated. The common case in tree walks: keep it
            # cheap.
            sig = op + ("|1?????" if a is b else "|0?????")
            s = sigs.get(site)
            if s is None:
                s = sigs[site] = set()
            s.add(sig)
            if lit:
                vals[site] = "*"
            return
        out = []
        for rel in RELS:
            if rel == "is":
                out.append("1" if a is b else "0")
                continue
            if rel == "==" and not (pa or pb or a is None or b is None):
                out.append("?")
                continue
            if rel != "==" and not (pa and pb):
                out.append("?")
                continue
            try:
                out.append("1" if FUNCS[rel](a, b) else "0")
            except Exception:  # noqa: BLE001
                out.append("E")
        sig = op + "|" + "".join(out)
        s = sigs.get(site)
        if s is None:
            s = sigs[site] = set()
        s.add(sig)
        if lit:
            other = a if lit == "R" else b
            v = vals.get(site)
            if v != "*":
                if v is None:
                    v = vals[site] = set()
                if _plain(other) and len(v) < VALUE_CAP:
                    v.add(repr(other))
                else:
                    vals[site] = "*"

    def _blast_cmp_(site, op, lit, a, b):
        try:
            result = FUNCS[op](a, b)
        except BaseException:
            record(site, op, lit, a, b, None, True)
            raise
        record(site, op, lit, a, b, result, False)
        return result

    builtins._blast_cmp_ = _blast_cmp_

    class Loader(importlib.abc.SourceLoader):
        def __init__(self, fullname, path):
            self.fullname, self.path = fullname, path

        def get_filename(self, fullname):
            return self.path

        def get_data(self, path):
            with open(path, "rb") as handle:
                return handle.read()

        def get_code(self, fullname):
            rel = os.path.relpath(self.path, root).replace(os.sep, "/")
            stamp = os.stat(self.path).st_mtime_ns
            cached = None
            if cache_dir:
                cpath = os.path.join(cache_dir, rel.replace("/", "__") + f".{stamp}.bin")
                try:
                    with open(cpath, "rb") as handle:
                        cached = marshal.load(handle)
                except (OSError, EOFError, ValueError, TypeError):
                    cached = None
            if cached is not None:
                code, sites = cached
            else:
                source = self.get_data(self.path).decode("utf-8")
                tree = ast.parse(source, self.path)
                rw = _Rewriter(rel)
                tree = ast.fix_missing_locations(rw.visit(tree))
                code = compile(tree, self.path, "exec", dont_inherit=True)
                sites = rw.sites
                if cache_dir:
                    os.makedirs(cache_dir, exist_ok=True)
                    tmp = cpath + f".{os.getpid()}.tmp"
                    with open(tmp, "wb") as handle:
                        marshal.dump((code, sites), handle)
                    os.replace(tmp, cpath)
            lines = site_lines.setdefault(rel, set())
            for _key, line in sites:
                lines.add(line)
            return code

    class Finder(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path, target=None):
            if name != "sqlglot" and not name.startswith("sqlglot."):
                return None
            spec = importlib.machinery.PathFinder.find_spec(name, path)
            if spec is None or not spec.origin or not spec.origin.endswith(".py"):
                return spec
            if not os.path.abspath(spec.origin).startswith(pkg_dir):
                return spec
            loader = Loader(name, spec.origin)
            new = importlib.util.spec_from_file_location(
                name, spec.origin, loader=loader,
                submodule_search_locations=spec.submodule_search_locations,
            )
            return new

    sys.meta_path.insert(0, Finder())

    mon = sys.monitoring
    tool = 5
    try:
        mon.use_tool_id(tool, "blast-cmp")
        prefix = root

        def on_line(code, line):
            name = code.co_filename
            if name.startswith(prefix):
                rel = name[len(prefix):]
                lines = site_lines.get(rel)
                if lines is not None and line in lines:
                    s = ran.get(rel)
                    if s is None:
                        s = ran[rel] = set()
                    s.add(line)
            return mon.DISABLE

        mon.register_callback(tool, mon.events.LINE, on_line)
        mon.set_events(tool, mon.events.LINE)
    except ValueError:
        pass

    def dump():
        try:
            mon.set_events(tool, 0)
        except Exception:  # noqa: BLE001
            pass
        data = {
            "pid": os.getpid(),
            "sigs": {k: sorted(v) for k, v in sigs.items()},
            "vals": {k: (v if v == "*" else sorted(v)) for k, v in vals.items()},
            "ran": {k: sorted(v) for k, v in ran.items()},
        }
        os.makedirs(out_dir, exist_ok=True)
        final = os.path.join(out_dir, f"cmp-{os.getpid()}.json.gz")
        tmp = final + ".tmp"
        with gzip.open(tmp, "wt", compresslevel=3) as handle:
            json.dump(data, handle)
        os.replace(tmp, final)

    atexit.register(dump)
