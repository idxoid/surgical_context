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
            dist = (
                f"d={cand.vector_distance:.3f}"
                if cand.vector_distance is not None
                else "d=-"
            )
            print(
                f"  #{i:2d}  score={cand.score:.3f}  {dist}  "
                f"contracts={cand.contract_count}  "
                f"{cand.name} ({short_path})"
            )


if __name__ == "__main__":
    main()
