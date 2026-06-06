"""End-to-end smoke for the axis query path.

Query text + explicit axis requirements -> LanceDB axis seed search -> Neo4j
compiled graph expansion. This is a QA tool, not a ranker replacement.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from sidecar.axis.graph_traversal import AxisGraphTraversal
from sidecar.axis.query_plan import (
    AxisQueryRequest,
    AxisRequirement,
    TraversalMode,
    compile_axis_query,
)
from sidecar.database.lancedb_client import LanceDBClient
from sidecar.database.neo4j_client import Neo4jClient
from sidecar.index_profile import AXIS_PYTHON_V1_PROFILE
from sidecar.indexer.fast.pipeline import NEO4J_PASSWORD, NEO4J_URI, NEO4J_USER
from sidecar.workspace import DEFAULT_WORKSPACE_ID


def parse_axis_requirement(value: str) -> AxisRequirement:
    axis, sep, bit = value.partition(":")
    if not sep:
        raise ValueError(f"Axis requirement must be axis:bit, got {value!r}")
    return AxisRequirement(axis.strip(), bit.strip())  # type: ignore[arg-type]


def execute_axis_query(
    *,
    query: str,
    workspace_id: str,
    request: AxisQueryRequest,
    lance: Any,
    traversal: Any,
    threshold: float = 0.4,
) -> dict[str, object]:
    plan = compile_axis_query(request, workspace_id=workspace_id)
    seeds = lance.search_axis_symbols(query, plan, threshold=threshold)
    seed_uids = [str(seed.get("uid") or "") for seed in seeds if seed.get("uid")]
    graph_hits = traversal.expand(seed_uids, plan)
    return {
        "query": query,
        "workspace_id": workspace_id,
        "plan": plan.to_dict(),
        "seeds": seeds,
        "graph_hits": [
            hit.to_dict() if hasattr(hit, "to_dict") else dict(hit)
            for hit in graph_hits
        ],
    }


def run_axis_query_smoke(
    *,
    query: str,
    workspace_id: str,
    traversal_mode: TraversalMode,
    required_bits: tuple[AxisRequirement, ...],
    container_kinds: tuple[str, ...],
    limit: int,
    threshold: float,
    out_path: Path,
) -> dict[str, object]:
    request = AxisQueryRequest(
        traversal_mode=traversal_mode,
        required_bits=required_bits,
        container_kinds=container_kinds,
        limit=limit,
    )
    lance = LanceDBClient(index_profile=AXIS_PYTHON_V1_PROFILE)
    db = Neo4jClient(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)
    result = execute_axis_query(
        query=query,
        workspace_id=workspace_id,
        request=request,
        lance=lance,
        traversal=AxisGraphTraversal(db, workspace_id),
        threshold=threshold,
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Smoke-test compiled axis query path")
    parser.add_argument("query")
    parser.add_argument("--workspace", default=DEFAULT_WORKSPACE_ID)
    parser.add_argument(
        "--mode",
        choices=("immediate_control_flow", "deferred_binding_flow"),
        default="deferred_binding_flow",
    )
    parser.add_argument("--required-bit", action="append", default=[])
    parser.add_argument("--container-kind", action="append", default=[])
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--threshold", type=float, default=0.4)
    parser.add_argument("--out", type=Path, default=Path("/tmp/axis_query_smoke.json"))
    args = parser.parse_args()

    result = run_axis_query_smoke(
        query=args.query,
        workspace_id=args.workspace,
        traversal_mode=args.mode,
        required_bits=tuple(parse_axis_requirement(bit) for bit in args.required_bit),
        container_kinds=tuple(str(kind) for kind in args.container_kind),
        limit=args.limit,
        threshold=args.threshold,
        out_path=args.out,
    )
    print(
        f"seeds={len(result['seeds'])} graph_hits={len(result['graph_hits'])} out={args.out}"
    )


if __name__ == "__main__":
    main()
