#!/usr/bin/env python3
"""Generate synthetic commits against sqlglot at the pinned commit.

The sample commits are single-token edits (operator flips, constants, index
shifts), inserted comment lines, typos in SQL fixtures, and mixes of those.
This generator makes the same kinds plus a few the sample does not show, so
the selector is tested on change kinds it was not tuned for.

    python tools/corpus/make_mutants.py --src work/sqlglot --out work/corpus --count 300 --seed 1

Writes <out>/commits/<id>.patch and <out>/kinds.json ({id: kind}). Labels
(which tests fail) come from tools/corpus/label.py.
"""

from __future__ import annotations

import argparse
import difflib
import io
import json
import os
import random
import re
import string
import tokenize
from pathlib import Path

COMMENTS = [
    "# Cheap check first",
    "# Kept for backwards compatibility",
    "# Intentionally not cached",
    "# Mirrors the behavior of the reference implementation",
    "# Keep in sync with the generator",
    "# TODO: revisit once the parser supports this",
    "# Order matters here",
    "# See the dialect docs for details",
    "# noqa: E501",
    "# type: ignore",
]

SWAPS = {
    "==": ["!="], "!=": ["=="], "<": ["<=", ">"], "<=": ["<", ">="], ">": [">=", "<"],
    ">=": [">", "<="], "+": ["-"], "-": ["+"], "*": ["//"], "//": ["*"],
    "and": ["or"], "or": ["and"], "True": ["False"], "False": ["True"],
    "is": ["is not", "=="], "in": ["not in"], "break": ["continue"], "continue": ["break"],
    "+=": ["-="], "-=": ["+="], "%": ["//"], "None": ["0"],
}

ALPHABET = string.ascii_letters + string.digits + " ,()=<>'.*"


def unified(rel: str, old: str, new: str) -> str:
    old_lines = old.splitlines(keepends=True)
    new_lines = new.splitlines(keepends=True)
    body = "".join(
        difflib.unified_diff(old_lines, new_lines, fromfile=f"a/{rel}", tofile=f"b/{rel}", n=3)
    )
    if not body:
        return ""
    return f"diff --git a/{rel} b/{rel}\nindex 0000000..1111111 100644\n" + body


def docstring_lines(source: str) -> set:
    import ast

    lines: set = set()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return lines
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) and isinstance(body[0].value.value, str):
                lines.update(range(body[0].lineno, body[0].end_lineno + 1))
    return lines


def table_lines(source: str) -> set:
    """Lines of class-level assignments whose value is a table (dict, set,
    list, tuple): dialect FUNCTIONS, KEYWORDS, TRANSFORMS, settings..."""
    import ast

    lines: set = set()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return lines
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for stmt in node.body:
            if isinstance(stmt, (ast.Assign, ast.AnnAssign)) and isinstance(
                getattr(stmt, "value", None), (ast.Dict, ast.Set, ast.List, ast.Tuple)
            ):
                lines.update(range(stmt.lineno, stmt.end_lineno + 1))
    return lines


def token_mutation(source: str, rng: random.Random, only_lines: set | None = None):
    """One token-level edit; returns (new_source, description) or None."""
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return None
    doc = docstring_lines(source)
    if only_lines is not None:
        doc = doc | {t.start[0] for t in tokens if t.start[0] not in only_lines}
    lines = source.splitlines(keepends=True)
    candidates = []
    for i, tok in enumerate(tokens):
        if tok.start[0] in doc:
            continue
        if tok.type == tokenize.OP and tok.string in SWAPS:
            candidates.append((i, "swap"))
        elif tok.type == tokenize.NAME and tok.string in SWAPS:
            candidates.append((i, "swap"))
        elif tok.type == tokenize.NUMBER:
            candidates.append((i, "number"))
        elif tok.type == tokenize.STRING and len(tok.string) < 40 and not tok.string.startswith(("f", "rf", "fr", "b", "rb", "F")) and tok.string.count("\n") == 0:
            candidates.append((i, "string"))
        elif tok.type == tokenize.NAME and tok.string in ("if", "while", "elif") and i + 1 < len(tokens):
            candidates.append((i, "not"))
    if not candidates:
        return None
    i, how = rng.choice(candidates)
    tok = tokens[i]
    (row, col), (erow, ecol) = tok.start, tok.end
    if row != erow:
        return None
    line = lines[row - 1]
    if how == "swap":
        replacement = rng.choice(SWAPS[tok.string])
        if tok.string == "is" and i + 1 < len(tokens) and tokens[i + 1].string == "not":
            return None
        if tok.string == "in" and i > 0 and tokens[i - 1].string == "not":
            return None
        new_line = line[:col] + replacement + line[ecol:]
    elif how == "number":
        text = tok.string
        try:
            value = int(text, 0)
            new_text = str(value + rng.choice([1, -1])) if value != 0 else "1"
        except ValueError:
            try:
                value = float(text)
                new_text = repr(value * 2 if value else 1.0)
            except ValueError:
                return None
        new_line = line[:col] + new_text + line[ecol:]
    elif how == "string":
        text = tok.string
        quote_start = text.index(text[-1])
        inner = text[quote_start + 1:-1]
        if not inner:
            inner_new = "x"
        else:
            k = rng.randrange(len(inner) + 1)
            choice = rng.choice(["insert", "delete", "case"])
            if choice == "delete" and len(inner) > 1:
                k = min(k, len(inner) - 1)
                inner_new = inner[:k] + inner[k + 1:]
            elif choice == "case" and inner.lower() != inner.upper():
                inner_new = inner.swapcase()
            else:
                inner_new = inner[:k] + rng.choice(string.ascii_uppercase) + inner[k:]
        if "\\" in inner_new[-1:]:
            return None
        new_line = line[:col] + text[: quote_start + 1] + inner_new + text[-1] + line[ecol:]
    else:  # insert not after if/while/elif
        nxt = tokens[i + 1]
        if nxt.start[0] != row:
            return None
        if nxt.string == "not":
            end = tokens[i + 2].start[1] if i + 2 < len(tokens) and tokens[i + 2].start[0] == row else None
            if end is None:
                return None
            new_line = line[: nxt.start[1]] + line[end:]
        else:
            new_line = line[: nxt.start[1]] + "not " + line[nxt.start[1]:]
    if new_line == line:
        return None
    lines[row - 1] = new_line
    return "".join(lines), f"{how} {tok.string!r} line {row}"


