#!/usr/bin/env bash
# Re-index benchmark Python repos under axis_python_v1 (skips sqlalchemy/django by default).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export PYTHONPATH="$ROOT"
PY="${ROOT}/.venv/bin/python"
TENANT="${AXIS_BENCH_TENANT:-qa_repo}"
REF="${AXIS_BENCH_REF:-main}"
SKIP="${REINDEX_SKIP:-sqlalchemy,django}"

skip_repo() {
  local repo="$1"
  [[ ",${SKIP}," == *",${repo},"* ]]
}

index_repo() {
  local repo="$1"
  local path="$2"
  local ws="${TENANT}/${repo}@${REF}"
  echo "=== reindex ${repo} (${ws}) ==="
  "$PY" -m sidecar.indexer.fast "$path" --workspace "$ws" --index-profile axis_python_v1
}

for repo in fastapi flask celery click pydantic; do
  skip_repo "$repo" && continue
  index_repo "$repo" "${ROOT}/QA/repos/${repo}"
done

if ! skip_repo dathund; then
  DATHUND_PATH="${DATHUND_PATH:-/home/idxoid/dathund/dathund}"
  if [[ -d "$DATHUND_PATH" ]]; then
    index_repo dathund "$DATHUND_PATH"
  else
    echo "skip dathund: missing $DATHUND_PATH" >&2
  fi
fi

for repo in sqlalchemy django; do
  skip_repo "$repo" && continue
  index_repo "$repo" "${ROOT}/QA/repos/${repo}"
done

echo "reindex complete"
