"""End-to-end ``/ask``-style demo over the axis pipeline.

Takes a free-text question, classifies it through the L4 intent
classifier into one or more roles, runs the role-driven retrieval
primitive for each role, and prints ranked candidates. This is the
shape the future ``/ask`` endpoint will take when the legacy cascade is
retired.

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

from sidecar.axis.context_builder import build_context_for_candidates
from sidecar.axis.intent_classifier import classify_intent
from sidecar.axis.role_retrieval import find_symbols_by_role


def _embed_function():
    from sidecar.database.lancedb_client import LanceDBClient
    from sidecar.index_profile import AXIS_PYTHON_V1_PROFILE

    client = LanceDBClient(index_profile=AXIS_PYTHON_V1_PROFILE)

    def embed(text: str):
        return client._embed([text])[0]  # noqa: SLF001

    return embed


def main() -> None:
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
        default=2,
        help="When --with-context is set, top-K candidates per role to expand.",
    )
    parser.add_argument(
        "--code-chars",
        type=int,
        default=400,
        help="Per-symbol code excerpt length when printing context.",
    )
    args = parser.parse_args()

    embed_fn = _embed_function()

    intent = classify_intent(
        args.question,
        embed_fn,
        top_k=args.top_roles,
        threshold=args.intent_threshold,
    )

    print(f"question: {args.question!r}")
    print(f"workspace: {args.workspace}")
    print()

    if not intent:
        print(
            "intent classifier found no role above threshold "
            f"({args.intent_threshold:.2f}). Try a more specific question."
        )
        return

    print("=== intent classification ===")
    for match in intent:
        print(f"  {match.role:25s}  sim={match.similarity:.3f}  ({match.description})")

    # Wire DB + Lance lazily — only build them when context expansion
    # is requested. Saves startup cost on the retrieval-only path.
    db = None
    lance = None
    if args.with_context:
        from sidecar.database.lancedb_client import LanceDBClient
        from sidecar.database.neo4j_client import Neo4jClient
        from sidecar.index_profile import AXIS_PYTHON_V1_PROFILE
        from sidecar.indexer.fast.pipeline import (
            NEO4J_PASSWORD,
            NEO4J_URI,
            NEO4J_USER,
        )

        db = Neo4jClient(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)
        lance = LanceDBClient(index_profile=AXIS_PYTHON_V1_PROFILE)

    for match in intent:
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
            continue
        for i, cand in enumerate(candidates, 1):
            short_path = (cand.file_path or "").split("/")[-1]
            dist = f"d={cand.vector_distance:.3f}" if cand.vector_distance is not None else "d=-"
            print(
                f"  #{i:2d}  score={cand.score:.3f}  {dist}  "
                f"contracts={cand.contract_count}  "
                f"{cand.name} ({short_path})"
            )

        if args.with_context:
            seeds_for_context = candidates[: args.context_seeds_per_role]
            bundles = build_context_for_candidates(
                seeds_for_context,
                workspace_id=args.workspace,
                db=db,
                lance=lance,
                max_per_seed=args.context_per_seed,
            )
            for bundle in bundles:
                print()
                print(f"  --- context bundle: seed={bundle.seed.name} (role={bundle.role}) ---")
                for sym in bundle.all_symbols():
                    short_path = (sym.file_path or "").split("/")[-1]
                    step = sym.expansion_step or "seed"
                    print(
                        f"    [depth={sym.distance_from_seed} step={step}] "
                        f"{sym.name} ({short_path})"
                    )
                    if sym.code:
                        snippet = sym.code.strip()
                        if len(snippet) > args.code_chars:
                            snippet = snippet[: args.code_chars] + "…"
                        # Indent code with 6 spaces for readability.
                        for line in snippet.splitlines():
                            print(f"      {line}")


if __name__ == "__main__":
    main()
