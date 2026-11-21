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
import subprocess
import sys
import time
import tomllib
import zipfile
from pathlib import Path

DATA = Path(os.environ.get("BLAST_DATA", "/data"))
HERE = Path(__file__).resolve().parent
MAPS = Path(os.environ.get("BLAST_MAPS", str(HERE / "maps")))
# Pristine sqlglot (importable, precompiled) and its attribute fingerprints,
# both prepared when the image is built (see Dockerfile).
PRISTINE = Path(os.environ.get("BLAST_PRISTINE", str(HERE / "tree")))
FP_BASELINE = Path(os.environ.get("BLAST_FP_BASELINE", str(HERE / "fp_baseline.json.gz")))
FP_SCRIPT = HERE / "fingerprint.py"
FP_TIMEOUT = float(os.environ.get("BLAST_FP_TIMEOUT", "40"))
USE_FP = os.environ.get("BLAST_USE_FP", "1") == "1"
# Proxy verification: when a selection holds heavy tests, run its cheap
# tests on the patched tree first and keep the heavy ones only if a cheap
# covering test fails (see Selector.verify_heavy).
VERIFY = os.environ.get("BLAST_VERIFY", "0") == "1"
VERIFY_TREE = Path(os.environ.get("BLAST_VERIFY_TREE", str(HERE / "tree")))
HEAVY_MS = int(float(os.environ.get("BLAST_HEAVY_SECONDS", "2.5")) * 1000)
VERIFY_MIN_PROXIES = int(os.environ.get("BLAST_VERIFY_MIN_PROXIES", "1"))
VERIFY_TIMEOUT = float(os.environ.get("BLAST_VERIFY_TIMEOUT", "60"))
# TPC executor tests: drop them when every changed line they run is in the
# query pipeline and the pipeline outputs of the affected queries are
# unchanged (selector/exec_check.py, tools/mapbuild/exec_profile.py).
EXEC_CHECK = os.environ.get("BLAST_EXEC_CHECK", "1") == "1"
EXEC_CHECK_SCRIPT = HERE / "exec_check.py"
EXEC_CHECK_TIMEOUT = float(os.environ.get("BLAST_EXEC_CHECK_TIMEOUT", "60"))
EXEC_TESTS = {
    "tests/test_executor.py::TestExecutor::test_execute_tpch": "tpch",
    "tests/test_executor.py::TestExecutor::test_execute_tpcds": "tpcds",
}
OPTIMIZED_TPCH = "tests/test_executor.py::TestExecutor::test_optimized_tpch"
# When only a few tpcds queries' pipeline outputs change, run those queries
# on the TPC-DS data (maps/data.zip) with the patched package and compare with
# the pristine results; equal results under two hash seeds drop the test.
RERUN = os.environ.get("BLAST_RERUN", "1") == "1"
RERUN_MAX_QUERIES = int(os.environ.get("BLAST_RERUN_MAX_QUERIES", "4"))
RERUN_MAX_SECS = float(os.environ.get("BLAST_RERUN_MAX_SECS", "4"))
RERUN_EXTRA_SEEDS = [12345]
# Opt-in (80/20 candidate): scaled-down proxy for the 28.8 s test_max_nodes.
PROXY_MAX_NODES = os.environ.get("BLAST_PROXY_MAX_NODES", "0") == "1"
PROXY_SCRIPT = HERE / "proxy_check.py"
MAX_NODES_TEST = "tests/test_parser.py::TestParser::test_max_nodes"
WORKER_MS = 2000
MAX_WORKERS = 8
TIME_BUDGET = float(os.environ.get("BLAST_TIME_BUDGET", "0"))  # 0: derive from batch size


class Budget:
    """Wall-clock guard. The run limit is 3 min for the practice batch and
    4.5 min for the final (~1.5x the commits); stay well inside it by
    dropping the optional subprocess steps (exec check first, then
    fingerprints) when time gets short. Line-level selection itself takes
    milliseconds per commit, so it always runs."""

    def __init__(self, commits: int):
        limit = 180.0 if commits <= 45 else 270.0
        self.total = TIME_BUDGET or 0.55 * limit
        self.commits = max(commits, 1)
        self.done = 0

    def remaining(self) -> float:
        return self.total - (time.monotonic() - STARTED)

    def allow(self, cost: float) -> bool:
        """Can we spend about `cost` seconds now and keep 0.3 s for every
        commit still to come?"""
        left = self.commits - self.done
        return self.remaining() - cost > 0.3 * left + 5


