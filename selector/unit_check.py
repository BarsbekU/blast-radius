"""Run a few selected tests on the patched package, in-process (forked from
the selector), and report which ones pass.

Used only for small selections of cheap tests in plain sqlglot test files,
for commits that edit sqlglot modules in place: a test that passes here runs
exactly as in the scorer's suite run except for state left by tests that ran
before it, so it is dropped; anything else (a failure, an error, a failing
subtest, a skip, a module that does not load) keeps the test.

    run({"root": ..., "overrides": {module: source}, "deleted": [...], "tests": [node ids]})
    -> {"ok": True, "passed": [node ids], "failed": [node ids]}
"""

from __future__ import annotations

import os
import sys


def _stub_optimizer_test_deps() -> None:
    """tests/test_optimizer.py imports duckdb and pandas and runs fixture
    cases on a process pool. Here duckdb accepts only the statements its
    setUpClass runs (any other use raises, so a test that needs duckdb
    errors and is kept), pandas' frame comparison raises, and the pool runs
    each case in-process with the same pickle round trip for its result."""
    import concurrent.futures
    import pickle
    import types

    class _Unavailable(RuntimeError):
        pass

    def _in_setup() -> bool:
        frame = sys._getframe(2)
        while frame is not None:
            if frame.f_code.co_name == "setUpClass":
                return True
            frame = frame.f_back
        return False

    class _Conn:
        def execute(self, *args, **kwargs):
            if not _in_setup():
                raise _Unavailable("duckdb is not available in the selector")
            return self

        def __enter__(self):
            raise _Unavailable("duckdb is not available in the selector")

        def __exit__(self, *args):
            return False

        def close(self):
            pass

        def __getattr__(self, name):
            raise _Unavailable("duckdb is not available in the selector")

    duckdb = types.ModuleType("duckdb")
    duckdb.connect = lambda *a, **k: _Conn()
    sys.modules.setdefault("duckdb", duckdb)

    def _no_frames(*args, **kwargs):
        raise _Unavailable("pandas is not available in the selector")

    pandas = types.ModuleType("pandas")
    testing = types.ModuleType("pandas.testing")
    testing.assert_frame_equal = _no_frames
    pandas.testing = testing
    pandas.DataFrame = _no_frames
    sys.modules.setdefault("pandas", pandas)
    sys.modules.setdefault("pandas.testing", testing)

    class _InProcessPool:
        def __init__(self, *args, **kwargs):
            pass

        def submit(self, fn, *args, **kwargs):
            future = concurrent.futures.Future()
            try:
                args, kwargs = pickle.loads(pickle.dumps((args, kwargs)))
                future.set_result(pickle.loads(pickle.dumps(fn(*args, **kwargs))))
            except BaseException as error:  # noqa: BLE001
                future.set_exception(error)
            return future

        def shutdown(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    concurrent.futures.ProcessPoolExecutor = _InProcessPool


def _name(node_id: str) -> str:
    path, cls, method = node_id.split("::")
    return f"{path[:-3].replace('/', '.')}.{cls}.{method}"


def run(request: dict) -> dict:
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, here)
    from fingerprint import _Finder  # noqa: E402

    root = request["root"]
    sys.path.insert(0, root)
    overrides, deleted = request.get("overrides", {}), set(request.get("deleted", []))
    if overrides or deleted:
        sys.meta_path.insert(0, _Finder(overrides, deleted))
    os.environ["SKIP_INTEGRATION"] = "0"
    os.chdir(root)
    _stub_optimizer_test_deps()
    import unittest

    node_ids = list(request["tests"])
    names = {_name(n): n for n in node_ids}
    bad: set = set()
    good: set = set()

    class Result(unittest.TestResult):
        def _mark(self, test) -> None:
            node = names.get(test.id())
            if node is None:  # a subtest: its id is the parent's plus a suffix
                parent = getattr(test, "test_case", None)
                node = names.get(parent.id()) if parent is not None else None
            if node is not None:
                bad.add(node)

        def addError(self, test, err):
            super().addError(test, err)
            self._mark(test)

        def addFailure(self, test, err):
            super().addFailure(test, err)
            self._mark(test)

        def addSubTest(self, test, subtest, err):
            super().addSubTest(test, subtest, err)
            if err is not None:
                bad.add(names.get(test.id(), test.id()))

        def addSkip(self, test, reason):
            super().addSkip(test, reason)
            self._mark(test)

        def addExpectedFailure(self, test, err):
            super().addExpectedFailure(test, err)
            self._mark(test)

        def addUnexpectedSuccess(self, test):
            super().addUnexpectedSuccess(test)
            self._mark(test)

        def addSuccess(self, test):
            super().addSuccess(test)
            node = names.get(test.id())
            if node is not None:
                good.add(node)

    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for name, node in names.items():
        try:
            suite.addTests(loader.loadTestsFromName(name))
        except BaseException:  # noqa: BLE001 - the module or test does not load
            bad.add(node)
    result = Result()
    suite.run(result)
    # Class/module fixtures that failed are reported as errors on a
    # placeholder test: they fail every test of that class.
    for test, _err in result.errors:
        if test.id() not in names:
            text = test.id()
            for name, node in names.items():
                if name.rsplit(".", 1)[0] in text or name.rsplit(".", 2)[0] in text:
                    bad.add(node)
    passed = sorted(n for n in good if n not in bad)
    return {"ok": True, "passed": passed, "failed": sorted(set(node_ids) - set(passed))}


def main() -> int:
    import json

    json.dump(run(json.load(sys.stdin)), sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
