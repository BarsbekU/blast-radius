"""Fingerprint sqlglot's class attributes and module-level data.

Used twice:
  * when building the maps, on the pristine checkout (the baseline);
  * by the selector, in a subprocess, on the patched code, to learn exactly
    which class attributes / module globals a definition-time change altered
    - including values derived from it elsewhere (metaclasses,
    __init_subclass__, subclasses that copy and extend a parent's table).

    python fingerprint.py < request.json > response.json

request:  {"root": dir holding the sqlglot package,
           "overrides": {module: new source}, "deleted": [module, ...],
           "baseline": path of the baseline json.gz (optional: then only
           differences are returned)}
response: {"ok": true, "fp": {...}} or {"ok": true, "changed": [...], "added": [...],
           "removed": [...], "failed": [...]} or {"ok": false, "error": ...}

Keys are "module|Qual.Name|attr" for class attributes and "module||name"
for module globals. Functions are identified by name; lambdas and closures
(made at definition time, e.g. by a factory in a table) by their code,
defaults and closure values. Changes inside named function bodies are the
line-coverage maps' job, not this one's.
"""

from __future__ import annotations

import enum
import functools
import gzip
import hashlib
import importlib
import importlib.abc
import importlib.util
import json
import pkgutil
import re
import sys
import types

ADDRESS = re.compile(r" at 0x[0-9a-fA-F]+")


def digest(*parts) -> str:
    h = hashlib.blake2b(digest_size=8)
    for part in parts:
        if isinstance(part, bytes):
            h.update(part)
        else:
            h.update(str(part).encode("utf-8", "surrogatepass"))
        h.update(b"\x00")
    return h.hexdigest()


class Fingerprinter:
    def __init__(self):
        self.memo: dict = {}
        self.active: set = set()

    def code(self, code: types.CodeType) -> str:
        consts = []
        for const in code.co_consts:
            if isinstance(const, types.CodeType):
                consts.append(self.code(const))
            else:
                consts.append(self.value(const))
        return digest(
            "code", code.co_name, code.co_qualname, code.co_code, code.co_names,
            code.co_varnames, code.co_freevars, code.co_cellvars, code.co_flags,
            code.co_argcount, code.co_kwonlyargcount, code.co_posonlyargcount,
            code.co_exceptiontable, *consts,
        )

    def function(self, fn) -> str:
        qual = getattr(fn, "__qualname__", "")
        parts = ["func", getattr(fn, "__module__", ""), qual]
        if "<lambda>" in qual or "<locals>" in qual:
            parts.append(self.code(fn.__code__))
            cells = []
            for cell in fn.__closure__ or ():
                try:
                    cells.append(self.value(cell.cell_contents))
                except ValueError:
                    cells.append("empty")
            parts.extend(cells)
        parts.append(self.value(fn.__defaults__))
        parts.append(self.value(fn.__kwdefaults__))
        return digest(*parts)

    def value(self, v, depth: int = 0) -> str:
        t = type(v)
        if v is None or t in (bool, int, float, complex, str, bytes):
            return digest(t.__name__, repr(v))
        key = id(v)
        cached = self.memo.get(key)
        if cached is not None:
            return cached
        if key in self.active:
            return "cycle"
        if depth > 60:
            return "deep"
        self.active.add(key)
        try:
            result = self._value(v, t, depth + 1)
        finally:
            self.active.discard(key)
        self.memo[key] = result
        return result

    def _value(self, v, t, depth: int) -> str:
        val = self.value
        if t in (tuple, list):
            parts = [val(x, depth) for x in v]
            if v and all(isinstance(x, type) for x in v):
                # e.g. tuple({exp.Add, exp.Date, ...}) for isinstance checks:
                # built from a set of classes, so the order is arbitrary.
                parts.sort()
            return digest(t.__name__, *parts)
        if t in (set, frozenset):
            return digest(t.__name__, *sorted(val(x, depth) for x in v))
        if isinstance(v, dict):
            # Contents, not order: many sqlglot tables are built by iterating
            # sets of classes, whose order follows memory addresses and so
            # differs between processes even with a fixed hash seed.
            extra = getattr(v, "default_factory", None)
            return digest(
                "dict", t.__qualname__, val(extra, depth) if extra is not None else "",
                *sorted(val(k, depth) + ":" + val(x, depth) for k, x in v.items()),
            )
        if isinstance(v, enum.Enum):
            return digest("enum", type(v).__module__, type(v).__qualname__, v.name)
        if isinstance(v, type):
            return digest("type", getattr(v, "__module__", ""), getattr(v, "__qualname__", ""))
        if isinstance(v, types.FunctionType):
            return self.function(v)
        if isinstance(v, types.MethodType):
            owner = v.__self__
            owner_fp = val(owner, depth) if isinstance(owner, type) else type(owner).__qualname__
            return digest("method", self.function(v.__func__), owner_fp)
        if isinstance(v, (types.BuiltinFunctionType, types.BuiltinMethodType)):
            return digest("builtin", getattr(v, "__module__", ""), getattr(v, "__qualname__", repr(v)))
        if isinstance(v, functools.partial):
            return digest("partial", val(v.func, depth), val(v.args, depth), val(v.keywords, depth))
        if isinstance(v, re.Pattern):
            return digest("re", v.pattern, v.flags)
        if isinstance(v, types.ModuleType):
            return digest("module", v.__name__)
        if isinstance(v, types.CodeType):
            return self.code(v)
        if isinstance(v, (staticmethod, classmethod)):
            return digest(t.__name__, val(v.__func__, depth))
        if isinstance(v, property):
            return digest("property", val(v.fget, depth), val(v.fset, depth), val(v.fdel, depth))
        state = getattr(v, "__dict__", None)
        if isinstance(state, dict):
            return digest("obj", t.__module__, t.__qualname__, val(state, depth))
        slots = []
        for klass in t.__mro__:
            names = klass.__dict__.get("__slots__", ())
            if isinstance(names, str):
                names = (names,)
            for name in names:
                slots.append(name + "=" + val(getattr(v, name, None), depth))
        if slots:
            return digest("slots", t.__module__, t.__qualname__, *slots)
        return digest("repr", t.__module__, t.__qualname__, ADDRESS.sub("", repr(v))[:2000])


