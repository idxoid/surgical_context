"""Impact-analysis traversal — the blast-radius pass.

The question shape *"if this changes, what breaks?"* needs a different
walk than retrieval. The intent classifier fires ``impact_analysis``;
this module takes the already-retrieved seeds (routing, dispatch, proxy,
whatever the question gestured at) and expands them through directional
walks over the shared ``graph_walk`` core:

  1. **Reverse CFG** — every caller that reaches a seed via ``CALLS_*``
     within ``max_hops``. "Who calls X".
  2. **HTTP endpoint counterpart** — client and handler symbols joined through
     the same workspace-local ``ApiEndpoint`` fingerprint.
  3. **Forward call spine** — the publisher/dependency chain X *drives*
     via outgoing ``CALLS_*`` (``apply_async -> send_task -> route ->
     send_task_message``). High global-fan-in nodes (shared utility hubs:
     a logging/coercion helper called from everywhere) are gated out by
     comparing each node's fan-in to the closure's own median, so the
     spine stays the dispatch path and not the repo's plumbing.
  4. **Impacted tests** — reverse ``CALLS_*`` from the seeds ∪ the gated
     forward spine, kept to *test* files only: the tests that exercise
     the changed symbol or any production component on its call spine.
     This is the "and tests affected" half of an impact question, and it
     rides the SAME hub gate — without it, reverse-walking from a utility
     hub pulls in the entire suite.
  5. **Structural inheritors / API carriers** — incoming
     ``EXTENDS_EXTERNAL`` / ``INHERITED_API`` ("who inherits X") and
     outgoing ``HAS_API`` ("what API surface X carries").
  6. **Forward impact closure** — the indexer's pre-computed ``AFFECTS``
     edges walked outward; broad fallback after the precise walks.

Each walk is workspace-scoped through the ``File-CONTAINS-Symbol`` join.
Results carry the synthetic role ``impact_analysis`` and a
``satisfying_kinds`` tag naming which walk surfaced them, so the cap can
prioritise the precise spine/test walks over the broad forward closure.
"""

from __future__ import annotations

from collections.abc import Iterable
from statistics import median

from context_engine.axis.graph_walk import EdgeProfile, Neighbour, call_fan_in, walk_neighbours
from context_engine.axis.role_retrieval import RoleCandidate
from context_engine.axis.test_file_filter import is_test_path

_CALLS_EDGE_TYPE = "CALLS_*"

_HTTP_ENDPOINT_EDGES = ("CALLS_ENDPOINT", "IMPLEMENTS_ENDPOINT")

# Intent roles whose canonical question shape asks about registration,
# routing, binding, or task publishing — the downstream dispatch spine,
# not "who calls this". When a publisher-axis role is at least as salient
# as the impact question-shape, forward CALLS outrank reverse CALLS.
_PUBLISHER_SPINE_INTENT_ROLES = frozenset(
    {
        "binding_surface",
        "dispatch_surface",
        "routing_surface",
        "task_surface",
    }
)


def publisher_spine_from_intent(
    intent_roles: Iterable[str] = (),
    *,
    intent_similarities: dict[str, float] | None = None,
) -> bool:
    """True when publisher-axis intent is at least as strong as impact shape.

    A test-surface impact question (``impact_analysis`` dominates) keeps the
    default reverse-first ranking so ``impacted_tests`` can compete; a
    publisher-chain question (``routing_surface`` / ``dispatch_surface`` ≥
    ``impact_analysis``) flips forward spine ahead of reverse callers.
    """
    roles = set(intent_roles)
    if intent_similarities is not None:
        publisher_best = max(
            (
                intent_similarities[r]
                for r in _PUBLISHER_SPINE_INTENT_ROLES
                if r in intent_similarities
            ),
            default=0.0,
        )
        if publisher_best <= 0.0:
            return False
        impact_sim = intent_similarities.get("impact_analysis", 0.0)
        return publisher_best >= impact_sim
    return bool(_PUBLISHER_SPINE_INTENT_ROLES.intersection(roles))


