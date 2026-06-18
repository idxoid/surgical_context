#!/usr/bin/env python3
"""Axis retrieval + Claude CLI judge (production 6k / 7/35 caps).

Replaces the deleted ``qa_benchmark.py --judge`` hook for the axis pipeline.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from QA.axis_benchmark import REPO_TO_WORKSPACE, _load_pack
from QA.llm_judge import EFFORTS, judge_question_matrix
from sidecar.axis.pipeline import run_axis_retrieval
from sidecar.axis.prompt_provider import axis_bundles_to_prompt_context
from sidecar.database.lancedb_client import LanceDBClient
from sidecar.database.neo4j_client import Neo4jClient
from sidecar.index_profile import AXIS_PYTHON_V1_PROFILE
from sidecar.indexer.fast.pipeline import NEO4J_PASSWORD, NEO4J_URI, NEO4J_USER
from sidecar.observability.metrics import estimate_text_tokens


def _judge_cell(result) -> dict[str, Any]:
    return result.to_dict() if hasattr(result, "to_dict") else dict(result)


def _run_judge(
    system_prompt: str,
    question: str,
    *,
    intent: str,
    efforts: tuple[str, ...],
) -> dict[str, Any]:
    payload = judge_question_matrix(
        system_prompt,
        question,
        intent=intent,
        efforts=efforts,  # type: ignore[arg-type]
        providers=("claude",),
        max_workers=len(efforts),
    )
    matrix = payload.get("matrix", {})
    serialized: dict[str, dict[str, Any]] = {}
    for effort, per_provider in matrix.items():
        serialized[effort] = {
            provider: _judge_cell(result) for provider, result in per_provider.items()
        }
    return {
        "mode": "claude",
        "efforts": list(efforts),
        "available": payload.get("available", {}),
        "matrix": serialized,
    }


def _summarise_judge(results: list[dict[str, Any]]) -> dict[str, Any]:
    cells: dict[str, dict[str, int]] = {}
    errors = 0
    judge_tokens = 0
    for row in results:
        judge = row.get("judge") or {}
        for effort, per_provider in (judge.get("matrix") or {}).items():
            for provider, cell in per_provider.items():
                key = f"{provider}/{effort}"
                bucket = cells.setdefault(key, {"pass": 0, "warn": 0, "fail": 0, "error": 0})
                if cell.get("error"):
                    bucket["error"] += 1
                    errors += 1
                else:
                    verdict = str(cell.get("verdict") or "fail").lower()
                    bucket[verdict if verdict in bucket else "fail"] += 1
                judge_tokens += int(cell.get("input_tokens", 0)) + int(cell.get("output_tokens", 0))
    return {
        "cells": cells,
        "errors": errors,
        "judge_tokens": judge_tokens,
        "questions": len(results),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Axis benchmark + Claude LLM judge")
    parser.add_argument("--pack", type=Path, default=Path("tests/fixtures/questions_python.yaml"))
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--repo", default=None, help="Optional single-repo filter")
    parser.add_argument(
        "--efforts",
        default="low,medium,high",
        help="Comma-separated judge tiers (default: all Claude tiers)",
    )
    parser.add_argument("--per-role-limit", type=int, default=7)
    parser.add_argument("--max-impacted", type=int, default=35)
    parser.add_argument("--token-budget", type=int, default=6000)
    parser.add_argument("--context-seeds-per-role", type=int, default=2)
    parser.add_argument("--top-roles", type=int, default=3)
    parser.add_argument("--intent-threshold", type=float, default=0.20)
    parser.add_argument("--context-per-seed", type=int, default=6)
    args = parser.parse_args()

    efforts = tuple(e.strip() for e in args.efforts.split(",") if e.strip())
    for effort in efforts:
        if effort not in EFFORTS:
            parser.error(f"unknown effort {effort!r}; expected one of {EFFORTS}")

    questions = _load_pack(args.pack)
    if args.repo:
        questions = [q for q in questions if q.get("repo") == args.repo]
    if not questions:
        print("no questions to run", file=sys.stderr)
        return 1

    db = Neo4jClient(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)
    lance = LanceDBClient(index_profile=AXIS_PYTHON_V1_PROFILE)

    results: list[dict[str, Any]] = []
    started = time.monotonic()
    total = len(questions)

    print(
        f"[judge] pack={args.pack} questions={total} "
        f"caps={args.per_role_limit}/{args.max_impacted} budget={args.token_budget} "
        f"claude efforts={efforts}",
        flush=True,
    )

    for index, entry in enumerate(questions, start=1):
        qid = str(entry.get("id") or "")
        repo = str(entry.get("repo") or "")
        question = str(entry.get("question") or "")
        workspace_id = REPO_TO_WORKSPACE.get(repo)
        row: dict[str, Any] = {
            "id": qid,
            "repo": repo,
            "question": question,
            "expected_files": entry.get("expected_files") or [],
        }
        if workspace_id is None:
            row["skipped_reason"] = f"repo {repo!r} not indexed"
            results.append(row)
            print(f"[{index}/{total}] {repo}/{qid} SKIP {row['skipped_reason']}", flush=True)
            _write_report(args.report, results, args, efforts, started)
            continue

        t0 = time.monotonic()
        retrieval = run_axis_retrieval(
            question,
            workspace_id=workspace_id,
            db=db,
            lance=lance,
            top_roles=args.top_roles,
            per_role_limit=args.per_role_limit,
            max_impacted=args.max_impacted,
            intent_threshold=args.intent_threshold,
            with_context=True,
            context_per_seed=args.context_per_seed,
            context_seeds_per_role=args.context_seeds_per_role,
            intent_budget=True,
            base_token_budget=args.token_budget,
            anchor_path=str(entry.get("anchor") or "") or None,
            hook_transparency=True,
        )
        intent_label = retrieval.intent[0].role if retrieval.intent else ""
        ctx = axis_bundles_to_prompt_context(
            retrieval.bundles,
            question=question,
            workspace_id=workspace_id,
            intent=intent_label,
        )
        if ctx is None:
            row["skipped_reason"] = "empty context"
            results.append(row)
            print(f"[{index}/{total}] {repo}/{qid} SKIP empty context", flush=True)
            _write_report(args.report, results, args, efforts, started)
            continue

        prompt = ctx.to_system_prompt()
        row["context_tokens"] = estimate_text_tokens(prompt)
        row["rendered_tokens"] = row["context_tokens"]
        row["intent_top_role"] = intent_label
        row["judge"] = _run_judge(prompt, question, intent=intent_label, efforts=efforts)
        results.append(row)
        elapsed = time.monotonic() - t0
        medium = (row["judge"]["matrix"].get("medium") or {}).get("claude") or {}
        verdict = medium.get("verdict") or next(
            (
                (cells.get("claude") or {}).get("verdict")
                for cells in row["judge"]["matrix"].values()
                if cells.get("claude")
            ),
            "?",
        )
        print(
            f"[{index}/{total}] {repo}/{qid} ctx={row['context_tokens']}tok "
            f"verdict={verdict} t={elapsed:.0f}s",
            flush=True,
        )
        _write_report(args.report, results, args, efforts, started)

    print(f"Report JSON: {args.report.resolve()}", flush=True)
    return 0


def _write_report(
    path: Path,
    results: list[dict[str, Any]],
    args: argparse.Namespace,
    efforts: tuple[str, ...],
    started: float,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "harness": "axis_judge_run",
        "pack": str(args.pack),
        "caps": {
            "per_role_limit": args.per_role_limit,
            "max_impacted": args.max_impacted,
            "token_budget": args.token_budget,
            "context_seeds_per_role": args.context_seeds_per_role,
        },
        "judge": {"provider": "claude", "efforts": list(efforts)},
        "elapsed_sec": round(time.monotonic() - started, 1),
        "results": results,
        "summary": {
            "questions": len(results),
            "judge": _summarise_judge(results),
        },
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
