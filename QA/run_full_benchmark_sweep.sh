#!/usr/bin/env bash
set -uo pipefail
cd "$(dirname "$0")/.."
source .venv/bin/activate

LOG=/tmp/benchmark_sweep.log
COMBINED=/tmp/benchmark_sweep_full.json
PACK=QA/fixtures/questions_python.yaml
: > "$LOG"

# Repos in questions_python.yaml with axis_python_v1 workspaces.
REPOS=(fastapi pydantic django flask sqlalchemy celery click surgical_context dathund)

run_one() {
  local repo="$1"
  local out="/tmp/axis_benchmark_${repo}"
  echo "===== REPO: $repo $(date -Is) =====" | tee -a "$LOG"
  PYTHONPATH=. python -m QA.axis_benchmark \
    --pack "$PACK" \
    --out "$out" \
    --repo "$repo" \
    --intent-budget \
    --token-budget 6000 \
    --context-seeds-per-role 2 \
    2>&1 | tee -a "$LOG"
  echo | tee -a "$LOG"
}

for repo in "${REPOS[@]}"; do
  run_one "$repo" || true
done

python3 - <<'PY' | tee -a "$LOG"
import json
from pathlib import Path

log = Path("/tmp/benchmark_sweep.log").read_text()
reports = []
for line in log.splitlines():
    if line.startswith("Report JSON:"):
        reports.append(Path(line.split(":", 1)[1].strip()))

rows = []
for path in reports:
    if not path.exists():
        continue
    summary = json.loads(path.read_text(encoding="utf-8"))
    scored = int(summary.get("scored", 0))
    full = int(summary.get("full_recall_questions", 0))
    rows.append({
        "report_path": str(path),
        "repo": summary.get("repo_filter", ""),
        "questions": scored + int(summary.get("skipped", 0)),
        "scored": scored,
        "pass_rate": full / scored if scored else 0.0,
        "file_recall": summary.get("overall_mean_recall", 0.0),
        "seed_recall": summary.get("overall_seed_mean_recall", 0.0),
        "pool_recall": summary.get("overall_pool_mean_recall", 0.0),
        "tokens_rendered_mean": summary.get("overall_mean_rendered_tokens", 0.0),
        "context_seconds_mean": summary.get("overall_mean_context_seconds", 0.0),
        "summary": summary,
    })

Path("/tmp/benchmark_sweep_full.json").write_text(
    json.dumps({"harness": "axis_benchmark", "repos": rows}, indent=2)
)
print(f"Combined: /tmp/benchmark_sweep_full.json ({len(rows)} repos)")
PY

echo "DONE $(date -Is)" | tee -a "$LOG"
