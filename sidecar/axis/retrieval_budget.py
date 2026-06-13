"""Intent-driven retrieval budget — axis as resource manager (Phase 1f).

The intent classifier is the sanctioned RESOURCE MANAGER (ranking + depth),
never a structure selector — see the engineering invariants. This module is
the budget half: it maps the *shape* of the question, read off the existing
structural mode-roles (no keyword match), to two echelons of resource policy.

  * **Echelon 1 — ``max_seeds``** (count cap, at axis entry): a hard cap on
    how many ranked candidates feed the per-seed context walk, so the walk
    stays bounded and fast regardless of how wide the pool grew.
  * **Echelon 2 — ``token_weight`` + ``render_mode``** (budget cap, inside
    ``build_context``): how much code to pack into the prompt and at what
    granularity; the tail of less-relevant files is dropped once the budget
    is hit.

Two profiles, picked by the structural mode-roles already produced by the
intent classifier:

  * **architecture / how-it-works** (default): few seeds, a *large* token
    share, full code — pour the core in whole so the LLM sees structure.
  * **impact / find-dependencies** (an ``impact_analysis`` / ``trace_dependency``
    intent is present): many seeds, a *smaller* token share, signature-only —
    surface as many connected sites as possible, each cheap.

Token shares are RELATIVE weights, not absolute counts. The caller's
``token_budget`` anchors the *smallest* profile and richer profiles scale up
by their ratio over that minimum::

    effective = base_token_budget * weight / min(all weights)

so the request budget sets the overall scale while the profiles set only the
proportions (impact -> base*1, architecture -> base*2 at the weights below).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

# Question-shape mode-roles (structural, classifier-produced) — their
# presence flips the profile from architecture to impact.
_MODE_ROLES = frozenset({"impact_analysis", "trace_dependency"})


@dataclass(frozen=True)
class RetrievalBudget:
    name: str
    max_seeds: int  # echelon 1: cap on seeds into the context walk
    token_weight: int  # echelon 2: RELATIVE share, scaled by the caller's budget
    render_mode: str  # "full" | "signature_only"

    def effective_tokens(self, base_token_budget: int) -> int:
        """Scale the caller's budget by this profile's share of the minimum
        profile weight (the smallest profile gets exactly ``base``)."""
        return max(1, round(base_token_budget * self.token_weight / _MIN_WEIGHT))


ARCHITECTURE = RetrievalBudget(
    name="architecture",
    max_seeds=10,
    token_weight=12000,
    render_mode="full",
)
IMPACT = RetrievalBudget(
    name="impact",
    max_seeds=40,
    token_weight=6000,
    render_mode="signature_only",
)

_PROFILES = (ARCHITECTURE, IMPACT)
_MIN_WEIGHT = min(p.token_weight for p in _PROFILES)


def budget_for_intent(intent: Iterable) -> RetrievalBudget:
    """Architecture by default; impact when a mode-role
    (``impact_analysis`` / ``trace_dependency``) is present in the intent."""
    if any(getattr(m, "role", "") in _MODE_ROLES for m in intent):
        return IMPACT
    return ARCHITECTURE


__all__ = ["RetrievalBudget", "ARCHITECTURE", "IMPACT", "budget_for_intent"]
