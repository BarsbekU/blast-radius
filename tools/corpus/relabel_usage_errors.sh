#!/usr/bin/env bash
# Re-label commits whose label came from a pytest run that stopped with a usage
# error (exit 4: a named test's file no longer imports, so nothing ran).
# Deletes those labels and labels them again with the whole suite (no pruning).
#   bash tools/corpus/relabel_usage_errors.sh work/corpus1 work/corpus2 work/corpus3
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
export PATH="$PATH:/c/Program Files/Docker/Docker/resources/bin"
to_win() { if command -v cygpath >/dev/null; then cygpath -w "$1"; else echo "$1"; fi; }
for corpus in "$@"; do
  n=0
  for f in "$ROOT/$corpus"/labels/*.json; do
    if grep -q '"exitstatus": 4' "$f"; then rm "$f"; n=$((n + 1)); fi
  done
  echo "$corpus: removed $n usage-error labels"
  if [ "$n" -gt 0 ]; then
    MSYS_NO_PATHCONV=1 docker run --rm -v "$(to_win "$ROOT"):/repo:ro" -v "$(to_win "$ROOT/$corpus"):/corpus" \
      blast-mapbuild:v5 python /repo/tools/corpus/label.py --corpus /corpus --jobs 10 --prune none --timeout 120
  fi
done