def key_id(key) -> str:
    """Stable text for a table key. Must match
    tools/mapbuild/blast_trace.py:key_id (what the tracer recorded)."""
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


ELEMENTS: dict = {}  # "mod|Qual|ATTR" -> {key id: fingerprint of the entry}
TABLES: dict = {}  # "mod|Qual|ATTR" -> the table, for computing ELEMENTS on demand


def table_elements(fingerprinter: "Fingerprinter", value) -> dict:
    """Per-entry fingerprints of a dict/set table, so a change can be
    narrowed to the entries that differ."""
    if type(value) is dict:
        table = {}
        for k, v in value.items():
            kid = key_id(k)
            fp = fingerprinter.value(v)
            table[kid] = fp if kid not in table else digest(table[kid], fp)
        return table
    return {key_id(e): "1" for e in value}


def collect(fingerprinter: Fingerprinter) -> dict:
    out: dict = {}
    seen_classes: set = set()

    def elements(key: str, name: str, value) -> None:
        if name.isupper() and type(value) in (dict, set, frozenset):
            TABLES[key] = value

    def visit_class(cls, module: str) -> None:
        if id(cls) in seen_classes:
            return
        seen_classes.add(id(cls))
        qual = cls.__qualname__
        for name, value in list(cls.__dict__.items()):
            if name.startswith("__") and name.endswith("__") and name not in ("__slots__",):
                continue
            # Methods are fingerprinted by identity and defaults only (see
            # Fingerprinter.function): edits inside their bodies are the
            # line-coverage maps' job, rebinding or new defaults are this one's.
            if isinstance(value, type):
                inner_module = value.__dict__.get("__module__")
                if inner_module == module or (isinstance(inner_module, str) and inner_module.startswith("sqlglot")):
                    visit_class(value, inner_module)
                out[f"{module}|{qual}|{name}"] = fingerprinter.value(value)
                continue
            try:
                out[f"{module}|{qual}|{name}"] = fingerprinter.value(value)
                elements(f"{module}|{qual}|{name}", name, value)
            except Exception as error:  # pragma: no cover - be robust
                out[f"{module}|{qual}|{name}"] = "error:" + type(error).__name__
        # Inherited-from list matters when attributes are added or removed.
        out[f"{module}|{qual}|__bases__"] = digest(*[
            f"{getattr(b, '__module__', '')}.{getattr(b, '__qualname__', '')}" for b in cls.__bases__
        ])

    for modname in sorted(m for m in sys.modules if m.startswith("sqlglot")):
        module = sys.modules.get(modname)
        if module is None:
            continue
        for name, value in sorted(vars(module).items()):
            if name.startswith("__") and name.endswith("__"):
                continue
            if isinstance(value, types.ModuleType):
                continue
            if isinstance(value, type):
                owner = value.__dict__.get("__module__")
                if owner == modname:
                    visit_class(value, modname)
                # Which class the name is bound to (identity only).
                out[f"{modname}||{name}"] = fingerprinter.value(value)
                continue
            try:
                out[f"{modname}||{name}"] = fingerprinter.value(value)
            except Exception as error:  # pragma: no cover
                out[f"{modname}||{name}"] = "error:" + type(error).__name__
    # Classes made by metaclasses with type(...) (e.g. a dialect's default
    # Tokenizer subclass) are reachable only through other classes.
    stack = [object]
    seen: set = set()
    while stack:
        cls = stack.pop()
        try:
            subs = type.__subclasses__(cls)
        except TypeError:
            continue
        for sub in subs:
            if id(sub) in seen:
                continue
            seen.add(id(sub))
            stack.append(sub)
            module = sub.__dict__.get("__module__")
            if isinstance(module, str) and module.startswith("sqlglot") and id(sub) not in seen_classes:
                visit_class(sub, module)
    return out


