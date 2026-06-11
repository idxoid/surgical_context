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

from sidecar.axis.role_resolver import ROLE_EVIDENCE_MAP


@dataclass(frozen=True)
class RoleCandidate:
    """One symbol satisfying a role, with both evidence channels (the
    L3 contracts and the L2 container kinds that fired) plus the
    ranking score components."""

    uid: str
    name: str
    file_path: str
    role: str
    satisfying_contracts: tuple[str, ...]
    satisfying_kinds: tuple[str, ...]
    contract_count: int
    kind_count: int
    vector_distance: float | None
    score: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "uid": self.uid,
            "name": self.name,
            "file_path": self.file_path,
            "role": self.role,
            "satisfying_contracts": list(self.satisfying_contracts),
            "satisfying_kinds": list(self.satisfying_kinds),
            "contract_count": self.contract_count,
            "kind_count": self.kind_count,
            "vector_distance": self.vector_distance,
            "score": self.score,
        }


def _structural_score(
    matched_contracts: int,
    matched_kinds: int,
    total_contracts: int,
    total_kinds: int,
) -> float:
    """``[0, 1]`` proportion of the role's evidence that fired on this
    symbol, computed across both contracts and kinds.

    Contracts are weighted slightly higher (1.0) than kinds (0.6)
    because contracts include the use-proof side of binding while kinds
    are existence-only. A symbol with a contract match outranks a
    symbol that only matches by kind, when both are otherwise tied.
    """
    contract_weight = 1.0
    kind_weight = 0.6
    total = total_contracts * contract_weight + total_kinds * kind_weight
    if total <= 0:
        return 0.0
    matched = matched_contracts * contract_weight + matched_kinds * kind_weight
    return min(1.0, matched / total)


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


def scan_workspace_rows(
    workspace_id: str,
    *,
    lance_db_path: str = "./data/lancedb",
    include_tests: bool = False,
    with_vector: bool = True,
) -> list[dict]:
    """One workspace-scoped Lance scan, JSON parsed once.

    The whole retrieval layer used to scan the *entire* symbol table
    (every workspace) and filter in Python — once per role, three times
    per question. For a 14k-symbol workspace inside a 43k-row table that
    materialised 43k rows × the 384-dim vector column three times over.

    This reads the workspace's rows in a single pass: the ``workspace_id``
    equality is pushed down into Lance (a C++ bitmask, not a Python
    loop), the test-file fence runs once, and each row's contracts/kinds
    JSON is parsed once into ``_contracts`` / ``_kinds`` sets so every
    downstream role match is a cheap set intersection. Pass the result
    to ``find_symbols_by_roles`` and ``find_seeds_by_vector`` to serve
    both off one scan.
    """
    table = lancedb.connect(lance_db_path).open_table("symbols_axis_python_v1")
    columns = [
        "uid",
        "name",
        "file_path",
        "axis_contracts_json",
        "axis_container_kinds_json",
        "workspace_id",
    ]
    if with_vector:
        columns.append("vector")
    ws_quoted = workspace_id.replace("'", "''")
    arrow = table.to_lance().to_table(
        columns=columns, filter=f"workspace_id = '{ws_quoted}'"
    )
    from sidecar.axis.test_file_filter import is_test_path

    out: list[dict] = []
    for r in arrow.to_pylist():
        if not include_tests and is_test_path(str(r.get("file_path") or "")):
            continue
        try:
            contract_objs = json.loads(r.get("axis_contracts_json") or "[]")
        except json.JSONDecodeError:
            contract_objs = []
        try:
            kind_objs = json.loads(r.get("axis_container_kinds_json") or "[]")
        except json.JSONDecodeError:
            kind_objs = []
        r["_contracts"] = {
            str(c.get("contract") or "") for c in contract_objs
        }
        r["_kinds"] = {str(k.get("kind") or "") for k in kind_objs}
        out.append(r)
    return out


