#!/usr/bin/env python3
"""Blast radius selector: pick the tests a change to sqlglot can break.

For every /data/commits/<id>.patch it prints one JSON line
{"commit": id, "workers": [[test numbers], ...]} on stdout. Logs go to stderr.

How a test is chosen (maps are built ahead of time from the pinned commit by
running every test alone under a tracer, see tools/mapbuild/):

  * Python files: apply the patch to the pinned source, compare the old and
    new syntax trees statement by statement, and map every changed statement
    to the tests that executed its lines. Lines run inside functions and
    lines run while a module or class body executes (import time) are kept
    apart, so a change inside a lambda only selects the tests that called it.
    Comment and docstring edits change nothing a test can see; a file that no
    longer parses breaks every test that imports it.
  * Other files (SQL fixtures, data): the tests that read the file.
  * pytest config files: everything when the pytest config changes or the
    file no longer parses, otherwise nothing.

Then the selected tests are split over 1-8 workers to minimise
"slowest worker + 2 s per worker".
"""

from __future__ import annotations

import ast
import difflib
import gzip
import heapq
import json
import os
import posixpath
import re
import sys
import time
import tomllib
import zipfile
from pathlib import Path

DATA = Path(os.environ.get("BLAST_DATA", "/data"))
MAPS = Path(os.environ.get("BLAST_MAPS", str(Path(__file__).resolve().parent / "maps")))
WORKER_MS = 2000
MAX_WORKERS = 8
TIME_BUDGET = float(os.environ.get("BLAST_TIME_BUDGET", "150"))  # seconds for the batch
VERBOSE = os.environ.get("BLAST_VERBOSE", "1") == "1"

STARTED = time.monotonic()


def log(*parts) -> None:
    if VERBOSE:
        print(*parts, file=sys.stderr)


# --------------------------------------------------------------------------- maps


def decode(enc) -> int:
    if isinstance(enc, str):
        return int(enc, 16) if enc else 0
    bits = 0
    for n in enc:
        bits |= 1 << n
    return bits


class Shard:
    """Coverage of one traced source file."""

    def __init__(self, raw: dict):
        self.raw = raw
        self.sets = raw["sets"]
        self.cache: dict = {}
        self.f = raw["f"]
        self.d = raw["d"]
        self.xf = set(raw.get("xf", ()))
        self.xd = set(raw.get("xd", ()))

    def bits(self, index: int) -> int:
        value = self.cache.get(index)
        if value is None:
            value = self.cache[index] = decode(self.sets[index])
        return value

    def executors(self, lines, kinds) -> int:
        out = 0
        for kind in kinds:
            table = self.f if kind == "f" else self.d
            for line in lines:
                index = table.get(str(line))
                if index is not None:
                    out |= self.bits(index)
        return out

    def executable(self, lines, kinds) -> bool:
        for kind in kinds:
            table = self.xf if kind == "f" else self.xd
            for line in lines:
                if line in table:
                    return True
        return False

    @property
    def importers(self) -> int:
        return self.bits(self.raw["imp"])

    @property
    def readers(self) -> int:
        return self.bits(self.raw["rd"]) | self.bits(self.raw["rl"])


class Maps:
    def __init__(self, root: Path):
        self.root = root
        self.shards: dict = {}
        with gzip.open(root / "files.json.gz", "rt") as handle:
            files = json.load(handle)
        self.data_reads = files["data"]
        self.cache_reads = files["linecache"]
        with gzip.open(root / "dirs.json.gz", "rt") as handle:
            self.dirs = json.load(handle)
        imports_path = root / "imports.json.gz"
        self.imports: dict = {}
        if imports_path.exists():
            with gzip.open(imports_path, "rt") as handle:
                self.imports = json.load(handle)
        self.src = zipfile.ZipFile(root / "src.zip")
        self.src_names = set(self.src.namelist())

    def shard(self, rel: str) -> Shard | None:
        if rel not in self.shards:
            path = self.root / "cov" / (rel.replace("/", "__") + ".json.gz")
            if path.exists():
                with gzip.open(path, "rb") as handle:
                    self.shards[rel] = Shard(json.loads(handle.read()))
            else:
                self.shards[rel] = None
        return self.shards[rel]

    def readers(self, rel: str) -> int:
        bits = 0
        if rel in self.data_reads:
            bits |= decode(self.data_reads[rel])
        if rel in self.cache_reads:
            bits |= decode(self.cache_reads[rel])
        return bits

    def listers(self, rel_dir: str) -> int:
        enc = self.dirs.get(rel_dir.rstrip("/"))
        return decode(enc) if enc is not None else 0

    def import_attempts(self, rel: str) -> int:
        """Tests that tried to import the module this path would provide."""
        if not rel.endswith(".py"):
            return 0
        module = rel[:-3].replace("/", ".")
        if module.endswith(".__init__"):
            module = module[: -len(".__init__")]
        enc = self.imports.get(module)
        return decode(enc) if enc is not None else 0

    def source(self, rel: str) -> bytes | None:
        if rel in self.src_names:
            return self.src.read(rel)
        return None


