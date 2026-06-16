"""Impact-analysis traversal — the blast-radius pass.

The question shape *"if this changes, what breaks?"* needs a different
walk than retrieval. The intent classifier fires ``impact_analysis``;
this module takes the already-retrieved seeds (routing, dispatch, proxy,
whatever the question gestured at) and expands them through directional
walks over the shared ``graph_walk`` core:

  1. **Reverse CFG** — every caller that reaches a seed via ``CALLS_*``
     within ``max_hops``. "Who calls X".
  2. **Forward call spine** — the publisher/dependency chain X *drives*
     via outgoing ``CALLS_*`` (``apply_async -> send_task -> route ->
     send_task_message``). High global-fan-in nodes (shared utility hubs:
     a logging/coercion helper called from everywhere) are gated out by
     comparing each node's fan-in to the closure's own median, so the
     spine stays the dispatch path and not the repo's plumbing.
  3. **Impacted tests** — reverse ``CALLS_*`` from the seeds ∪ the gated
     forward spine, kept to *test* files only: the tests that exercise
     the changed symbol or any production component on its call spine.
     This is the "and tests affected" half of an impact question, and it
     rides the SAME hub gate — without it, reverse-walking from a utility
     hub pulls in the entire suite.
  4. **Structural inheritors / API carriers** — incoming
     ``EXTENDS_EXTERNAL`` / ``INHERITED_API`` ("who inherits X") and
     outgoing ``HAS_API`` ("what API surface X carries").
  5. **Forward impact closure** — the indexer's pre-computed ``AFFECTS``
     edges walked outward; broad fallback after the precise walks.

Each walk is workspace-scoped through the ``File-CONTAINS-Symbol`` join.
Results carry the synthetic role ``impact_analysis`` and a
``satisfying_kinds`` tag naming which walk surfaced them, so the cap can
prioritise the precise spine/test walks over the broad forward closure.
"""

from __future__ import annotations

from collections.abc import Iterable
from statistics import median

from sidecar.axis.graph_walk import EdgeProfile, Neighbour, call_fan_in, walk_neighbours
from sidecar.axis.role_retrieval import RoleCandidate
from sidecar.axis.test_file_filter import is_test_path