def comment_insertion(source: str, rng: random.Random):
    lines = source.splitlines(keepends=True)
    if len(lines) < 3:
        return None
    for _ in range(20):
        k = rng.randrange(1, len(lines))
        nxt = lines[k]
        if not nxt.strip():
            continue
        indent = nxt[: len(nxt) - len(nxt.lstrip())]
        comment = indent + rng.choice(COMMENTS) + "\n"
        new = lines[:k] + [comment] + lines[k:]
        return "".join(new), f"comment before line {k + 1}"
    return None


def fixture_typo(text: str, rng: random.Random):
    lines = text.splitlines(keepends=True)
    candidates = [i for i, l in enumerate(lines) if l.strip()]
    if not candidates:
        return None
    k = rng.choice(candidates)
    line = lines[k].rstrip("\n")
    choice = rng.choice(["insert", "delete", "swap", "token", "replace"])
    pos = rng.randrange(len(line) + 1)
    if choice == "delete" and line:
        pos = min(pos, len(line) - 1)
        new = line[:pos] + line[pos + 1:]
    elif choice == "swap" and len(line) > 2:
        pos = min(pos, len(line) - 2)
        new = line[:pos] + line[pos + 1] + line[pos] + line[pos + 2:]
    elif choice == "token":
        spaces = [m.start() for m in re.finditer(" ", line)] or [len(line)]
        p = rng.choice(spaces)
        new = line[:p] + " " + rng.choice(["x", "1", "a", "NULL", "AS", "("]) + line[p:]
    elif choice == "replace" and line:
        pos = min(pos, len(line) - 1)
        new = line[:pos] + rng.choice(ALPHABET) + line[pos + 1:]
    else:
        new = line[:pos] + rng.choice(ALPHABET) + line[pos:]
    if new == line:
        return None
    lines[k] = new + "\n"
    return "".join(lines), f"typo line {k + 1}"


def docstring_edit(source: str, rng: random.Random):
    import ast

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    docs = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.Module)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) and isinstance(body[0].value.value, str):
                docs.append(body[0])
    if not docs:
        return None
    d = rng.choice(docs)
    lines = source.splitlines(keepends=True)
    rows = [r for r in range(d.lineno - 1, d.end_lineno) if re.search(r"[A-Za-z]{3,}", lines[r])]
    if not rows:
        return None
    row = rng.choice(rows)
    line = lines[row]
    w = rng.choice(re.findall(r"[A-Za-z]{3,}", line))
    lines[row] = line.replace(w, w + "s", 1)
    return "".join(lines), f"docstring line {row + 1}"


def line_edit(source: str, rng: random.Random, how: str):
    lines = source.splitlines(keepends=True)
    code = [i for i, l in enumerate(lines) if l.strip() and not l.strip().startswith("#")]
    if len(code) < 3:
        return None
    k = rng.choice(code[1:-1])
    if how == "delete":
        new = lines[:k] + lines[k + 1:]
    elif how == "duplicate":
        new = lines[: k + 1] + [lines[k]] + lines[k + 1:]
    else:  # swap with next line
        new = lines[:k] + [lines[k + 1], lines[k]] + lines[k + 2:]
    return "".join(new), f"{how} line {k + 1}"


