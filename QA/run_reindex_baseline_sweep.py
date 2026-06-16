#!/usr/bin/env python3
"""Run full benchmark pack with re-index per repo; emit combined baseline JSON."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
QA = ROOT / "QA"
QUESTIONS = ROOT / "tests/fixtures/questions_python.yaml"
OUT_DIR = QA / "baselines/reindex_2026-05-30"
COMBINED = QA / "baseline_reindex_2026-05-30.json"
BASELINES_JSONL = QA / "baselines.jsonl"

REPOS = [
    "fastapi",
    "pydantic",
    "redux_toolkit",
    "django",
    "flask",
    "express",
    "nestjs",
    "sqlalchemy",
    "vue",
    "surgical_context",
    "dathund",
    "celery",
    "click",
]


def run_repo(repo: str, *, skip_existing: bool = True) -> dict:
    report_path = OUT_DIR / f"{repo}.json"
    if skip_existing and report_path.exists():
        data = json.loads(report_path.read_text(encoding="utf-8"))
        summary = data.get("summary", {})
        indexing = data.get("indexing", {})
        return {
            "repo": repo,
            "skipped": True,
            "report_path": str(report_path),
            "questions": summary.get("total_questions", 0),
            "pass_count": summary.get("pass_count", 0),
            "pass_rate": summary.get("pass_rate", 0),
            "recall_at_5": summary.get("recall_at_5", 0),
            "precision_at_5": summary.get("precision_at_5", 0),
            "precision_at_5_prompt_order": summary.get("precision_at_5_prompt_order", 0),
            "context_precision": summary.get("context_precision", 0),
            "file_recall": summary.get("file_recall", 0),
            "role_recall": summary.get("role_recall", 0),
            "reduction_ratio": summary.get("reduction_ratio", 0),
            "tokens_surgical": summary.get("tokens_surgical", 0),
            "assembly_ms_avg": summary.get("assembly_ms_avg", 0),
            "index_timings_total_sec": (indexing.get("timings") or {}).get("total"),
            "repository_readiness": (indexing.get("repository_profile") or {}).get(
                "retrieval_readiness", ""
            ),
        }
    cmd = [
        sys.executable,
        str(QA / "qa_benchmark.py"),
        "--questions",
        str(QUESTIONS),
        "--repo",
        repo,
        "--report",
        str(report_path),
    ]
    started = time.time()
    print(f"\n>>> {' '.join(cmd)}\n", flush=True)
    proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True)
    elapsed = round(time.time() - started, 1)
    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    if stdout:
        print(stdout)
    if stderr:
        print(stderr, file=sys.stderr)

    row: dict = {
        "repo": repo,
        "elapsed_sec": elapsed,
        "returncode": proc.returncode,
        "report_path": str(report_path),
    }
    if proc.returncode != 0 and not report_path.exists():
        row["error"] = stderr or stdout or f"exit {proc.returncode}"
        return row

    if not report_path.exists():
        row["error"] = "report file missing"
        return row

    data = json.loads(report_path.read_text(encoding="utf-8"))
    summary = data.get("summary", {})
    indexing = data.get("indexing", {})
    row.update(
        {
            "questions": summary.get("total_questions", 0),
            "pass_count": summary.get("pass_count", 0),
            "pass_rate": summary.get("pass_rate", 0),
            "recall_at_5": summary.get("recall_at_5", 0),
            "precision_at_5": summary.get("precision_at_5", 0),
            "precision_at_5_prompt_order": summary.get("precision_at_5_prompt_order", 0),
            "context_precision": summary.get("context_precision", 0),
            "file_recall": summary.get("file_recall", 0),
            "role_recall": summary.get("role_recall", 0),
            "reduction_ratio": summary.get("reduction_ratio", 0),
            "tokens_surgical": summary.get("tokens_surgical", 0),
            "assembly_ms_avg": summary.get("assembly_ms_avg", 0),
            "index_timings_total_sec": (indexing.get("timings") or {}).get("total"),
            "repository_readiness": (indexing.get("repository_profile") or {}).get(
                "retrieval_readiness", ""
            ),
        }
    )
    return row


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    started = time.time()
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows: list[dict] = []
    for repo in REPOS:
        rows.append(run_repo(repo))

    totals = {
        "questions": sum(r.get("questions", 0) for r in rows if "error" not in r),
        "pass_count": sum(r.get("pass_count", 0) for r in rows if "error" not in r),
    }
    ok = [r for r in rows if "error" not in r and r.get("questions")]
    if ok:
        w = lambda key: sum(r[key] * r["questions"] for r in ok) / totals["questions"]
        aggregate = {
            "pass_rate": totals["pass_count"] / totals["questions"] if totals["questions"] else 0,
            "recall_at_5": w("recall_at_5"),
            "precision_at_5": w("precision_at_5"),
            "precision_at_5_prompt_order": w("precision_at_5_prompt_order"),
            "context_precision": w("context_precision"),
            "file_recall": w("file_recall"),
            "role_recall": w("role_recall"),
        }
    else:
        aggregate = {}

    payload = {
        "label": "full_pack_reindex_baseline",
        "timestamp": stamp,
        "branch": subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "commit": subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "questions_path": str(QUESTIONS),
        "reindex": True,
        "elapsed_sec": round(time.time() - started, 1),
        "repos": rows,
        "aggregate": {**totals, **aggregate},
    }
    COMBINED.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nCombined baseline: {COMBINED}")

    with BASELINES_JSONL.open("a", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "label": payload["label"],
                    "timestamp": stamp,
                    "commit": payload["commit"],
                    **aggregate,
                    **totals,
                }
            )
            + "\n"
        )
    print(f"Aggregate line appended: {BASELINES_JSONL}")

    errors = [r for r in rows if "error" in r]
    if errors:
        print(f"Errors: {[r['repo'] for r in errors]}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