def expand_impact_neighbourhood(
    seed_candidates: Iterable[RoleCandidate],
    *,
    db,
    workspace_id: str,
    max_hops: int = 3,
    max_impacted: int = 40,
    base_score: float = 0.35,
    exclude_uids: Iterable[str] = (),
    include_tests: bool = False,
    hub_fanin_factor: float = 2.0,
    test_reverse_hops: int = 2,
) -> list[RoleCandidate]:
    """Run the blast-radius walks from the given seeds and return a
    deduplicated list of impacted ``RoleCandidate``s tagged
    ``impact_analysis``.

    Candidates from every walk compete in ONE ranking by ``utility_score``
    (per-walk base × depth decay), so a depth-1 reverse-caller or forward
    publisher outranks a depth-3 ``AFFECTS`` leaf regardless of which walk
    found it — no walk can starve another the way a per-walk slice would.
    ``exclude_uids`` keeps the result disjoint from the input pool.

    ``include_tests`` lets the test-surface walks reach test files (impact
    questions explicitly ask "what tests are affected"); when false the
    pass stays on the production fence. ``hub_fanin_factor`` is the
    *relative* outlier multiple over the forward closure's median CALLS
    fan-in above which a node is treated as a shared utility hub and kept
    out of the test-reverse anchor set.
    """
    seeds_list = list(seed_candidates)
    if not seeds_list:
        return []
    seed_uids = [c.uid for c in seeds_list]
    excluded = set(exclude_uids) | set(seed_uids)
    exclude_tests = not include_tests

    # 1. Reverse CALLS — direct callers of the changed symbol(s).
    reverse_calls = walk_neighbours(
        db,
        workspace_id,
        seed_uids,
        edges=EdgeProfile.REVERSE_CALL,
        direction="reverse",
        max_hops=max_hops,
        exclude_tests=exclude_tests,
    )

    # 2. Forward CALLS spine — the publisher/dependency chain the change
    #    drives. Production only (tests come from walk 3); hub-gated so the
    #    chain is the dispatch path, not the repo's shared plumbing.
    forward_calls = walk_neighbours(
        db,
        workspace_id,
        seed_uids,
        edges=EdgeProfile.CALLS,
        direction="forward",
        max_hops=max_hops,
        exclude_tests=True,
    )
    spine = _hub_gate(db, workspace_id, forward_calls, hub_fanin_factor)

    # 3. Impacted tests — reverse CALLS from seeds ∪ gated spine, tests only.
    impacted_tests: list[Neighbour] = []
    if include_tests:
        anchor = seed_uids + [n.uid for n in spine]
        reverse_from_spine = walk_neighbours(
            db,
            workspace_id,
            anchor,
            edges=EdgeProfile.CALLS,
            direction="reverse",
            max_hops=test_reverse_hops,
            exclude_tests=False,
        )
        impacted_tests = [n for n in reverse_from_spine if is_test_path(n.file_path)]

    # 4. Structural dependents / API carriers.
    structural_inheritor = walk_neighbours(
        db,
        workspace_id,
        seed_uids,
        edges=EdgeProfile.STRUCTURAL_REVERSE,
        direction="reverse",
        max_hops=max_hops,
        exclude_tests=exclude_tests,
    )
    structural_api_carrier = walk_neighbours(
        db,
        workspace_id,
        seed_uids,
        edges=EdgeProfile.STRUCTURAL_FORWARD,
        direction="forward",
        max_hops=max_hops,
        exclude_tests=exclude_tests,
    )

    # 5. Broad pre-computed dataflow closure.
    forward_affects = walk_neighbours(
        db,
        workspace_id,
        seed_uids,
        edges=EdgeProfile.AFFECTS,
        direction="forward",
        max_hops=max_hops,
        exclude_tests=exclude_tests,
    )

    walks: list[tuple[str, str, list[Neighbour]]] = [
        ("reverse_calls", "CALLS_*", reverse_calls),
        ("forward_calls", "CALLS_*", spine),
        ("impacted_tests", "CALLS_*", impacted_tests),
        ("structural_inheritor", "EXTENDS_EXTERNAL|INHERITED_API", structural_inheritor),
        ("structural_api_carrier", "HAS_API", structural_api_carrier),
        ("forward_affects", "AFFECTS", forward_affects),
    ]

    # Flatten every walk into one candidate set, deduping by uid and keeping
    # the highest-utility tag for each node (a node reached by both a precise
    # reverse-call and the broad AFFECTS closure should rank as the former).
    best: dict[str, RoleCandidate] = {}
    for tag, edge_type, neighbours in walks:
        for n in neighbours:
            if n.uid in excluded:
                continue
            utility = _impact_utility(tag, n.depth)
            prior = best.get(n.uid)
            if prior is not None and (prior.utility_score or 0.0) >= utility:
                continue
            best[n.uid] = RoleCandidate(
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
                utility_score=utility,
            )

    # ONE global ranking by utility — depth-1 precise hits beat deep broad
    # ones across walks; uid breaks ties for a reproducible cut.
    ranked = sorted(best.values(), key=lambda c: (c.utility_score or 0.0, c.uid), reverse=True)
    return ranked[:max_impacted]


def _hub_gate(
    db,
    workspace_id: str,
    forward: list[Neighbour],
    hub_fanin_factor: float,
) -> list[Neighbour]:
    """Drop shared-utility hubs from the forward closure.

    A node whose global CALLS fan-in is an outlier above the closure's own
    median is plumbing (logging/coercion/exception helpers reached by many
    callers), not part of the change's dispatch spine. Comparing to the
    *median of this closure* keeps the rule relative — no absolute, per-repo
    threshold — mirroring the relative fan comparisons the role cascade uses.
    """
    if not forward:
        return []
    # Production-only fan-in: a routing/API node hammered by the test suite
    # (``route``, ``Router.prepare``) is not a god utility — counting test
    # callers would clip the very dispatch spine the impact walk needs.
    fanin = call_fan_in(db, workspace_id, [n.uid for n in forward], exclude_tests=True)
    if not fanin:
        return list(forward)
    med = median(fanin.values())
    cap = max(2.0, hub_fanin_factor * med)
    return [n for n in forward if fanin.get(n.uid, 0) <= cap]


def _impact_utility(tag: str, depth: int) -> float:
    base = {
        "reverse_calls": 0.95,
        "forward_calls": 0.90,
        "impacted_tests": 0.80,
        "structural_api_carrier": 0.86,
        "structural_inheritor": 0.82,
        "forward_affects": 0.58,
    }.get(tag, 0.50)
    return max(0.10, round(base - max(depth - 1, 0) * 0.08, 3))


__all__ = ["expand_impact_neighbourhood"]
