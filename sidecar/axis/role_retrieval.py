"""Role-driven retrieval primitive.

Sits between L4 roles and the actual `/ask`-style consumer. Given a role
name (and optionally a free-text query), returns ranked candidate
symbols from a workspace whose persisted L3 contracts satisfy that role.

Today the legacy ``unified_ranker`` answers ``/ask``. This module is the
first cleanly-shaped entry point for the axis pipeline so future
ranker / endpoint integration has something to call without untangling
``sidecar/context``. The ranking is intentionally simple — vector
distance for semantic narrowing plus a small structural boost when more
contracts in the role fire on the symbol — so the role-match dimension
is observable in the result ordering instead of buried inside a black
box.

Workflow:

  1. Caller picks a role (``routing_surface``, ``binding_surface``, …).
     The L4 role map names the contracts that satisfy it.
  2. We scan the workspace's Lance symbol rows, parse each
     ``axis_contracts_json``, and keep rows whose contract set
     intersects the role.
  3. (Optional) embed the query text and reweight by L2 vector
     distance — symbols semantically close to the query rise.

This is read-only; no graph writes, no Lance mutations.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import lancedb

from sidecar.axis.role_resolver import ROLE_CONTRACT_MAP


@dataclass(frozen=True)
class RoleCandidate:
    """One symbol satisfying a role, with the contracts that fired and
    the ranking score components."""

    uid: str
    name: str
    file_path: str
    role: str
    satisfying_contracts: tuple[str, ...]
    contract_count: int
    vector_distance: float | None
    score: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "uid": self.uid,
            "name": self.name,
            "file_path": self.file_path,
            "role": self.role,
            "satisfying_contracts": list(self.satisfying_contracts),
            "contract_count": self.contract_count,
            "vector_distance": self.vector_distance,
            "score": self.score,
        }


def _structural_score(
    matched_count: int,
    total_role_contracts: int,
) -> float:
    """``[0, 1]`` proportion of the role's contracts that fired on this
    symbol. A symbol satisfying 3 of 4 possible contracts for the role
    scores higher than one satisfying just 1.
    """
    if total_role_contracts <= 0:
        return 0.0
    return min(1.0, matched_count / float(total_role_contracts))


def _semantic_score(distance: float | None) -> float:
    """Map L2 distance to ``[0, 1]`` (1 = exact match, 0 = far).
    Identity ordering: small distance → high score.
    """
    if distance is None:
        return 0.0
    if distance <= 0:
        return 1.0
    return max(0.0, 1.0 / (1.0 + float(distance)))


def _combined_score(
    structural: float,
    semantic: float,
    has_query: bool,
) -> float:
    """If a query was supplied, weight equally; otherwise structural only."""
    if not has_query:
        return structural
    return 0.5 * structural + 0.5 * semantic


def find_symbols_by_role(
    workspace_id: str,
    role: str,
    *,
    query_text: str | None = None,
    limit: int = 25,
    lance_db_path: str = "./data/lancedb",
    embed_fn=None,
) -> list[RoleCandidate]:
    """Return symbols satisfying ``role`` in ``workspace_id``, ranked.

    Pipeline:

      1. Structural filter — scan the workspace's Lance symbol rows,
         keep only those whose persisted ``axis_contracts_json`` contains
         ≥1 contract from the role's contract set.
      2. (Optional) vector rerank — when ``query_text`` + ``embed_fn``
         are supplied, compute the L2 distance between the query
         embedding and each candidate's stored vector, and fold the
         normalised distance into the score.

    Structural narrowing comes FIRST so vector top-N doesn't drown out
    role-satisfying-but-rare symbols. The trade-off is one Lance table
    scan per call — acceptable for workspaces in the thousands; if it
    becomes a bottleneck, a dedicated ``axis_roles`` Lance column will
    let the filter run as a Lance prefilter instead.
    """
    contracts_for_role = ROLE_CONTRACT_MAP.get(role)
    if not contracts_for_role:
        return []

    table = lancedb.connect(lance_db_path).open_table("symbols_axis_python_v1")

    has_query = bool(query_text and embed_fn is not None)
    columns = ["uid", "name", "file_path", "axis_contracts_json", "workspace_id"]
    if has_query:
        columns = columns + ["vector"]

    all_rows = [
        r
        for r in table.to_lance().to_table(columns=columns).to_pylist()
        if r.get("workspace_id") == workspace_id
    ]

    # Structural filter — keep only rows matching role's contracts.
    matched_rows: list[tuple[dict, list[str]]] = []
    for row in all_rows:
        try:
            contract_objs = json.loads(row.get("axis_contracts_json") or "[]")
        except json.JSONDecodeError:
            continue
        matched = sorted({
            str(c.get("contract") or "")
            for c in contract_objs
            if str(c.get("contract") or "") in contracts_for_role
        })
        if matched:
            matched_rows.append((row, matched))

    # Optional vector rerank on the structurally-narrowed candidates.
    distances: dict[str, float] = {}
    if has_query and matched_rows:
        query_vec = embed_fn(query_text)
        if hasattr(query_vec, "tolist"):
            query_vec = query_vec.tolist()
        for row, _ in matched_rows:
            vec = row.get("vector")
            if vec is None:
                continue
            if hasattr(vec, "tolist"):
                vec = vec.tolist()
            distances[row["uid"]] = _l2_distance(query_vec, vec)

    candidates: list[RoleCandidate] = []
    total = len(contracts_for_role)
    for row, matched in matched_rows:
        distance = distances.get(row.get("uid"))
        structural = _structural_score(len(matched), total)
        semantic = _semantic_score(distance)
        candidates.append(
            RoleCandidate(
                uid=str(row.get("uid") or ""),
                name=str(row.get("name") or ""),
                file_path=str(row.get("file_path") or ""),
                role=role,
                satisfying_contracts=tuple(matched),
                contract_count=len(matched),
                vector_distance=(
                    float(distance) if distance is not None else None
                ),
                score=_combined_score(structural, semantic, has_query),
            )
        )

    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates[:limit]


def _l2_distance(a, b) -> float:
    """Plain L2 distance between two flat float sequences."""
    import math

    if a is None or b is None:
        return float("inf")
    return math.sqrt(sum((float(x) - float(y)) ** 2 for x, y in zip(a, b)))


__all__ = [
    "RoleCandidate",
    "find_symbols_by_role",
]
