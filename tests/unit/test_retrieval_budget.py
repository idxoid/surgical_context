"""Phase 1f: intent-driven retrieval budget profiles."""

from __future__ import annotations

from context_engine.axis.retrieval_budget import (
    ARCHITECTURE,
    IMPACT,
    budget_for_intent,
)


class _M:
    def __init__(self, role: str) -> None:
        self.role = role


def test_default_profile_is_architecture():
    assert budget_for_intent([]) is ARCHITECTURE
    assert budget_for_intent([_M("routing_surface"), _M("proxy_mechanism")]) is ARCHITECTURE


def test_mode_role_selects_impact():
    assert budget_for_intent([_M("impact_analysis")]) is IMPACT
    assert budget_for_intent([_M("trace_dependency")]) is IMPACT
    # impact wins even mixed with role intents
    assert budget_for_intent([_M("routing_surface"), _M("impact_analysis")]) is IMPACT


def test_profile_shapes():
    # max_walk_seeds caps only the WALK (active seeds); both profiles use the
    # same walk budget and differ in render granularity — architecture keeps
    # the core full (hybrid), impact is all-signatures for max breadth.
    assert ARCHITECTURE.max_walk_seeds >= 1
    # simple "how does X" questions need fewer walks than impact analysis.
    assert ARCHITECTURE.max_walk_seeds <= IMPACT.max_walk_seeds
    assert ARCHITECTURE.render_mode == "hybrid"
    assert IMPACT.render_mode == "impact_tiered"
    assert IMPACT.per_transaction_share < ARCHITECTURE.per_transaction_share
    assert IMPACT.file_soft_cap_share < ARCHITECTURE.file_soft_cap_share
    assert not IMPACT.signature_only_initial


def test_effective_tokens_are_proportional_to_request_budget():
    # impact is the minimum weight -> anchors the caller's budget (base*1);
    # architecture scales up by its weight ratio (2x at the shipped weights).
    assert IMPACT.effective_tokens(4000) == 4000
    assert ARCHITECTURE.effective_tokens(4000) == 8000
    # scale moves with the request budget; proportions hold.
    assert IMPACT.effective_tokens(6000) == 6000
    assert ARCHITECTURE.effective_tokens(6000) == 12000
