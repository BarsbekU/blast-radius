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

    # Module-qualified references: "module||name" -> {file: [lines]}, only
    # from the defining module itself and from files that import the name
    # from it (or import the module and use module.name).
    def module_of(rel: str) -> str:
        mod = rel[:-3].replace("/", ".")
        return mod[: -len(".__init__")] if mod.endswith(".__init__") else mod

    defined = {module_of(rel): module_level_names(tree) for rel, tree in trees.items()}
    qualified: dict = defaultdict(lambda: defaultdict(set))
    for rel, tree in trees.items():
        here = module_of(rel)
        package = here if rel.endswith("__init__.py") else here.rsplit(".", 1)[0]
        aliases_name: dict = defaultdict(set)   # local name -> {(module, name)}
        aliases_module: dict = defaultdict(set)  # local name -> {module}
        for name in defined.get(here, ()):
            aliases_name[name].add((here, name))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    parts = package.split(".")
                    parts = parts[: len(parts) - (node.level - 1)] if node.level > 1 else parts
                    base = ".".join(parts + ([base] if base else []))
                for alias in node.names:
                    local = alias.asname or alias.name
                    if alias.name == "*":
                        for name in defined.get(base, ()):
                            aliases_name[name].add((base, name))
                        continue
                    aliases_name[local].add((base, alias.name))
                    aliases_module[local].add(f"{base}.{alias.name}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.asname:
                        aliases_module[alias.asname].add(alias.name)
                    else:
                        aliases_module[alias.name.split(".")[0]].add(alias.name.split(".")[0])
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            body = node.body if isinstance(node.body, list) else [node.body]
            for stmt in body:
                for inner in ast.walk(stmt):
                    if isinstance(inner, ast.Name) and inner.id in aliases_name:
                        for module, name in aliases_name[inner.id]:
                            if name in defined.get(module, ()):
                                qualified[f"{module}||{name}"][rel].add(inner.lineno)
                    elif isinstance(inner, ast.Attribute) and isinstance(inner.value, ast.Name):
                        for module in aliases_module.get(inner.value.id, ()):
                            if inner.attr in defined.get(module, ()):
                                qualified[f"{module}||{inner.attr}"][rel].add(inner.end_lineno or inner.lineno)

    # Globals re-exported by a star import or through a package __init__ can
    # be read as pkg.NAME in ways the import resolution above does not
    # follow: leave them out, so the selector falls back to every mention
    # of the name.
    reexported: set = set()
    for rel, tree in trees.items():
        here = module_of(rel)
        package = here if rel.endswith("__init__.py") else here.rsplit(".", 1)[0]
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            base = node.module or ""
            if node.level:
                parts = package.split(".")
                parts = parts[: len(parts) - (node.level - 1)] if node.level > 1 else parts
                base = ".".join(parts + ([base] if base else []))
            for alias in node.names:
                if alias.name == "*":
                    reexported.update((base, n) for n in defined.get(base, ()))
                elif rel.endswith("__init__.py"):
                    reexported.add((base, alias.name))
    out_q = {}
    for module, names in defined.items():
        for name in names:
            if (module, name) in reexported:
                continue
            files = qualified.get(f"{module}||{name}", {})
            out_q[f"{module}||{name}"] = {f: sorted(l) for f, l in files.items()}
    # `from X import N` in an ordinary module binds the same object as X.N;
    # reads through that alias are already counted under X||N.
    for rel, tree in trees.items():
        if rel.endswith("__init__.py"):
            continue
        here = module_of(rel)
        package = here.rsplit(".", 1)[0]
        for node in tree.body:
            if not isinstance(node, ast.ImportFrom):
                continue
            base = node.module or ""
            if node.level:
                parts = package.split(".")
                parts = parts[: len(parts) - (node.level - 1)] if node.level > 1 else parts
                base = ".".join(parts + ([base] if base else []))
            for alias in node.names:
                if alias.name == "*":
                    continue
                source = f"{base}||{alias.name}"
                target = f"{here}||{alias.asname or alias.name}"
                if source in out_q and target not in out_q and (here, alias.asname or alias.name) not in reexported:
                    out_q[target] = out_q[source]

    os.makedirs(args.out, exist_ok=True)
    with gzip.open(os.path.join(args.out, "refs.json.gz"), "wt", compresslevel=9) as handle:
        json.dump({n: {f: sorted(l) for f, l in files.items()} for n, files in refs.items()},
                  handle, separators=(",", ":"))
    with gzip.open(os.path.join(args.out, "qrefs.json.gz"), "wt", compresslevel=9) as handle:
        json.dump(out_q, handle, separators=(",", ":"))
    print(f"qualified refs for {len(out_q)} module globals ({len(reexported)} re-exported left out)")
    print(f"qualified refs for {len(qualified)} module globals")
    with gzip.open(os.path.join(args.out, "funcs.json.gz"), "wt", compresslevel=9) as handle:
        json.dump({n: dict(files) for n, files in funcs.items()}, handle, separators=(",", ":"))
    print(f"refs for {len(refs)} module-level names; {len(funcs)} function names")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