# --------------------------------------------------------------------------- patches


class Hunk:
    __slots__ = ("old_start", "old_len", "new_start", "new_len", "lines")

    def __init__(self, old_start, old_len, new_start, new_len):
        self.old_start, self.old_len = old_start, old_len
        self.new_start, self.new_len = new_start, new_len
        self.lines: list = []  # (tag, text) with tag in " -+\\"


class FilePatch:
    def __init__(self):
        self.old_path: str | None = None
        self.new_path: str | None = None
        self.hunks: list = []
        self.binary = False
        self.is_new = False
        self.is_deleted = False
        self.header_old: str | None = None
        self.header_new: str | None = None

    @property
    def path(self) -> str:
        return self.new_path or self.old_path or self.header_new or self.header_old or ""


HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def unquote_path(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith('"') and raw.endswith('"'):
        body = raw[1:-1]
        out = bytearray()
        i = 0
        while i < len(body):
            ch = body[i]
            if ch == "\\" and i + 1 < len(body):
                nxt = body[i + 1]
                if nxt in "01234567" and i + 3 < len(body) + 1:
                    out.append(int(body[i + 1:i + 4], 8))
                    i += 4
                    continue
                out.extend({"n": b"\n", "t": b"\t", '"': b'"', "\\": b"\\"}.get(nxt, nxt.encode()))
                i += 2
                continue
            out.extend(ch.encode())
            i += 1
        raw = out.decode("utf-8", "replace")
    return raw


def strip_prefix(path: str) -> str | None:
    path = unquote_path(path.split("\t")[0])
    if path == "/dev/null":
        return None
    if path.startswith(("a/", "b/")):
        path = path[2:]
    return path


def parse_patch(text: str) -> list:
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    patches: list = []
    cur: FilePatch | None = None
    i = 0
    while i < len(lines):
        line = lines[i].rstrip("\r")
        if line.startswith("diff --git "):
            cur = FilePatch()
            patches.append(cur)
            rest = line[len("diff --git "):]
            if rest.startswith('"'):
                end = rest.find('"', 1)
                cur.header_old = strip_prefix(rest[: end + 1])
                cur.header_new = strip_prefix(rest[end + 1:].strip())
            else:
                split = rest.find(" b/")
                if split > 0:
                    cur.header_old = strip_prefix(rest[:split])
                    cur.header_new = strip_prefix(rest[split + 1:])
            i += 1
            continue
        if line.startswith("--- ") and (i + 1 < len(lines) and lines[i + 1].startswith("+++ ")):
            if cur is None or cur.hunks:
                cur = FilePatch()
                patches.append(cur)
            cur.old_path = strip_prefix(line[4:])
            cur.new_path = strip_prefix(lines[i + 1].rstrip("\r")[4:])
            if cur.old_path is None:
                cur.is_new = True
            if cur.new_path is None:
                cur.is_deleted = True
            i += 2
            continue
        if cur is None:
            i += 1
            continue
        match = HUNK_RE.match(line)
        if match:
            hunk = Hunk(
                int(match.group(1)), int(match.group(2) or 1),
                int(match.group(3)), int(match.group(4) or 1),
            )
            cur.hunks.append(hunk)
            old_left, new_left = hunk.old_len, hunk.new_len
            i += 1
            while i < len(lines) and (old_left > 0 or new_left > 0):
                body = lines[i]
                if body.endswith("\r"):
                    body = body[:-1]
                tag = body[:1] if body else " "
                text_part = body[1:] if body else ""
                if tag == "\\":
                    hunk.lines.append(("\\", text_part))
                    i += 1
                    continue
                if tag not in " -+":
                    break
                hunk.lines.append((tag, text_part))
                if tag in " -":
                    old_left -= 1
                if tag in " +":
                    new_left -= 1
                i += 1
            while i < len(lines) and lines[i].startswith("\\"):
                hunk.lines.append(("\\", lines[i][1:]))
                i += 1
            continue
        if line.startswith("new file mode"):
            cur.is_new = True
        elif line.startswith("deleted file mode"):
            cur.is_deleted = True
        elif line.startswith("rename from "):
            cur.old_path = unquote_path(line[len("rename from "):])
        elif line.startswith("rename to "):
            cur.new_path = unquote_path(line[len("rename to "):])
        elif line.startswith("copy from "):
            cur.old_path = unquote_path(line[len("copy from "):])
        elif line.startswith("copy to "):
            cur.new_path = unquote_path(line[len("copy to "):])
        elif line.startswith("Binary files") or line.startswith("GIT binary patch"):
            cur.binary = True
        i += 1
    for fp in patches:
        if fp.old_path is None and not fp.is_new:
            fp.old_path = fp.header_old
        if fp.new_path is None and not fp.is_deleted:
            fp.new_path = fp.header_new
    return patches


class Applied:
    """Result of applying one file's hunks to the pinned source."""

    def __init__(self):
        self.new_text = ""
        self.removed: set = set()  # old line numbers removed or replaced
        self.inserts: set = set()  # p: new lines inserted between old p-1 and p
        self.added: set = set()  # new line numbers added
        self.old_to_new: dict = {}  # unchanged old line -> new line


def apply_hunks(old_text: str, hunks: list) -> Applied | None:
    old_lines = old_text.splitlines(keepends=True)
    result = Applied()
    out: list = []
    pos = 0  # 0-based index into old_lines
    new_no = 1
    for hunk in hunks:
        start = hunk.old_start - 1 if hunk.old_len > 0 else hunk.old_start
        if start < pos or start > len(old_lines):
            return None
        for k in range(pos, start):
            result.old_to_new[k + 1] = new_no
            out.append(old_lines[k])
            new_no += 1
        pos = start
        pending_insert = False
        last_tag = None
        for tag, text in hunk.lines:
            if tag == "\\":
                if last_tag == "+" and out and out[-1].endswith("\n"):
                    out[-1] = out[-1][:-1]
                continue
            last_tag = tag
            if tag in " -":
                if pos >= len(old_lines):
                    return None
                if old_lines[pos].rstrip("\r\n") != text.rstrip("\r"):
                    return None
                if pending_insert:
                    result.inserts.add(pos + 1)
                    pending_insert = False
                if tag == " ":
                    result.old_to_new[pos + 1] = new_no
                    out.append(old_lines[pos])
                    new_no += 1
                else:
                    result.removed.add(pos + 1)
                pos += 1
            else:
                out.append(text + "\n")
                result.added.add(new_no)
                new_no += 1
                pending_insert = True
        if pending_insert:
            result.inserts.add(pos + 1)
    for k in range(pos, len(old_lines)):
        result.old_to_new[k + 1] = new_no
        out.append(old_lines[k])
        new_no += 1
    result.new_text = "".join(out)
    return result


# --------------------------------------------------------------------------- AST diff

HEADER_FIELDS = {
    ast.FunctionDef: ("name", "args", "decorator_list", "returns", "type_params"),
    ast.AsyncFunctionDef: ("name", "args", "decorator_list", "returns", "type_params"),
    ast.ClassDef: ("name", "bases", "keywords", "decorator_list", "type_params"),
    ast.If: ("test",),
    ast.While: ("test",),
    ast.For: ("target", "iter"),
    ast.AsyncFor: ("target", "iter"),
    ast.With: ("items",),
    ast.AsyncWith: ("items",),
    ast.Try: (),
    ast.Match: ("subject",),
}
if hasattr(ast, "TryStar"):
    HEADER_FIELDS[ast.TryStar] = ()


def eq(a, b) -> bool:
    """Structural equality that ignores positions."""
    if type(a) is not type(b):
        return False
    if isinstance(a, ast.AST):
        for name in a._fields:
            if not eq(getattr(a, name, None), getattr(b, name, None)):
                return False
        return True
    if isinstance(a, list):
        return len(a) == len(b) and all(eq(x, y) for x, y in zip(a, b))
    if isinstance(a, float):
        return repr(a) == repr(b)
    if isinstance(a, complex):
        return repr(a) == repr(b)
    return a == b


def start_line(node) -> int:
    line = node.lineno
    for dec in getattr(node, "decorator_list", ()) or ():
        line = min(line, dec.lineno)
    return line


def span(node) -> range:
    return range(start_line(node), (node.end_lineno or node.lineno) + 1)


def is_docstring(stmt) -> bool:
    return (
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Constant)
        and isinstance(stmt.value.value, str)
    )


