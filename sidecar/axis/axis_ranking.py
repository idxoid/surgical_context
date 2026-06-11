"""Intent-axis ranking — intent as a ranker, not a selector.

The reactive-axis refactor took the intent classifier OUT of structure
selection (seeds come from role-agnostic vectors, traversal axis comes
from node kinds). What intent keeps is resource management: depth
bounding and *ranking*. This module is the ranking half.

The idea idxoid specified: when intent points at a role, the candidates
that sit on the same traversal *axis* as that role are more likely to
be the answer, so they get a score boost. A routing question
(``routing_surface`` → REGISTRY/CONTROL axes) boosts candidates whose
kinds live on REGISTRY/CONTROL; it does not boost a pure data-model
(STRUCTURAL-only) candidate that a tangential pass dragged in.

The boost is additive and capped — it reorders within a pool, it never
gates. Candidates with no classified kind (vector seeds, structural
neighbours) have no axis and are left untouched, so the role-agnostic
seed channel is unaffected.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import replace

from sidecar.axis.axis_profiles import axes_for_kinds
from sidecar.axis.role_resolver import ROLE_EVIDENCE_MAP
from sidecar.axis.role_retrieval import RoleCandidate


def intent_axes(intent_roles: Iterable[str]) -> frozenset[str]:
    """Union of traversal axes natural to the intent's roles.

    Each role's evidence kinds map to axes via ``axes_for_kinds``;
    a routing question contributes REGISTRY/CONTROL, an error question
    STRUCTURAL/CONTROL, etc. Question-shape pseudo-roles
    (``impact_analysis`` / ``trace_dependency`` / ``vector_seed`` /
    ``structural_neighbour``) carry no kinds and contribute nothing —
    they are modes, not axes.
    """
    out: set[str] = set()
    for role in intent_roles:
        evidence = ROLE_EVIDENCE_MAP.get(role)
        if evidence:
            out |= axes_for_kinds(evidence.kinds)
    return frozenset(out)


def apply_intent_axis_boost(
    raw_by_role: Mapping[str, list[RoleCandidate]],
    intent_roles: Iterable[str],
    *,
    boost: float = 0.15,
    score_ceiling: float = 1.0,
) -> dict[str, list[RoleCandidate]]:
    """Boost candidates whose kind-axes intersect the intent's axes.

    Returns a fresh dict; each pool is re-sorted by score after the
    boost. A candidate is boosted once (not per overlapping axis) so a
    multi-axis kind does not run away with the ranking. Candidates with
    no kinds — vector seeds, structural neighbours — are passed through
    unchanged.
    """
    axes = intent_axes(intent_roles)
    if not axes:
        return {role: list(cands) for role, cands in raw_by_role.items()}

    out: dict[str, list[RoleCandidate]] = {}
    for role, cands in raw_by_role.items():
        boosted: list[RoleCandidate] = []
        for c in cands:
            cand_axes = axes_for_kinds(frozenset(c.satisfying_kinds))
            if cand_axes & axes:
                boosted.append(
                    replace(c, score=min(score_ceiling, c.score + boost))
                )
            else:
                boosted.append(c)
        boosted.sort(key=lambda c: c.score, reverse=True)
        out[role] = boosted
    return out


__all__ = ["apply_intent_axis_boost", "intent_axes"]
