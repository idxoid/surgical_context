#!/usr/bin/env bash
set -uo pipefail
cd /home/idxoid/surgical_context
source .venv/bin/activate

LOG=/tmp/benchmark_sweep.log
COMBINED=/tmp/benchmark_sweep_full.json
: > "$LOG"

REPOS_NO_INDEX=(fastapi pydantic redux_toolkit django flask express nestjs sqlalchemy vue)
REPOS_INDEX=(surgical_context dathund)

run_one() {
  local repo="$1"
  local no_index="$2"
  echo "===== REPO: $repo $(date -Is) =====" | tee -a "$LOG"
  if [[ "$no_index" == "1" ]]; then
    python QA/qa_benchmark.py --repo "$repo" --no-index 2>&1 | tee -a "$LOG"
  else
    python QA/qa_benchmark.py --repo "$repo" 2>&1 | tee -a "$LOG"
  fi
  echo | tee -a "$LOG"
}

for repo in "${REPOS_NO_INDEX[@]}"; do
  run_one "$repo" 1 || true
done
for repo in "${REPOS_INDEX[@]}"; do
  run_one "$repo" 0 || true
done

python3 - <<'PY' | tee -a "$LOG"
import json, re
from pathlib import Path
log = Path("/tmp/benchmark_sweep.log").read_text()
reports = re.findall(r"Report JSON:\s+(\S+)", log)
rows = []
for path in reports:
    p = Path(path)
    if not p.exists():
        continue
    data = json.loads(p.read_text())
    s = data.get("summary", {})
    rows.append({
        "report_path": str(p),
        "repo": data.get("question_pack", {}).get("repo_filter", ""),
        "questions": s.get("total_questions", 0),
        "pass_rate": s.get("pass_rate", 0),
        "recall_at_5": s.get("recall_at_5", 0),
        "precision_at_5": s.get("precision_at_5", 0),
        "context_precision": s.get("context_precision", 0),
        "file_recall": s.get("file_recall", 0),
        "role_recall": s.get("role_recall", 0),
        "tokens_surgical": s.get("tokens_surgical", 0),
        "reduction_ratio": s.get("reduction_ratio", 0),
        "assembly_ms_avg": s.get("assembly_ms_avg", 0),
        "results": data.get("results", []),
    })
Path("/tmp/benchmark_sweep_full.json").write_text(json.dumps({"repos": rows}, indent=2))
print(f"Combined: /tmp/benchmark_sweep_full.json ({len(rows)} repos)")
PY

echo "DONE $(date -Is)" | tee -a "$LOG"
