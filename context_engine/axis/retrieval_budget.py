"""Intent-driven retrieval budget — axis as resource manager (Phase 1f).

The intent classifier is the sanctioned RESOURCE MANAGER (ranking + depth),
never a structure selector — see the engineering invariants. This module is
the budget half: it maps the *shape* of the question, read off the existing
structural mode-roles (no keyword match), to prompt-packing policy.

  * **``token_weight`` + ``render_mode``** (budget cap, inside
    ``build_context``): how much code to pack into the prompt and at what
    granularity; the tail of less-relevant files is dropped once the budget
    is hit.

Candidate-count knobs live on ``AxisRetrievalConfig`` instead:
``per_role_limit`` controls retrieval seed breadth, ``context_seeds_per_role``
optionally caps the pool before context expansion, and ``context_per_seed``
caps graph fanout per seed. Those are quality/latency tradeoffs, not automatic
budget-profile decisions; changing them materially affects candidate recall.

Two profiles, picked by the structural mode-roles already produced by the
intent classifier:

  * **architecture / how-it-works** (default): few seeds, a *large* token
    share, full code — pour the core in whole so the LLM sees structure.
  * **impact / find-dependencies** (an ``impact_analysis`` / ``trace_dependency``
    intent is present): many seeds, a *smaller* token share,
    ``impact_tiered`` render — core-tier class blocks fold compact, anchor
    seeds keep a full signature header, tail sites one-line stubs; tighter
    per-file and per-transaction caps so a small request budget still buys
    breadth.

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
    token_weight: int  # RELATIVE share, scaled by the caller's budget
    render_mode: str  # "full" | "impact_tiered" | "impact_surface" | "signature_only" | "hybrid"
    per_transaction_share: float = 0.10  # legacy profile knob; currently ignored by packer
    file_soft_cap_share: float = 0.25  # per-file saturation vs budget
    signature_only_initial: bool = False  # skip fold_compact in phase-1 buys

    def effective_tokens(self, base_token_budget: int) -> int:
        """Scale the caller's budget by this profile's share of the minimum
        profile weight (the smallest profile gets exactly ``base``)."""
        return max(1, round(base_token_budget * self.token_weight / _MIN_WEIGHT))


# The budget profile deliberately does not cap context candidates. Historical
# active/passive walk caps were strong recall levers, not safe budget policy.
# Keep candidate caps explicit on AxisRetrievalConfig / request schemas, where
# callers can opt into the quality/latency tradeoff with eyes open.
ARCHITECTURE = RetrievalBudget(
    name="architecture",
    token_weight=12000,
    render_mode="hybrid",
)
IMPACT = RetrievalBudget(
    name="impact",
    token_weight=6000,
    render_mode="impact_tiered",
    per_transaction_share=0.06,
    file_soft_cap_share=0.12,
    signature_only_initial=False,
)

_PROFILES = (ARCHITECTURE, IMPACT)
_MIN_WEIGHT = min(p.token_weight for p in _PROFILES)


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
    "budget_for_intent",
]
