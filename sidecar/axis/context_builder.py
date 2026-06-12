"""Context builder — RoleCandidate → expanded code bundle for an LLM.

The role-driven retrieval primitive returns ranked seed symbols. An
``/ask``-style consumer needs the *code* around those seeds — not just
the seed name and uid. This module is the bridge: walk every candidate's
structural neighbourhood via the shared ``graph_walk`` core (one batched
grouped walk per expansion step), dedupe + depth-rank the related
symbols per seed, and pull their ``code`` from Lance.

What "neighbourhood" means depends on the contract that satisfied the
role. ``deferred_binding_flow`` (the only mode any current contract
uses) walks ``DECORATED_BY | USES_TYPE | INJECTS | HANDLES | REFERENCES
| HAS_API | INHERITED_API`` first (the structural binding ring) then
``CALLS_*`` for runtime dispatch — exactly what a question about a
registry or dependency-binding pattern needs to surface.

The output is a ``ContextBundle`` per candidate, ready for prompt
assembly. We do not produce the final prompt: prompt shape is the
consumer's choice (chat format, tool-use schema, etc.).
"""

from __future__ import annotations

from collections import namedtuple
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from sidecar.axis.graph_walk import steps_for_mode, walk_neighbours_grouped
from sidecar.axis.role_retrieval import RoleCandidate

# One expansion hit: a neighbour reached from a seed, tagged with the
# step that found it. Mirrors the fields the bundle builder reads off the
# legacy ``AxisGraphHit``.
_Hit = namedtuple("_Hit", "uid name file_path depth step")


@dataclass(frozen=True)
class ContextSymbol:
    """One symbol in the assembled context: the seed (depth 0) or a
    related symbol reached through graph expansion."""

    uid: str
    name: str
    file_path: str
    role: str
    distance_from_seed: int
    expansion_step: str | None
    code: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "uid": self.uid,
            "name": self.name,
            "file_path": self.file_path,
            "role": self.role,
            "distance_from_seed": self.distance_from_seed,
            "expansion_step": self.expansion_step,
            "code": self.code,
        }


@dataclass(frozen=True)
class ContextBundle:
    """Bundle for one seed candidate: the seed plus its expanded
    related symbols, ordered closest-first."""

    role: str
    seed: ContextSymbol
    related: tuple[ContextSymbol, ...] = field(default_factory=tuple)

    def all_symbols(self) -> list[ContextSymbol]:
        return [self.seed, *self.related]

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "seed": self.seed.to_dict(),
            "related": [s.to_dict() for s in self.related],
        }


def _fetch_codes(
    lance,
    workspace_id: str,
    uids: set[str],
) -> dict[str, str | None]:
    """Pull ``code`` for a set of uids in one table scan.

    Lance does not give us a clean WHERE-by-list across heterogeneous
    columns; one full scan filtered in-process is acceptable for the
    workspaces we currently target (thousands of symbols, not millions).
    """
    if not uids:
        return {}
    table = lance._sym_table  # noqa: SLF001
    rows = (
        table.to_lance()
        .to_table(columns=["uid", "code", "workspace_id"])
        .to_pylist()
    )
    return {
        r["uid"]: r.get("code")
        for r in rows
        if r.get("workspace_id") == workspace_id and r.get("uid") in uids
    }


def build_context_for_candidates(
    candidates: Iterable[RoleCandidate],
    *,
    workspace_id: str,
    db,
    lance,
    max_per_seed: int = 6,
    traversal_mode: str = "deferred_binding_flow",
    include_tests: bool = False,
) -> list[ContextBundle]:
    """Expand each candidate into a ``ContextBundle`` of related code.

    ``max_per_seed`` caps how many related symbols come back per seed
    (depth-then-name ordering). ``traversal_mode`` picks the expansion
    pattern from ``AxisQueryPlan``; defaults to deferred-binding
    because every current contract uses it.

    ``include_tests`` mirrors the retrieval-pass flag — by default,
    expansion hits that land in conventional test surfaces are
    dropped. Impact-style consumers can flip the flag to keep them.
    """
    from sidecar.axis.test_file_filter import is_test_path
    candidates = list(candidates)
    if not candidates:
        return []

    # One batched grouped walk per expansion step over ALL candidate uids,
    # instead of a per-candidate traversal (N graph round-trips collapse to
    # one per step). Each seed still gets its OWN neighbourhood —
    # ``walk_neighbours_grouped`` returns ``{seed_uid: [neighbours]}`` — so
    # the per-seed dedupe/fence/cap below is byte-identical to the old
    # AxisGraphTraversal path. Steps run in order so a uid reached by an
    # earlier step keeps its (shallower) label on a depth tie.
    all_uids = [c.uid for c in candidates]
    hits_per_seed: dict[str, list[_Hit]] = {u: [] for u in all_uids}
    for step_name, edges, direction, max_hops in steps_for_mode(traversal_mode):
        grouped = walk_neighbours_grouped(
            db,
            workspace_id,
            all_uids,
            edges=edges,
            direction=direction,
            max_hops=max_hops,
        )
        for su, neighbours in grouped.items():
            bucket = hits_per_seed.get(su)
            if bucket is None:
                continue
            for nb in neighbours:
                bucket.append(
                    _Hit(nb.uid, nb.name, nb.file_path, nb.depth, step_name)
                )

    expansion_per_candidate: list[
        tuple[RoleCandidate, list]
    ] = []
    uids_to_fetch: set[str] = set()
    for cand in candidates:
        uids_to_fetch.add(cand.uid)
        hits = hits_per_seed.get(cand.uid, [])
        # Dedupe by uid, keep the shallowest occurrence (closer wins).
        # The test-file fence applies after dedup: an expansion hit
        # that lands in a test surface is dropped unless the caller
        # opted in via ``include_tests``.
        nearest_by_uid: dict[str, _Hit] = {}
        for h in hits:
            if not include_tests and is_test_path(h.file_path or ""):
                continue
            existing = nearest_by_uid.get(h.uid)
            if existing is None or h.depth < existing.depth:
                nearest_by_uid[h.uid] = h
        ordered = sorted(
            nearest_by_uid.values(),
            key=lambda h: (h.depth, (h.name or "").lower()),
        )[:max_per_seed]
        expansion_per_candidate.append((cand, ordered))
        for h in ordered:
            uids_to_fetch.add(h.uid)

    code_by_uid = _fetch_codes(lance, workspace_id, uids_to_fetch)

    bundles: list[ContextBundle] = []
    for cand, hits in expansion_per_candidate:
        seed = ContextSymbol(
            uid=cand.uid,
            name=cand.name,
            file_path=cand.file_path,
            role=cand.role,
            distance_from_seed=0,
            expansion_step=None,
            code=code_by_uid.get(cand.uid),
        )
        related = tuple(
            ContextSymbol(
                uid=h.uid,
                name=h.name,
                file_path=h.file_path,
                role=cand.role,
                distance_from_seed=h.depth,
                expansion_step=h.step,
                code=code_by_uid.get(h.uid),
            )
            for h in hits
        )
        bundles.append(
            ContextBundle(role=cand.role, seed=seed, related=related)
        )
    return bundles


__all__ = [
    "ContextBundle",
    "ContextSymbol",
    "build_context_for_candidates",
]