BUDGET = Budget(0)
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

    # ---- definition-time support (loaded on first use)
    def _load(self, name: str) -> dict:
        path = self.root / name
        if not path.exists():
            return {}
        with gzip.open(path, "rt") as handle:
            return json.load(handle)

    @property
    def attrs(self) -> dict:
        if not hasattr(self, "_attrs"):
            self._attrs = self._load("attrs.json.gz")
            by_name: dict = {}
            for key in self._attrs:
                by_name.setdefault(key.rsplit("|", 1)[-1], []).append(key)
            self._attrs_by_name = by_name
        return self._attrs

    def attr_readers(self, key: str) -> int:
        enc = self.attrs.get(key)
        return decode(enc) if enc is not None else 0

    @property
    def akeys(self) -> dict:
        if not hasattr(self, "_akeys"):
            self._akeys = self._load("akeys.json.gz")
        return self._akeys

    def table_readers(self, key: str, entries) -> int:
        """Readers of a class attribute; narrowed to the changed entries of a
        table when the per-key map covers it. A reader with no per-key record
        for the table (read it in another trace, or only took its length)
        stays selected."""
        readers = self.attr_readers(key)
        table = self.akeys.get(key) if entries else None
        if not table:
            return readers
        keyed = 0
        for enc in table.values():
            keyed |= decode(enc)
        bits = decode(table["*"]) if "*" in table else 0
        for entry in entries:
            if entry in table:
                bits |= decode(table[entry])
        return bits | (readers & ~keyed)

    def attr_readers_named(self, name: str) -> int:
        self.attrs  # noqa: B018 - load
        bits = 0
        for key in self._attrs_by_name.get(name, ()):
            bits |= decode(self._attrs[key])
        return bits

    @property
    def exec_lines(self) -> dict:
        """Per TPC test: per query pipeline/data line sets, and their unions."""
        if not hasattr(self, "_exec_lines"):
            raw = self._load("exec_lines.json.gz")
            out = {}
            for test, records in raw.items():
                queries = []
                union_p: dict = {}
                union_d: dict = {}
                for rec in records:
                    p = {f: set(l) for f, l in rec["P"].items()}
                    d = {f: set(l) for f, l in rec["D"].items()}
                    for f, l in p.items():
                        union_p.setdefault(f, set()).update(l)
                    for f, l in d.items():
                        union_d.setdefault(f, set()).update(l)
                    queries.append(p)
                out[test] = {"queries": queries, "P": union_p, "D": union_d}
            self._exec_lines = out
        return self._exec_lines

    @property
    def fixture_deps(self) -> dict:
        if not hasattr(self, "_fixture_deps"):
            path = self.root / "fixture_deps.json"
            self._fixture_deps = json.loads(path.read_text()) if path.exists() else {}
        return self._fixture_deps

    @property
    def refs(self) -> dict:
        if not hasattr(self, "_refs"):
            self._refs = self._load("refs.json.gz")
        return self._refs

    @property
    def funcs(self) -> dict:
        if not hasattr(self, "_funcs"):
            self._funcs = self._load("funcs.json.gz")
        return self._funcs

    def ref_tests(self, name: str, module: str | None = None) -> int:
        """Tests that ran a line inside a function that reads this global:
        through its own module or an import of it when `module` is given
        (qrefs, import-resolved), else any mention of the name."""
        if not hasattr(self, "_qrefs"):
            self._qrefs = self._load("qrefs.json.gz")
        table = None
        if module is not None and self._qrefs:
            table = self._qrefs.get(f"{module}||{name}")  # None: not resolvable
        if table is None:
            table = self.refs.get(name, {})
        bits = 0
        for rel, lines in table.items():
            shard = self.shard(rel)
            if shard is not None:
                bits |= shard.executors(lines, ("f",))
        return bits

    def same_name_tests(self, name: str, only_file: str | None = None) -> int:
        """Tests that called any function or method called `name`."""
        bits = 0
        for rel, spans in self.funcs.get(name, {}).items():
            if only_file is not None and rel != only_file:
                continue
            shard = self.shard(rel)
            if shard is None:
                continue
            for start, end in spans:
                bits |= shard.executors(range(start, end + 1), ("f",))
        return bits

    def class_users(self, module: str, qual: str) -> int:
        """Tests that read any data attribute defined on this class."""
        self.attrs  # noqa: B018 - load
        prefix = f"{module}|{qual}|"
        if not hasattr(self, "_by_class"):
            by_class: dict = {}
            for key in self._attrs:
                head = key.rsplit("|", 1)[0] + "|"
                by_class.setdefault(head, []).append(key)
            self._by_class = by_class
        bits = 0
        for key in self._by_class.get(prefix, ()):
            bits |= decode(self._attrs[key])
        return bits


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

    @property
    def import_time(self) -> bool:
        """Runs once when the module is imported (module body or a class body
        not nested in a function)."""
        scope = self
        while scope is not None:
            if scope.kind == "f":
                return False
            scope = scope.parent
        return True


