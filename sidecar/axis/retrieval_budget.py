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
    max_walk_seeds: int  # echelon 1: how many ranked seeds get a GRAPH WALK
    token_weight: int  # echelon 2: RELATIVE share, scaled by the caller's budget
    render_mode: str  # "full" | "signature_only" | "hybrid"

    def effective_tokens(self, base_token_budget: int) -> int:
        """Scale the caller's budget by this profile's share of the minimum
        profile weight (the smallest profile gets exactly ``base``)."""
        return max(1, round(base_token_budget * self.token_weight / _MIN_WEIGHT))


# echelon 1 is NOT a hard cap on the candidate pool — that craters recall, since
# the pool's breadth is where the long tail lives. It caps only the expensive
# part: how many top-ranked seeds get a Neo4j WALK (active seeds). The rest stay
# in the pool as passive, code-bearing context (no walk, no CPU) so their files
# survive for the token budget. Seeds are cheap to carry (20 vs 200 ≈ same
# time); only their expansion costs, so we bound the walk and keep the pool.
# Render granularity (signatures) is the real token economy, not the walk cap.
#
# ``max_walk_seeds`` is the per-intent knob (the "auto mode"): empirically
# walk=20 -> bundle 0.95 / 0 zeros / ~0.07s, walk=40 -> 0.974 / 0.11s. The plan
# is to differentiate — simple "how does X work" questions need fewer walks
# (~20), heavy impact/dependency analysis more (~40) — but that split waits
# until the token budget is layered on. For now both sit at a generous 40
# (97%+, sub-second); the CLI ``--max-walk-seeds`` overrides it for sweeps.
ARCHITECTURE = RetrievalBudget(
    name="architecture",
    max_walk_seeds=20,
    token_weight=12000,
    render_mode="hybrid",
)
IMPACT = RetrievalBudget(
    name="impact",
    max_walk_seeds=40,
    token_weight=6000,
    render_mode="signature_only",
)

_PROFILES = (ARCHITECTURE, IMPACT)
_MIN_WEIGHT = min(p.token_weight for p in _PROFILES)

# The echelon-2 token cut is a TAIL GUARD, not the primary economy lever:
# signatures + the 20/40 active/passive split already cap the typical prompt
# (~25k) and the tail (~147k token-off). The guard therefore sits ABOVE that
# norm so it fires only on anomalies, not on real impact questions — base 160k
# -> impact cap 160k (> the 147k norm), architecture cap 320k. At 96k the cut
# was counterproductive (clipped impact questions, recall 0.963 -> 0.952).
# ``_context_from_axis`` floors the request budget at this; a caller may raise
# ``AskRequest.token_budget`` above it but not below.
#
# TODO(idxoid): PROVISIONAL — revisit. 160k is reasoned from the benchmark tail
# (147k token-off max), not yet validated on live /ask traffic or a fresh full
# reindex. Confirm the guard fires only on genuine anomalies and re-tune if the
# live token distribution differs.
DEFAULT_BASE_TOKEN_BUDGET = 160000


def budget_for_intent(intent: Iterable) -> RetrievalBudget:
    """Architecture by default; impact when a mode-role
    (``impact_analysis`` / ``trace_dependency``) is present in the intent."""
    if any(getattr(m, "role", "") in _MODE_ROLES for m in intent):
        return IMPACT
    return ARCHITECTURE


__all__ = [
    "RetrievalBudget",
    "ARCHITECTURE",
    "IMPACT",
    "DEFAULT_BASE_TOKEN_BUDGET",
    "budget_for_intent",
]
