"""End-to-end ``/ask``-style demo over the axis pipeline.

Takes a free-text question, classifies it through the L4 intent
classifier into one or more roles, runs the role-driven retrieval
primitive for each role, and prints ranked candidates — the same read
path ``/ask`` uses via ``run_axis_retrieval``.

Usage::

    python -m QA.axis_ask \\
        --workspace qa_repo/flask@axis-v4+axis_python_v1 \\
        --question "how do flask globals like current_app work"

    python -m QA.axis_ask \\
        --workspace qa_repo/fastapi_consumer@axis-v4+axis_python_v1 \\
        --question "how does dependency injection work in this app" \\
        --top-roles 2 \\
        --per-role-limit 5
"""

from __future__ import annotations

import argparse
from typing import Any

from context_engine.axis.context_builder import build_context_for_candidates
from context_engine.axis.intent_classifier import IntentMatch, classify_intent
from context_engine.axis.role_retrieval import RoleCandidate, find_symbols_by_role


def _embed_function():
    from context_engine.database.lancedb_client import LanceDBClient
    from context_engine.index_profile import AXIS_PYTHON_V1_PROFILE

    client = LanceDBClient(index_profile=AXIS_PYTHON_V1_PROFILE)

    def embed(text: str):
        return client._embed([text])[0]  # noqa: SLF001

    return embed


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="/ask-style end-to-end demo over the axis pipeline",
    )
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--question", required=True)
    parser.add_argument("--top-roles", type=int, default=3)
    parser.add_argument("--intent-threshold", type=float, default=0.20)
    parser.add_argument("--per-role-limit", type=int, default=5)
    parser.add_argument(
        "--with-context",
        action="store_true",
        help=(
            "After ranking, expand each top candidate via the axis "
            "graph traversal and print related symbols + code excerpts."
        ),
    )
    parser.add_argument(
        "--context-per-seed",
        type=int,
        default=4,
        help="When --with-context is set, max related symbols per seed.",
    )
    parser.add_argument(
        "--context-seeds-per-role",
        type=int,
        default=None,
        nargs="?",
        const=2,
        help="When --with-context is set, optional top-K candidates per role to expand.",
    )
    parser.add_argument(
        "--code-chars",
        type=int,
        default=400,
        help="Per-symbol code excerpt length when printing context.",
    )
    return parser


def _print_intent_classification(
    *,
    question: str,
    workspace: str,
    intent: list[IntentMatch],
    threshold: float,
) -> bool:
    print(f"question: {question!r}")
    print(f"workspace: {workspace}")
    print()

    if not intent:
        print(
            "intent classifier found no role above threshold "
            f"({threshold:.2f}). Try a more specific question."
        )
        return False

    print("=== intent classification ===")
    for match in intent:
        print(f"  {match.role:25s}  sim={match.similarity:.3f}  ({match.description})")
    return True


def _open_context_clients() -> tuple[Any, Any]:
    from context_engine.database.lancedb_client import LanceDBClient
    from context_engine.database.neo4j_client import Neo4jClient
    from context_engine.index_profile import AXIS_PYTHON_V1_PROFILE
    from context_engine.indexer.fast.pipeline import (
        NEO4J_PASSWORD,
        NEO4J_URI,
        NEO4J_USER,
    )

    db = Neo4jClient(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)
    lance = LanceDBClient(index_profile=AXIS_PYTHON_V1_PROFILE)
    return db, lance


def _print_candidates(candidates: list[RoleCandidate]) -> None:
    for i, cand in enumerate(candidates, 1):
        short_path = (cand.file_path or "").split("/")[-1]
        dist = f"d={cand.vector_distance:.3f}" if cand.vector_distance is not None else "d=-"
        print(
            f"  #{i:2d}  score={cand.score:.3f}  {dist}  "
            f"contracts={cand.contract_count}  "
            f"{cand.name} ({short_path})"
        )


def _seeds_for_context(
    candidates: list[RoleCandidate],
    context_seeds_per_role: int | None,
) -> list[RoleCandidate]:
    if context_seeds_per_role is None:
        return candidates
    return candidates[:context_seeds_per_role]


def _print_code_snippet(code: str, code_chars: int) -> None:
    snippet = code.strip()
    if len(snippet) > code_chars:
        snippet = snippet[:code_chars] + "…"
    for line in snippet.splitlines():
        print(f"      {line}")


def _print_context_bundles(bundles: list[Any], *, code_chars: int) -> None:
    for bundle in bundles:
        print()
        print(f"  --- context bundle: seed={bundle.seed.name} (role={bundle.role}) ---")
        for sym in bundle.all_symbols():
            short_path = (sym.file_path or "").split("/")[-1]
            step = sym.expansion_step or "seed"
            print(f"    [depth={sym.distance_from_seed} step={step}] {sym.name} ({short_path})")
            if sym.code:
                _print_code_snippet(sym.code, code_chars)


def _process_role_match(
    match: IntentMatch,
    *,
    args: argparse.Namespace,
    embed_fn: Any,
    db: Any,
    lance: Any,
) -> None:
    print()
    print(f"=== retrieval for role: {match.role} ===")
    candidates = find_symbols_by_role(
        args.workspace,
        match.role,
        query_text=args.question,
        embed_fn=embed_fn,
        limit=args.per_role_limit,
    )
    if not candidates:
        print("  (no candidates)")
        return

    _print_candidates(candidates)
    if not args.with_context:
        return

    seeds = _seeds_for_context(candidates, args.context_seeds_per_role)
    bundles = build_context_for_candidates(
        seeds,
        workspace_id=args.workspace,
        db=db,
        lance=lance,
        max_per_seed=args.context_per_seed,
    )
    _print_context_bundles(bundles, code_chars=args.code_chars)


def main() -> None:
    args = _build_parser().parse_args()
    embed_fn = _embed_function()
    intent = classify_intent(
        args.question,
        embed_fn,
        top_k=args.top_roles,
        threshold=args.intent_threshold,
    )
    if not _print_intent_classification(
        question=args.question,
        workspace=args.workspace,
        intent=intent,
        threshold=args.intent_threshold,
    ):
        return

    db = lance = None
    if args.with_context:
        db, lance = _open_context_clients()

    for match in intent:
        _process_role_match(match, args=args, embed_fn=embed_fn, db=db, lance=lance)


if __name__ == "__main__":
    main()
