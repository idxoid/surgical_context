"""File-level structural neighbour expansion via ``AFFECTS``.

Some answers sit in workspace files that have no role kind, no contract,
no shared external dependency with the retrieved candidates — but the
indexer's ``AFFECTS`` closure already proves they belong to the same
structural region. ``fastapi/concurrency.py``'s
``contextmanager_in_threadpool`` is the case the user named: its
``AFFECTS`` reach loops through ``routing.get_request_handler`` (which
calls ``routing.run_endpoint_function``), but no other retrieval pass
sees concurrency.py because:

  - it has no axis container_kind (only `_T` TypeVar and one
    ``@asynccontextmanager`` helper),
  - it shares no small-fan external with routing.py (``run_endpoint_function``
    consumes ``starlette.concurrency.run_in_threadpool`` directly;
    ``contextmanager_in_threadpool`` consumes ``anyio.*``),
  - and its CALLS edges to ``run_in_threadpool`` are aliased away by
    ``from starlette.concurrency import run_in_threadpool as run_in_threadpool``
    which the Python adapter does not trace.

This pass is a *file-level* bridge — it walks ``AFFECTS`` undirected
from each seed (via the shared ``graph_walk`` core) and returns one or
two reached symbols from each *previously-unseen* file, capped tightly.
The pass is intentionally broad (no kind filter) and precision-
controlled by the caps. Score (``base_score=0.25``) sits below
lookahead's 0.40 so vector candidates always rank above
structural-neighbours.
"""

from __future__ import annotations

from collections.abc import Iterable

from context_engine.axis.graph_walk import EdgeProfile, cap_by_file, walk_neighbours
from context_engine.axis.role_retrieval import RoleCandidate

# The broad file-bridge stays pinned to the AFFECTS closure — NOT the full
# DATAFLOW axis. FLOWS_INTO joined the axis as its primary member, but folding
# it into this 2-hop union walk drowns the tight sibling signal in closure
# fan-out (hub files reached by most of the pool win every file slot); the
# direct pairs get their own 1-hop pass below instead.
_AFFECTS_EDGES = EdgeProfile.AFFECTS
_FLOW_SIBLING_EDGES: tuple[str, ...] = ("FLOWS_INTO",)


def expand_structural_neighbours(
    seed_candidates: Iterable[RoleCandidate],
    *,
    db,
    workspace_id: str,
    max_hops: int = 2,
    max_files: int = 5,
    max_per_file: int = 2,
    max_total: int = 10,
    base_score: float = 0.25,
    exclude_uids: Iterable[str] = (),
    include_tests: bool = False,
) -> list[RoleCandidate]:
    """Walk ``AFFECTS`` undirected K hops from each seed, return
    deduplicated symbols from *previously-unseen* files (relative to
    the seeds' own files), capped.

    Returned candidates carry the pseudo-role ``structural_neighbour``
    and ``satisfying_kinds=("affects_bridge",)`` so the consumer can
    tell file-level bridges apart from lookahead's kind-evidenced
    injections.
    """
    seeds = list(seed_candidates)
    if not seeds:
        return []
    seed_uids = [c.uid for c in seeds]
    seed_files = {c.file_path for c in seeds if c.file_path}

    neighbours = walk_neighbours(
        db,
        workspace_id,
        seed_uids,
        edges=_AFFECTS_EDGES,
        direction="undirected",
        max_hops=max_hops,
        exclude_tests=not include_tests,
    )
    capped = cap_by_file(
        neighbours,
        seed_files=seed_files,
        exclude_uids=set(exclude_uids) | set(seed_uids),
        max_per_file=max_per_file,
        max_files=max_files,
        max_total=max_total,
    )
    return [
        RoleCandidate(
            uid=n.uid,
            name=n.name,
            file_path=n.file_path,
            role="structural_neighbour",
            satisfying_contracts=(),
            satisfying_kinds=("affects_bridge",),
            contract_count=0,
            kind_count=1,
            vector_distance=None,
            score=base_score,
        )
        for n in capped
    ]


def expand_dataflow_siblings(
    seed_candidates: Iterable[RoleCandidate],
    *,
    db,
    workspace_id: str,
    max_files: int = 3,
    max_per_file: int = 2,
    max_total: int = 6,
    # Above the AFFECTS bridge's 0.25 (a direct per-call-site fact is a
    # strictly stronger evidence class than membership in the 2-hop closure
    # region), still below lookahead's 0.40 so vector candidates keep rank.
    base_score: float = 0.35,
    exclude_uids: Iterable[str] = (),
    include_tests: bool = False,
) -> list[RoleCandidate]:
    """Direct co-invocation dataflow siblings of the pool, one hop only.

    ``FLOWS_INTO`` is a primary per-call-site fact (some caller binds a pool
    candidate's result and passes it into the neighbour, or vice versa), so a
    single undirected hop is the whole honest reach — no closure walk. The fan
    is naturally tight (measured ~19 new files from an 83-seed pool on the
    dogfood repo vs ~600 for the 2-hop AFFECTS region), which is exactly why
    it must not share the broad bridge's walk: hub files reached by most of
    the pool would claim every file slot ahead of a reach-3 direct sibling.
    """
    seeds = list(seed_candidates)
    if not seeds:
        return []
    seed_uids = [c.uid for c in seeds]
    seed_files = {c.file_path for c in seeds if c.file_path}

    neighbours = walk_neighbours(
        db,
        workspace_id,
        seed_uids,
        edges=_FLOW_SIBLING_EDGES,
        direction="undirected",
        max_hops=1,
        exclude_tests=not include_tests,
    )
    capped = cap_by_file(
        neighbours,
        seed_files=seed_files,
        exclude_uids=set(exclude_uids) | set(seed_uids),
        max_per_file=max_per_file,
        max_files=max_files,
        max_total=max_total,
    )
    return [
        RoleCandidate(
            uid=n.uid,
            name=n.name,
            file_path=n.file_path,
            role="structural_neighbour",
            satisfying_contracts=(),
            satisfying_kinds=("dataflow_sibling",),
            contract_count=0,
            kind_count=1,
            vector_distance=None,
            score=base_score,
        )
        for n in capped
    ]


__all__ = ["expand_dataflow_siblings", "expand_structural_neighbours"]