def raw_delete(rel: str, old: str) -> str:
    lines = old.splitlines(keepends=True)
    body = "".join(f"-{l}" if l.endswith("\n") else f"-{l}\n\\ No newline at end of file\n" for l in lines)
    return (
        f"diff --git a/{rel} b/{rel}\ndeleted file mode 100644\nindex 1111111..0000000\n"
        f"--- a/{rel}\n+++ /dev/null\n@@ -1,{len(lines)} +0,0 @@\n{body}"
    )


def raw_new(rel: str, new: str) -> str:
    lines = new.splitlines(keepends=True)
    body = "".join(f"+{l}" for l in lines)
    return (
        f"diff --git a/{rel} b/{rel}\nnew file mode 100644\nindex 0000000..1111111\n"
        f"--- /dev/null\n+++ b/{rel}\n@@ -0,0 +1,{len(lines)} @@\n{body}"
    )


def raw_rename(old_rel: str, new_rel: str) -> str:
    return f"diff --git a/{old_rel} b/{new_rel}\nsimilarity index 100%\nrename from {old_rel}\nrename to {new_rel}\n"


def _funcs(tree):
    import ast
    return [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def stmt_delete(source: str, rng: random.Random):
    """Remove one whole simple statement from a function body (body keeps >= 1)."""
    import ast
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    cands = []
    for node in ast.walk(tree):
        for field in ("body", "orelse", "finalbody"):
            block = getattr(node, field, None)
            if not isinstance(block, list) or len(block) < 2:
                continue
            if not isinstance(node, (ast.FunctionDef, ast.If, ast.For, ast.While, ast.With, ast.Try)):
                continue
            for s in block:
                if isinstance(s, (ast.Assign, ast.AugAssign, ast.Expr, ast.Return, ast.If)) and not (
                    isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant)
                ) and s.end_lineno - s.lineno < 8:
                    cands.append(s)
    if not cands:
        return None
    s = rng.choice(cands)
    lines = source.splitlines(keepends=True)
    new = lines[: s.lineno - 1] + lines[s.end_lineno:]
    return "".join(new), f"delete statement lines {s.lineno}-{s.end_lineno}"


def name_error(source: str, rng: random.Random):
    """Misspell one loaded name inside a function body (NameError/AttributeError at run time)."""
    import ast
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    cands = []
    for f in _funcs(tree):
        for n in ast.walk(f):
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id not in ("self", "cls") and len(n.id) > 2:
                cands.append(n)
            elif isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Load) and len(n.attr) > 2:
                cands.append(n)
    if not cands:
        return None
    n = rng.choice(cands)
    lines = source.splitlines(keepends=True)
    row = n.end_lineno - 1
    line = lines[row]
    end = n.end_col_offset
    # n's text ends at end_col_offset; append a suffix there
    if end > len(line) or n.lineno != n.end_lineno:
        return None
    lines[row] = line[:end] + "_" + line[end:]
    return "".join(lines), f"misspell name at line {row + 1}"


def change_default(source: str, rng: random.Random):
    import ast
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    cands = []
    for f in _funcs(tree):
        for d in list(f.args.defaults) + [d for d in f.args.kw_defaults if d is not None]:
            if isinstance(d, ast.Constant) and d.lineno == d.end_lineno:
                cands.append(d)
    if not cands:
        return None
    d = rng.choice(cands)
    v = d.value
    if v is True:
        new = "False"
    elif v is False:
        new = "True"
    elif v is None:
        new = rng.choice(["0", "''", "False"])
    elif isinstance(v, int):
        new = str(v + 1)
    elif isinstance(v, str):
        new = repr(v + "x")
    else:
        return None
    lines = source.splitlines(keepends=True)
    row = d.lineno - 1
    line = lines[row]
    lines[row] = line[: d.col_offset] + new + line[d.end_col_offset:]
    return "".join(lines), f"default -> {new} line {row + 1}"


NEW_FUNC = '''

def _unused_helper_{n}(value, default=None):
    """Return value unless it is None."""
    return default if value is None else value
'''

NEW_METHOD = '''
    def _unused_method_{n}(self, *args):
        return len(args)
'''


def add_function(source: str, rng: random.Random):
    import ast
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    n = rng.randrange(1000)
    classes = [c for c in tree.body if isinstance(c, ast.ClassDef) and c.col_offset == 0]
    lines = source.splitlines(keepends=True)
    if classes and rng.random() < 0.6:
        c = rng.choice(classes)
        indent = " " * c.body[-1].col_offset
        if indent != "    ":
            return None
        new = lines[: c.end_lineno] + [NEW_METHOD.format(n=n)] + lines[c.end_lineno:]
        return "".join(new), f"add method to {c.name}"
    if not source.endswith("\n"):
        return None
    return source + NEW_FUNC.format(n=n), "add function at end"


