"""Impact-analysis traversal — the blast-radius pass.

The question shape *"if this changes, what breaks?"* needs a different
walk than retrieval. The intent classifier fires
``impact_analysis``; this module takes the already-retrieved seeds
(routing, dispatch, proxy, whatever the question gestured at) and
expands them through four directional walks over the shared
``graph_walk`` core:

  1. **Reverse CFG** — every caller that reaches a seed via
     ``CALLS_*`` within ``max_hops``. "Who calls X".
  2. **Forward impact closure** — the indexer's pre-computed
     ``AFFECTS`` edges walked outward; already merges return-,
     parameter- and attribute-flow.
  3. **Structural inheritors** — incoming ``EXTENDS_EXTERNAL`` /
     ``INHERITED_API`` ("who implements / inherits from X").
  4. **Structural API carriers** — outgoing ``HAS_API`` ("what API
     surface X carries through").

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
    first, then the forward AFFECTS closure, then structural
    inheritors and API carriers. ``exclude_uids`` keeps the result
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
    walks: list[tuple[str, list[Neighbour]]] = [
        (
            "reverse_calls",
            walk_neighbours(
                db, workspace_id, seed_uids,
                edges=EdgeProfile.REVERSE_CALL,
                direction="reverse", max_hops=max_hops,
            ),
        ),
        (
            "forward_affects",
            walk_neighbours(
                db, workspace_id, seed_uids,
                edges=EdgeProfile.AFFECTS,
                direction="forward", max_hops=max_hops,
            ),
        ),
        (
            "structural_inheritor",
            walk_neighbours(
                db, workspace_id, seed_uids,
                edges=EdgeProfile.STRUCTURAL_REVERSE,
                direction="reverse", max_hops=max_hops,
            ),
        ),
        (
            "structural_api_carrier",
            walk_neighbours(
                db, workspace_id, seed_uids,
                edges=EdgeProfile.STRUCTURAL_FORWARD,
                direction="forward", max_hops=max_hops,
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
                    role="impact_analysis",
                    satisfying_contracts=(),
                    satisfying_kinds=(tag,),
                    contract_count=0,
                    kind_count=1,
                    vector_distance=None,
                    score=base_score,
                )
            )
            if len(out) >= max_impacted:
                return out
    return out


__all__ = ["expand_impact_neighbourhood"]