class Scope:
    """Where a block of statements runs: in a function (run time) or in a
    module / class body (definition time)."""

    def __init__(self, kind: str, node, parent: "Scope | None"):
        self.kind = kind  # "f" or "d"
        self.node = node
        self.parent = parent

    @property
    def kinds(self):
        # A definition-time statement can contain comprehensions (function-like
        # code that runs at import) and lambdas; include both.
        return ("f",) if self.kind == "f" else ("d", "f")


class Differ:
    def __init__(self, shard: Shard, applied: Applied, reasons: list):
        self.shard = shard
        self.applied = applied
        self.tests = 0
        self.reasons = reasons

    # ---- mapping to tests
    def lines_tests(self, lines, kinds) -> int:
        return self.shard.executors(lines, kinds)

    def scope_tests(self, scope: Scope) -> int:
        """Every test that can reach code in this scope."""
        if scope.kind == "f" and scope.node is not None:
            return self.shard.executors(span(scope.node), ("f",))
        return self.shard.importers

    def point_tests(self, block, before: int, after: int, scope: Scope, why: str) -> int:
        lines: list = []
        for index in (before, after):
            if 0 <= index < len(block):
                lines.extend(span(block[index]))
        if lines and self.shard.executable(lines, scope.kinds):
            self.reasons.append(f"{why}: neighbours {lines[0]}-{lines[-1]}")
            return self.lines_tests(lines, scope.kinds)
        self.reasons.append(f"{why}: whole scope")
        return self.scope_tests(scope)

    def stmt_tests(self, stmt, block, index: int, scope: Scope, why: str, lines=None) -> int:
        lines = list(lines if lines is not None else span(stmt))
        if self.shard.executable(lines, scope.kinds):
            self.reasons.append(f"{why} {scope.kind} L{lines[0]}-{lines[-1]}")
            return self.lines_tests(lines, scope.kinds)
        return self.point_tests(block, index - 1, index + 1, scope, why + " (no bytecode)")

    # ---- dirtiness
    def old_clean(self, node) -> bool:
        sp = span(node)
        removed, inserts = self.applied.removed, self.applied.inserts
        for line in sp:
            if line in removed:
                return False
        for p in inserts:
            if sp.start < p <= sp.stop - 1:
                return False
        return True

    def pair_block(self, old_block, new_block):
        """Match unchanged statements; yield (old_slice, new_slice) segments
        that changed, with the old block index where each segment starts."""
        new_by_start = {start_line(t): j for j, t in enumerate(new_block)}
        anchors = []
        last_j = -1
        for i, s in enumerate(old_block):
            if not self.old_clean(s):
                continue
            first = self.applied.old_to_new.get(start_line(s))
            last = self.applied.old_to_new.get(s.end_lineno or s.lineno)
            if first is None or last is None:
                continue
            j = new_by_start.get(first)
            if j is None or j <= last_j:
                continue
            t = new_block[j]
            if (t.end_lineno or t.lineno) != last or type(t) is not type(s):
                continue
            anchors.append((i, j))
            last_j = j
        prev_i, prev_j = -1, -1
        for i, j in anchors + [(len(old_block), len(new_block))]:
            if i - prev_i > 1 or j - prev_j > 1:
                yield prev_i + 1, i, prev_j + 1, j
            prev_i, prev_j = i, j

    # ---- walking
    def block(self, old_block, new_block, scope: Scope) -> None:
        for oi, oj, ni, nj in self.pair_block(old_block, new_block):
            old_seg = old_block[oi:oj]
            new_seg = new_block[ni:nj]
            if len(old_seg) == len(new_seg) and all(
                type(a) is type(b) for a, b in zip(old_seg, new_seg)
            ):
                for k, (a, b) in enumerate(zip(old_seg, new_seg)):
                    self.stmt(a, b, old_block, oi + k, scope)
                continue
            keys_old = [ast.dump(s) for s in old_seg]
            keys_new = [ast.dump(s) for s in new_seg]
            matcher = difflib.SequenceMatcher(None, keys_old, keys_new, autojunk=False)
            for op, a1, a2, b1, b2 in matcher.get_opcodes():
                if op == "equal":
                    continue
                if op == "replace" and a2 - a1 == b2 - b1:
                    for k in range(a2 - a1):
                        a, b = old_seg[a1 + k], new_seg[b1 + k]
                        if type(a) is type(b):
                            self.stmt(a, b, old_block, oi + a1 + k, scope)
                        else:
                            self.tests |= self.stmt_tests(a, old_block, oi + a1 + k, scope, "replaced")
                    continue
                for k in range(a1, a2):
                    self.tests |= self.stmt_tests(old_seg[k], old_block, oi + k, scope, "deleted")
                if b2 > b1:
                    at = oi + a1
                    if self.only_docstring_insert(new_seg[b1:b2], at, scope):
                        continue
                    self.tests |= self.point_tests(old_block, at - 1, at, scope, "inserted")

    def only_docstring_insert(self, stmts, at: int, scope: Scope) -> bool:
        return at == 0 and len(stmts) == 1 and is_docstring(stmts[0]) and not isinstance(
            scope.node, type(None)
        )

    def stmt(self, a, b, block, index: int, scope: Scope) -> None:
        if eq(a, b):
            return
        if index == 0 and is_docstring(a) and is_docstring(b):
            # Nothing reads docstrings at run time in sqlglot or its tests.
            self.reasons.append(f"docstring L{a.lineno}")
            return
        header = HEADER_FIELDS.get(type(a))
        if header is None:
            self.simple(a, b, block, index, scope)
            return
        header_changed = any(not eq(getattr(a, f, None), getattr(b, f, None)) for f in header)
        if isinstance(a, (ast.FunctionDef, ast.AsyncFunctionDef)):
            inner = Scope("f", a, scope)
            if header_changed:
                head = range(start_line(a), max(a.lineno, a.body[0].lineno - 1) + 1)
                self.tests |= self.stmt_tests(a, block, index, scope, "def header", head)
                self.tests |= self.shard.executors(span(a), ("f",))
            self.block(a.body, b.body, inner)
            return
        if isinstance(a, ast.ClassDef):
            inner = Scope("d", a, scope)
            if header_changed:
                head = range(start_line(a), max(a.lineno, a.body[0].lineno - 1) + 1)
                self.tests |= self.stmt_tests(a, block, index, scope, "class header", head)
            self.block(a.body, b.body, inner)
            return
        if header_changed:
            last = a.lineno
            for f in header:
                value = getattr(a, f, None)
                for node in value if isinstance(value, list) else [value]:
                    if isinstance(node, ast.AST) and getattr(node, "end_lineno", None):
                        last = max(last, node.end_lineno)
            self.tests |= self.stmt_tests(a, block, index, scope, type(a).__name__ + " header",
                                          range(a.lineno, last + 1))
        for field in ("body", "orelse", "finalbody"):
            old_body = getattr(a, field, None)
            new_body = getattr(b, field, None)
            if isinstance(old_body, list) and isinstance(new_body, list):
                if not old_body and new_body:
                    self.tests |= self.stmt_tests(a, block, index, scope, f"new {field}")
                elif old_body and not new_body:
                    self.tests |= self.stmt_tests(a, block, index, scope, f"dropped {field}")
                elif old_body:
                    self.block(old_body, new_body, scope)
        if isinstance(a, (ast.Try,) + ((ast.TryStar,) if hasattr(ast, "TryStar") else ())):
            if len(a.handlers) != len(b.handlers):
                self.tests |= self.stmt_tests(a, block, index, scope, "handlers")
            else:
                for ha, hb in zip(a.handlers, b.handlers):
                    if not (eq(ha.type, hb.type) and ha.name == hb.name):
                        end = ha.type.end_lineno if ha.type is not None else ha.lineno
                        self.tests |= self.stmt_tests(a, block, index, scope, "except header",
                                                      range(ha.lineno, end + 1))
                    self.block(ha.body, hb.body, scope)
        if isinstance(a, ast.Match):
            if len(a.cases) != len(b.cases):
                self.tests |= self.stmt_tests(a, block, index, scope, "match cases")
            else:
                for ca, cb in zip(a.cases, b.cases):
                    if not (eq(ca.pattern, cb.pattern) and eq(ca.guard, cb.guard)):
                        self.tests |= self.stmt_tests(a, block, index, scope, "case header",
                                                      range(ca.pattern.lineno, ca.pattern.end_lineno + 1))
                    self.block(ca.body, cb.body, scope)

    def simple(self, a, b, block, index: int, scope: Scope) -> None:
        """A simple statement changed. If every difference sits inside lambda
        bodies, only the callers of those lambdas can notice."""
        diffs: list = []
        self.expr_diff(a, b, None, diffs)
        if diffs and all(lam is not None for _, lam in diffs):
            for _node, lam in diffs:
                lines = list(span(lam.body))
                if self.shard.executable(lines, ("f",)):
                    self.reasons.append(f"lambda body L{lines[0]}-{lines[-1]}")
                    self.tests |= self.lines_tests(lines, ("f",))
                else:
                    self.tests |= self.stmt_tests(a, block, index, scope, "lambda (no bytecode)")
            return
        self.tests |= self.stmt_tests(a, block, index, scope, "stmt")

    def expr_diff(self, a, b, lam, out: list) -> None:
        if type(a) is not type(b):
            out.append((a, lam))
            return
        if isinstance(a, ast.AST):
            for name in a._fields:
                va, vb = getattr(a, name, None), getattr(b, name, None)
                if eq(va, vb):
                    continue
                inner = lam
                if isinstance(a, ast.Lambda) and name == "body":
                    inner = a
                elif isinstance(a, ast.Lambda) and name == "args":
                    out.append((a, None))
                    continue
                if isinstance(va, list) and isinstance(vb, list):
                    if len(va) != len(vb):
                        out.append((a, lam))
                    else:
                        for x, y in zip(va, vb):
                            if not eq(x, y):
                                self.expr_diff(x, y, inner, out)
                elif isinstance(va, ast.AST) and isinstance(vb, ast.AST):
                    self.expr_diff(va, vb, inner, out)
                else:
                    out.append((a, lam))
            return
        out.append((a, lam))