def find_symbols_by_roles(
    workspace_id: str,
    roles: list[str],
    *,
    query_text: str | None = None,
    limit: int = 25,
    lance_db_path: str = "./data/lancedb",
    embed_fn=None,
    include_tests: bool = False,
    prescanned: list[dict] | None = None,
) -> dict[str, list[RoleCandidate]]:
    """Batch role retrieval off a single scan.

    Distributes the pre-scanned, pre-parsed workspace rows to each role
    by set intersection, embeds the query once, and caches per-uid
    distances across roles. Equivalent to calling ``find_symbols_by_role``
    per role but with one scan + one embed instead of N of each.
    """
    rows = (
        prescanned
        if prescanned is not None
        else scan_workspace_rows(
            workspace_id,
            lance_db_path=lance_db_path,
            include_tests=include_tests,
        )
    )
    has_query = bool(query_text and embed_fn is not None)
    query_vec = None
    if has_query:
        query_vec = embed_fn(query_text)
        if hasattr(query_vec, "tolist"):
            query_vec = query_vec.tolist()
    distance_cache: dict[str, float | None] = {}

    def _distance(row: dict) -> float | None:
        uid = str(row.get("uid") or "")
        if uid in distance_cache:
            return distance_cache[uid]
        d: float | None = None
        if has_query:
            vec = row.get("vector")
            if vec is not None:
                if hasattr(vec, "tolist"):
                    vec = vec.tolist()
                d = _l2_distance(query_vec, vec)
        distance_cache[uid] = d
        return d

    out: dict[str, list[RoleCandidate]] = {}
    for role in roles:
        evidence = ROLE_EVIDENCE_MAP.get(role)
        if evidence is None or (not evidence.contracts and not evidence.kinds):
            out[role] = []
            continue
        total_contracts = len(evidence.contracts)
        total_kinds = len(evidence.kinds)
        candidates: list[RoleCandidate] = []
        for row in rows:
            matched_contracts = sorted(row["_contracts"] & evidence.contracts)
            matched_kinds = sorted(row["_kinds"] & evidence.kinds)
            if not (matched_contracts or matched_kinds):
                continue
            distance = _distance(row)
            structural = _structural_score(
                len(matched_contracts),
                len(matched_kinds),
                total_contracts,
                total_kinds,
            )
            semantic = _semantic_score(distance)
            candidates.append(
                RoleCandidate(
                    uid=str(row.get("uid") or ""),
                    name=str(row.get("name") or ""),
                    file_path=str(row.get("file_path") or ""),
                    role=role,
                    satisfying_contracts=tuple(matched_contracts),
                    satisfying_kinds=tuple(matched_kinds),
                    contract_count=len(matched_contracts),
                    kind_count=len(matched_kinds),
                    vector_distance=(
                        float(distance) if distance is not None else None
                    ),
                    score=_combined_score(structural, semantic, has_query),
                )
            )
        candidates.sort(key=lambda c: c.score, reverse=True)
        out[role] = candidates[:limit]
    return out


def find_symbols_by_role(
    workspace_id: str,
    role: str,
    *,
    query_text: str | None = None,
    limit: int = 25,
    lance_db_path: str = "./data/lancedb",
    embed_fn=None,
    include_tests: bool = False,
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
    # Thin wrapper over the batch path — one role, one scan (now with
    # workspace predicate pushdown). Kept for the many existing callers
    # and tests that retrieve a single role.
    return find_symbols_by_roles(
        workspace_id,
        [role],
        query_text=query_text,
        limit=limit,
        lance_db_path=lance_db_path,
        embed_fn=embed_fn,
        include_tests=include_tests,
    ).get(role, [])


def find_seeds_by_vector(
    workspace_id: str,
    query_text: str,
    *,
    embed_fn,
    limit: int = 12,
    lance_db_path: str = "./data/lancedb",
    include_tests: bool = False,
    prescanned: list[dict] | None = None,
) -> list[RoleCandidate]:
    """Role-AGNOSTIC vector seed retrieval — top-``limit`` symbols by
    embedding similarity, with NO role/kind filter.

    This is the seed source that keeps the intent classifier out of
    structure selection. ``find_symbols_by_role`` gates Lance rows by a
    role's evidence kinds/contracts; when intent picks the wrong role
    (django ``proxy_mechanism`` for a QuerySet, click ``proxy_mechanism``
    for ``Context.parse_args``) the gate discards the right nodes. Pure
    similarity does not gate — it finds the structurally-nearest symbols
    regardless of role, and the reactive traversal + intent ranking take
    it from there.

    Returns candidates tagged ``role="vector_seed"``; ``score`` is the
    normalised semantic score so the consumer can rank them against
    role-evidenced candidates.
    """
    if not query_text or embed_fn is None:
        return []
    rows = (
        prescanned
        if prescanned is not None
        else scan_workspace_rows(
            workspace_id,
            lance_db_path=lance_db_path,
            include_tests=include_tests,
        )
    )
    if not rows:
        return []
    query_vec = embed_fn(query_text)
    if hasattr(query_vec, "tolist"):
        query_vec = query_vec.tolist()

    scored: list[tuple[float, dict]] = []
    for row in rows:
        vec = row.get("vector")
        if vec is None:
            continue
        if hasattr(vec, "tolist"):
            vec = vec.tolist()
        scored.append((_l2_distance(query_vec, vec), row))
    scored.sort(key=lambda t: t[0])

    out: list[RoleCandidate] = []
    for distance, row in scored[:limit]:
        out.append(
            RoleCandidate(
                uid=str(row.get("uid") or ""),
                name=str(row.get("name") or ""),
                file_path=str(row.get("file_path") or ""),
                role="vector_seed",
                satisfying_contracts=(),
                satisfying_kinds=(),
                contract_count=0,
                kind_count=0,
                vector_distance=float(distance),
                score=_semantic_score(distance),
            )
        )
    return out


def _l2_distance(a, b) -> float:
    """Plain L2 distance between two flat float sequences."""
    import math

    if a is None or b is None:
        return float("inf")
    return math.sqrt(sum((float(x) - float(y)) ** 2 for x, y in zip(a, b)))


__all__ = [
    "RoleCandidate",
    "find_seeds_by_vector",
    "find_symbols_by_role",
    "find_symbols_by_roles",
    "scan_workspace_rows",
]
