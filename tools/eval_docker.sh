#!/usr/bin/env bash
# Run tools/eval.py inside the submission image (same Python, pristine copy
# and fingerprint baseline as the scorer sees), against a labeled data dir.
#   tools/eval_docker.sh sample --kinds
#   tools/eval_docker.sh work/corpus1 --top 20 --reasons
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DATA_DIR="$1"; shift
IMAGE="${IMAGE:-blast-radius-entry:v2}"
to_win() { if command -v cygpath >/dev/null; then cygpath -w "$1"; else echo "$1"; fi; }
MSYS_NO_PATHCONV=1 docker run --rm \
  -v "$(to_win "$ROOT/tools"):/repo/tools:ro" \
  -v "$(to_win "$ROOT/selector"):/repo/selector:ro" \
  -v "$(to_win "$ROOT/$DATA_DIR"):/evaldata:ro" \
  -v "$(to_win "$ROOT/work"):/work" \
  -e BLAST_PRISTINE=/app/tree -e BLAST_VERIFY_TREE=/app/tree -e BLAST_FP_BASELINE=/app/fp_baseline.json.gz \
  -e BLAST_VERBOSE=0 -e BLAST_TIME_BUDGET=1000000 -e PYTHONHASHSEED=0 \
  -e BLAST_UNIT_MAX_TESTS="${BLAST_UNIT_MAX_TESTS:-16}" -e BLAST_UNIT_MAX_MS="${BLAST_UNIT_MAX_MS:-300}" \
  -e BLAST_UNIT_CHECK="${BLAST_UNIT_CHECK:-1}" \
  -e BLAST_RERUN_EXTRA_SEEDS="${BLAST_RERUN_EXTRA_SEEDS-}" -e BLAST_TPC_SAMPLE_SECS="${BLAST_TPC_SAMPLE_SECS:-1.5}" \
  -e BLAST_PROXY_MAX_NODES="${BLAST_PROXY_MAX_NODES:-1}" \
  "$IMAGE" python /repo/tools/eval.py --data /evaldata --maps /app/maps "$@"
