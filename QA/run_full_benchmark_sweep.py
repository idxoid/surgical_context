#!/usr/bin/env python3
"""Run axis benchmark sweep across indexed repos; emit combined summary JSON.

Uses ``python -m QA.axis_benchmark`` (read-side only — assumes workspaces are
already indexed under the axis_python_v1 profile). Replaces the deleted
``QA/qa_benchmark.py`` harness.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

# Repos present in tests/fixtures/questions_python.yaml and REPO_TO_WORKSPACE.
REPOS = [
    "fastapi",
    "pydantic",
    "django",
    "flask",
    "sqlalchemy",
    "celery",
    "click",
    "surgical_context",
    "dathund",
]

ROOT = Path(__file__).resolve().parent.parent
PACK = ROOT / "tests" / "fixtures" / "questions_python.yaml"
OUT = Path("/tmp/benchmark_sweep_full.json")


def _axis_row(repo: str, summary: dict, report_path: Path, results: list[dict]) -> dict:
    scored = int(summary.get("scored", 0))
    full = int(summary.get("full_recall_questions", 0))
    return {
        "repo": repo,
        "report_path": str(report_path),
        "summary": summary,
        "results": results,
        "questions": scored + int(summary.get("skipped", 0)),
        "scored": scored,
        "skipped": int(summary.get("skipped", 0)),
        "pass_count": full,
        "pass_rate": full / scored if scored else 0.0,
        "file_recall": float(summary.get("overall_mean_recall", 0.0)),
        "seed_recall": float(summary.get("overall_seed_mean_recall", 0.0)),
        "pool_recall": float(summary.get("overall_pool_mean_recall", 0.0)),
        "tokens_rendered_mean": float(summary.get("overall_mean_rendered_tokens", 0.0)),
        "context_seconds_mean": float(summary.get("overall_mean_context_seconds", 0.0)),
    }


def run_repo(repo: str) -> dict:
    out_dir = Path(f"/tmp/axis_benchmark_{repo}")
    cmd = [
        sys.executable,
        "-m",
        "QA.axis_benchmark",
        "--pack",
        str(PACK),
        "--out",
        str(out_dir),
        "--repo",
        repo,
        "--intent-budget",
        "--token-budget",
        "6000",
        "--context-seeds-per-role",
        "2",
    ]
    print(f"\n>>> {' '.join(cmd)}\n", flush=True)
    proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True)
    print(proc.stdout)
    if proc.stderr:
        print(proc.stderr, file=sys.stderr)
    if proc.returncode != 0:
        return {"repo": repo, "error": proc.stderr or proc.stdout, "returncode": proc.returncode}

    summary_path = out_dir / "summary.json"
    if not summary_path.exists():
        report_line = [ln for ln in proc.stdout.splitlines() if ln.startswith("Report JSON:")]
        if report_line:
            summary_path = Path(report_line[-1].split(":", 1)[1].strip())
    if not summary_path.exists():
        return {"repo": repo, "error": "summary.json missing"}

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    results: list[dict] = []
    results_path = summary_path.parent / "results.jsonl"
    if results_path.exists():
        results = [
            json.loads(line)
            for line in results_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    return _axis_row(repo, summary, summary_path, results)


def main() -> int:
    started = time.time()
    rows = [run_repo(repo) for repo in REPOS]
    payload = {
        "harness": "axis_benchmark",
        "timestamp": time.time(),
        "elapsed_sec": round(time.time() - started, 1),
        "repos": rows,
    }
    OUT.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nCombined report: {OUT}")
    return 0 if all("error" not in r for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
