"""Impact-analysis traversal — the blast-radius pass.

The question shape *"if this changes, what breaks?"* needs a different
walk than retrieval. The intent classifier fires
``impact_analysis``; this module takes the already-retrieved seeds
(routing, dispatch, proxy, whatever the question gestured at) and
expands them through three directional walks:

  1. **Reverse CFG** — every caller that reaches a seed via
     ``CALLS_*`` within ``max_hops``. Encodes "who calls X". For
     ``Flask.dispatch_request`` this is ``full_dispatch_request``,
     ``wsgi_app``, and any test-client harness above them.
  2. **Forward impact closure** — the indexer's pre-computed
     ``AFFECTS`` edges, walked outward. ``AFFECTS`` already merges
     return-flow, parameter-flow, and attribute-flow into a single
     impact closure, so we do not have to recompute the dataflow at
     query time.
  3. **Structural dependencies** — incoming ``EXTENDS_EXTERNAL`` and
     ``INHERITED_API`` cover inheritors and interface implementers
     respectively; outgoing ``HAS_API`` covers the API surface a
     subtype carries through.

Each walk is workspace-scoped through the ``File-CONTAINS-Symbol``
join, so an impact reach for one workspace never leaks Symbol nodes
that belong to another.

Results carry the synthetic role ``impact_analysis``. The caller
treats them as a candidate pool exactly like a retrieval role, but
the score is fixed at ``base_score`` because impact relevance is
*structural*, not vector-similarity-driven.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from sidecar.axis.role_retrieval import RoleCandidate


# Reverse-CALLS whitelist. ``CALLS_EXTERNAL`` is excluded — its target
# is an ExternalSymbol, not a Symbol the workspace owns.
_REVERSE_CALL_RELS: tuple[str, ...] = (
    "CALLS",
    "CALLS_DIRECT",
    "CALLS_SCOPED",
    "CALLS_IMPORTED",
    "CALLS_DYNAMIC",
    "CALLS_INFERRED",
    "CALLS_GUESS",
)

# Forward impact closure — the indexer pre-computes ``AFFECTS`` from
# return-flow, parameter-flow and attribute-flow merged. Walking it
# saves us from recomputing dataflow at query time.
_FORWARD_IMPACT_RELS: tuple[str, ...] = ("AFFECTS",)

# Structural dependents. Reverse direction = "who implements / inherits
# from X". Forward direction = "what API surface X carries through".
_STRUCTURAL_REVERSE_RELS: tuple[str, ...] = (
    "EXTENDS_EXTERNAL",
    "INHERITED_API",
)
_STRUCTURAL_FORWARD_RELS: tuple[str, ...] = ("HAS_API",)


def _safe_rel_pattern(edge_types: Iterable[str]) -> str:
    """Concatenate edge types into a Cypher ``|``-pattern; reject
    anything that isn't an uppercase identifier so a malformed name can
    never smuggle a fragment into the query."""
    safe: list[str] = []
    pattern = re.compile(r"^[A-Z][A-Z0-9_]*$")
    for et in edge_types:
        if not pattern.match(et):
            raise ValueError(f"unsafe edge type: {et!r}")
        safe.append(et)
    return "|".join(safe)


def _walk_reverse(
    db,
    workspace_id: str,
    seed_uids: list[str],
    rels: tuple[str, ...],
    *,
    max_hops: int,
) -> list[dict[str, Any]]:
    """Walk against the edge direction (incoming edges) from each
    seed, return ``[{uid, name, file_path}]`` for every reached
    Symbol that belongs to the workspace."""
    if not seed_uids or not rels:
        return []
    pattern = _safe_rel_pattern(rels)
    cypher = f"""
    UNWIND $seed_uids AS su
    MATCH (origin:Symbol)-[r:{pattern}*1..{max_hops}]->(seed:Symbol {{uid: su}})
    MATCH (f:File {{workspace_id: $workspace_id}})-[:CONTAINS]->(origin)
    RETURN DISTINCT
        origin.uid AS uid,
        coalesce(origin.name, '') AS name,
        f.path AS file_path
    """
    out: list[dict[str, Any]] = []
    try:
        with db.driver.session() as session:
            for rec in session.run(
                cypher, seed_uids=list(seed_uids), workspace_id=workspace_id,
            ):
                uid = str(rec.get("uid") or "")
                if not uid:
                    continue
                out.append(
                    {
                        "uid": uid,
                        "name": str(rec.get("name") or ""),
                        "file_path": str(rec.get("file_path") or ""),
                    }
                )
    except Exception:
        return []
    return out


def _walk_forward(
    db,
    workspace_id: str,
    seed_uids: list[str],
    rels: tuple[str, ...],
    *,
    max_hops: int,
) -> list[dict[str, Any]]:
    """Walk along the edge direction (outgoing edges) from each seed,
    return ``[{uid, name, file_path}]`` for every reached Symbol that
    belongs to the workspace."""
    if not seed_uids or not rels:
        return []
    pattern = _safe_rel_pattern(rels)
    cypher = f"""
    UNWIND $seed_uids AS su
    MATCH (seed:Symbol {{uid: su}})-[r:{pattern}*1..{max_hops}]->(target:Symbol)
    MATCH (f:File {{workspace_id: $workspace_id}})-[:CONTAINS]->(target)
    RETURN DISTINCT
        target.uid AS uid,
        coalesce(target.name, '') AS name,
        f.path AS file_path
    """
    out: list[dict[str, Any]] = []
    try:
        with db.driver.session() as session:
            for rec in session.run(
                cypher, seed_uids=list(seed_uids), workspace_id=workspace_id,
            ):
                uid = str(rec.get("uid") or "")
                if not uid:
                    continue
                out.append(
                    {
                        "uid": uid,
                        "name": str(rec.get("name") or ""),
                        "file_path": str(rec.get("file_path") or ""),
                    }
                )
    except Exception:
        return []
    return out


def expand_impact_neighbourhood(
    seed_candidates: Iterable[RoleCandidate],
    *,
    db,
    workspace_id: str,
    max_hops: int = 3,
    max_impacted: int = 30,
    base_score: float = 0.35,
    exclude_uids: Iterable[str] = (),
) -> list[RoleCandidate]:
    """Run the three blast-radius walks from the given seeds and
    return a deduplicated list of impacted ``RoleCandidate``s tagged
    ``impact_analysis``.

    ``exclude_uids`` keeps the result disjoint from the input pool —
    the consumer typically passes every seed's uid so the impacted
    list contains only *new* symbols.
    """
    seeds_list = list(seed_candidates)
    if not seeds_list:
        return []
    seed_uids = [c.uid for c in seeds_list]
    excluded = set(exclude_uids) | set(seed_uids)

    reverse_calls = _walk_reverse(
        db, workspace_id, seed_uids, _REVERSE_CALL_RELS, max_hops=max_hops,
    )
    forward_impact = _walk_forward(
        db,
        workspace_id,
        seed_uids,
        _FORWARD_IMPACT_RELS,
        max_hops=max_hops,
    )
    structural_rev = _walk_reverse(
        db,
        workspace_id,
        seed_uids,
        _STRUCTURAL_REVERSE_RELS,
        max_hops=max_hops,
    )
    structural_fwd = _walk_forward(
        db,
        workspace_id,
        seed_uids,
        _STRUCTURAL_FORWARD_RELS,
        max_hops=max_hops,
    )

    # Each pass tags its results with the structural reason — useful
    # for downstream explainability and for prioritising the cap.
    tagged: list[tuple[str, dict[str, Any]]] = []
    for rec in reverse_calls:
        tagged.append(("reverse_calls", rec))
    for rec in forward_impact:
        tagged.append(("forward_affects", rec))
    for rec in structural_rev:
        tagged.append(("structural_inheritor", rec))
    for rec in structural_fwd:
        tagged.append(("structural_api_carrier", rec))

    seen: set[str] = set()
    out: list[RoleCandidate] = []
    for kind_tag, rec in tagged:
        uid = rec["uid"]
        if uid in excluded or uid in seen:
            continue
        seen.add(uid)
        out.append(
            RoleCandidate(
                uid=uid,
                name=rec["name"],
                file_path=rec["file_path"],
                role="impact_analysis",
                satisfying_contracts=(),
                satisfying_kinds=(kind_tag,),
                contract_count=0,
                kind_count=1,
                vector_distance=None,
                score=base_score,
            )
        )
        if len(out) >= max_impacted:
            break
    return out


__all__ = ["expand_impact_neighbourhood"]