def add_import(source: str, rng: random.Random, bad: bool):
    import ast
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    imports = [s for s in tree.body if isinstance(s, (ast.Import, ast.ImportFrom))]
    if not imports:
        return None
    s = rng.choice(imports)
    if bad:
        stmt = rng.choice([
            "from sqlglot.helpers import ensure_listt\n",
            "import sqlglot.missing_module\n",
            "from sqlglot.errors import ParseErorr\n",
        ])
    else:
        stmt = rng.choice(["import itertools\n", "import functools\n", "from collections import OrderedDict\n"])
    lines = source.splitlines(keepends=True)
    new = lines[: s.end_lineno] + [stmt] + lines[s.end_lineno:]
    return "".join(new), f"add import {stmt.strip()}"


def swap_methods(source: str, rng: random.Random):
    import ast
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    pairs = []
    for c in ast.walk(tree):
        if isinstance(c, ast.ClassDef):
            b = c.body
            for a, z in zip(b, b[1:]):
                if isinstance(a, ast.FunctionDef) and isinstance(z, ast.FunctionDef) and not a.decorator_list and not z.decorator_list:
                    pairs.append((a, z))
    if not pairs:
        return None
    a, z = rng.choice(pairs)
    lines = source.splitlines(keepends=True)
    if a.end_lineno + 1 != z.lineno and not all(not l.strip() for l in lines[a.end_lineno: z.lineno - 1]):
        return None
    gap = lines[a.end_lineno: z.lineno - 1]
    new = lines[: a.lineno - 1] + lines[z.lineno - 1: z.end_lineno] + gap + lines[a.lineno - 1: a.end_lineno] + lines[z.end_lineno:]
    return "".join(new), f"swap methods {a.name}/{z.name}"


def whitespace_edit(source: str, rng: random.Random):
    lines = source.splitlines(keepends=True)
    blanks = [i for i, l in enumerate(lines) if not l.strip()]
    if blanks and rng.random() < 0.5:
        k = rng.choice(blanks)
        return "".join(lines[:k] + lines[k + 1:]), f"remove blank line {k + 1}"
    k = rng.randrange(1, len(lines))
    return "".join(lines[:k] + ["\n"] + lines[k:]), f"insert blank line {k + 1}"



# --------------------------------------------------------------------------- final-style kinds
# Kinds the practice batch may not show: control-flow edits, error paths,
# decorators, class structure, and behaviour-preserving refactors (where a
# selector over-selects rather than misses).


def _ast(source):
    import ast
    try:
        return ast.parse(source)
    except SyntaxError:
        return None


def _indent_of(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def early_return(source: str, rng: random.Random):
    """Insert `return None` before a function's first statement."""
    import ast
    tree = _ast(source)
    if tree is None:
        return None
    cands = []
    for f in _funcs(tree):
        if any(isinstance(n, (ast.Yield, ast.YieldFrom)) for n in ast.walk(f)):
            continue
        body = f.body
        k = 1 if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) else 0
        if k < len(body):
            cands.append((f, body[k]))
    if not cands:
        return None
    f, first = rng.choice(cands)
    lines = source.splitlines(keepends=True)
    line = lines[first.lineno - 1]
    indent = _indent_of(line)
    if len(indent) != first.col_offset:
        return None
    new = lines[: first.lineno - 1] + [indent + "return None\n"] + lines[first.lineno - 1:]
    return "".join(new), f"early return in {f.name} at line {first.lineno}"


def raise_to_pass(source: str, rng: random.Random):
    """Replace a one-line `raise ...` with `pass` (an error path stops raising)."""
    import ast
    tree = _ast(source)
    if tree is None:
        return None
    cands = [n for n in ast.walk(tree) if isinstance(n, ast.Raise) and n.lineno == n.end_lineno]
    if not cands:
        return None
    n = rng.choice(cands)
    lines = source.splitlines(keepends=True)
    line = lines[n.lineno - 1]
    indent = _indent_of(line)
    if len(indent) != n.col_offset or not line.strip().startswith("raise"):
        return None
    lines[n.lineno - 1] = indent + "pass\n"
    return "".join(lines), f"raise -> pass at line {n.lineno}"


