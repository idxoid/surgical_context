"""End-to-end demo of role-driven retrieval over the axis pipeline.

Takes a role name (and optionally a query text), runs the
:func:`sidecar.axis.role_retrieval.find_symbols_by_role` primitive
against an indexed workspace, and prints the ranked candidates with
their ranking components. This is the read-side surface that any
future ``/ask``-style consumer would call instead of the legacy
``unified_ranker``.

Usage::

    # purely structural (no query text):
    python -m QA.axis_role_query \\
        --workspace qa_repo/fastapi_consumer@axis-v4+axis_python_v1 \\
        --role routing_surface

    # vector-narrowed by query text:
    python -m QA.axis_role_query \\
        --workspace qa_repo/flask@axis-v4+axis_python_v1 \\
        --role binding_surface \\
        --query "how do flask globals like current_app work"
"""

from __future__ import annotations

import argparse

from sidecar.axis.role_resolver import ROLE_CONTRACT_MAP
from sidecar.axis.role_retrieval import find_symbols_by_role


def _embed_function():
    """Return ``query_text -> vector`` using the LanceDB client's embedder
    (same model as the indexed symbols)."""
    from sidecar.database.lancedb_client import LanceDBClient
    from sidecar.index_profile import AXIS_PYTHON_V1_PROFILE

    client = LanceDBClient(index_profile=AXIS_PYTHON_V1_PROFILE)

    def embed(text: str):
        return client._embed([text])[0]  # noqa: SLF001 — used here intentionally

    return embed


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Role-driven retrieval over the axis pipeline",
    )
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--role", required=True)
    parser.add_argument("--query", default=None)
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()

    if args.role not in ROLE_CONTRACT_MAP:
        print(f"unknown role: {args.role!r}")
        print(f"available: {sorted(ROLE_CONTRACT_MAP)}")
        return

    embed_fn = _embed_function() if args.query else None
    candidates = find_symbols_by_role(
        args.workspace,
        args.role,
        query_text=args.query,
        embed_fn=embed_fn,
        limit=args.limit,
    )

    print(f"role: {args.role}")
    print(f"workspace: {args.workspace}")
    if args.query:
        print(f"query: {args.query!r}")
    print(f"role's satisfying contracts: {sorted(ROLE_CONTRACT_MAP[args.role])}")
    print()
    if not candidates:
        print("(no candidates)")
        return
    for i, cand in enumerate(candidates, 1):
        path = (cand.file_path or "").split("/")[-1]
        dist = f"d={cand.vector_distance:.3f}" if cand.vector_distance is not None else "d=-"
        print(
            f"  #{i:2d}  score={cand.score:.3f}  {dist}  "
            f"contracts={cand.contract_count}  "
            f"{cand.name} ({path})"
        )
        print(f"        satisfying: {list(cand.satisfying_contracts)}")


if __name__ == "__main__":
    main()