# --------------------------------------------------------------------------- per file

PYTEST_CONFIG_FILES = {"pytest.ini", ".pytest.ini", "pyproject.toml", "tox.ini", "setup.cfg"}
BUILD_TEST_FILES = ("tests/test_build.py", "tests/test_sqlglotc.py")


def ini_parse(text: str) -> dict | None:
    """iniconfig's grammar (what pytest uses for .ini/.cfg); None if invalid."""
    sections: dict = {}
    section = None
    last_key = None
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped[0] in "#;":
            continue
        line = raw.rstrip()
        if line[0] == "[":
            head = line
            for c in "#;":
                head = head.split(c)[0].rstrip()
            if head.endswith("]"):
                name = head[1:-1]
                if not name or name in sections:
                    return None
                section = name
                sections[section] = {}
                last_key = None
                continue
            return None
        if not line[0].isspace():
            if "=" in line and ":" not in line.split("=", 1)[0]:
                name, value = line.split("=", 1)
            elif ": " in line:
                name, value = line.split(": ", 1)
            else:
                return None
            if section is None:
                return None
            name = name.strip()
            if name in sections[section]:
                return None
            sections[section][name] = value.strip()
            last_key = name
            continue
        if section is None or last_key is None:
            return None
        sections[section][last_key] += "\n" + line.strip()
    return sections