def message_edit(source: str, rng: random.Random):
    """Change the text of an error, warning or log message."""
    import ast
    tree = _ast(source)
    if tree is None:
        return None
    cands = []
    for n in ast.walk(tree):
        call = None
        if isinstance(n, ast.Raise) and isinstance(n.exc, ast.Call):
            call = n.exc
        elif isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in (
            "warning", "warn", "error", "info", "debug", "exception", "raise_error", "unsupported"
        ):
            call = n
        if call is None:
            continue
        for a in call.args[:1]:
            if isinstance(a, (ast.Constant, ast.JoinedStr)) and a.lineno == a.end_lineno and (
                not isinstance(a, ast.Constant) or (isinstance(a.value, str) and len(a.value) > 3)
            ):
                cands.append(a)
    if not cands:
        return None
    a = rng.choice(cands)
    lines = source.splitlines(keepends=True)
    raw = lines[a.lineno - 1].encode("utf-8")
    seg = raw[a.col_offset:a.end_col_offset].decode("utf-8")
    qs = [i for i in (seg.find('"'), seg.find("'")) if i >= 0]
    if not qs:
        return None
    q = min(qs)
    new_seg = seg[: q + 1] + rng.choice(["Error: ", "Unable to proceed: ", "Note - "]) + seg[q + 1:]
    lines[a.lineno - 1] = (raw[: a.col_offset] + new_seg.encode("utf-8") + raw[a.end_col_offset:]).decode("utf-8")
    return "".join(lines), f"message text at line {a.lineno}"


def remove_decorator(source: str, rng: random.Random):
    import ast
    tree = _ast(source)
    if tree is None:
        return None
    cands = []
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            cands.extend(d for d in n.decorator_list if d.lineno == d.end_lineno)
    if not cands:
        return None
    d = rng.choice(cands)
    lines = source.splitlines(keepends=True)
    if not lines[d.lineno - 1].strip().startswith("@"):
        return None
    what = lines[d.lineno - 1].strip()[:40]
    new = lines[: d.lineno - 1] + lines[d.lineno:]
    return "".join(new), f"remove decorator at line {d.lineno}: {what}"


