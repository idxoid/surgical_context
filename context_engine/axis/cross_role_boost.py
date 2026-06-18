"""Multi-role intersection boost.

When the intent classifier produces multiple roles for a single
question (``proxy_mechanism (0.65)`` + ``routing_surface (0.33)`` for a
flask question about request handling), the legacy approach would
``union`` the two candidate lists — easy to implement but it drowns the
primary answer in secondary-role noise, and it forces the consumer to
choose a threshold that artificially expands the funnel.

This module takes the opposite cut: weak secondary intents are used as
**structural constraints** rather than as a separate candidate pool. A
primary-role candidate is *boosted* when it sits within ``max_hops`` of
any candidate that satisfies a secondary role, via the workspace's
graph relations (``CALLS_*``, ``HAS_API``, ``INHERITED_API``,
``HANDLES``, ``INSTANTIATES``, ``DEPENDS_ON``). If a flask Variable
carrying ``proxy_mechanism`` is structurally connected to a routing
node carrying ``routing_surface``, it is more likely to be the answer.

The pass is gentle by design — *boost*, not *filter*. A primary
candidate without secondary-role neighbours keeps its original score,
so the worst case is "no change" rather than "answer drops out".
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace

from context_engine.axis.graph_walk import EdgeProfile, _safe_max_hops, _safe_rel_pattern
from context_engine.axis.role_retrieval import RoleCandidate

# Edge whitelist for the proximity walk — shared with the rest of the
# axis expansion stack via ``EdgeProfile.PROXIMITY`` so widening it in
# one place reaches intersection, lookahead, and the structural passes
# alike. ``cross_role_boost`` keeps its own Cypher (it filters the walk
# to a *secondary-uid set* and groups by primary, which the generic
# ``walk_neighbours`` does not) but draws the relation list from the
# single source of truth.
_PROXIMITY_RELS: tuple[str, ...] = EdgeProfile.PROXIMITY


def _query_proximity_roles(
    db,
    workspace_id: str,
    primary_uids: list[str],
    secondary_role_uids: Mapping[str, set[str]],
    *,
    max_hops: int,
) -> dict[str, set[str]]:
    """For each primary uid, return the set of secondary role names
    whose candidates lie within ``max_hops`` via the proximity-edge
    whitelist.
    """
    if not primary_uids or not secondary_role_uids:
        return {}
    flat_secondary_uids: set[str] = set()
    role_by_uid: dict[str, set[str]] = {}
    for role, uids in secondary_role_uids.items():
        for uid in uids:
            flat_secondary_uids.add(uid)
            role_by_uid.setdefault(uid, set()).add(role)
    if not flat_secondary_uids:
        return {}

    from context_engine.axis import graph_walk_inproc

    if graph_walk_inproc.should_use(workspace_id):
        return graph_walk_inproc.query_proximity_roles(
            db,
            workspace_id,
            list(primary_uids),
            secondary_role_uids,
            edges=frozenset(_PROXIMITY_RELS),
            max_hops=max_hops,
        )

    rel_pattern = _safe_rel_pattern(_PROXIMITY_RELS)
    hops = _safe_max_hops(max_hops)
    cypher = f"""
    UNWIND $primary_uids AS pu
    MATCH (p:Symbol {{uid: pu}})
    MATCH (p)-[r:{rel_pattern}*1..{hops}]-(n:Symbol)
    WHERE n.uid IN $secondary_uids
      AND all(rel IN r WHERE coalesce(rel.workspace_id, $workspace_id) = $workspace_id)
    RETURN p.uid AS primary_uid, collect(DISTINCT n.uid) AS reachable
    """
    out: dict[str, set[str]] = {}
    try:
        with db.driver.session() as session:
            for record in session.run(
                cypher,
                primary_uids=list(primary_uids),
                secondary_uids=list(flat_secondary_uids),
                workspace_id=workspace_id,
            ):
                primary_uid = str(record.get("primary_uid") or "")
                if not primary_uid:
                    continue
                roles: set[str] = set()
                for reached in record.get("reachable") or []:
                    roles |= role_by_uid.get(str(reached), set())
                if roles:
                    out[primary_uid] = roles
    except Exception:
        return {}
    return out


def intersect_by_cross_role_proximity(
    primary: list[RoleCandidate],
    secondary_by_role: Mapping[str, list[RoleCandidate]],
    *,
    db,
    workspace_id: str,
    max_hops: int = 2,
    boost_per_role: float = 0.15,
    score_ceiling: float = 1.0,
    fallback_on_empty: bool = True,
) -> list[RoleCandidate]:
    """Intersection-based multi-role pass.

    A primary candidate is **kept** only when it sits within
    ``max_hops`` of at least one candidate that satisfies a secondary
    role, via the proximity-edge whitelist. Surviving candidates also
    get a per-secondary-role score boost so the consumer sees how
    structurally connected they are.

    The intersection is filtering by structural constraint — exactly
    what the L4 layer needs when multiple weak intents fire: it
    converts the conjunction into a graph join over the workspace
    rather than a fragile union of independent lists.

    ``fallback_on_empty`` (default ``True``) returns the original
    primary list when no candidate has cross-role neighbours, so a
    single-role question, an indexer gap, or a too-narrow intent does
    not silently produce zero results.
    """
    if not primary:
        return list(primary)
    secondary_role_uids = {
        role: {c.uid for c in cands} for role, cands in secondary_by_role.items() if cands
    }
    if not secondary_role_uids:
        return list(primary)
    proximity = _query_proximity_roles(
        db,
        workspace_id,
        [c.uid for c in primary],
        secondary_role_uids,
        max_hops=max_hops,
    )
    intersected: list[RoleCandidate] = []
    for cand in primary:
        roles_hit = proximity.get(cand.uid, set())
        if not roles_hit:
            continue
        new_score = min(score_ceiling, cand.score + boost_per_role * len(roles_hit))
        intersected.append(replace(cand, score=new_score))
    if not intersected and fallback_on_empty:
        return list(primary)
    intersected.sort(key=lambda c: c.score, reverse=True)
    return intersected


# Backward-compatible alias for the older boost-only behaviour. The
# intersection function is the new default; both share the same proximity
# query so wiring stays uniform.
def boost_by_cross_role_proximity(
    primary: list[RoleCandidate],
    secondary_by_role: Mapping[str, list[RoleCandidate]],
    *,
    db,
    workspace_id: str,
    max_hops: int = 2,
    boost_per_role: float = 0.15,
    score_ceiling: float = 1.0,
) -> list[RoleCandidate]:
    """Score-only variant — keeps every primary candidate, boosts the
    ones with cross-role neighbours. Kept for callers that want the
    older soft-rerank behaviour.
    """
    if not primary:
        return list(primary)
    secondary_role_uids = {
        role: {c.uid for c in cands} for role, cands in secondary_by_role.items() if cands
    }
    if not secondary_role_uids:
        return list(primary)
    proximity = _query_proximity_roles(
        db,
        workspace_id,
        [c.uid for c in primary],
        secondary_role_uids,
        max_hops=max_hops,
    )
    boosted: list[RoleCandidate] = []
    for cand in primary:
        roles_hit = proximity.get(cand.uid, set())
        if not roles_hit:
            boosted.append(cand)
            continue
        new_score = min(score_ceiling, cand.score + boost_per_role * len(roles_hit))
        boosted.append(replace(cand, score=new_score))
    boosted.sort(key=lambda c: c.score, reverse=True)
    return boosted


__all__ = [
    "boost_by_cross_role_proximity",
    "intersect_by_cross_role_proximity",
]