class Differ:
    def __init__(self, shard: Shard, applied: Applied, reasons: list, use_fp: bool = False,
                 same_name=None):
        self.shard = shard
        self.applied = applied
        self.tests = 0
        self.reasons = reasons
        # Import-time data changes are resolved by the fingerprint diff when
        # use_fp is set; `fallback` is what to select if that is unavailable.
        self.use_fp = use_fp
        self.need_fp = False
        self.fallback = 0
        self.same_name = same_name or (lambda name: 0)
        # Old-file lines whose executors were selected, and whether anything
        # was selected more broadly (whole scope, importers, same name).
        self.touched: set = set()
        self.broad = False
        self.future_annotations = False

    def dunder_class_stmt(self, stmt, scope: "Scope") -> int:
        """A class-body assignment to a dunder name (__slots__, __hash__,
        __match_args__...) changes how instances behave, not data a test
        reads: the tests that ran the class's methods (importers if none
        did). Returns the bits; callers OR them in."""
        if not isinstance(scope.node, ast.ClassDef):
            return 0
        targets = []
        if isinstance(stmt, ast.Assign):
            targets = stmt.targets
        elif isinstance(stmt, (ast.AnnAssign, ast.AugAssign)):
            targets = [stmt.target]
        names = [t.id for t in targets if isinstance(t, ast.Name)]
        if not any(n.startswith("__") and n.endswith("__") for n in names):
            return 0
        bits = self.lines_tests(span(scope.node), ("f",))
        if not bits:
            bits = self.shard.importers
        self.broad = True
        self.reasons.append(f"class dunder {names[0]} L{stmt.lineno}: users of {scope.node.name}")
        return bits

    def defer(self, scope: "Scope", lines, why: str) -> bool:
        """Leave an import-time change to the fingerprint diff."""
        if not (self.use_fp and scope.import_time):
            return False
        lines = list(lines)
        self.need_fp = True
        if lines and self.shard.executable(lines, scope.kinds):
            self.fallback |= self.lines_tests(lines, scope.kinds)
        else:
            self.fallback |= self.shard.importers
        self.reasons.append(f"{why} L{lines[0] if lines else '?'}: import-time -> fingerprint")
        return True

    def removed_stmt(self, stmt, block, index: int, scope: "Scope", why: str) -> int:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) and self.use_fp and scope.import_time:
            # Callers of the removed function (an override now falls back to
            # the parent's implementation); decorators may have side effects.
            self.defer(scope, span(stmt), why + " def")
            return self.lines_tests(span(stmt), ("f",))
        if isinstance(stmt, ast.ClassDef):
            self.reasons.append(f"{why} class L{stmt.lineno}: importers")
            self.broad = True
            return self.shard.importers
        return self.stmt_tests(stmt, block, index, scope, why)  # handles dunders

    def added_stmts(self, stmts, block, at: int, scope: "Scope") -> int:
        dunder = 0
        for stmt in stmts:
            dunder |= self.dunder_class_stmt(stmt, scope)
        if self.use_fp and scope.import_time:
            # New methods show up as added fingerprint keys (callers of the
            # name among tests using the class); this is only the fallback.
            for stmt in stmts:
                if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    self.fallback |= self.same_name(stmt.name)
            self.defer(scope, [], "inserted")
            return dunder
        return dunder | self.point_tests(block, at - 1, at, scope, "inserted")

    # ---- mapping to tests
    def lines_tests(self, lines, kinds) -> int:
        lines = list(lines)
        self.touched.update(lines)
        return self.shard.executors(lines, kinds)

    def scope_tests(self, scope: Scope) -> int:
        """Every test that can reach code in this scope."""
        self.broad = True
        if scope.kind == "f" and scope.node is not None:
            return self.shard.executors(span(scope.node), ("f",))
        return self.shard.importers

    def point_tests(self, block, before: int, after: int, scope: Scope, why: str) -> int:
        lines: list = []
        for index in (before, after):
            if 0 <= index < len(block):
                lines.extend(span(block[index]))
        if self.defer(scope, lines, why):
            return 0
        if lines and self.shard.executable(lines, scope.kinds):
            self.reasons.append(f"{why}: neighbours {lines[0]}-{lines[-1]}")
            return self.lines_tests(lines, scope.kinds)
        self.reasons.append(f"{why}: whole scope")
        return self.scope_tests(scope)

    def stmt_tests(self, stmt, block, index: int, scope: Scope, why: str, lines=None) -> int:
        lines = list(lines if lines is not None else span(stmt))
        dunder = self.dunder_class_stmt(stmt, scope)
        if self.defer(scope, lines, why):
            return dunder
        if self.shard.executable(lines, scope.kinds):
            self.reasons.append(f"{why} {scope.kind} L{lines[0]}-{lines[-1]}")
            return dunder | self.lines_tests(lines, scope.kinds)
        return dunder | self.point_tests(block, index - 1, index + 1, scope, why + " (no bytecode)")

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
        movable = self.movable_defs(old_block, new_block, scope)
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
            # Function definitions moved within a module/class body: same
            # code, same name, only position differs.
            old_pos = list(range(len(old_seg)))
            new_pos = list(range(len(new_seg)))
            moved = None
            if movable:
                mo = {k for k, key in enumerate(keys_old) if key in movable}
                mn = {k for k, key in enumerate(keys_new) if key in movable}
                if mo or mn:
                    moved = (mo, mn)
                    self.need_fp = True  # module/class values still get compared
                    for k in mo:
                        self.reasons.append(f"moved def {old_seg[k].name} L{old_seg[k].lineno}: same code")
            if moved:
                old_pos = [k for k in old_pos if k not in moved[0]]
                new_pos = [k for k in new_pos if k not in moved[1]]
                keys_old = [keys_old[k] for k in old_pos]
                keys_new = [keys_new[k] for k in new_pos]
            matcher = difflib.SequenceMatcher(None, keys_old, keys_new, autojunk=False)
            for op, a1, a2, b1, b2 in matcher.get_opcodes():
                if op == "equal":
                    continue
                if op == "replace" and a2 - a1 == b2 - b1:
                    for k in range(a2 - a1):
                        ka, kb = old_pos[a1 + k], new_pos[b1 + k]
                        a, b = old_seg[ka], new_seg[kb]
                        if type(a) is type(b):
                            self.stmt(a, b, old_block, oi + ka, scope)
                        else:
                            self.tests |= self.removed_stmt(a, old_block, oi + ka, scope, "replaced")
                            self.tests |= self.added_stmts([b], old_block, oi + ka, scope)
                    continue
                for k in range(a1, a2):
                    self.tests |= self.removed_stmt(old_seg[old_pos[k]], old_block, oi + old_pos[k], scope, "deleted")
                if b2 > b1:
                    at = oi + (old_pos[a1] if a1 < len(old_pos) else len(old_seg))
                    stmts = [new_seg[new_pos[k]] for k in range(b1, b2)]
                    if self.only_docstring_insert(stmts, at, scope):
                        continue
                    self.tests |= self.added_stmts(stmts, old_block, at, scope)

    SAFE_DECORATORS = {"property", "staticmethod", "classmethod", "cached_property", "abstractmethod"}

    def movable_defs(self, old_block, new_block, scope) -> set:
        """ast.dump keys of function definitions that may have moved within
        this import-time block without changing: identical AST occurring
        exactly once in each block, a name bound exactly once in each, and
        no decorators beyond the built-in descriptors (a registering
        decorator could depend on order). Where one actually moved, the
        fingerprint diff still compares the module/class values."""
        if not scope.import_time or not self.use_fp:
            return set()

        def bound_names(block):
            names: dict = {}
            for s in block:
                if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    names[s.name] = names.get(s.name, 0) + 1
                else:
                    for node in ast.walk(s):
                        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                            names[node.id] = names.get(node.id, 0) + 1
                        elif isinstance(node, ast.alias):
                            n = (node.asname or node.name).split(".")[0]
                            names[n] = names.get(n, 0) + 1
            return names

        def safe(fn):
            for d in fn.decorator_list:
                name = d.id if isinstance(d, ast.Name) else d.attr if isinstance(d, ast.Attribute) else None
                if name not in self.SAFE_DECORATORS:
                    return False
            return True

        def defs(block):
            out: dict = {}
            for s in block:
                if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out.setdefault(ast.dump(s), []).append(s)
            return out

        old_defs, new_defs = defs(old_block), defs(new_block)
        names_old, names_new = bound_names(old_block), bound_names(new_block)
        keys = set()
        for key, items in old_defs.items():
            if len(items) != 1 or len(new_defs.get(key, [])) != 1:
                continue
            fn = items[0]
            if safe(fn) and names_old.get(fn.name) == 1 and names_new.get(fn.name) == 1:
                keys.add(key)
        return keys

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
            if header_changed and self.future_annotations and annotations_only(a, b):
                # Under `from __future__ import annotations` parameter and
                # return annotations are strings nothing in sqlglot reads.
                self.reasons.append(f"annotation-only header L{a.lineno}: no effect")
                header_changed = False
            if header_changed:
                head = range(start_line(a), max(a.lineno, a.body[0].lineno - 1) + 1)
                # Callers see new defaults/decorators; a rename also reaches
                # functions of the new name it may now override.
                self.tests |= self.lines_tests(span(a), ("f",))
                if a.name != b.name:
                    if self.use_fp and scope.import_time:
                        self.fallback |= self.same_name(b.name)
                    else:
                        self.broad = True
                        self.tests |= self.same_name(b.name)
                self.tests |= self.stmt_tests(a, block, index, scope, "def header", head)
            self.block(a.body, b.body, inner)
            return
        if isinstance(a, ast.ClassDef):
            inner = Scope("d", a, scope)
            if header_changed:
                head = range(start_line(a), max(a.lineno, a.body[0].lineno - 1) + 1)
                if self.use_fp and scope.import_time:
                    # A new name, base or metaclass changes how everything in
                    # the class resolves: stay conservative.
                    self.reasons.append(f"class header L{a.lineno}: importers")
                    self.broad = True
                    self.tests |= self.shard.importers
                else:
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
        if (
            isinstance(a, ast.AnnAssign) and scope.kind == "f"
            and eq(a.target, b.target) and eq(a.value, b.value) and a.simple == b.simple
        ):
            # Python never evaluates annotations of local variables.
            self.reasons.append(f"local annotation L{a.lineno}: no effect")
            return
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