def local_rename(source: str, rng: random.Random):
    """Behaviour-preserving: rename one local variable throughout its function."""
    import ast
    tree = _ast(source)
    if tree is None:
        return None
    funcs = _funcs(tree)
    rng.shuffle(funcs)
    for f in funcs:
        if any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in ("locals", "vars", "eval", "exec")
               for n in ast.walk(f)):
            continue
        params = set()
        for g in [f] + [n for n in ast.walk(f) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))]:
            a = g.args
            params |= {x.arg for x in a.args + a.kwonlyargs + a.posonlyargs}
            params |= {x.arg for x in (a.vararg, a.kwarg) if x is not None}
        declared = {name for n in ast.walk(f) if isinstance(n, (ast.Global, ast.Nonlocal)) for name in n.names}
        stores = {n.id for n in ast.walk(f) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
        cand = sorted(v for v in stores - params - declared if len(v) > 1)
        if not cand:
            continue
        v = rng.choice(cand)
        new_name = v + "_val"
        if new_name in source:
            continue
        nodes = [n for n in ast.walk(f) if isinstance(n, ast.Name) and n.id == v]
        if any(n.lineno != n.end_lineno for n in nodes):
            continue
        lines = source.splitlines(keepends=True)
        by_line = {}
        for n in nodes:
            by_line.setdefault(n.lineno, []).append(n)
        ok = True
        for row, ns in by_line.items():
            raw = lines[row - 1].encode("utf-8")
            for n in sorted(ns, key=lambda n: -n.col_offset):
                if raw[n.col_offset:n.end_col_offset] != v.encode():
                    ok = False
                    break
                raw = raw[: n.col_offset] + new_name.encode() + raw[n.end_col_offset:]
            lines[row - 1] = raw.decode("utf-8")
        if ok and len(by_line) >= 2:
            return "".join(lines), f"rename local {v} -> {new_name} in {f.name}"
    return None


def wrap_parens(source: str, rng: random.Random):
    """Behaviour-preserving (AST-identical): parenthesize a returned value."""
    import ast
    tree = _ast(source)
    if tree is None:
        return None
    lines = source.splitlines(keepends=True)
    cands = [n for n in ast.walk(tree) if isinstance(n, ast.Return) and n.value is not None
             and n.lineno == n.end_lineno and "#" not in lines[n.lineno - 1]]
    if not cands:
        return None
    n = rng.choice(cands)
    raw = lines[n.lineno - 1].encode("utf-8")
    v = n.value
    raw = raw[: v.col_offset] + b"(" + raw[v.col_offset:v.end_col_offset] + b")" + raw[v.end_col_offset:]
    lines[n.lineno - 1] = raw.decode("utf-8")
    return "".join(lines), f"parenthesize return at line {n.lineno}"


def flag_flip(source: str, rng: random.Random):
    """Flip a boolean class or module setting (`NAME = True`)."""
    import ast
    tree = _ast(source)
    if tree is None:
        return None
    cands = []
    for holder in [tree] + [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
        for st in holder.body:
            if isinstance(st, (ast.Assign, ast.AnnAssign)) and isinstance(st.value, ast.Constant) \
                    and isinstance(st.value.value, bool) and st.lineno == st.end_lineno:
                cands.append(st)
    if not cands:
        return None
    st = rng.choice(cands)
    lines = source.splitlines(keepends=True)
    raw = lines[st.lineno - 1].encode("utf-8")
    v = st.value
    flipped = b"False" if v.value else b"True"
    lines[st.lineno - 1] = (raw[: v.col_offset] + flipped + raw[v.end_col_offset:]).decode("utf-8")
    return "".join(lines), f"flip setting at line {st.lineno}"


def remove_base(source: str, rng: random.Random):
    """Drop the last base class of a class with two or more bases."""
    import ast
    tree = _ast(source)
    if tree is None:
        return None
    lines = source.splitlines(keepends=True)
    cands = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and len(n.bases) >= 2
             and n.bases[-1].lineno == n.bases[-2].lineno == n.lineno]
    if not cands:
        return None
    n = rng.choice(cands)
    raw = lines[n.lineno - 1].encode("utf-8")
    a, b = n.bases[-2], n.bases[-1]
    raw = raw[: a.end_col_offset] + raw[b.end_col_offset:]
    lines[n.lineno - 1] = raw.decode("utf-8")
    return "".join(lines), f"remove base of {n.name}"


def move_function(source: str, rng: random.Random):
    """Move a top-level function below the next top-level definition."""
    import ast
    tree = _ast(source)
    if tree is None:
        return None
    body = tree.body
    cands = []
    for i, st in enumerate(body[:-1]):
        nxt = body[i + 1]
        if isinstance(st, ast.FunctionDef) and isinstance(nxt, (ast.FunctionDef, ast.ClassDef)):
            cands.append((st, nxt))
    if not cands:
        return None
    f, g = rng.choice(cands)
    lines = source.splitlines(keepends=True)
    start = min([f.lineno] + [d.lineno for d in f.decorator_list]) - 1
    gstart = min([g.lineno] + [d.lineno for d in g.decorator_list]) - 1
    block_f = lines[start:gstart]
    block_g = lines[gstart:g.end_lineno]
    new = lines[:start] + block_g + ["\n", "\n"] + block_f + lines[g.end_lineno:]
    return "".join(new), f"move {f.name} below {g.name}"


def annotation_edit(source: str, rng: random.Random):
    """Change a return annotation (no effect under postponed annotations)."""
    import ast
    tree = _ast(source)
    if tree is None or "import typing as t" not in source:
        return None
    lines = source.splitlines(keepends=True)
    cands = [f for f in _funcs(tree) if f.returns is not None and f.returns.lineno == f.returns.end_lineno]
    if not cands:
        return None
    f = rng.choice(cands)
    r = f.returns
    raw = lines[r.lineno - 1].encode("utf-8")
    if raw[r.col_offset:r.end_col_offset] == b"t.Any":
        return None
    lines[r.lineno - 1] = (raw[: r.col_offset] + b"t.Any" + raw[r.end_col_offset:]).decode("utf-8")
    return "".join(lines), f"annotation of {f.name}"


def swap_args(source: str, rng: random.Random):
    """Swap the two positional arguments of a one-line call."""
    import ast
    tree = _ast(source)
    if tree is None:
        return None
    lines = source.splitlines(keepends=True)
    cands = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and len(n.args) == 2 and not n.keywords
             and n.args[0].lineno == n.args[1].end_lineno == n.lineno
             and not any(isinstance(a, ast.Starred) for a in n.args)]
    if not cands:
        return None
    n = rng.choice(cands)
    raw = lines[n.lineno - 1].encode("utf-8")
    a, b = n.args
    ta, tb = raw[a.col_offset:a.end_col_offset], raw[b.col_offset:b.end_col_offset]
    if ta == tb:
        return None
    raw = raw[: a.col_offset] + tb + raw[a.end_col_offset:b.col_offset] + ta + raw[b.end_col_offset:]
    lines[n.lineno - 1] = raw.decode("utf-8")
    return "".join(lines), f"swap call arguments at line {n.lineno}"


