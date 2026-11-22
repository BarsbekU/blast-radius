#!/usr/bin/env bash
# Evaluate any selector version (its selector/ and tools/ dirs) on a labeled data dir.
#   SEL=work/v10tree IMAGE=blast-radius-entry:v10 bash tools/eval_with.sh work/corpus5 --top 0 --dump /work/x.json
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SEL_ROOT="$(cd "${SEL:-$ROOT}" && pwd)"
DATA_DIR="$1"; shift
to_win() { if command -v cygpath >/dev/null; then cygpath -w "$1"; else echo "$1"; fi; }
MSYS_NO_PATHCONV=1 docker run --rm \
  -v "$(to_win "$SEL_ROOT/tools"):/repo/tools:ro" \
  -v "$(to_win "$SEL_ROOT/selector"):/repo/selector:ro" \
  -v "$(to_win "$ROOT/$DATA_DIR"):/evaldata:ro" \
  -v "$(to_win "$ROOT/work"):/work" \
  -e BLAST_PRISTINE=/app/tree -e BLAST_VERIFY_TREE=/app/tree -e BLAST_FP_BASELINE=/app/fp_baseline.json.gz \
  -e BLAST_VERBOSE=0 -e BLAST_TIME_BUDGET=1000000 -e PYTHONHASHSEED=0 \
  "${IMAGE:?set IMAGE}" python /repo/tools/eval.py --data /evaldata --maps /app/maps "$@"
