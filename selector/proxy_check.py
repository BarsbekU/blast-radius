"""Scaled-down run of tests/test_parser.py::TestParser::test_max_nodes on the
patched package (on by default; BLAST_PROXY_MAX_NODES=0 disables it).

The real test parses a 10,000-wide, 100-deep expression list (about 2M AST
nodes, ~29 s). This runs the same assertions in the same order with the
same depth and a 300-wide list, and node limits scaled to that size, so it
exercises the same parser paths (node counting, the limit error and its
message, recovery for the next parse) in well under a second. Any failure
or error means "keep the real test".

    python proxy_check.py < request.json
request:  {"root", "overrides", "deleted"}
response: {"ok": true, "passed": bool, "error": str|None}
"""

from __future__ import annotations

import json
import os
import sys


def body():
    from sqlglot import exp, parse_one
    from sqlglot.errors import ParseError

    assert isinstance(parse_one("SELECT 1", max_nodes=1_000_000), exp.Select)

    cols = [f"col{i}" for i in range(500)]
    sql = f"SELECT {','.join(cols)} FROM t"
    assert isinstance(parse_one(sql, max_nodes=1_000_000), exp.Select)

    width = 300
    depth = 100
    expr = "x" + "=x" * depth
    cols = [expr for _ in range(width)]
    sql = f"SELECT {','.join(cols)}"

    # ~60k nodes: over both scaled limits, as 2M is over 1M and 50k.
    for limit, text in ((30_000, "Maximum number of AST nodes"), (1_500, "(1500)")):
        try:
            parse_one(sql, max_nodes=limit)
        except ParseError as error:
            assert text in str(error), str(error)[:200]
        else:
            raise AssertionError(f"no ParseError at max_nodes={limit}")

    try:
        parse_one("SELECT " + ",".join(f"x={i}" for i in range(100)), max_nodes=5)
    except ParseError:
        pass
    else:
        raise AssertionError("no ParseError at max_nodes=5")
    assert isinstance(parse_one("SELECT 1, 2, 3"), exp.Select)


def main() -> int:
    request = json.load(sys.stdin)
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, here)
    from fingerprint import _Finder  # noqa: E402

    sys.path.insert(0, request["root"])
    overrides, deleted = request.get("overrides", {}), set(request.get("deleted", []))
    if overrides or deleted:
        sys.meta_path.insert(0, _Finder(overrides, deleted))
    try:
        body()
    except BaseException as error:  # noqa: BLE001
        json.dump({"ok": True, "passed": False, "error": f"{type(error).__name__}: {error}"[:300]}, sys.stdout)
        return 0
    json.dump({"ok": True, "passed": True, "error": None}, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
