"""Intent-axis ranking — boost candidates on the intent's axis."""

from __future__ import annotations

from context_engine.axis.axis_profiles import Axis
from context_engine.axis.axis_ranking import apply_intent_axis_boost, intent_axes
from context_engine.axis.role_retrieval import RoleCandidate


def _cand(uid, kinds=(), score=0.5, role="x"):
    return RoleCandidate(
        uid=uid,
        name=uid,
        file_path=f"/{uid}.py",
        role=role,
        satisfying_contracts=(),
        satisfying_kinds=tuple(kinds),
        contract_count=0,
        kind_count=len(kinds),
        vector_distance=None,
        score=score,
    )


def test_intent_axes_routing_is_registry_control():
    # routing_surface kinds = web_route_register + keyed_register_callable
    # → REGISTRY (+ CONTROL from web_route_register).
    axes = intent_axes(["routing_surface"])
    assert Axis.REGISTRY in axes
    assert Axis.CONTROL in axes


def test_intent_axes_data_model_is_structural_only():
    axes = intent_axes(["data_model_surface"])
    assert axes == frozenset({Axis.STRUCTURAL})


def test_mode_pseudo_roles_contribute_no_axis():
    assert intent_axes(["impact_analysis", "trace_dependency"]) == frozenset()


def test_boost_lifts_on_axis_candidate():
    # routing intent (REGISTRY/CONTROL); a web_route_register candidate
    # is on REGISTRY → boosted above a data_model candidate that is not.
    pools = {
        "routing_surface": [
            _cand("u:model", kinds=["data_model"], score=0.6),  # STRUCTURAL
            _cand("u:route", kinds=["web_route_register"], score=0.5),  # REGISTRY
        ]
    }
    out = apply_intent_axis_boost(pools, ["routing_surface"], boost=0.2)
    # route 0.5+0.2=0.7 overtakes model 0.6 (unboosted, off-axis).
    assert [c.uid for c in out["routing_surface"]] == ["u:route", "u:model"]
    assert out["routing_surface"][0].score == 0.7
    assert out["routing_surface"][1].score == 0.6


def test_no_kinds_candidate_passes_through():
    # vector_seed / structural candidates have no kinds → no axis → no boost.
    pools = {"vector_seed": [_cand("u:v", kinds=(), score=0.5)]}
    out = apply_intent_axis_boost(pools, ["routing_surface"], boost=0.2)
    assert out["vector_seed"][0].score == 0.5


def test_boost_capped_at_ceiling():
    pools = {"r": [_cand("u:r", kinds=["web_route_register"], score=0.95)]}
    out = apply_intent_axis_boost(pools, ["routing_surface"], boost=0.2)
    assert out["r"][0].score == 1.0


def test_boost_once_not_per_axis():
    # keyed_dispatch_callable is on REGISTRY+CONTROL (two intent axes);
    # the boost still applies once, not twice.
    pools = {"r": [_cand("u:d", kinds=["keyed_dispatch_callable"], score=0.5)]}
    out = apply_intent_axis_boost(pools, ["routing_surface"], boost=0.2)
    assert out["r"][0].score == 0.7  # 0.5 + one boost, not 0.9


def test_empty_intent_axes_is_noop():
    pools = {"m": [_cand("u:a", kinds=["data_model"], score=0.5)]}
    out = apply_intent_axis_boost(pools, ["impact_analysis"], boost=0.2)
    assert out["m"][0].score == 0.5