class _Loader(importlib.abc.Loader):
    def __init__(self, source: str, origin: str):
        self.source = source
        self.origin = origin

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        module.__file__ = self.origin
        code = compile(self.source, self.origin, "exec", dont_inherit=True)
        exec(code, module.__dict__)


class _Finder(importlib.abc.MetaPathFinder):
    def __init__(self, overrides: dict, deleted: set):
        self.overrides = overrides
        self.deleted = deleted

    def find_spec(self, name, path, target=None):
        if name in self.deleted:
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)
        if name not in self.overrides:
            return None
        original = None
        for finder in sys.meta_path:
            if finder is self or not hasattr(finder, "find_spec"):
                continue
            original = finder.find_spec(name, path, target)
            if original is not None:
                break
        origin = original.origin if original is not None else f"<patched {name}>"
        is_package = bool(original is not None and original.submodule_search_locations is not None)
        if original is None and name.endswith(".__init__"):
            is_package = True
        spec = importlib.util.spec_from_loader(
            name, _Loader(self.overrides[name], origin), origin=origin, is_package=is_package
        )
        if is_package and original is not None:
            spec.submodule_search_locations = original.submodule_search_locations
        spec.has_location = True
        return spec


def import_everything(root: str, overrides: dict, deleted: set) -> list:
    sys.path.insert(0, root)
    if overrides or deleted:
        sys.meta_path.insert(0, _Finder(overrides, deleted))
    failed = []
    import sqlglot  # noqa: F401  (a failure here is reported by the caller)

    names = set()
    for info in pkgutil.walk_packages(sys.modules["sqlglot"].__path__, prefix="sqlglot."):
        names.add(info.name)
    names.update(m for m in overrides if m.startswith("sqlglot"))
    names -= set(deleted)
    names.discard("sqlglot.__main__")
    for name in sorted(names):
        try:
            importlib.import_module(name)
        except BaseException as error:  # noqa: BLE001 - report anything
            failed.append([name, type(error).__name__, str(error)[:300]])
    return failed


def main() -> int:
    request = json.load(sys.stdin)
    overrides = request.get("overrides", {})
    deleted = set(request.get("deleted", []))
    try:
        failed = import_everything(request["root"], overrides, deleted)
    except BaseException as error:  # noqa: BLE001
        json.dump({"ok": False, "error": f"{type(error).__name__}: {str(error)[:500]}"}, sys.stdout)
        return 0
    fingerprinter = Fingerprinter()
    fps = collect(fingerprinter)
    callables = function_keys()
    baseline_path = request.get("baseline")
    if not baseline_path:
        # The baseline needs every table's entries; a comparison only the
        # changed ones (below).
        for key, value in TABLES.items():
            ELEMENTS[key] = table_elements(fingerprinter, value)
        json.dump({"ok": True, "fp": fps, "failed": failed, "elements": ELEMENTS}, sys.stdout)
        return 0
    with gzip.open(baseline_path, "rt") as handle:
        baseline = json.load(handle)
    base_fp = baseline["fp"]
    base_elements = baseline.get("elements", {})
    base_failed = {f[0] for f in baseline.get("failed", [])}
    changed = [k for k, v in fps.items() if k in base_fp and base_fp[k] != v]
    added = [k for k in fps if k not in base_fp]
    removed = [k for k in base_fp if k not in fps]
    newly_failed = [f for f in failed if f[0] not in base_failed]
    # For changed tables: exactly which entries differ (added, removed or
    # with a different value).
    entries = {}
    for k in changed:
        if k in TABLES:
            ELEMENTS[k] = table_elements(fingerprinter, TABLES[k])
        old, new = base_elements.get(k), ELEMENTS.get(k)
        if old is None or new is None:
            continue
        entries[k] = sorted(e for e in set(old) | set(new) if old.get(e) != new.get(e))
    json.dump(
        {"ok": True, "changed": changed, "added": added, "removed": removed, "failed": newly_failed,
         "functions": sorted(k for k in changed + added if k in callables), "entries": entries},
        sys.stdout,
    )
    return 0


def function_keys() -> set:
    """Keys whose current value is a function or method (rebinding or new
    defaults affect the code that calls it, not data readers)."""
    keys = set()
    kinds = (types.FunctionType, staticmethod, classmethod, types.MethodType, functools.partial)
    for modname in [m for m in sys.modules if m.startswith("sqlglot")]:
        module = sys.modules.get(modname)
        if module is None:
            continue
        for name, value in vars(module).items():
            if isinstance(value, kinds):
                keys.add(f"{modname}||{name}")
            elif isinstance(value, type) and value.__dict__.get("__module__") == modname:
                for attr, inner in value.__dict__.items():
                    if isinstance(inner, kinds):
                        keys.add(f"{modname}|{value.__qualname__}|{attr}")
    return keys


if __name__ == "__main__":
    sys.exit(main())