def pytest_config(rel: str, text: str | None):
    """Return (valid, pytest_settings) for a pytest config candidate file."""
    if text is None:
        return True, None
    name = posixpath.basename(rel)
    if name == "pyproject.toml":
        try:
            data = tomllib.loads(text)
        except Exception:
            return False, None
        tool = data.get("tool", {})
        return True, json.dumps(tool.get("pytest"), sort_keys=True, default=str)
    parsed = ini_parse(text)
    if parsed is None:
        return False, None
    if name in ("pytest.ini", ".pytest.ini"):
        return True, json.dumps(parsed.get("pytest", {}), sort_keys=True) + "|exists"
    key = "tool:pytest" if name == "setup.cfg" else "pytest"
    return True, json.dumps(parsed.get(key), sort_keys=True)


class Selector:
    def __init__(self, maps: Maps, tests: list):
        self.maps = maps
        self.tests = tests
        self.all_bits = 0
        self.by_file: dict = {}
        for t in tests:
            self.all_bits |= 1 << t["number"]
            self.by_file.setdefault(t["file"], 0)
            self.by_file[t["file"]] |= 1 << t["number"]
        self.old_trees: dict = {}

    def tests_under(self, prefix: str) -> int:
        bits = 0
        for file, b in self.by_file.items():
            if file.startswith(prefix):
                bits |= b
        return bits

    def old_tree(self, rel: str, source: bytes):
        tree = self.old_trees.get(rel)
        if tree is None:
            tree = self.old_trees[rel] = ast.parse(source, filename=rel)
        return tree

    def commit(self, patch_text: str, reasons: list) -> int:
        patches = parse_patch(patch_text)
        if not patches:
            reasons.append("no file changes found")
            return 0
        bits = 0
        for fp in patches:
            try:
                bits |= self.file(fp, reasons)
            except Exception as error:  # never lose a commit to a bug here
                reasons.append(f"{fp.path}: analysis failed ({error!r}); broad fallback")
                bits |= self.broad(fp)
        return bits

    def broad(self, fp: FilePatch) -> int:
        bits = 0
        for rel in {fp.old_path, fp.new_path} - {None}:
            shard = self.maps.shard(rel)
            if shard is not None:
                bits |= shard.importers | shard.readers
            bits |= self.maps.readers(rel)
            bits |= self.maps.listers(posixpath.dirname(rel))
            if posixpath.basename(rel) in PYTEST_CONFIG_FILES or rel.endswith("conftest.py"):
                bits |= self.all_bits
        return bits

    def file(self, fp: FilePatch, reasons: list) -> int:
        rel = fp.path
        old_rel, new_rel = fp.old_path, fp.new_path
        name = posixpath.basename(rel)

        # pytest config candidates at the repository root
        if "/" not in rel and name in PYTEST_CONFIG_FILES:
            return self.config_file(fp, reasons)
        if name == "conftest.py" or (name == "__init__.py" and rel.startswith("tests/") and (fp.is_new or fp.is_deleted)):
            scope = posixpath.dirname(rel)
            reasons.append(f"{rel}: conftest/package change -> tests under {scope or '.'}")
            return self.tests_under(scope + "/" if scope else "")

        bits = 0
        renamed = old_rel and new_rel and old_rel != new_rel
        if fp.is_deleted or renamed or (old_rel is None and new_rel is None):
            target = old_rel or rel
            shard = self.maps.shard(target)
            if shard is not None:
                bits |= shard.importers | shard.readers
            bits |= self.maps.readers(target) | self.maps.listers(posixpath.dirname(target))
            reasons.append(f"{target}: removed/renamed")
            if fp.is_deleted:
                return bits
        if fp.is_new or renamed:
            target = new_rel or rel
            bits |= self.maps.listers(posixpath.dirname(target))
            bits |= self.maps.readers(target)
            bits |= self.maps.import_attempts(target)
            reasons.append(f"{target}: added")
            return bits
        if fp.binary:
            reasons.append(f"{rel}: binary change")
            return self.broad(fp)
        if not fp.hunks:
            reasons.append(f"{rel}: no content change")
            return 0

        old_source = self.maps.source(rel)
        if old_source is None:
            reasons.append(f"{rel}: not in the pinned checkout")
            return self.maps.readers(rel) | self.maps.listers(posixpath.dirname(rel))

        if rel.endswith(".py"):
            return self.python_file(fp, rel, old_source, reasons)

        readers = self.maps.readers(rel)
        reasons.append(f"{rel}: data file, {bin(readers).count('1')} readers")
        return readers

    def config_file(self, fp: FilePatch, reasons: list) -> int:
        rel = fp.path
        old_source = self.maps.source(rel)
        old_text = old_source.decode("utf-8", "replace") if old_source is not None else None
        if fp.is_deleted:
            new_text = None
        elif fp.is_new or old_text is None:
            new_text = None
            if fp.hunks:
                new_text = "".join(t + "\n" for h in fp.hunks for tag, t in h.lines if tag == "+")
        else:
            applied = apply_hunks(old_text, fp.hunks)
            if applied is None:
                reasons.append(f"{rel}: patch does not apply -> all tests")
                return self.all_bits
            new_text = applied.new_text
        valid, new_cfg = pytest_config(rel, new_text)
        _, old_cfg = pytest_config(rel, old_text)
        extra = 0
        for test_file in BUILD_TEST_FILES:
            extra |= self.by_file.get(test_file, 0)
        if not valid:
            reasons.append(f"{rel}: no longer parses -> pytest cannot start -> all tests")
            return self.all_bits
        if new_cfg != old_cfg:
            reasons.append(f"{rel}: pytest settings changed -> all tests")
            return self.all_bits
        reasons.append(f"{rel}: pytest settings unchanged")
        return extra | self.maps.readers(rel) & ~self.all_bits_readers(rel)

    def all_bits_readers(self, rel: str) -> int:
        # pytest itself reads config candidates in every test; that read does
        # not make a test depend on unrelated sections of the file.
        readers = self.maps.readers(rel)
        return readers if bin(readers).count("1") > len(self.tests) * 0.9 else 0

    def python_file(self, fp: FilePatch, rel: str, old_source: bytes, reasons: list) -> int:
        shard = self.maps.shard(rel)
        readers = self.maps.readers(rel)
        if shard is not None:
            readers |= shard.readers
        old_text = old_source.decode("utf-8")
        applied = apply_hunks(old_text, fp.hunks)
        if applied is None:
            reasons.append(f"{rel}: patch does not apply cleanly -> broad")
            return self.broad(fp)
        if shard is None:
            reasons.append(f"{rel}: never executed by any test")
            return readers
        try:
            new_tree = ast.parse(applied.new_text.encode("utf-8"), filename=rel)
            compile(new_tree, rel, "exec", dont_inherit=True)
        except (SyntaxError, ValueError) as error:
            reasons.append(f"{rel}: no longer compiles ({type(error).__name__}) -> importers")
            return shard.importers | readers
        old_tree = self.old_tree(rel, old_source)
        differ = Differ(shard, applied, reasons)
        differ.block(old_tree.body, new_tree.body, Scope("d", None, None))
        return differ.tests | readers


