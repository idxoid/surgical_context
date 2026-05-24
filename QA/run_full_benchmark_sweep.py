#!/usr/bin/env python3
"""Run full real-repo benchmark sweep and emit a combined summary JSON."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

REPOS_NO_INDEX = [
    "fastapi",
    "pydantic",
    "redux_toolkit",
    "django",
    "flask",
    "express",
    "nestjs",
    "sqlalchemy",
    "vue",
]

REPOS_INDEX = [
    "surgical_context",
    "dathund",
]

ROOT = Path(__file__).resolve().parent.parent
QA = ROOT / "QA"
OUT = Path("/tmp/benchmark_sweep_full.json")


def run_repo(repo: str, *, no_index: bool) -> dict:
    cmd = [
        sys.executable,
        str(QA / "qa_benchmark.py"),
        "--repo",
        repo,
    ]
    if no_index:
        cmd.append("--no-index")
    print(f"\n>>> {' '.join(cmd)}\n", flush=True)
    proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True)
    print(proc.stdout)
    if proc.stderr:
        print(proc.stderr, file=sys.stderr)
    if proc.returncode != 0:
        return {"repo": repo, "error": proc.stderr or proc.stdout, "returncode": proc.returncode}

    report_line = [ln for ln in proc.stdout.splitlines() if ln.startswith("Report JSON:")]
    if not report_line:
        return {"repo": repo, "error": "no report path in stdout"}
    report_path = report_line[-1].split(":", 1)[1].strip()
    with open(report_path, encoding="utf-8") as fh:
        metrics = json.load(fh)
    return {
        "repo": repo,
        "report_path": report_path,
        "summary": metrics.get("summary", {}),
        "results": metrics.get("results", []),
        "indexing": metrics.get("indexing", {}),
    }


def main() -> int:
    started = time.time()
    rows: list[dict] = []
    for repo in REPOS_NO_INDEX:
        rows.append(run_repo(repo, no_index=True))
    for repo in REPOS_INDEX:
        rows.append(run_repo(repo, no_index=False))

    payload = {
        "timestamp": time.time(),
        "elapsed_sec": round(time.time() - started, 1),
        "repos": rows,
    }
    OUT.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nCombined report: {OUT}")
    return 0 if all("error" not in r for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
