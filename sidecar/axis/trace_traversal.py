"""Trace-dependency traversal -- call-chain pass.

``trace_dependency`` asks "who calls this?" and "what does this call or
delegate to?". That is narrower than impact analysis: it should walk
CALLS_* edges only, not AFFECTS, inheritance, or API-carrier edges.

Results carry role ``trace_dependency`` and a direction tag:

* ``trace_callers`` -- reverse CALLS_* into the seed.
* ``trace_callees`` -- forward CALLS_* out of the seed.
"""

from __future__ import annotations

from collections.abc import Iterable

from sidecar.axis.graph_walk import EdgeProfile, Neighbour, walk_neighbours
from sidecar.axis.role_retrieval import RoleCandidate


def expand_trace_neighbourhood(
    seed_candidates: Iterable[RoleCandidate],
    *,
    db,
    workspace_id: str,
    max_hops: int = 3,
    max_traced: int = 30,
    base_score: float = 0.35,
    exclude_uids: Iterable[str] = (),
) -> list[RoleCandidate]:
    """Return call-chain neighbours for ``trace_dependency`` questions.

    Reverse callers are emitted before forward callees so "who reaches
    this?" stays the strongest trace signal. ``exclude_uids`` keeps the
    result disjoint from the caller's existing candidate pool.
    """
    seeds_list = list(seed_candidates)
    if not seeds_list:
        return []
    seed_uids = [c.uid for c in seeds_list]
    excluded = set(exclude_uids) | set(seed_uids)

    walks: list[tuple[str, list[Neighbour]]] = [
        (
            "trace_callers",
            walk_neighbours(
                db,
                workspace_id,
                seed_uids,
                edges=EdgeProfile.CALLS,
                direction="reverse",
                max_hops=max_hops,
            ),
        ),
        (
            "trace_callees",
            walk_neighbours(
                db,
                workspace_id,
                seed_uids,
                edges=EdgeProfile.CALLS,
                direction="forward",
                max_hops=max_hops,
            ),
        ),
    ]

    seen: set[str] = set()
    out: list[RoleCandidate] = []
    for tag, neighbours in walks:
        for n in neighbours:
            if n.uid in excluded or n.uid in seen:
                continue
            seen.add(n.uid)
            out.append(
                RoleCandidate(
                    uid=n.uid,
                    name=n.name,
                    file_path=n.file_path,
                    role="trace_dependency",
                    satisfying_contracts=(),
                    satisfying_kinds=(tag,),
                    contract_count=0,
                    kind_count=1,
                    vector_distance=None,
                    score=base_score,
                )
            )
            if len(out) >= max_traced:
                return out
    return out


__all__ = ["expand_trace_neighbourhood"]
