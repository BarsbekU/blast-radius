# Blast Radius: test selection for sqlglot changes

Entry for the RWX *Blast Radius* track. For each patch to sqlglot (pinned at
`eb11aec`), the image prints which tests to run and how to split them over
up to 8 workers. The kit's original instructions are in
[KIT_README.md](KIT_README.md) and the exact rules in [SCORING.md](SCORING.md).

## How it decides

Everything the selector knows about the tests was measured on the pinned
commit ahead of time; nothing is keyed by patch or by kind of edit.

**Dependency maps** (`tools/mapbuild/`, results in `selector/maps/`). Every
one of the 1,620 tests was run alone, in its own process, under a tracer
loaded by a `.pth` file, so it also follows multiprocessing "spawn" workers
and `python -c` subprocesses the tests start. For each test it records:

* each source line executed, split by whether it ran inside a function
  (run time) or in a module/class body (import time);
* files opened other than by the import system (SQL fixtures, CSV data,
  source files some tests parse), directories listed, modules it tried to
  import (including ones that don't exist yet);
* which class-level data attributes of sqlglot classes it read at run time.

**Per patch** (`selector/blast.py`):

1. Apply the hunks to the pinned source and compare old and new syntax
   trees statement by statement, only where the hunks are.
2. A changed statement inside a function selects the tests that executed
   its lines at run time. When the only change is a comparison operator or
   a literal compared against, the comparison map narrows that further:
   every test was also run with each comparison in sqlglot instrumented
   (`tools/mapbuild/cmp_trace.py`), recording how the two sides related
   each time (identical, equal, less, greater, or the value compared with
   a literal). A test whose every recorded comparison gives the same
   answer under the new operator or literal runs exactly as before and is
   not selected; untraced tests and tests that did not reach the line in
   that run stay selected. A change confined to a lambda body selects only
   that lambda's callers. Statements with no bytecode (or dead code) fall
   back to the tests that reach the neighbouring statements.
3. Comment, whitespace and docstring edits change nothing a test can
   observe (nothing in sqlglot reads docstrings); a file that no longer
   compiles breaks every test that imports it.
4. Import-time changes (a dialect table entry, a class constant, a
   decorator...) are resolved by importing the patched package in a
   subprocess and fingerprinting every class attribute and module global
   (`selector/fingerprint.py`). Comparing with the pristine fingerprints
   gives the exact values that changed, including ones derived elsewhere
   (metaclasses, `__init_subclass__`, subclasses extending a parent's
   table). The tests that read those values are selected.
5. Fixtures and other data files select the tests that read them; pytest
   config files select everything only if pytest's settings change or the
   file stops parsing.
6. A function definition that only moved within a module or class body
   (identical code, name bound once, no order-sensitive decorators) is not
   a change; the fingerprint diff still checks the module/class values.
7. The TPC executor tests (`selector/exec_check.py`): when every changed
   line they run belongs to the query pipeline, the affected queries'
   pipeline outputs are recomputed with the patched code. If a few
   queries' outputs differ, those queries are executed on the TPC-DS data
   (`maps/data.zip`, loaded exactly as the test does) under two hash seeds
   and compared with the pristine results. Changed data-phase lines (the
   executor's row processing) send the queries that run them straight to
   execution. When more queries are affected than fit the budget, a
   sample is executed instead (the cheapest query running each changed
   line, then queries spread over the cost range, ~4 s); the test is
   dropped only if every sampled result matches. Changes the line-level
   analysis cannot attribute to particular queries (import-time values,
   broad selections, lines outside the profiled queries) are decided the
   same way with every query a candidate: all parse-only queries are
   checked, a sample is executed, and `test_optimized_tpch` gets the exact
   duckdb-SQL comparison (executor-package changes stay conservative,
   since the tests' class setup loads data through it). Re-runs stop at the
   first difference. This is the one place the selector trades certainty
   for time: a change whose effect shows only in unsampled queries would be
   missed.
8. `test_max_nodes` (29 s) is replaced
   by the same assertions on a 300-wide instead of 10,000-wide statement
   (`selector/proxy_check.py`) when the patch only edits sqlglot modules.
9. The selected tests are packed over 1-8 workers (longest first onto the
   least loaded worker) to minimise `slowest worker + 2 s per worker`.

## Reproducing

```sh
# maps (about 15 min on 16 cores)
docker build -f tools/mapbuild/Dockerfile -t blast-mapbuild .
docker run --rm -v blast-traces:/traces blast-mapbuild python /tools/run_tests.py --out /traces/seed0 --seed 0
docker run --rm -e BLAST_TRACE_ATTRS=1 -v blast-traces:/traces blast-mapbuild python /tools/run_tests.py --out /traces/seed1 --seed 1
docker run --rm -v blast-traces:/traces -v "$PWD/selector/maps:/out" blast-mapbuild \
  sh -c "python /tools/aggregate.py --traces /traces/seed0 /traces/seed1 --out /out && python /tools/refs.py --out /out"

# score on the sample like the board does (3 runs, container CPU included)
python tools/run_local.py --runs 3
```

`tools/corpus/` generates synthetic commits and labels them by running the
full suite, which is how the selector was checked on change kinds the
sample does not contain.
