"""Per-process execution tracer used only when building the dependency maps.

It is imported from a .pth file at interpreter startup, so it is active in
every Python process of a traced test: the pytest process, multiprocessing
"spawn" children and `python -c` subprocesses. It does nothing unless
BLAST_TRACE_DIR is set.

For files under BLAST_TRACE_ROOT it records:
  - lines executed, per code object, and whether the code object is
    function-like (CO_OPTIMIZED: functions, lambdas, comprehensions,
    generator expressions) or a module/class body that runs at definition
    time;
  - files opened, split by who opened them (the import system, linecache for
    tracebacks, or anything else, e.g. a test reading a fixture);
  - directories listed (pkgutil, glob, iterdir...).

Each process writes BLAST_TRACE_DIR/<pid>.json.gz when it exits.
"""

import os


def _install(out_dir: str) -> None:
    import atexit
    import gzip
    import json
    import sys

    root = os.environ.get("BLAST_TRACE_ROOT", "/src/")
    mon = sys.monitoring
    tool = 4
    try:
        mon.use_tool_id(tool, "blast-trace")
    except ValueError:
        return

    executed: dict = {}

    def on_line(code, line):
        if code.co_filename.startswith(root):
            seen = executed.get(code)
            if seen is None:
                seen = executed[code] = set()
            seen.add(line)
        # Each location reports once per process; the process is one test.
        return mon.DISABLE

    def on_start(code, offset):
        # A function whose whole body sits on its `def`/`lambda` line (e.g.
        # `exp.X: lambda *_: "VIRTUAL"` in a TRANSFORMS table) produces no
        # LINE event at all: its only instructions are on the line where it
        # starts. Record the start line so such calls are not invisible.
        if code.co_filename.startswith(root):
            seen = executed.get(code)
            if seen is None:
                seen = executed[code] = set()
            seen.add(code.co_firstlineno)
        return mon.DISABLE

    mon.register_callback(tool, mon.events.LINE, on_line)
    mon.register_callback(tool, mon.events.PY_START, on_start)
    mon.set_events(tool, mon.events.LINE | mon.events.PY_START)

    reads: dict = {}
    listed: set = set()
    imported: set = set()
    getframe = sys._getframe
    normpath = os.path.normpath
    join = os.path.join
    getcwd = os.getcwd

    def resolve(path):
        if isinstance(path, bytes):
            try:
                path = path.decode()
            except UnicodeDecodeError:
                return None
        if not isinstance(path, str):
            return None
        if not path.startswith("/"):
            path = join(getcwd(), path)
        path = normpath(path)
        return path if path.startswith(root) else None

    import sysconfig

    stdlib = sysconfig.get_paths()["stdlib"].rstrip("/") + "/"

    def caller_kind(frame) -> str:
        """Who asked: the nearest caller that is not a stdlib helper (io,
        pathlib, gzip, pkgutil...). The import system and pytest's own
        machinery (config, assertion rewriting) are not data dependencies."""
        depth = 0
        while frame is not None and depth < 40:
            name = frame.f_code.co_filename
            if name.startswith("<frozen importlib") or name.startswith("<frozen zipimport"):
                return "import"
            if name.startswith(stdlib) and "/site-packages/" not in name:
                if name.endswith("/linecache.py"):
                    return "linecache"
                frame = frame.f_back
                depth += 1
                continue
            if name.startswith("<frozen"):
                frame = frame.f_back
                depth += 1
                continue
            if "/_pytest/" in name or "/pluggy/" in name:
                return "pytest"
            return "data"
        return "data"

    def audit(event, args):
        if event == "open":
            path = resolve(args[0])
            if path is None:
                return
            mode = args[1] if len(args) > 1 else None
            if isinstance(mode, str) and any(c in mode for c in "wax+"):
                kind = "write"
            else:
                kind = caller_kind(getframe(1))
                if kind == "pytest":
                    # pytest reading its config files or rewriting a test
                    # module it imports: config files are handled specially
                    # by the selector, imports by the coverage maps.
                    kind = "pytest"
            kinds = reads.get(path)
            if kinds is None:
                kinds = reads[path] = set()
            kinds.add(kind)
        elif event in ("os.listdir", "os.scandir"):
            path = resolve(args[0] if args else ".")
            if path is None:
                return
            # The import system lists every package directory it searches
            # and pytest lists the test tree; only listings made by other
            # code (pkgutil in a test, glob, sqlglot itself) count.
            if caller_kind(getframe(1)) == "data":
                listed.add(path)
        elif event == "import":
            # Raised for every import of a module not yet in sys.modules,
            # including attempts that fail (e.g. optional modules).
            if args and isinstance(args[0], str):
                imported.add(args[0])

    sys.addaudithook(audit)

    attr_reads: set = set()
    if os.environ.get("BLAST_TRACE_ATTRS") == "1":
        _install_attr_recorder(attr_reads)

    def dump():
        mon.set_events(tool, 0)
        code_records = [
            [
                code.co_filename,
                code.co_qualname,
                code.co_firstlineno,
                1 if code.co_flags & 0x1 else 0,
                sorted(lines),
            ]
            for code, lines in list(executed.items())
        ]
        data = {
            "pid": os.getpid(),
            "argv": sys.argv[:4],
            "code": code_records,
            "reads": {path: sorted(kinds) for path, kinds in reads.items()},
            "listed": sorted(listed),
            "imported": sorted(imported),
            "attrs": sorted("|".join(key) for key in attr_reads),
        }
        os.makedirs(out_dir, exist_ok=True)
        final = join(out_dir, f"{os.getpid()}.json.gz")
        temp = final + ".tmp"
        with gzip.open(temp, "wt", compresslevel=3) as handle:
            json.dump(data, handle)
        os.replace(temp, final)

    atexit.register(dump)