def expand_impact_neighbourhood(
    seed_candidates: Iterable[RoleCandidate],
    *,
    db,
    workspace_id: str,
    max_hops: int = 3,
    max_impacted: int = 35,
    base_score: float = 0.35,
    exclude_uids: Iterable[str] = (),
    include_tests: bool = False,
    hub_fanin_factor: float = 2.0,
    test_reverse_hops: int = 2,
    intent_roles: Iterable[str] = (),
    intent_similarities: dict[str, float] | None = None,
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
    out of the test-reverse anchor set. ``intent_roles`` carries the
    classifier's role list; when a publisher-axis role is present the
    forward dispatch spine outranks reverse callers in utility ranking.
    """
    seeds_list = list(seed_candidates)
    publisher_spine = publisher_spine_from_intent(
        intent_roles,
        intent_similarities=intent_similarities,
    )
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

    # Client/server counterpart through a shared ApiEndpoint. This cannot use
    # the in-process Symbol→Symbol adjacency because ApiEndpoint is an
    # intentional non-Symbol bridge node.
    http_endpoint_counterparts = _http_endpoint_counterparts(
        db,
        workspace_id,
        seed_uids,
        exclude_tests=exclude_tests,
    )

    # 3. Forward CALLS spine — the publisher/dependency chain the change
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

    # 4. Impacted tests — reverse CALLS from seeds ∪ gated spine, tests only.
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

    # 5. Structural dependents / API carriers.
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

    # 6. Broad pre-computed dataflow closure.
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
        ("reverse_calls", _CALLS_EDGE_TYPE, reverse_calls),
        (
            "http_endpoint_counterpart",
            "CALLS_ENDPOINT|IMPLEMENTS_ENDPOINT",
            http_endpoint_counterparts,
        ),
        ("forward_calls", _CALLS_EDGE_TYPE, spine),
        ("impacted_tests", _CALLS_EDGE_TYPE, impacted_tests),
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
            utility = _impact_utility(tag, n.depth, publisher_spine=publisher_spine)
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


def _http_endpoint_counterparts(
    db,
    workspace_id: str,
    seed_uids: list[str],
    *,
    exclude_tests: bool,
) -> list[Neighbour]:
    """Return the opposite side of workspace-local client↔handler bridges."""
    if not seed_uids:
        return []
    test_clause = ""
    if exclude_tests:
        from context_engine.axis.test_file_filter import cypher_test_exclusion_clause

        test_clause = f"AND {cypher_test_exclusion_clause('fn')}"
    query = f"""
    UNWIND $seed_uids AS su
    MATCH (:File {{workspace_id: $workspace_id}})-[:CONTAINS]->(s:Symbol {{uid: su}})
    MATCH (s)-[source_rel:CALLS_ENDPOINT|IMPLEMENTS_ENDPOINT]->(endpoint:ApiEndpoint)
          <-[target_rel:CALLS_ENDPOINT|IMPLEMENTS_ENDPOINT]-(n:Symbol)
    MATCH (fn:File {{workspace_id: $workspace_id}})-[:CONTAINS]->(n)
    WHERE type(source_rel) <> type(target_rel)
      AND source_rel.workspace_id = $workspace_id
      AND target_rel.workspace_id = $workspace_id
      {test_clause}
    RETURN n.uid AS uid, coalesce(n.name, '') AS name, fn.path AS file_path,
           1 AS depth, count(DISTINCT su) AS reach
    ORDER BY reach DESC, uid ASC
    """
    try:
        with db.driver.session() as session:
            return [
                Neighbour(
                    uid=str(row.get("uid") or ""),
                    name=str(row.get("name") or ""),
                    file_path=str(row.get("file_path") or ""),
                    depth=1,
                    reach=int(row.get("reach") or 1),
                )
                for row in session.run(
                    query,
                    seed_uids=seed_uids,
                    workspace_id=workspace_id,
                )
                if row.get("uid")
            ]
    except Exception:
        return []


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


def _impact_utility(tag: str, depth: int, *, publisher_spine: bool = False) -> float:
    if publisher_spine:
        base = {
            "forward_calls": 0.95,
            "reverse_calls": 0.90,
            "http_endpoint_counterpart": 0.92,
            "impacted_tests": 0.80,
            "structural_api_carrier": 0.86,
            "structural_inheritor": 0.82,
            "forward_affects": 0.58,
        }.get(tag, 0.50)
    else:
        base = {
            "reverse_calls": 0.95,
            "http_endpoint_counterpart": 0.92,
            "forward_calls": 0.90,
            "impacted_tests": 0.80,
            "structural_api_carrier": 0.86,
            "structural_inheritor": 0.82,
            "forward_affects": 0.58,
        }.get(tag, 0.50)
    return max(0.10, round(base - max(depth - 1, 0) * 0.08, 3))


__all__ = [
    "expand_impact_neighbourhood",
    "publisher_spine_from_intent",
]
