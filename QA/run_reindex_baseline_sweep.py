#!/usr/bin/env python3
"""Re-index each benchmark repo, run axis benchmark, emit combined baseline JSON.

Replaces the deleted ``QA/qa_benchmark.py`` harness. Indexing uses
``python -m sidecar.indexer.fast --fresh``; evaluation uses
``python -m QA.axis_benchmark``.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from QA.axis_benchmark import REPO_TO_WORKSPACE
from QA.reset_databases import _default_repo_checkout_path, _repo_meta_from_pack
from sidecar.index_profile import AXIS_PYTHON_V1_PROFILE

ROOT = Path(__file__).resolve().parent.parent
QUESTIONS = ROOT / "tests" / "fixtures" / "questions_python.yaml"
OUT_DIR = ROOT / "QA" / "baselines" / "reindex_2026-05-30"
COMBINED = ROOT / "QA" / "baseline_reindex_2026-05-30.json"
BASELINES_JSONL = ROOT / "QA" / "baselines.jsonl"

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


def _project_path(repo: str) -> Path:
    meta = _repo_meta_from_pack(str(QUESTIONS), repo) or {}
    raw = meta.get("project_path") or _default_repo_checkout_path(repo)
    path = Path(raw).resolve()
    if not path.exists():
        raise FileNotFoundError(f"checkout not found for {repo}: {path}")
    return path


def _summary_row(repo: str, summary: dict, report_path: Path) -> dict:
    scored = int(summary.get("scored", 0))
    full = int(summary.get("full_recall_questions", 0))
    return {
        "questions": scored + int(summary.get("skipped", 0)),
        "scored": scored,
        "pass_count": full,
        "pass_rate": full / scored if scored else 0.0,
        "file_recall": float(summary.get("overall_mean_recall", 0.0)),
        "seed_recall": float(summary.get("overall_seed_mean_recall", 0.0)),
        "pool_recall": float(summary.get("overall_pool_mean_recall", 0.0)),
        "tokens_rendered_mean": float(summary.get("overall_mean_rendered_tokens", 0.0)),
        "context_seconds_mean": float(summary.get("overall_mean_context_seconds", 0.0)),
        "report_path": str(report_path),
    }


def run_repo(repo: str, *, skip_existing: bool = True) -> dict:
    report_path = OUT_DIR / f"{repo}.json"
    if skip_existing and report_path.exists():
        data = json.loads(report_path.read_text(encoding="utf-8"))
        summary = data.get("summary", data)
        row = {"repo": repo, "skipped": True, **_summary_row(repo, summary, report_path)}
        row["indexing"] = data.get("indexing", {})
        return row

    workspace_id = REPO_TO_WORKSPACE.get(repo)
    if workspace_id is None:
        return {"repo": repo, "error": f"repo {repo!r} has no axis workspace mapping"}

    try:
        project_path = _project_path(repo)
    except FileNotFoundError as exc:
        return {"repo": repo, "error": str(exc)}

    index_cmd = [
        sys.executable,
        "-m",
        "sidecar.indexer.fast",
        str(project_path),
        "--workspace",
        workspace_id,
        "--index-profile",
        AXIS_PYTHON_V1_PROFILE,
        "--fresh",
    ]
    bench_out = OUT_DIR / f"{repo}_axis"
    bench_cmd = [
        sys.executable,
        "-m",
        "QA.axis_benchmark",
        "--pack",
        str(QUESTIONS),
        "--out",
        str(bench_out),
        "--repo",
        repo,
        "--intent-budget",
        "--token-budget",
        "6000",
        "--context-seeds-per-role",
        "2",
    ]

    started = time.time()
    print(f"\n>>> {' '.join(index_cmd)}\n", flush=True)
    index_proc = subprocess.run(index_cmd, cwd=str(ROOT), capture_output=True, text=True)
    index_stdout = index_proc.stdout or ""
    index_stderr = index_proc.stderr or ""
    if index_stdout:
        print(index_stdout)
    if index_stderr:
        print(index_stderr, file=sys.stderr)

    row: dict = {
        "repo": repo,
        "workspace_id": workspace_id,
        "project_path": str(project_path),
        "index_returncode": index_proc.returncode,
        "elapsed_sec": 0.0,
    }
    if index_proc.returncode != 0:
        row["error"] = index_stderr or index_stdout or f"index exit {index_proc.returncode}"
        row["elapsed_sec"] = round(time.time() - started, 1)
        return row

    print(f"\n>>> {' '.join(bench_cmd)}\n", flush=True)
    bench_proc = subprocess.run(bench_cmd, cwd=str(ROOT), capture_output=True, text=True)
    bench_stdout = bench_proc.stdout or ""
    bench_stderr = bench_proc.stderr or ""
    if bench_stdout:
        print(bench_stdout)
    if bench_stderr:
        print(bench_stderr, file=sys.stderr)
    row["elapsed_sec"] = round(time.time() - started, 1)
    row["benchmark_returncode"] = bench_proc.returncode

    summary_path = bench_out / "summary.json"
    if bench_proc.returncode != 0 or not summary_path.exists():
        row["error"] = bench_stderr or bench_stdout or "benchmark summary missing"
        return row

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    payload = {
        "harness": "axis_benchmark",
        "repo_filter": repo,
        "summary": summary,
        "indexing": {
            "workspace_id": workspace_id,
            "project_path": str(project_path),
            "profile": AXIS_PYTHON_V1_PROFILE,
            "fresh": True,
        },
    }
    report_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    row.update(_summary_row(repo, summary, report_path))
    return row


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    started = time.time()
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = [run_repo(repo) for repo in REPOS]

    ok = [r for r in rows if "error" not in r and r.get("scored", r.get("questions", 0))]
    totals = {
        "questions": sum(r.get("questions", 0) for r in ok),
        "pass_count": sum(r.get("pass_count", 0) for r in ok),
    }
    if ok and totals["questions"]:

        def weighted_avg(key: str) -> float:
            return (
                sum(r[key] * r.get("scored", r.get("questions", 0)) for r in ok)
                / totals["questions"]
            )

        aggregate = {
            "pass_rate": totals["pass_count"] / totals["questions"],
            "file_recall": weighted_avg("file_recall"),
            "seed_recall": weighted_avg("seed_recall"),
            "pool_recall": weighted_avg("pool_recall"),
        }
    else:
        aggregate = {}

    payload = {
        "label": "full_pack_reindex_baseline",
        "harness": "axis_benchmark",
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

    BASELINES_JSONL.parent.mkdir(parents=True, exist_ok=True)
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