# --------------------------------------------------------------------------- packing


def pack(numbers: list, ms: dict) -> list:
    if not numbers:
        return []
    items = sorted(numbers, key=lambda n: (-ms[n], n))
    total = sum(ms[n] for n in items)
    best_cost, best_bins = None, None
    for workers in range(1, min(MAX_WORKERS, len(items)) + 1):
        lower = max(ms[items[0]], -(-total // workers)) + WORKER_MS * workers
        if best_cost is not None and lower >= best_cost:
            continue
        heap = [(0, k) for k in range(workers)]
        bins: list = [[] for _ in range(workers)]
        loads = [0] * workers
        for n in items:
            load, k = heapq.heappop(heap)
            bins[k].append(n)
            loads[k] = load + ms[n]
            heapq.heappush(heap, (loads[k], k))
        cost = max(loads) + WORKER_MS * workers
        if best_cost is None or cost < best_cost:
            best_cost, best_bins = cost, bins
    return [b for b in best_bins if b]


# --------------------------------------------------------------------------- main


def main() -> int:
    tests = json.loads((DATA / "tests.json").read_text("utf-8"))["tests"]
    ms = {t["number"]: int(t["seconds"] * 1000 + 0.5) for t in tests}
    valid_numbers = set(ms)
    patches = sorted((DATA / "commits").glob("*.patch"))
    maps = Maps(MAPS)
    selector = Selector(maps, tests)
    out = sys.stdout
    total_cost = 0
    for patch_path in patches:
        commit = patch_path.stem
        reasons: list = []
        try:
            if time.monotonic() - STARTED > TIME_BUDGET:
                raise TimeoutError("time budget spent")
            text = patch_path.read_bytes().decode("utf-8", "replace")
            bits = selector.commit(text, reasons)
        except Exception as error:
            reasons.append(f"selector failed ({error!r}); running everything")
            bits = selector.all_bits
        chosen = []
        n = 0
        while bits:
            if bits & 1 and n in valid_numbers:
                chosen.append(n)
            bits >>= 1
            n += 1
        workers = pack(chosen, ms)
        cost = (max(sum(ms[n] for n in w) for w in workers) + WORKER_MS * len(workers)) if workers else 0
        total_cost += cost
        out.write(json.dumps({"commit": commit, "workers": workers}, separators=(",", ":")) + "\n")
        log(f"{commit}: {len(chosen)} tests, {len(workers)} workers, {cost/1000:.2f}s | " + "; ".join(reasons[:6]))
    out.flush()
    log(f"total simulated {total_cost/1000:.1f}s over {len(patches)} commits, "
        f"{time.monotonic()-STARTED:.2f}s wall")
    return 0


if __name__ == "__main__":
    sys.exit(main())