def add_test(source: str, rng: random.Random):
    """Add a new (passing) test method to a test class."""
    import ast
    tree = _ast(source)
    if tree is None:
        return None
    classes = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name.startswith("Test")]
    if not classes:
        return None
    c = rng.choice(classes)
    lines = source.splitlines(keepends=True)
    end = c.end_lineno
    method = ["\n", "    def test_added_by_patch(self):\n", "        self.assertEqual(1 + 1, 2)\n"]
    new = lines[:end] + method + lines[end:]
    return "".join(new), f"add test to {c.name}"

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--src", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--count", type=int, default=300)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--prefix", default="m")
    parser.add_argument("--kinds", default="", help="comma separated kinds to draw from (default: the usual mix)")
    parser.add_argument("--plan", default="", help="'final': the final-style mix (sample kinds plus many others)")
    args = parser.parse_args()
    rng = random.Random(args.seed)

    src = args.src
    py_files = sorted(str(p.relative_to(src)).replace(os.sep, "/") for p in (src / "sqlglot").rglob("*.py"))
    test_files = sorted(str(p.relative_to(src)).replace(os.sep, "/") for p in (src / "tests").rglob("test_*.py"))
    fixtures = sorted(
        str(p.relative_to(src)).replace(os.sep, "/")
        for p in (src / "tests" / "fixtures").rglob("*")
        if p.is_file() and p.suffix in (".sql", ".json", ".csv") and p.stat().st_size < 2_000_000
    )
    sizes = {f: (src / f).stat().st_size for f in py_files}

    def read(rel):
        return (src / rel).read_text("utf-8")

    def pick_py(files=None):
        files = files or py_files
        weights = [sizes.get(f, 1000) ** 0.7 for f in files]
        return rng.choices(files, weights)[0]

    def one(kind: str):
        """Return list of (rel, old, new) edits for a commit of this kind."""
        if kind == "code":
            rel = pick_py()
            r = token_mutation(read(rel), rng)
            return [(rel, r)] if r else None
        if kind == "table":
            rel = pick_py()
            source = read(rel)
            lines = table_lines(source)
            if not lines:
                return None
            r = token_mutation(source, rng, only_lines=lines)
            return [(rel, r)] if r else None
        if kind == "comment":
            rel = pick_py()
            r = comment_insertion(read(rel), rng)
            return [(rel, r)] if r else None
        if kind == "fixture":
            rel = rng.choice(fixtures)
            r = fixture_typo(read(rel), rng)
            return [(rel, r)] if r else None
        if kind == "test":
            rel = rng.choice(test_files)
            r = token_mutation(read(rel), rng)
            return [(rel, r)] if r else None
        if kind == "docstring":
            rel = pick_py()
            r = docstring_edit(read(rel), rng)
            return [(rel, r)] if r else None
        if kind in ("delete", "duplicate", "swapline"):
            rel = pick_py()
            r = line_edit(read(rel), rng, {"swapline": "swap"}.get(kind, kind))
            return [(rel, r)] if r else None
        if kind == "multi":
            parts = []
            used = set()
            for sub in rng.sample(["code", "comment", "fixture", "comment", "code"], rng.choice([2, 3])):
                edit = one(sub)
                if edit and edit[0][0] not in used:
                    used.add(edit[0][0])
                    parts.extend(edit)
            return parts if len(parts) >= 2 else None
        if kind == "docs":
            rel = rng.choice(["README.md", "CHANGELOG.md", "CONTRIBUTING.md"])
            text = read(rel)
            lines = text.splitlines(keepends=True)
            k = rng.randrange(len(lines))
            lines.insert(k, "Updated wording for clarity.\n")
            return [(rel, ("".join(lines), "docs line"))]
        if kind == "config":
            rel = rng.choice(["setup.cfg", "pyproject.toml"])
            text = read(rel)
            lines = text.splitlines(keepends=True)
            k = rng.randrange(len(lines))
            lines.insert(k, "# maintained by the release script\n")
            return [(rel, ("".join(lines), "config comment"))]
        if kind == "stmtdel":
            rel = pick_py()
            r = stmt_delete(read(rel), rng)
            return [(rel, r)] if r else None
        if kind == "nameerr":
            rel = pick_py()
            r = name_error(read(rel), rng)
            return [(rel, r)] if r else None
        if kind == "default":
            rel = pick_py()
            r = change_default(read(rel), rng)
            return [(rel, r)] if r else None
        if kind == "addfunc":
            rel = pick_py()
            r = add_function(read(rel), rng)
            return [(rel, r)] if r else None
        if kind in ("addimport", "badimport"):
            rel = pick_py()
            r = add_import(read(rel), rng, bad=kind == "badimport")
            return [(rel, r)] if r else None
        if kind == "swapmethods":
            rel = pick_py()
            r = swap_methods(read(rel), rng)
            return [(rel, r)] if r else None
        if kind == "whitespace":
            rel = pick_py()
            r = whitespace_edit(read(rel), rng)
            return [(rel, r)] if r else None
        if kind == "testhelper":
            rel = rng.choice(["tests/helpers.py", "tests/dialects/test_dialect.py", "tests/test_executor.py", "tests/test_optimizer.py"])
            r = rng.choice([token_mutation, stmt_delete, name_error])(read(rel), rng)
            return [(rel, r)] if r else None
        if kind == "newmodule":
            pkg = rng.choice(["sqlglot", "sqlglot/dialects", "sqlglot/optimizer", "sqlglot/parsers", "sqlglot/generators", "tests"])
            name = rng.choice(["extra_utils", "newdialect", "compat", "test_extra"]) + ".py"
            rel = f"{pkg}/{name}"
            if (src / rel).exists():
                return None
            body = "from __future__ import annotations\n\n\ndef helper(x):\n    return x\n"
            if pkg == "tests":
                body = "import unittest\n\n\nclass TestExtra(unittest.TestCase):\n    def test_extra(self):\n        self.assertTrue(True)\n"
            return [("__raw__", (raw_new(rel, body), f"new file {rel}"))]
        if kind == "delmodule":
            rel = rng.choice(py_files + fixtures[:40])
            return [("__raw__", (raw_delete(rel, read(rel)), f"delete {rel}"))]
        if kind == "rename":
            rel = pick_py()
            new_rel = rel[:-3] + "_impl.py"
            return [("__raw__", (raw_rename(rel, new_rel), f"rename {rel} -> {new_rel}"))]
        if kind == "nonpy":
            rel = rng.choice(["setup.py", "Makefile", "MANIFEST.in", "sqlglotc/setup.py", "sqlglotc/pyproject.toml", "AGENTS.md", "tests/gen_fixtures.py", "tests/leakcheck.py"])
            text = read(rel)
            lines = text.splitlines(keepends=True)
            k = rng.randrange(len(lines) + 1)
            ins = "# note\n" if not rel.endswith(".md") else "Note.\n"
            return [(rel, ("".join(lines[:k] + [ins] + lines[k:]), f"insert line {k + 1}"))]
        simple = {
            "earlyreturn": early_return, "raisepass": raise_to_pass, "message": message_edit,
            "decorator": remove_decorator, "localrename": local_rename, "parens": wrap_parens,
            "flagflip": flag_flip, "removebase": remove_base, "movefunc": move_function,
            "annotation": annotation_edit, "swapargs": swap_args,
        }
        if kind in simple:
            rel = pick_py()
            r = simple[kind](read(rel), rng)
            return [(rel, r)] if r else None
        if kind == "testadd":
            rel = rng.choice(test_files)
            r = add_test(read(rel), rng)
            return [(rel, r)] if r else None
        if kind == "multi2":
            # a code edit plus a behaviour-preserving edit elsewhere
            parts, used = [], set()
            for sub in (rng.choice(["code", "earlyreturn", "message", "flagflip"]),
                        rng.choice(["localrename", "parens", "comment", "fixture"])):
                edit = one(sub)
                if edit and edit[0][0] not in used:
                    used.add(edit[0][0])
                    parts.extend(edit)
            return parts if len(parts) >= 2 else None
        raise ValueError(kind)

    kinds_plan = (
        ["code"] * 52 + ["comment"] * 10 + ["fixture"] * 14 + ["multi"] * 10
        + ["test"] * 5 + ["docstring"] * 3 + ["delete"] * 2 + ["duplicate"] * 1
        + ["swapline"] * 1 + ["docs"] * 1 + ["config"] * 1
    )
    if args.plan == "final":
        kinds_plan = (
            ["code"] * 30 + ["fixture"] * 10 + ["comment"] * 8 + ["multi"] * 6 + ["multi2"] * 4 + ["test"] * 4
            + ["docstring"] * 2 + ["delete"] * 2 + ["swapline"] + ["duplicate"] + ["docs"] + ["config"]
            + ["earlyreturn"] * 3 + ["raisepass"] * 3 + ["message"] * 3 + ["decorator"] * 3 + ["localrename"] * 4
            + ["parens"] * 2 + ["flagflip"] * 3 + ["removebase"] * 2 + ["movefunc"] * 3 + ["annotation"] * 2
            + ["swapargs"] * 3 + ["testadd"] * 2 + ["stmtdel"] * 2 + ["nameerr"] * 2 + ["default"] * 2
            + ["addfunc"] + ["addimport"] + ["swapmethods"] * 2 + ["whitespace"] + ["rename"] + ["newmodule"]
            + ["delmodule"] + ["nonpy"] + ["testhelper"] + ["table"] * 3
        )
    if args.kinds:
        kinds_plan = args.kinds.split(",")
    (args.out / "commits").mkdir(parents=True, exist_ok=True)
    kinds: dict = {}
    made = 0
    attempts = 0
    while made < args.count and attempts < args.count * 50:
        attempts += 1
        kind = rng.choice(kinds_plan)
        try:
            edits = one(kind)
        except Exception:
            edits = None
        if not edits:
            continue
        patch = ""
        desc = []
        for rel, (new, what) in edits:
            if rel == "__raw__":
                patch += new
                desc.append(what)
                continue
            old = read(rel)
            if new == old:
                patch = ""
                break
            patch += unified(rel, old, new)
            desc.append(f"{rel}: {what}")
        if not patch:
            continue
        commit = f"{args.prefix}{made:04d}"
        (args.out / "commits" / f"{commit}.patch").write_text(patch, "utf-8", newline="\n")
        kinds[commit] = {"kind": kind, "what": desc}
        made += 1
    (args.out / "kinds.json").write_text(json.dumps(kinds, indent=1), "utf-8")
    print(f"made {made} commits in {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
