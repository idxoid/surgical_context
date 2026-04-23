#!/usr/bin/env python3
"""
Evaluation harness for Phase 2.5 — retrieval quality and token metrics.

Usage:
    python QA/qa_benchmark.py [--report out.json] [--questions QUESTIONS_YAML]
    python QA/qa_benchmark.py --no-index (skip re-indexing if DBs already populated)

Requires:
    - Neo4j running at bolt://localhost:7687
    - LanceDB at ./data/lancedb
    - Golden fixture at tests/fixtures/sample_project/ (auto-indexed on first run)
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import tiktoken
import yaml


def setup_fixture_db():
    """Index the golden fixture project into Neo4j + LanceDB (idempotent)."""
    from sidecar.indexer.code import run_indexing
    from sidecar.indexer.docs import index_docs

    fixture_path = Path(__file__).parent.parent / "tests" / "fixtures" / "sample_project"
    print(f"\n[1/2] Indexing fixture: {fixture_path}")
    run_indexing(str(fixture_path))

    docs_path = Path(__file__).parent.parent / "docs"
    if docs_path.exists():
        print(f"[2/2] Indexing docs: {docs_path}")
        index_docs(str(docs_path))


def load_question_pack(questions_path: str) -> dict:
    """Load a question pack from YAML.

    Supports:
    - legacy fixture format: top-level list[question]
    - real-repo pack format: {repositories: [...], questions: [...]}
    """
    with open(questions_path) as f:
        payload = yaml.safe_load(f) or []

    if isinstance(payload, list):
        return {
            "repositories": [],
            "questions": payload,
            "kind": "fixture",
        }
    if isinstance(payload, dict):
        return {
            "repositories": payload.get("repositories", []),
            "questions": payload.get("questions", []),
            "kind": "real_repo" if payload.get("repositories") else "fixture",
        }
    raise ValueError(f"Unsupported question pack format in {questions_path}")


def load_questions(
    questions_path: str,
    *,
    repo: str | None = None,
    core12_only: bool = False,
) -> list:
    """Load question set from YAML, with optional filters."""
    pack = load_question_pack(questions_path)
    questions = pack["questions"]
    if repo:
        questions = [question for question in questions if question.get("repo") == repo]
    if core12_only:
        questions = [question for question in questions if question.get("core12", False)]
    return questions


def count_tokens(text: str) -> int:
    """Count tokens using cl100k_base encoding."""
    enc = tiktoken.get_encoding("cl100k_base")
    return len(enc.encode(text))


def run_benchmark(
    questions_path: str = None,
    no_index: bool = False,
    repo: str | None = None,
    core12_only: bool = False,
) -> dict:
    """Run the benchmark suite and return metrics dict."""
    if not questions_path:
        questions_path = str(
            Path(__file__).parent.parent / "tests" / "fixtures" / "sample_project" / "questions.yaml"
        )

    question_pack = load_question_pack(questions_path)
    is_real_repo_pack = question_pack["kind"] == "real_repo"

    print("="*70)
    print("EVALUATION HARNESS — Phase 2.5")
    print("="*70)

    if not no_index and not is_real_repo_pack:
        setup_fixture_db()
    elif not no_index and is_real_repo_pack:
        print("\n[info] Real-repository question pack detected.")
        print("[info] Automatic sample fixture indexing skipped.")
        print("[info] Index target repositories separately before running this benchmark.\n")

    from sidecar.context.arbitrator import ContextArbitrator
    from sidecar.database.neo4j_client import Neo4jClient

    neo4j_uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    neo4j_user = os.getenv("NEO4J_USER", "neo4j")
    neo4j_password = os.getenv("NEO4J_PASSWORD", "password")

    db = Neo4jClient(neo4j_uri, neo4j_user, neo4j_password)
    arb = ContextArbitrator(db)

    questions = load_questions(questions_path, repo=repo, core12_only=core12_only)
    results = []

    print(f"\n{'-'*70}")
    print(f"Running {len(questions)} questions...")
    print(f"{'-'*70}\n")

    for q in questions:
        symbol = q.get("symbol")
        question_text = q.get("question")
        expected_symbols = set(q.get("expected_symbols", []))
        difficulty = q.get("difficulty", "unknown")
        intent = q.get("intent", "unknown")

        # Measure assembly time
        start_ms = time.time()
        ctx = arb.get_context_for_symbol(symbol)
        end_ms = time.time()

        assembly_ms = (end_ms - start_ms) * 1000

        # Handle error case
        if isinstance(ctx, str):
            print(f"  ❌ {q['id']}: {symbol} — {ctx}")
        results.append({
            "id": q["id"],
            "repo": q.get("repo", ""),
            "symbol": symbol,
            "status": "error",
            "error": ctx,
        })
        continue

        # Extract retrieved symbols
        retrieved_symbols = {dep.symbol for dep in ctx.graph_context}
        primary_symbol = {ctx.primary_source.symbol}
        all_retrieved = retrieved_symbols | primary_symbol

        # Compute recall@k and precision@k
        intersection = all_retrieved & expected_symbols
        recall_at_k = len(intersection) / len(expected_symbols) if expected_symbols else 0.0
        precision_at_k = len(intersection) / len(all_retrieved) if all_retrieved else 0.0

        # Token counts
        tokens_surgical = ctx.token_count()

        # Carpet-bomb baseline: all files containing any expected symbol
        # (For now, estimate as sum of file token counts containing expected symbols)
        tokens_carpet_bomb = tokens_surgical * 2  # Placeholder; improve later with actual file union

        # Calculate reduction ratio
        reduction_ratio = 1 - (tokens_surgical / tokens_carpet_bomb) if tokens_carpet_bomb > 0 else 0.0

        status = "pass" if recall_at_k >= 0.8 and precision_at_k >= 0.6 else "warn"
        status_emoji = "✅" if status == "pass" else "⚠️"

        print(
            f"  {status_emoji} {q['id']}: {symbol:20} "
            f"| recall={recall_at_k:.2f} | precision={precision_at_k:.2f} | {tokens_surgical}t"
        )

        results.append({
            "id": q["id"],
            "repo": q.get("repo", ""),
            "symbol": symbol,
            "question": question_text,
            "difficulty": difficulty,
            "intent": intent,
            "status": status,
            "retrieved_symbols": sorted(list(all_retrieved)),
            "expected_symbols": sorted(list(expected_symbols)),
            "recall_at_k": recall_at_k,
            "precision_at_k": precision_at_k,
            "tokens_surgical": tokens_surgical,
            "tokens_carpet_bomb": tokens_carpet_bomb,
            "reduction_ratio": reduction_ratio,
            "assembly_ms": assembly_ms,
        })

    db.close()

    # Aggregate metrics
    passes = sum(1 for r in results if r.get("status") == "pass")
    total = len(results)
    avg_recall = sum(r.get("recall_at_k", 0) for r in results) / total if total > 0 else 0.0
    avg_precision = sum(r.get("precision_at_k", 0) for r in results) / total if total > 0 else 0.0
    total_tokens_surgical = sum(r.get("tokens_surgical", 0) for r in results)
    total_tokens_carpet = sum(r.get("tokens_carpet_bomb", 0) for r in results)
    avg_assembly_ms = sum(r.get("assembly_ms", 0) for r in results) / total if total > 0 else 0.0

    metrics = {
        "timestamp": time.time(),
        "question_pack": {
            "path": questions_path,
            "kind": question_pack["kind"],
            "repo_filter": repo or "",
            "core12_only": core12_only,
        },
        "summary": {
            "total_questions": total,
            "pass_count": passes,
            "pass_rate": passes / total if total > 0 else 0.0,
            "recall_at_5": avg_recall,
            "precision_at_5": avg_precision,
            "tokens_surgical": total_tokens_surgical,
            "tokens_carpet_bomb": total_tokens_carpet,
            "reduction_ratio": 1 - (total_tokens_surgical / total_tokens_carpet)
            if total_tokens_carpet > 0
            else 0.0,
            "assembly_ms_avg": avg_assembly_ms,
        },
        "results": results,
    }

    print(f"\n{'='*70}")
    print("SUMMARY")
    print(f"{'='*70}")
    print(f"Pass rate:       {metrics['summary']['pass_rate']:.1%} ({passes}/{total})")
    print(f"Recall@5:        {metrics['summary']['recall_at_5']:.2f}")
    print(f"Precision@5:     {metrics['summary']['precision_at_5']:.2f}")
    print(f"Tokens (surgical): {metrics['summary']['tokens_surgical']:,}")
    print(f"Tokens (carpet):   {metrics['summary']['tokens_carpet_bomb']:,}")
    print(f"Reduction:       {metrics['summary']['reduction_ratio']:.1%}")
    print(f"Avg assembly:    {metrics['summary']['assembly_ms_avg']:.1f}ms")
    print(f"{'='*70}\n")

    return metrics


def main():
    parser = argparse.ArgumentParser(
        description="Evaluation harness for Phase 2.5",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--report",
        help="Output metrics to JSON file",
        default=None,
    )
    parser.add_argument(
        "--questions",
        help="Path to questions.yaml",
        default=None,
    )
    parser.add_argument(
        "--repo",
        help="Filter a real-repo question pack to one repository id",
        default=None,
    )
    parser.add_argument(
        "--core12",
        action="store_true",
        help="Run only questions marked core12: true",
    )
    parser.add_argument(
        "--no-index",
        action="store_true",
        help="Skip re-indexing (use existing DB)",
    )
    args = parser.parse_args()

    metrics = run_benchmark(
        questions_path=args.questions,
        no_index=args.no_index,
        repo=args.repo,
        core12_only=args.core12,
    )

    if args.report:
        with open(args.report, "w") as f:
            json.dump(metrics, f, indent=2)
        print(f"Metrics saved to: {args.report}")

        # Append to baselines.jsonl for historical tracking
        baseline_file = Path(__file__).parent / "baselines.jsonl"
        with open(baseline_file, "a") as f:
            f.write(json.dumps(metrics["summary"]) + "\n")
        print(f"Baseline appended to: {baseline_file}")

    return 0 if metrics["summary"]["pass_rate"] >= 0.8 else 1


if __name__ == "__main__":
    sys.exit(main())
