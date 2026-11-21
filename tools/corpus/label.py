"""Label synthetic commits: apply each patch to a copy of sqlglot and run the
whole suite, recording which tests fail. Runs inside the mapbuild image:

    python /repo/tools/corpus/label.py --corpus /corpus --jobs 8

Writes <corpus>/labels/<id>.json per commit and, at the end,
<corpus>/answers.json and <corpus>/tests.json in the sample's format.
"failing" follows the README: a failure, error or timeout reported for the
test (subtest failures included), or its file failing to import.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

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
    path = os.environ.get("BLAST_LABEL_OUT")
    if path:
        with open(path, "w") as f:
            json.dump({"failed": sorted(_failed), "collect_failed": sorted(_collect),
                       "exitstatus": int(exitstatus)}, f)
'''


def load_blast(repo: Path):
    spec = importlib.util.spec_from_file_location("blast", repo / "selector" / "blast.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def apply_patch(blast, workdir: Path, text: str) -> None:
    for fp in blast.parse_patch(text):
        if fp.is_deleted:
            (workdir / fp.old_path).unlink(missing_ok=True)
            continue
        target = workdir / fp.new_path
        if fp.old_path and fp.new_path and fp.old_path != fp.new_path:
            src = workdir / fp.old_path
            old = src.read_text("utf-8") if src.exists() else ""
            src.unlink(missing_ok=True)
        elif fp.is_new:
            old = ""
        else:
            old = target.read_text("utf-8")
        applied = blast.apply_hunks(old, fp.hunks) if fp.hunks else None
        new = applied.new_text if applied is not None else old
        if fp.hunks and applied is None:
            raise RuntimeError(f"patch does not apply to {fp.new_path}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(new, "utf-8", newline="")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--src", type=Path, default=Path("/src"))
    parser.add_argument("--repo", type=Path, default=Path("/repo"))
    parser.add_argument("--tests", type=Path, default=Path("/data/tests.json"))
    parser.add_argument("--jobs", type=int, default=8)
    parser.add_argument("--timeout", type=int, default=300, help="per test")
    parser.add_argument("--hang", type=int, default=1800, help="per commit")
    parser.add_argument("--work", type=Path, default=Path("/work"))
    parser.add_argument("--only", default="")
    args = parser.parse_args()

    blast = load_blast(args.repo)
    tests = json.loads(args.tests.read_text())["tests"]
    number = {t["id"]: t["number"] for t in tests}
    by_file: dict = {}
    for t in tests:
        by_file.setdefault(t["file"], []).append(t["number"])

    plugin_dir = args.work / "_plugin"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "blast_label_plugin.py").write_text(PLUGIN)

    labels = args.corpus / "labels"
    labels.mkdir(parents=True, exist_ok=True)
    commits = sorted((args.corpus / "commits").glob("*.patch"))
    if args.only:
        keep = set(args.only.split(","))
        commits = [c for c in commits if c.stem in keep]
    todo = [c for c in commits if not (labels / f"{c.stem}.json").exists()]
    print(f"{len(todo)} commits to label ({len(commits) - len(todo)} done), jobs={args.jobs}", flush=True)

    lock = threading.Lock()
    done = [0]
    started = time.monotonic()

    def run(patch: Path) -> None:
        commit = patch.stem
        workdir = args.work / commit
        shutil.rmtree(workdir, ignore_errors=True)
        # Bytecode is copied too (copytree keeps mtimes, so it stays valid for
        # unchanged files; a patched file gets a new mtime and recompiles).
        shutil.copytree(args.src, workdir, symlinks=True, ignore=shutil.ignore_patterns(".git"))
        result = {"commit": commit}
        try:
            apply_patch(blast, workdir, patch.read_text("utf-8"))
        except Exception as error:
            result.update(status="unappliable", error=repr(error))
            (labels / f"{commit}.json").write_text(json.dumps(result))
            shutil.rmtree(workdir, ignore_errors=True)
            return
        out_json = workdir / "_label.json"
        env = {k: v for k, v in os.environ.items() if not k.startswith("BLAST_TRACE")}
        env.update(
            PYTHONPATH=f"{workdir}:{plugin_dir}",
            SKIP_INTEGRATION="0",
            BLAST_LABEL_OUT=str(out_json),
            PYTHON_CPU_COUNT="2",
            PYTHONDONTWRITEBYTECODE="1",
        )
        t0 = time.monotonic()
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-p", "blast_label_plugin",
                 "-q", "--no-header", "-o", "addopts=", "--continue-on-collection-errors",
                 f"--timeout={args.timeout}", "tests"],
                cwd=workdir, env=env, capture_output=True, text=True, timeout=args.hang,
            )
            status = "ok"
            tail = proc.stdout[-1500:]
        except subprocess.TimeoutExpired:
            status, tail = "hung", ""
        secs = time.monotonic() - t0
        failing: set = set()
        if out_json.exists():
            data = json.loads(out_json.read_text())
            for nodeid in data["failed"]:
                base = nodeid.split("[")[0]
                if base in number:
                    failing.add(number[base])
                elif nodeid in number:
                    failing.add(number[nodeid])
            for nodeid in data["collect_failed"]:
                for n in by_file.get(nodeid, []):
                    failing.add(n)
            result["exitstatus"] = data["exitstatus"]
        elif status == "ok":
            status = "no_report"
        result.update(status=status, failing=sorted(failing), secs=round(secs, 1))
        if status != "ok":
            result["tail"] = tail
        (labels / f"{commit}.json").write_text(json.dumps(result))
        shutil.rmtree(workdir, ignore_errors=True)
        with lock:
            done[0] += 1
            elapsed = time.monotonic() - started
            print(f"[{done[0]}/{len(todo)} {elapsed:.0f}s] {commit}: {status} {len(failing)} failing ({secs:.0f}s)", flush=True)

    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        list(pool.map(run, todo))

    kinds_path = args.corpus / "kinds.json"
    kinds = json.loads(kinds_path.read_text()) if kinds_path.exists() else {}
    answers = {}
    for patch in commits:
        label_path = labels / f"{patch.stem}.json"
        if not label_path.exists():
            continue
        label = json.loads(label_path.read_text())
        if label.get("status") != "ok":
            continue
        answers[patch.stem] = {
            "failing": label["failing"],
            "kind": kinds.get(patch.stem, {}).get("kind", "?"),
        }
    (args.corpus / "answers.json").write_text(json.dumps({"commits": answers}, indent=0))
    shutil.copy(args.tests, args.corpus / "tests.json")
    print(f"answers for {len(answers)} commits")
    return 0


if __name__ == "__main__":
    sys.exit(main())
