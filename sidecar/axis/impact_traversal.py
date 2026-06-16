"""Impact-analysis traversal — the blast-radius pass.

The question shape *"if this changes, what breaks?"* needs a different
walk than retrieval. The intent classifier fires
``impact_analysis``; this module takes the already-retrieved seeds
(routing, dispatch, proxy, whatever the question gestured at) and
expands them through four directional walks over the shared
``graph_walk`` core:

  1. **Reverse CFG** — every caller that reaches a seed via
     ``CALLS_*`` within ``max_hops``. "Who calls X".
  2. **Structural inheritors** — incoming ``EXTENDS_EXTERNAL`` /
     ``INHERITED_API`` ("who implements / inherits from X").
  3. **Structural API carriers** — outgoing ``HAS_API`` ("what API
     surface X carries through").
  4. **Forward impact closure** — the indexer's pre-computed
     ``AFFECTS`` edges walked outward; broad fallback after the more
     precise seed-local walks.

Each walk is workspace-scoped through the ``File-CONTAINS-Symbol``
join. Results carry the synthetic role ``impact_analysis`` and a
``satisfying_kinds`` tag naming which walk surfaced them, so the cap
can prioritise reverse-callers over the broader forward closure.
"""

from __future__ import annotations

from collections.abc import Iterable

from sidecar.axis.graph_walk import EdgeProfile, Neighbour, walk_neighbours
from sidecar.axis.role_retrieval import RoleCandidate


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
    """Run the four blast-radius walks from the given seeds and return
    a deduplicated list of impacted ``RoleCandidate``s tagged
    ``impact_analysis``.

    Walk priority is preserved in the output order: reverse-callers
    first, then structural inheritors and API carriers, then the
    broader forward AFFECTS closure. ``exclude_uids`` keeps the result
    disjoint from the input pool.
    """
    seeds_list = list(seed_candidates)
    if not seeds_list:
        return []
    seed_uids = [c.uid for c in seeds_list]
    excluded = set(exclude_uids) | set(seed_uids)

    # Four walks, each tagged with the structural reason. Order of the
    # list sets the cap priority: a reverse-caller outranks a node only
    # reachable through the broad forward closure.
    walks: list[tuple[str, str, list[Neighbour]]] = [
        (
            "reverse_calls",
            "CALLS_*",
            walk_neighbours(
                db,
                workspace_id,
                seed_uids,
                edges=EdgeProfile.REVERSE_CALL,
                direction="reverse",
                max_hops=max_hops,
            ),
        ),
        (
            "structural_inheritor",
            "EXTENDS_EXTERNAL|INHERITED_API",
            walk_neighbours(
                db,
                workspace_id,
                seed_uids,
                edges=EdgeProfile.STRUCTURAL_REVERSE,
                direction="reverse",
                max_hops=max_hops,
            ),
        ),
        (
            "structural_api_carrier",
            "HAS_API",
            walk_neighbours(
                db,
                workspace_id,
                seed_uids,
                edges=EdgeProfile.STRUCTURAL_FORWARD,
                direction="forward",
                max_hops=max_hops,
            ),
        ),
        (
            "forward_affects",
            "AFFECTS",
            walk_neighbours(
                db,
                workspace_id,
                seed_uids,
                edges=EdgeProfile.AFFECTS,
                direction="forward",
                max_hops=max_hops,
            ),
        ),
    ]

    seen: set[str] = set()
    out: list[RoleCandidate] = []
    for tag, edge_type, neighbours in walks:
        for n in neighbours:
            if n.uid in excluded or n.uid in seen:
                continue
            seen.add(n.uid)
            out.append(
                RoleCandidate(
                    uid=n.uid,
                    name=n.name,
                    file_path=n.file_path,
                    role="impact_analysis",
                    satisfying_contracts=(),
                    satisfying_kinds=(tag,),
                    contract_count=0,
                    kind_count=1,
                    vector_distance=None,
                    score=base_score,
                    depth=n.depth,
                    edge_type=edge_type,
                    utility_score=_impact_utility(tag, n.depth),
                )
            )
            if len(out) >= max_impacted:
                return out
    return out


def _impact_utility(tag: str, depth: int) -> float:
    base = {
        "reverse_calls": 0.95,
        "structural_api_carrier": 0.86,
        "structural_inheritor": 0.82,
        "forward_affects": 0.58,
    }.get(tag, 0.50)
    return max(0.10, round(base - max(depth - 1, 0) * 0.08, 3))


__all__ = ["expand_impact_neighbourhood"]