def has_future_annotations(tree: ast.Module) -> bool:
    for stmt in tree.body:
        if isinstance(stmt, ast.ImportFrom) and stmt.module == "__future__":
            if any(alias.name == "annotations" for alias in stmt.names):
                return True
    return False


def annotations_only(a, b) -> bool:
    """Do two function definitions differ only in annotations?"""
    def strip(node):
        args = node.args
        parts = [node.name, node.decorator_list, node.type_params if hasattr(node, "type_params") else None]
        for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]:
            parts.append(None if arg is None else arg.arg)
        parts.extend([args.defaults, args.kw_defaults])
        return parts

    return all(eq(x, y) for x, y in zip(strip(a), strip(b))) and len(strip(a)) == len(strip(b))


def split_fixture_pairs(text: str) -> list:
    """Statements as tests/helpers.py:load_sql_fixture_pairs groups them:
    comment lines and blank lines dropped, then split on ';'. Even entries
    are inputs (with their # metadata), odd ones expected outputs."""
    lines = [line for line in text.splitlines() if line and not line.startswith("--")]
    return "\n".join(lines).split(";")


def module_file(module: str, maps: "Maps") -> str | None:
    base = module.replace(".", "/")
    for rel in (base + ".py", base + "/__init__.py"):
        if rel in maps.src_names:
            return rel
    return None


