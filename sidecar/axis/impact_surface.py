"""Structured impact surface for the live `/impact` endpoint.

This is the non-LLM sibling of ask-axis: the endpoint already knows the
intent (`impact_analysis`) and the target symbol, so it seeds the axis
impact traversal directly instead of inventing a prompt-facing question.
"""

from __future__ import annotations

from typing import Any

from sidecar.axis.impact_traversal import expand_impact_neighbourhood
from sidecar.axis.role_retrieval import RoleCandidate

DEFAULT_IMPACT_SURFACE_DEPTH = 3
MAX_IMPACT_SURFACE_DEPTH = 4
MAX_IMPACT_SURFACE_ITEMS = 50


def build_impact_surface(
    *,
    db: Any,
    symbol_uid: str,
    symbol_name: str,
    file_path: str,
    workspace_id: str,
    max_depth: int = DEFAULT_IMPACT_SURFACE_DEPTH,
    max_items: int = MAX_IMPACT_SURFACE_ITEMS,
) -> dict[str, Any]:
    """Return structured impact rows for one resolved symbol.

    The target symbol is the explicit seed. Traversal is ordered from precise
    seed-local evidence to broad closure: reverse callers, structural API /
    inheritance, and only then AFFECTS fallback.
    """
    walk_depth = max(1, min(int(max_depth), MAX_IMPACT_SURFACE_DEPTH))
    seed = RoleCandidate(
        uid=symbol_uid,
        name=symbol_name,
        file_path=file_path,
        role="impact_analysis",
        satisfying_contracts=(),
        satisfying_kinds=("target_seed",),
        contract_count=0,
        kind_count=1,
        vector_distance=None,
        score=1.0,
        depth=0,
        edge_type="TARGET",
        utility_score=1.0,
    )
    candidates = expand_impact_neighbourhood(
        [seed],
        db=db,
        workspace_id=workspace_id,
        max_hops=walk_depth,
        max_impacted=max_items,
    )
    rows = [_row_from_candidate(candidate) for candidate in candidates]
    return {
        "affected_symbols": rows,
        "affected_files": sorted(
            {
                row["file_path"]
                for row in rows
                if row.get("file_path") and row["file_path"] != "<unknown>"
            }
        ),
        "max_depth": walk_depth,
    }


def _row_from_candidate(candidate: RoleCandidate) -> dict[str, Any]:
    kind = candidate.satisfying_kinds[0] if candidate.satisfying_kinds else "impact"
    zone, severity, role = _surface_classification(kind)
    return {
        "uid": candidate.uid,
        "name": candidate.name,
        "symbol": candidate.name,
        "file_path": candidate.file_path,
        "depth": candidate.depth or 1,
        "edge_type": candidate.edge_type or _edge_type_for_kind(kind),
        "kind": kind,
        "role": role,
        "zone": zone,
        "severity": severity,
        "utility_score": candidate.utility_score
        if candidate.utility_score is not None
        else candidate.score,
        "relevance_score": candidate.score,
        "satisfying_kinds": list(candidate.satisfying_kinds),
    }


def _surface_classification(kind: str) -> tuple[str, str, str]:
    if kind == "reverse_calls":
        return "direct", "high", "direct_consumer"
    if kind == "structural_api_carrier":
        return "reach", "high", "api_surface"
    if kind == "structural_inheritor":
        return "reach", "high", "structural_dependent"
    if kind == "forward_affects":
        return "reach", "medium", "affects_closure"
    return "reach", "medium", "impact_candidate"


def _edge_type_for_kind(kind: str) -> str:
    return {
        "reverse_calls": "CALLS_*",
        "structural_api_carrier": "HAS_API",
        "structural_inheritor": "EXTENDS_EXTERNAL|INHERITED_API",
        "forward_affects": "AFFECTS",
    }.get(kind, "IMPACT")


__all__ = [
    "DEFAULT_IMPACT_SURFACE_DEPTH",
    "MAX_IMPACT_SURFACE_DEPTH",
    "build_impact_surface",
]
