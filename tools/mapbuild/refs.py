"""Static indexes the selector uses for definition-time changes.

    python /tools/refs.py --src /src --out /out

refs.json.gz
    name -> {file: [lines]}: lines inside function bodies / lambdas that
    mention the name (as a variable or an attribute), for every name that is
    assigned at module level somewhere in sqlglot or the tests. When the
    fingerprint diff says a module global changed, the tests that ran one of
    those lines are the ones that can see it.
funcs.json.gz
    function name -> {file: [[start, end], ...]}: where functions/methods of
    that name are defined, for added or removed methods that may override or
    un-override one with the same name.
"""

from __future__ import annotations

import argparse
import ast
import gzip
import json
import os
from collections import defaultdict


def module_level_names(tree: ast.Module) -> set:
    names = set()

    def targets(node):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, (ast.Tuple, ast.List)):
            for elt in node.elts:
                targets(elt)
        elif isinstance(node, ast.Starred):
            targets(node.value)

    def walk(stmts):
        for stmt in stmts:
            if isinstance(stmt, ast.Assign):
                for target in stmt.targets:
                    targets(target)
            elif isinstance(stmt, (ast.AnnAssign, ast.AugAssign)):
                targets(stmt.target)
            elif isinstance(stmt, (ast.If, ast.Try, ast.With, ast.For, ast.While)):
                for field in ("body", "orelse", "finalbody"):
                    walk(getattr(stmt, field, []) or [])
                for handler in getattr(stmt, "handlers", []) or []:
                    walk(handler.body)

    walk(tree.body)
    return names


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--src", default="/src")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    trees = {}
    for root, dirs, files in os.walk(args.src):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__")]
        for name in files:
            if not name.endswith(".py"):
                continue
            full = os.path.join(root, name)
            rel = os.path.relpath(full, args.src).replace(os.sep, "/")
            if not (rel.startswith("sqlglot/") or rel.startswith("tests/")):
                continue
            try:
                with open(full, "rb") as handle:
                    trees[rel] = ast.parse(handle.read(), filename=rel)
            except SyntaxError:
                continue

    globals_ = set()
    for tree in trees.values():
        globals_ |= module_level_names(tree)

    refs: dict = defaultdict(lambda: defaultdict(set))
    funcs: dict = defaultdict(lambda: defaultdict(list))
    for rel, tree in trees.items():
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                start = min([node.lineno] + [d.lineno for d in node.decorator_list])
                funcs[node.name][rel].append([start, node.end_lineno])
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                body = node.body if isinstance(node.body, list) else [node.body]
                for stmt in body:
                    for inner in ast.walk(stmt):
                        if isinstance(inner, ast.Name) and inner.id in globals_:
                            refs[inner.id][rel].add(inner.lineno)
                        elif isinstance(inner, ast.Attribute) and inner.attr in globals_:
                            refs[inner.attr][rel].add(inner.end_lineno or inner.lineno)

    os.makedirs(args.out, exist_ok=True)
    with gzip.open(os.path.join(args.out, "refs.json.gz"), "wt", compresslevel=9) as handle:
        json.dump({n: {f: sorted(l) for f, l in files.items()} for n, files in refs.items()},
                  handle, separators=(",", ":"))
    with gzip.open(os.path.join(args.out, "funcs.json.gz"), "wt", compresslevel=9) as handle:
        json.dump({n: dict(files) for n, files in funcs.items()}, handle, separators=(",", ":"))
    print(f"refs for {len(refs)} module-level names; {len(funcs)} function names")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
