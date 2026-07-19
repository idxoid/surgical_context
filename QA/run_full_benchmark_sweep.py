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

# Repos present in QA/fixtures/questions_python.yaml and REPO_TO_WORKSPACE.
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
PACK = ROOT / "QA" / "fixtures" / "questions_python.yaml"
_ALLOWED_REPOS = frozenset(REPOS)


def _combined_report_path() -> Path:
    from QA.output_paths import resolve_output_path

    return resolve_output_path(None, default_name="benchmark_sweep_full.json")


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
    from QA.output_paths import resolve_benchmark_workspace

    if repo not in _ALLOWED_REPOS:
        return {"repo": repo, "error": f"unknown repo: {repo}"}

    out_dir = resolve_benchmark_workspace(repo, _ALLOWED_REPOS)
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
        "--token-budget",
        "6000",
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


def _print_sweep_table(rows: list[dict]) -> None:
    """Print a fixed-width console table across sweep repos."""
    headers = (
        "repo",
        "q",
        "scored",
        "seed",
        "pool",
        "bundle",
        "pass_rate",
        "tok mean",
        "ctx s",
        "status",
    )
    table_rows: list[tuple[str, ...]] = []
    for row in rows:
        if "error" in row:
            table_rows.append(
                (
                    str(row.get("repo", "?")),
                    "—",
                    "—",
                    "—",
                    "—",
                    "—",
                    "—",
                    "—",
                    "—",
                    "error",
                )
            )
            continue
        table_rows.append(
            (
                str(row["repo"]),
                str(row.get("questions", 0)),
                str(row.get("scored", 0)),
                f"{float(row.get('seed_recall', 0.0)):.3f}",
                f"{float(row.get('pool_recall', 0.0)):.3f}",
                f"{float(row.get('file_recall', 0.0)):.3f}",
                f"{float(row.get('pass_rate', 0.0)):.3f}",
                f"{float(row.get('tokens_rendered_mean', 0.0)):.0f}",
                f"{float(row.get('context_seconds_mean', 0.0)):.2f}",
                "ok",
            )
        )
    if not table_rows:
        return

    widths = [max(len(headers[i]), *(len(r[i]) for r in table_rows)) for i in range(len(headers))]

    def _fmt(cells: tuple[str, ...]) -> str:
        return "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(cells))

    print("\nSweep summary")
    print(_fmt(headers))
    print(_fmt(tuple("-" * w for w in widths)))
    for table_row in table_rows:
        print(_fmt(table_row))


def main() -> int:
    started = time.time()
    rows = [run_repo(repo) for repo in REPOS]
    payload = {
        "harness": "axis_benchmark",
        "timestamp": time.time(),
        "elapsed_sec": round(time.time() - started, 1),
        "repos": rows,
    }
    out_path = _combined_report_path()
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    _print_sweep_table(rows)
    print(f"\nCombined report: {out_path}")
    return 0 if all("error" not in r for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
