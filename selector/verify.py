"""Run a handful of cheap tests against the patched tree at decision time.

The scorer charges CPU, so this is only worth it when a selection would
otherwise include an expensive test that the change very likely cannot break
(see blast.py: Verifier policy). The patched tree is a symlink farm over the
pristine, precompiled checkout in /app/tree: only directories on the path of
a changed file are real, so building it costs a few milliseconds and fits the
64 MB /tmp easily.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

PLUGIN = '''
import json, os
_failed = set()
_collect = set()
def pytest_runtest_logreport(report):
    if report.failed:
        _failed.add(report.nodeid)
def pytest_collectreport(report):
    if report.failed:
        _collect.add(report.nodeid)
def pytest_sessionfinish(session, exitstatus):
    with open(os.environ["BLAST_VERIFY_OUT"], "w") as f:
        json.dump({"failed": sorted(_failed), "collect_failed": sorted(_collect),
                   "exitstatus": int(exitstatus)}, f)
'''


def build_tree(pristine: str, changes: dict, root: str) -> None:
    """Make `root` look like `pristine` with `changes` applied.

    changes: relative path -> new bytes, or None for a deleted file.
    """
    materialized = {""}
    for rel in changes:
        parts = rel.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            materialized.add("/".join(parts[:i]))

    def populate(rel_dir: str) -> None:
        src_dir = os.path.join(pristine, rel_dir) if rel_dir else pristine
        dst_dir = os.path.join(root, rel_dir) if rel_dir else root
        os.makedirs(dst_dir, exist_ok=True)
        try:
            entries = os.listdir(src_dir)
        except FileNotFoundError:
            entries = []
        for name in entries:
            rel = f"{rel_dir}/{name}" if rel_dir else name
            if rel in changes:
                continue
            if rel in materialized:
                populate(rel)
            else:
                os.symlink(os.path.join(src_dir, name), os.path.join(dst_dir, name))
        for sub in materialized:
            if sub and os.path.dirname(sub) == rel_dir and not os.path.exists(os.path.join(root, sub)):
                populate(sub)

    populate("")
    for rel, content in changes.items():
        path = os.path.join(root, rel)
        if content is None:
            continue
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(content)


def run(pristine: str, changes: dict, node_ids: list, timeout: float, per_test_timeout: int = 60) -> dict:
    """Returns {"ok": bool, "failed": set of node ids, "secs": float}."""
    started = time.monotonic()
    work = tempfile.mkdtemp(prefix="blast-verify-", dir="/tmp")
    try:
        tree = os.path.join(work, "tree")
        build_tree(pristine, changes, tree)
        plugin_dir = os.path.join(work, "plugin")
        os.makedirs(plugin_dir)
        with open(os.path.join(plugin_dir, "blast_verify_plugin.py"), "w") as handle:
            handle.write(PLUGIN)
        out = os.path.join(work, "result.json")
        env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": work,
            "PYTHONPATH": f"{tree}:{plugin_dir}",
            "PYTHONDONTWRITEBYTECODE": "1",
            "SKIP_INTEGRATION": "0",
            "BLAST_VERIFY_OUT": out,
            "TMPDIR": work,
        }
        try:
            proc = subprocess.run(
                [sys.executable, "-P", "-s", "-m", "pytest", "-p", "no:cacheprovider",
                 "-p", "blast_verify_plugin", "-q", "--no-header", "-o", "addopts=",
                 f"--timeout={per_test_timeout}", "--continue-on-collection-errors",
                 *node_ids],
                cwd=tree, env=env, capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "failed": set(), "secs": time.monotonic() - started, "why": "timeout"}
        if not os.path.exists(out):
            return {"ok": False, "failed": set(), "secs": time.monotonic() - started,
                    "why": f"no report (exit {proc.returncode}): {proc.stdout[-300:]}"}
        with open(out) as handle:
            data = json.load(handle)
        failed = set(n.split("[")[0] for n in data["failed"])
        for nodeid in data["collect_failed"]:
            failed.add(nodeid)
        # exit status 0 = all passed, 1 = some failed; anything else is unusual
        ok = data["exitstatus"] in (0, 1)
        return {"ok": ok, "failed": failed, "secs": time.monotonic() - started,
                "why": "" if ok else f"exit status {data['exitstatus']}"}
    finally:
        shutil.rmtree(work, ignore_errors=True)
