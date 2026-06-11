"""Phased reactive traversal — pattern 3 of reactive axis selection.

The FSM the project note calls "REGISTRY* then CONTROL": move along the
discovery axes (REGISTRY / STRUCTURAL) that are natural to the seed's
kind while they carry, then at the boundary fall into CONTROL to reach
business logic. The start axis is reactive — it comes from the seed's
L2 kind via ``axis_profiles.KIND_AXES`` — so the walk does not depend on
the intent classifier choosing a role correctly.

Two phases:

  1. **Discovery** — walk the seed-kind's REGISTRY/STRUCTURAL axes.
     From a router/registry seed this reaches the registered entrypoints
     (``@route`` handlers) and the type hierarchy.
  2. **Execution** — from the seeds *and* the discovery frontier, walk
     CONTROL (``CALLS_*``). This falls past the entrypoint into the
     code it runs.

The pass is a measurement vehicle first: does a topology-only walk from
a router seed reach the same handler+logic files that intent-driven
retrieval finds, *without* leaning on intent? It returns candidates
tagged by phase so the caller (and the benchmark) can see which axis
surfaced each file.
"""

from __future__ import annotations

import json
from collections.abc import Iterable

from sidecar.axis.axis_profiles import Axis, axes_for_kinds, edges_for_axes
from sidecar.axis.graph_walk import walk_neighbours
from sidecar.axis.role_retrieval import RoleCandidate


_DISCOVERY_AXES = frozenset({Axis.REGISTRY, Axis.STRUCTURAL})


def _flat_kinds(raw) -> set[str]:
    if not raw:
        return set()
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return set()
    out: set[str] = set()
    for item in parsed:
        if isinstance(item, dict):
            name = item.get("kind") or item.get("name")
            if name:
                out.add(str(name))
        elif item is not None:
            out.add(str(item))
    return out


def _fetch_kinds(lance, workspace_id: str, uids: set[str]) -> dict[str, set[str]]:
    """Read the container kinds of each uid from the Lance symbol table."""
    if not uids:
        return {}
    sym = getattr(lance, "_sym_table", None)
    if sym is None:
        return {}
    rows = sym.to_lance().to_table(
        columns=["uid", "axis_container_kinds_json", "workspace_id"]
    ).to_pylist()
    out: dict[str, set[str]] = {}
    for r in rows:
        if r.get("workspace_id") != workspace_id:
            continue
        uid = str(r.get("uid") or "")
        if uid in uids:
            out[uid] = _flat_kinds(r.get("axis_container_kinds_json"))
    return out


def expand_phased(
    seed_candidates: Iterable[RoleCandidate],
    *,
    db,
    lance,
    workspace_id: str,
    max_hops: int = 2,
    max_discovery: int = 12,
    max_execution: int = 12,
    base_score: float = 0.4,
    exclude_uids: Iterable[str] = (),
    include_tests: bool = False,
) -> list[RoleCandidate]:
    """Run the REGISTRY*→CONTROL phased walk from the seeds.

    Discovery axes come reactively from the union of the seeds' kinds
    (intersected with REGISTRY/STRUCTURAL). If no seed carries a
    discovery-natural kind, discovery falls back to STRUCTURAL so the
    walk still has a channel. Execution always walks CONTROL from the
    seeds plus the discovery frontier.

    Returns candidates tagged ``structural_neighbour`` with
    ``satisfying_kinds=("phase_discovery",)`` or ``("phase_execution",)``
    so phase provenance is visible. Deduplicated; discovery before
    execution; each phase capped independently.
    """
    seeds = list(seed_candidates)
    if not seeds:
        return []
    seed_uids = [c.uid for c in seeds]
    seed_uid_set = set(seed_uids)
    excluded = set(exclude_uids) | seed_uid_set

    # Reactive start axis: union of the seeds' kinds → discovery axes.
    seed_kinds = _fetch_kinds(lance, workspace_id, seed_uid_set)
    all_seed_kinds: set[str] = set()
    for ks in seed_kinds.values():
        all_seed_kinds |= ks
    discovery_axes = axes_for_kinds(all_seed_kinds) & _DISCOVERY_AXES
    if not discovery_axes:
        # No registry/structural nature in the seeds — fall back to
        # STRUCTURAL so discovery still has a channel rather than
        # collapsing straight to CONTROL.
        discovery_axes = frozenset({Axis.STRUCTURAL})

    # Phase 1: discovery.
    discovery = walk_neighbours(
        db, workspace_id, seed_uids,
        edges=edges_for_axes(discovery_axes),
        direction="undirected",
        max_hops=max_hops,
        exclude_tests=not include_tests,
    )
    discovery_uids = [n.uid for n in discovery if n.uid not in excluded]

    # Phase 2: execution from seeds + discovery frontier along CONTROL.
    exec_seeds = list(seed_uids) + discovery_uids
    execution = walk_neighbours(
        db, workspace_id, exec_seeds,
        edges=edges_for_axes(frozenset({Axis.CONTROL})),
        direction="undirected",
        max_hops=max_hops,
        exclude_tests=not include_tests,
    )

    out: list[RoleCandidate] = []
    seen: set[str] = set()

    def _emit(n, tag: str, limit: int, count: int) -> int:
        if count >= limit:
            return count
        if n.uid in excluded or n.uid in seen:
            return count
        seen.add(n.uid)
        out.append(
            RoleCandidate(
                uid=n.uid,
                name=n.name,
                file_path=n.file_path,
                role="structural_neighbour",
                satisfying_contracts=(),
                satisfying_kinds=(tag,),
                contract_count=0,
                kind_count=1,
                vector_distance=None,
                score=base_score,
            )
        )
        return count + 1

    dcount = 0
    for n in discovery:
        dcount = _emit(n, "phase_discovery", max_discovery, dcount)
    ecount = 0
    for n in execution:
        ecount = _emit(n, "phase_execution", max_execution, ecount)
    return out


__all__ = ["expand_phased"]