def key_id(key) -> str:
    """Stable text for a table key, identical across processes. Must match
    selector/fingerprint.py:key_id."""
    import enum

    if isinstance(key, str):
        return "s:" + key
    if isinstance(key, bool) or key is None or isinstance(key, (int, float)):
        return "r:" + repr(key)
    if isinstance(key, enum.Enum):
        return f"e:{type(key).__qualname__}.{key.name}"
    if isinstance(key, type):
        return f"t:{key.__module__}.{key.__qualname__}"
    if isinstance(key, tuple):
        return "u:(" + "|".join(key_id(k) for k in key) + ")"
    return "o:" + type(key).__qualname__


def _install_attr_recorder(reads: set) -> None:
    """Record which class-level data attributes of sqlglot classes each test
    reads at run time (not while modules are being imported).

    After every import finishes, the data attributes (anything that is not a
    descriptor: dicts, sets, numbers, strings, classes...) of every sqlglot
    class are replaced by a non-data descriptor that returns the same object
    and notes the first read. Instance attributes still shadow it, so
    behaviour is unchanged. Reads made while an import is running are
    definition-time derivations; the selector finds those by comparing
    fingerprints of the old and new class attributes instead.
    """
    import builtins
    import enum
    import importlib
    import sys

    import weakref

    depth = [0]
    # id -> weakref: ids of garbage-collected classes get reused, so an id
    # alone would make a new class (say TSQLParser) look already wrapped.
    wrapped: dict = {}
    module_count = [0]

    def is_wrapped(cls) -> bool:
        ref = wrapped.get(id(cls))
        return ref is not None and ref() is cls

    class Rec:
        __slots__ = ("value", "key", "hit")

        def __init__(self, value, key):
            self.value = value
            self.key = key
            self.hit = False

        def __get__(self, obj, owner=None):
            if not self.hit and not depth[0]:
                self.hit = True
                reads.add(self.key)
            return self.value

    # Tables: which keys a test looks up (hits and misses), "*" for anything
    # that depends on the whole table. Recorded as "module|Qual|attr|key".
    def note(tag, key):
        if not depth[0]:
            try:
                reads.add(tag + (key_id(key),))
            except Exception:
                reads.add(tag + ("*",))

    def note_all(tag):
        if not depth[0]:
            reads.add(tag + ("*",))

    class KeyDict(dict):
        __slots__ = ("_tag",)

        def __getitem__(self, key):
            note(self._tag, key)
            return dict.__getitem__(self, key)

        def get(self, key, default=None):
            note(self._tag, key)
            return dict.get(self, key, default)

        def __contains__(self, key):
            note(self._tag, key)
            return dict.__contains__(self, key)

        def setdefault(self, key, default=None):
            note(self._tag, key)
            return dict.setdefault(self, key, default)

        def pop(self, key, *default):
            note(self._tag, key)
            return dict.pop(self, key, *default)

        def __setitem__(self, key, value):
            note(self._tag, key)
            dict.__setitem__(self, key, value)

        def __delitem__(self, key):
            note(self._tag, key)
            dict.__delitem__(self, key)

        def __iter__(self):
            note_all(self._tag)
            return dict.__iter__(self)

        def keys(self):
            note_all(self._tag)
            return dict.keys(self)

        def values(self):
            note_all(self._tag)
            return dict.values(self)

        def items(self):
            note_all(self._tag)
            return dict.items(self)

        def copy(self):
            note_all(self._tag)
            return dict(dict.items(self))

        def __eq__(self, other):
            note_all(self._tag)
            return dict.__eq__(self, other)

        def __ne__(self, other):
            note_all(self._tag)
            return dict.__ne__(self, other)

        __hash__ = None

        def __or__(self, other):
            note_all(self._tag)
            return dict(dict.items(self)) | other

        def __ror__(self, other):
            note_all(self._tag)
            return dict(other) | dict(dict.items(self))

        def __repr__(self):
            note_all(self._tag)
            return dict.__repr__(self)

        def __reduce__(self):
            note_all(self._tag)
            return (dict, (dict(dict.items(self)),))

        def update(self, *args, **kwargs):
            note_all(self._tag)
            dict.update(self, *args, **kwargs)

        def popitem(self):
            note_all(self._tag)
            return dict.popitem(self)

        def clear(self):
            note_all(self._tag)
            dict.clear(self)

    class KeySet(set):
        __slots__ = ("_tag",)

        def __contains__(self, key):
            note(self._tag, key)
            return set.__contains__(self, key)

        def __iter__(self):
            note_all(self._tag)
            return set.__iter__(self)

        def __reduce__(self):
            note_all(self._tag)
            return (set, (set(set.__iter__(self)),))

        def __repr__(self):
            note_all(self._tag)
            return set.__repr__(self)

    for _name in ("copy", "union", "intersection", "difference", "symmetric_difference",
                  "issubset", "issuperset", "isdisjoint", "__or__", "__and__", "__sub__",
                  "__xor__", "__ror__", "__rand__", "__rsub__", "__rxor__", "__eq__", "__ne__",
                  "__le__", "__lt__", "__ge__", "__gt__", "update", "add", "discard", "remove",
                  "pop", "clear", "difference_update", "intersection_update",
                  "symmetric_difference_update", "__ior__", "__iand__", "__isub__", "__ixor__"):
        def _make(name):
            base = getattr(set, name)

            def method(self, *args, **kwargs):
                note_all(self._tag)
                return base(self, *args, **kwargs)

            method.__name__ = name
            return method

        setattr(KeySet, _name, _make(_name))
    KeySet.__hash__ = None

    def tracked_table(value, tag):
        if type(value) is dict:
            table = KeyDict(value)
        elif type(value) is set:
            table = KeySet(value)
        else:
            return value
        table._tag = tag
        return table

    def wrap(cls) -> None:
        try:
            wrapped[id(cls)] = weakref.ref(cls)
        except TypeError:
            return
        module = cls.__dict__.get("__module__")
        if not isinstance(module, str) or not module.startswith("sqlglot"):
            return
        if isinstance(cls, enum.EnumMeta):
            return
        qual = cls.__qualname__
        for name, value in list(cls.__dict__.items()):
            if name.startswith("__") and name.endswith("__"):
                continue
            if isinstance(value, Rec) or hasattr(type(value), "__get__"):
                continue
            key = (module, qual, name)
            if name.isupper() and os.environ.get("BLAST_TRACE_KEYS") == "1":
                value = tracked_table(value, key)
            try:
                type.__setattr__(cls, name, Rec(value, key))
            except (TypeError, AttributeError):
                pass

    def scan(force: bool = False) -> None:
        names = [name for name in list(sys.modules) if name.startswith("sqlglot")]
        if not force and len(names) == module_count[0]:
            return
        module_count[0] = len(names)
        # Start from the classes sqlglot's modules define; their subclasses
        # (including ones a metaclass made with type(...)) are reached below.
        stack = []
        for name in names:
            module = sys.modules.get(name)
            if module is None:
                continue
            for value in list(vars(module).values()):
                if isinstance(value, type):
                    stack.append(value)
        seen = set()
        while stack:
            cls = stack.pop()
            if id(cls) in seen:
                continue
            seen.add(id(cls))
            if not is_wrapped(cls):
                wrap(cls)
            try:
                subs = type.__subclasses__(cls)
            except TypeError:
                continue
            for sub in subs:
                if id(sub) not in seen:
                    stack.append(sub)
            # Nested classes (e.g. Snowflake.Tokenizer) and classes held in
            # class attributes (parser_class...) are reachable through dicts.
            for value in list(cls.__dict__.values()):
                inner = value.value if isinstance(value, Rec) else value
                if isinstance(inner, type) and id(inner) not in seen:
                    stack.append(inner)

    original_import = builtins.__import__
    original_import_module = importlib.import_module
    original_build_class = builtins.__build_class__

    def is_sqlglot(name, globs, level) -> bool:
        # Only sqlglot's own imports are "definition time": a test module
        # that copies sqlglot data at import depends on it like a test does.
        if level and isinstance(globs, dict):
            package = globs.get("__package__") or globs.get("__name__") or ""
            return isinstance(package, str) and package.startswith("sqlglot")
        return isinstance(name, str) and name.startswith("sqlglot")

    def tracked_import(name, globals=None, locals=None, fromlist=(), level=0):
        counted = is_sqlglot(name, globals, level)
        if counted:
            depth[0] += 1
        try:
            return original_import(name, globals, locals, fromlist, level)
        finally:
            if counted:
                depth[0] -= 1
            if not depth[0]:
                scan()

    def tracked_import_module(name, package=None):
        counted = is_sqlglot(name if not name.startswith(".") else (package or ""), None, 0)
        if counted:
            depth[0] += 1
        try:
            return original_import_module(name, package)
        finally:
            if counted:
                depth[0] -= 1
            if not depth[0]:
                scan()

    def tracked_build_class(*args, **kwargs):
        cls = original_build_class(*args, **kwargs)
        if not depth[0] and isinstance(cls, type):
            # A class made at run time (e.g. a dialect defined inside a test)
            # can create more sqlglot classes through its metaclass.
            if any(
                isinstance(getattr(base, "__module__", None), str) and base.__module__.startswith("sqlglot")
                for base in cls.__mro__[1:]
            ):
                scan(force=True)
        return cls

    builtins.__import__ = tracked_import
    importlib.import_module = tracked_import_module
    builtins.__build_class__ = tracked_build_class


_out = os.environ.get("BLAST_TRACE_DIR")
if _out:
    _install(_out)

_cmp_out = os.environ.get("BLAST_CMP_DIR")
if _cmp_out:
    import cmp_trace

    cmp_trace.install(_cmp_out)