def module_name(rel: str) -> str | None:
    if not rel.endswith(".py"):
        return None
    module = rel[:-3].replace("/", ".")
    if module.endswith(".__init__"):
        module = module[: -len(".__init__")]
    return module


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
        self.id_of: dict = {}
        self.ms: dict = {}
        for t in tests:
            self.all_bits |= 1 << t["number"]
            self.by_file.setdefault(t["file"], 0)
            self.by_file[t["file"]] |= 1 << t["number"]
            self.id_of[t["number"]] = t["id"]
            self.ms[t["number"]] = int(t["seconds"] * 1000 + 0.5)
        self.old_trees: dict = {}
        self.changes = None
        self.pending = {"overrides": {}, "deleted": [], "need": False, "fallback": 0}
        self.number_of = {t["id"]: t["number"] for t in tests}
        self.exec_info: list = []
        self.exec_ok = True

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

    def verify_heavy(self, bits: int, ms: dict, reasons: list) -> int:
        """Run the cheap selected tests on the patched tree; drop the heavy
        ones when every cheap test covering the change passes."""
        if not (VERIFY and VERIFY_TREE.is_dir() and self.changes is not None):
            return bits
        chosen = [n for n in ms if bits >> n & 1]
        heavy = [n for n in chosen if ms[n] >= HEAVY_MS]
        cheap = [n for n in chosen if ms[n] < HEAVY_MS]
        if not heavy or len(cheap) < VERIFY_MIN_PROXIES:
            return bits
        if str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))
        import verify  # noqa: E402  (only needed here)

        ids = {self.id_of[n]: n for n in cheap}
        result = verify.run(str(VERIFY_TREE), self.changes, sorted(ids), VERIFY_TIMEOUT)
        if not result["ok"]:
            reasons.append(f"verify: could not run ({result.get('why', '')[:60]}); keeping all")
            return bits
        failed = {ids[i] for i in result["failed"] if i in ids}
        unknown = [i for i in result["failed"] if i not in ids]
        if unknown:
            # e.g. a whole test file failed to collect: count every cheap
            # test of that file as failing.
            for i in unknown:
                for n in cheap:
                    if self.id_of[n].startswith(i):
                        failed.add(n)
        reasons.append(f"verify: {len(cheap)} cheap tests, {len(failed)} failed, "
                       f"{result['secs']:.1f}s")
        if failed:
            out = 0
            for n in failed | set(heavy):
                out |= 1 << n
            return out
        return 0

    def commit(self, patch_text: str, reasons: list) -> int:
        patches = parse_patch(patch_text)
        if not patches:
            reasons.append("no file changes found")
            return 0
        # Import-time changes are collected over the whole commit, then
        # resolved by one fingerprint run of the patched package.
        self.pending = {"overrides": {}, "deleted": [], "need": False, "fallback": 0}
        # Patched file contents (relative path -> bytes, None if deleted), for
        # running tests on the patched tree.
        self.changes = {}
        # Line-level Python changes (path, touched old lines, broad?) for the
        # TPC executor check; any other kind of change disables it.
        self.exec_info = []
        self.exec_ok = True
        self.fp_only_functions = False
        self.fp_entries = {}
        bits = 0
        for fp in patches:
            try:
                self.record_change(fp)
                bits |= self.file(fp, reasons)
            except Exception as error:  # never lose a commit to a bug here
                reasons.append(f"{fp.path}: analysis failed ({error!r}); broad fallback")
                bits |= self.broad(fp)
                self.changes = None
        if self.pending["need"]:
            bits |= self.resolve_fingerprint(reasons)
        try:
            bits = self.exec_refine(bits, reasons)
        except Exception as error:
            reasons.append(f"exec check failed ({error!r}); keeping all")
        try:
            bits = self.proxy_refine(bits, reasons)
        except Exception as error:
            reasons.append(f"proxy check failed ({error!r}); keeping all")
        try:
            bits = self.verify_heavy(bits, self.ms, reasons)
        except Exception as error:
            reasons.append(f"verify failed ({error!r}); keeping all")
        return bits

    def proxy_refine(self, bits: int, reasons: list) -> int:
        """Opt-in: replace test_max_nodes by a scaled-down run of its body
        (selector/proxy_check.py) when the commit only edits sqlglot modules
        in place and they all still compile."""
        n = self.number_of.get(MAX_NODES_TEST)
        if not (PROXY_MAX_NODES and n is not None and bits >> n & 1 and self.exec_ok and self.changes is not None):
            return bits
        if not PROXY_SCRIPT.exists() or not (PRISTINE / "sqlglot").is_dir():
            return bits
        if not BUDGET.allow(3.0):
            reasons.append("proxy check skipped: time budget")
            return bits
        request = {"root": str(PRISTINE), "overrides": self.pending["overrides"], "deleted": self.pending["deleted"]}
        env = {
            "PYTHONHASHSEED": "0",
            "PYTHONDONTWRITEBYTECODE": "1",
            "HOME": os.environ.get("HOME", "/tmp"),
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        }
        try:
            proc = subprocess.run(
                [sys.executable, "-P", "-s", str(PROXY_SCRIPT)],
                input=json.dumps(request), capture_output=True, text=True, timeout=20, env=env,
            )
            result = json.loads(proc.stdout)
        except Exception as error:
            reasons.append(f"proxy check unavailable ({error!r})")
            return bits
        if result.get("ok") and result.get("passed"):
            reasons.append("proxy: scaled test_max_nodes passed -> dropped")
            return bits & ~(1 << n)
        reasons.append(f"proxy: scaled test_max_nodes failed ({(result.get('error') or '')[:80]}) -> kept")
        return bits

    def exec_refine(self, bits: int, reasons: list) -> int:
        if not (EXEC_CHECK and EXEC_CHECK_SCRIPT.exists() and self.exec_ok and self.exec_info):
            return bits
        if not BUDGET.allow(6.0):
            reasons.append("exec check skipped: time budget")
            return bits
        if self.pending["need"] and not getattr(self, "fp_only_functions", False):
            return bits
        if any(broad for _, _, broad in self.exec_info):
            return bits
        lines = self.maps.exec_lines
        profile_path = self.maps.root / "exec_profile.json.gz"
        if not lines or not profile_path.exists() or not (PRISTINE / "sqlglot").is_dir():
            return bits
        targets = {}
        for test_id, key in EXEC_TESTS.items():
            n = self.number_of.get(test_id)
            if n is not None and bits >> n & 1 and key in lines:
                targets[key] = n
        opt_n = self.number_of.get(OPTIMIZED_TPCH)
        want_opt = opt_n is not None and bool(bits >> opt_n & 1) and "tpch" in lines
        if not targets and not want_opt:
            return bits

        def run_lines(n):
            """Changed lines this test executes at run time, per file. (What
            changed at import time is the fingerprint's business; this check
            only runs when that was nothing but function rebinding.)"""
            out = {}
            for rel, touched, _ in self.exec_info:
                shard = self.maps.shard(rel)
                if shard is None:
                    continue
                mine = {ln for ln in touched if shard.executors([ln], ("f",)) >> n & 1}
                if mine:
                    out[rel] = mine
            return out

        queries = {}
        for key, n in targets.items():
            run = run_lines(n)
            if not run:
                continue
            info = lines[key]
            affected = set()
            ok = True
            for rel, mine in run.items():
                if mine & info["D"].get(rel, set()) or not mine <= info["P"].get(rel, set()):
                    ok = False
                    break
                for i, p in enumerate(info["queries"]):
                    if mine & p.get(rel, set()):
                        affected.add(i)
            if ok and affected:
                queries[key] = sorted(affected)
        opt_ok = False
        if want_opt:
            run = run_lines(opt_n)
            info = lines["tpch"]
            opt_ok = bool(run) and all(
                not (mine & info["D"].get(rel, set())) and mine <= info["P"].get(rel, set())
                for rel, mine in run.items()
            )
        if not queries and not opt_ok:
            return bits

        request = {
            "root": str(PRISTINE),
            "overrides": self.pending["overrides"],
            "deleted": self.pending["deleted"],
            "profile": str(profile_path),
            "queries": queries,
            "optimized_tpch": opt_ok,
        }
        data_zip = self.maps.root / "data.zip"
        costs_path = self.maps.root / "exec_costs.json"
        if RERUN and "tpcds" in queries and data_zip.exists() and costs_path.exists() and BUDGET.allow(6.0 + 2 * RERUN_MAX_SECS):
            request["rerun"] = {"tpcds": {
                "data": str(data_zip),
                "prefix": "tests/fixtures/optimizer/tpc-ds/",
                "costs": json.loads(costs_path.read_text()).get("tpcds", {}),
                "max_queries": RERUN_MAX_QUERIES,
                "max_secs": RERUN_MAX_SECS,
                "extra_seeds": RERUN_EXTRA_SEEDS,
                "root": str(PRISTINE),
                "overrides": self.pending["overrides"],
                "deleted": self.pending["deleted"],
                "timeout": 30,
            }}
        env = {
            "PYTHONHASHSEED": "0",
            "PYTHONDONTWRITEBYTECODE": "1",
            "SKIP_INTEGRATION": "0",
            "HOME": os.environ.get("HOME", "/tmp"),
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        }
        try:
            proc = subprocess.run(
                [sys.executable, "-P", "-s", str(EXEC_CHECK_SCRIPT)],
                input=json.dumps(request), capture_output=True, text=True,
                timeout=EXEC_CHECK_TIMEOUT, env=env,
            )
            result = json.loads(proc.stdout)
        except Exception as error:
            reasons.append(f"exec check unavailable ({error!r})")
            return bits
        if not result.get("ok"):
            reasons.append(f"exec check: {result.get('error', '')[:80]}")
            return bits
        for key, note in (result.get("rerun") or {}).items():
            reasons.append(f"exec check: {key} re-ran queries {note['tried']}, still different {note['still']}")
        for key, idx in queries.items():
            if not result["changed"].get(key):
                bits &= ~(1 << targets[key])
                reasons.append(f"exec check: {key} pipeline unchanged for {len(idx)} queries -> dropped")
            else:
                reasons.append(f"exec check: {key} changed queries {result['changed'][key][:5]}")
        if opt_ok and not result.get("optimized_tpch_changed"):
            bits &= ~(1 << opt_n)
            reasons.append("exec check: optimized_tpch duckdb SQL unchanged -> dropped")
        return bits

    def record_change(self, fp: FilePatch) -> None:
        """Keep the patched content of every file, for verification runs."""
        if self.changes is None:
            return
        if fp.binary:
            self.changes = None
            return
        old_rel, new_rel = fp.old_path, fp.new_path
        if fp.is_deleted:
            self.changes[old_rel or fp.path] = None
            return
        if old_rel and new_rel and old_rel != new_rel:
            self.changes[old_rel] = None
        if fp.is_new:
            text = "".join(t + "\n" for h in fp.hunks for tag, t in h.lines if tag == "+")
            self.changes[new_rel or fp.path] = text.encode("utf-8")
            return
        source = self.maps.source(old_rel or fp.path)
        if source is None:
            self.changes = None
            return
        if not fp.hunks:
            self.changes[new_rel or fp.path] = source
            return
        applied = apply_hunks(source.decode("utf-8", "surrogateescape"), fp.hunks)
        if applied is None:
            self.changes = None
            return
        self.changes[new_rel or fp.path] = applied.new_text.encode("utf-8", "surrogateescape")

    @property
    def fp_available(self) -> bool:  # noqa: D401
        """Fingerprint machinery present (built into the image) and time left."""
        return (
            USE_FP and FP_BASELINE.exists() and (PRISTINE / "sqlglot").is_dir()
            and FP_SCRIPT.exists() and BUDGET.allow(2.0)
        )

    def resolve_fingerprint(self, reasons: list) -> int:
        pending = self.pending
        request = {
            "root": str(PRISTINE),
            "overrides": pending["overrides"],
            "deleted": pending["deleted"],
            "baseline": str(FP_BASELINE),
        }
        env = {
            "PYTHONHASHSEED": "0",
            "PYTHONDONTWRITEBYTECODE": "1",
            "HOME": os.environ.get("HOME", "/tmp"),
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        }
        try:
            proc = subprocess.run(
                [sys.executable, "-P", "-s", str(FP_SCRIPT)],
                input=json.dumps(request), capture_output=True, text=True,
                timeout=FP_TIMEOUT, env=env,
            )
            result = json.loads(proc.stdout)
        except Exception as error:
            reasons.append(f"fingerprint unavailable ({error!r}); import-time fallback")
            return pending["fallback"]
        if not result.get("ok"):
            reasons.append(f"patched sqlglot does not import ({result.get('error', '')[:80]}); fallback")
            bits = pending["fallback"]
            for module in pending["overrides"]:
                bits |= self.module_importers(module)
            return bits
        bits = 0
        self.fp_entries = result.get("entries", {})
        changed_keys = set(result.get("changed", [])) | set(result.get("added", []))
        # Only functions rebound or given new defaults/decorators: their
        # effect flows through calls, which the TPC exec check re-runs.
        self.fp_only_functions = (
            not result.get("failed") and not result.get("removed")
            and bool(changed_keys) and changed_keys <= set(result.get("functions", []))
        )
        for name, kind, _message in result.get("failed", []):
            bits |= self.module_importers(name)
            reasons.append(f"fingerprint: {name} no longer imports ({kind})")
        for key in result.get("changed", []) + result.get("removed", []):
            bits |= self.key_tests(key, added=False)
        for key in result.get("added", []):
            bits |= self.key_tests(key, added=True)
        changed = result.get("changed", []) + result.get("added", []) + result.get("removed", [])
        reasons.append(f"fingerprint: {len(changed)} changed values {changed[:4]}")
        return bits

    def module_importers(self, module: str) -> int:
        base = module.replace(".", "/")
        for rel in (base + ".py", base + "/__init__.py"):
            shard = self.maps.shard(rel)
            if shard is not None:
                return shard.importers
        return 0

    def key_tests(self, key: str, added: bool) -> int:
        module, qual, name = key.split("|", 2)
        rel = module_file(module, self.maps)
        bits = 0
        if qual:
            if name == "__bases__":
                return self.module_importers(module)
            if key in self.maps.attrs:
                # A data attribute: the tests that read it at run time, or for
                # a table whose changed entries are known, those that looked
                # up one of them or used the whole table.
                entries = getattr(self, "fp_entries", {}).get(key)
                bits |= self.maps.table_readers(key, entries)
            elif not added:
                # Never read as data at run time: a method whose defaults or
                # decorators changed (or an unused constant): its callers.
                bits |= self.maps.same_name_tests(name, only_file=rel)
            if added:
                # A new attribute or method may shadow one inherited from a
                # parent, for the tests that use this class at all.
                users = self.maps.class_users(module, qual)
                inherited = self.maps.attr_readers_named(name) | self.maps.same_name_tests(name)
                bits |= (inherited & users) if users else inherited
            return bits
        # Module global: functions that read it (own module or importers), and
        # functions of that name in the same module.
        return self.maps.ref_tests(name, module) | self.maps.same_name_tests(name, only_file=rel)

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
        if not (
            rel.startswith("sqlglot/") and rel.endswith(".py") and old_rel == new_rel
            and not fp.is_new and not fp.is_deleted and not fp.binary
        ):
            self.exec_ok = False  # only in-place edits of sqlglot modules qualify

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
            module = module_name(target)
            if module and module.startswith("sqlglot"):
                self.pending["deleted"].append(module)
            if fp.is_deleted:
                return bits
        if fp.is_new or renamed:
            target = new_rel or rel
            bits |= self.maps.listers(posixpath.dirname(target))
            bits |= self.maps.readers(target)
            bits |= self.maps.import_attempts(target)
            reasons.append(f"{target}: added")
            module = module_name(target)
            if module and module.startswith("sqlglot") and fp.hunks:
                # Imported by name from changed code, or registering itself.
                self.pending["overrides"][module] = "".join(
                    t + "\n" for h in fp.hunks for tag, t in h.lines if tag == "+"
                )
                self.pending["need"] = self.pending["need"] or self.fp_available
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
        readers = self.fixture_refine(rel, old_source, fp, readers, reasons)
        reasons.append(f"{rel}: data file, {bin(readers).count('1')} readers")
        return readers

    def fixture_refine(self, rel: str, old_source: bytes, fp: FilePatch, readers: int, reasons: list) -> int:
        """Drop heavy readers that were measured not to depend on the part of
        a SQL pair fixture this patch changes (tools/mapbuild/fixture_deps.py)."""
        deps = self.maps.fixture_deps.get(rel)
        if not deps:
            return readers
        old_text = old_source.decode("utf-8", "replace")
        applied = apply_hunks(old_text, fp.hunks)
        if applied is None:
            return readers
        old_parts, new_parts = split_fixture_pairs(old_text), split_fixture_pairs(applied.new_text)
        expected_only = len(old_parts) == len(new_parts) and all(
            i % 2 == 1 for i, (a, b) in enumerate(zip(old_parts, new_parts)) if a != b
        )
        for number, dep in deps.items():
            n = int(number)
            if not readers >> n & 1:
                continue
            if dep == "none" or (dep == "inputs" and expected_only):
                readers &= ~(1 << n)
                reasons.append(f"{rel}: test {n} does not use the changed part ({dep})")
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
            self.exec_ok = False
            return shard.importers | readers
        old_tree = self.old_tree(rel, old_source)
        module = module_name(rel)
        importable = bool(module and module.startswith("sqlglot."))
        if importable:
            # Every changed module is imported patched, so values other
            # modules derive from it at import time come out right too.
            self.pending["overrides"][module] = applied.new_text
        differ = Differ(
            shard, applied, reasons,
            use_fp=importable and self.fp_available,
            same_name=self.maps.same_name_tests,
        )
        differ.future_annotations = has_future_annotations(old_tree) and has_future_annotations(new_tree)
        differ.block(old_tree.body, new_tree.body, Scope("d", None, None))
        if differ.need_fp:
            self.pending["need"] = True
            self.pending["fallback"] |= differ.fallback
        if rel.startswith("sqlglot/"):
            self.exec_info.append((rel, differ.touched, differ.broad or bool(readers)))
        else:
            self.exec_ok = False
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
    global BUDGET
    tests = json.loads((DATA / "tests.json").read_text("utf-8"))["tests"]
    ms = {t["number"]: int(t["seconds"] * 1000 + 0.5) for t in tests}
    valid_numbers = set(ms)
    patches = sorted((DATA / "commits").glob("*.patch"))
    BUDGET = Budget(len(patches))
    maps = Maps(MAPS)
    selector = Selector(maps, tests)
    out = sys.stdout
    total_cost = 0
    for patch_path in patches:
        commit = patch_path.stem
        reasons: list = []
        commit_started = time.monotonic()
        try:
            text = patch_path.read_bytes().decode("utf-8", "replace")
            bits = selector.commit(text, reasons)
        except Exception as error:
            reasons.append(f"selector failed ({error!r}); running everything")
            bits = selector.all_bits
        BUDGET.done += 1
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
        log(f"{commit}: {len(chosen)} tests, {len(workers)} workers, {cost/1000:.2f}s "
            f"[{time.monotonic() - commit_started:.2f}s] | " + "; ".join(reasons[:6]))
    out.flush()
    log(f"total simulated {total_cost/1000:.1f}s over {len(patches)} commits, "
        f"{time.monotonic()-STARTED:.2f}s wall")
    return 0


if __name__ == "__main__":
    sys.exit(main())
