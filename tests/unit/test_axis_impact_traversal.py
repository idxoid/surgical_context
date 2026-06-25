"""Impact-traversal — blast-radius walk over CALLS / AFFECTS / API.

The traversal is exercised by stubbing the shared ``graph_walk`` core
(``walk_neighbours`` / ``call_fan_in``) rather than a positional fake
Neo4j session: the walks are routed by *(edge profile, direction)* so a
test stays valid when the pass order changes. Each stub key names the
structural relation under test, not the Nth ``run`` call.
"""

from __future__ import annotations

import pytest

from context_engine.axis import impact_traversal
from context_engine.axis.graph_walk import EdgeProfile, Neighbour
from context_engine.axis.role_retrieval import RoleCandidate

from tests.unit.axis_helpers import axis_test_file_path

WORKSPACE = "qa_repo/test@axis"

_CALLS = frozenset(EdgeProfile.CALLS)
_AFFECTS = frozenset(EdgeProfile.AFFECTS)
_STRUCT_REV = frozenset(EdgeProfile.STRUCTURAL_REVERSE)
_STRUCT_FWD = frozenset(EdgeProfile.STRUCTURAL_FORWARD)


def _seed(uid: str, *, role: str = "dispatch_surface") -> RoleCandidate:
    return RoleCandidate(
        uid=uid,
        name=uid.split(":")[-1],
        file_path=axis_test_file_path(uid.split(":")[-1]),
        role=role,
        satisfying_contracts=(),
        satisfying_kinds=(),
        contract_count=0,
        kind_count=0,
        vector_distance=None,
        score=0.5,
    )


def _n(uid: str, *, name: str = "x", path: str | None = None, depth: int = 1) -> Neighbour:
    return Neighbour(uid=uid, name=name, file_path=path or axis_test_file_path("x"), depth=depth, reach=1)


def _install(monkeypatch, *, seed_uids, by_label: dict[str, list[Neighbour]], fanin=None):
    """Route ``walk_neighbours`` by (edges, direction) to a canned list.

    The two reverse-CALLS walks share an edge profile and direction, so
    they are split by anchor: ``reverse_calls`` walks the original seeds,
    ``impacted_tests`` walks the seeds ∪ forward spine (a strict superset).
    """
    orig = set(seed_uids)
    calls: list[tuple[str, tuple]] = []

    def fake_walk(
        db, workspace_id, seeds, *, edges, direction, max_hops, exclude_tests=False, **kw
    ):
        es = frozenset(edges)
        seen = set(seeds)
        if es == _AFFECTS:
            label = "forward_affects"
        elif es == _STRUCT_REV:
            label = "structural_inheritor"
        elif es == _STRUCT_FWD:
            label = "structural_api_carrier"
        elif es == _CALLS and direction == "forward":
            label = "forward_calls"
        elif es == _CALLS and direction == "reverse":
            label = "reverse_calls" if seen == orig else "impacted_tests"
        else:  # pragma: no cover - defensive
            label = "?"
        calls.append((label, tuple(sorted(seen)), exclude_tests))
        return list(by_label.get(label, []))

    def fake_fan_in(db, workspace_id, uids, *, edges=EdgeProfile.CALLS, exclude_tests=False):
        return dict(fanin or {})

    monkeypatch.setattr(impact_traversal, "walk_neighbours", fake_walk)
    monkeypatch.setattr(impact_traversal, "call_fan_in", fake_fan_in)
    return calls


def test_no_seeds_returns_empty():
    out = expand([], monkeypatch=None)
    assert out == []


def expand(seeds, *, monkeypatch=None, **kw):
    return impact_traversal.expand_impact_neighbourhood(
        seeds, db=object(), workspace_id=WORKSPACE, **kw
    )


def test_reverse_calls_pass_emits_callers(monkeypatch):
    _install(
        monkeypatch,
        seed_uids=["u:dispatch"],
        by_label={"reverse_calls": [_n("u:caller", name="full_dispatch_request")]},
    )
    out = expand([_seed("u:dispatch")])
    assert len(out) == 1
    assert out[0].uid == "u:caller"
    assert out[0].role == "impact_analysis"
    assert out[0].satisfying_kinds == ("reverse_calls",)
    assert out[0].edge_type == "CALLS_*"
    assert out[0].utility_score == pytest.approx(0.95)


def test_forward_calls_pass_emits_publisher_spine(monkeypatch):
    """The forward-CALLS walk surfaces the dependency chain the change
    drives (``apply_async -> send_task``), tagged ``forward_calls``."""
    _install(
        monkeypatch,
        seed_uids=["u:apply_async"],
        by_label={"forward_calls": [_n("u:send_task", name="send_task", path=axis_test_file_path("base"))]},
        fanin={"u:send_task": 1},
    )
    out = expand([_seed("u:apply_async")])
    assert [c.uid for c in out] == ["u:send_task"]
    assert out[0].satisfying_kinds == ("forward_calls",)
    assert out[0].utility_score == pytest.approx(0.90)


def test_http_endpoint_counterpart_surfaces_client_or_handler(monkeypatch):
    _install(monkeypatch, seed_uids=["u:client"], by_label={})
    monkeypatch.setattr(
        impact_traversal,
        "_http_endpoint_counterparts",
        lambda *_args, **_kwargs: [
            _n("u:handler", name="ask", path="/repo/context_engine/api/routes/ask.py")
        ],
    )

    out = expand([_seed("u:client")])

    counterpart = next(c for c in out if c.uid == "u:handler")
    assert counterpart.satisfying_kinds == ("http_endpoint_counterpart",)
    assert counterpart.edge_type == "CALLS_ENDPOINT|IMPLEMENTS_ENDPOINT"
    assert counterpart.utility_score == pytest.approx(0.92)


def test_hub_gate_uses_production_only_fan_in(monkeypatch):
    """The hub gate must count PRODUCTION callers only — a routing/API node
    hammered by the test suite is not a god utility, and counting test
    callers would clip the dispatch spine the impact walk needs."""
    seen = {}

    def capture_fan_in(db, workspace_id, uids, *, edges=EdgeProfile.CALLS, exclude_tests=False):
        seen["exclude_tests"] = exclude_tests
        return {u: 1 for u in uids}

    monkeypatch.setattr(impact_traversal, "call_fan_in", capture_fan_in)

    def fake_walk(db, ws, seeds, *, edges, direction, max_hops, exclude_tests=False, **kw):
        if frozenset(edges) == _CALLS and direction == "forward":
            return [_n("u:route", name="route")]
        return []

    monkeypatch.setattr(impact_traversal, "walk_neighbours", fake_walk)
    expand([_seed("u:apply_async")])
    assert seen.get("exclude_tests") is True


def test_hub_gate_drops_high_fanin_utility(monkeypatch):
    """A forward node whose global CALLS fan-in is an outlier above the
    closure median is a shared utility hub — kept out of the spine."""
    _install(
        monkeypatch,
        seed_uids=["u:apply_async"],
        by_label={
            "forward_calls": [
                _n("u:send_task", name="send_task"),
                _n("u:route", name="route"),
                _n("u:warn", name="warn"),  # the hub
            ]
        },
        # median = 3 → cap = 2*3 = 6; warn (40) is dropped, the spine stays.
        fanin={"u:send_task": 1, "u:route": 3, "u:warn": 40},
    )
    out = expand([_seed("u:apply_async")])
    spine = {c.uid for c in out if c.satisfying_kinds == ("forward_calls",)}
    assert spine == {"u:send_task", "u:route"}
    assert "u:warn" not in {c.uid for c in out}


def test_impacted_tests_only_with_include_tests(monkeypatch):
    """Tests reach the pool only when ``include_tests`` is set, and only
    test-file nodes from the reverse walk are kept."""
    by_label = {
        "forward_calls": [_n("u:route", name="route")],
        "impacted_tests": [
            _n("u:test_routes", name="test_route", path="/repo/t/unit/test_routes.py"),
            _n("u:prod", name="helper", path="/repo/celery/app/base.py"),
        ],
    }
    _install(monkeypatch, seed_uids=["u:apply_async"], by_label=by_label, fanin={"u:route": 1})
    # Off by default: no impacted_tests walk result enters.
    out_off = expand([_seed("u:apply_async")])
    assert all(c.satisfying_kinds != ("impacted_tests",) for c in out_off)
    # On: only the test-file node is kept (prod node filtered by is_test_path).
    out_on = expand([_seed("u:apply_async")], include_tests=True)
    tests = [c for c in out_on if c.satisfying_kinds == ("impacted_tests",)]
    assert [c.uid for c in tests] == ["u:test_routes"]
    assert tests[0].utility_score == pytest.approx(0.80)


def test_global_utility_ranking_beats_walk_order(monkeypatch):
    """A depth-1 reverse-caller (0.95) outranks a depth-1 AFFECTS leaf
    (0.58) even though AFFECTS is the last walk — ranking is global."""
    _install(
        monkeypatch,
        seed_uids=["u:s"],
        by_label={
            "forward_affects": [_n("u:affect", depth=1)],
            "reverse_calls": [_n("u:caller", depth=1)],
        },
    )
    out = expand([_seed("u:s")], max_impacted=1)
    assert [c.uid for c in out] == ["u:caller"]


def test_publisher_spine_boosts_forward_over_reverse(monkeypatch):
    """When a publisher-axis intent role is present, depth-1 forward spine
    nodes outrank depth-1 reverse callers."""
    _install(
        monkeypatch,
        seed_uids=["u:s"],
        by_label={
            "forward_calls": [_n("u:route", name="route", depth=1)],
            "reverse_calls": [_n("u:caller", depth=1)],
        },
        fanin={"u:route": 1},
    )
    out = expand(
        [_seed("u:s")],
        max_impacted=1,
        intent_roles=["routing_surface", "impact_analysis"],
        intent_similarities={"routing_surface": 0.30, "impact_analysis": 0.28},
    )
    assert [c.uid for c in out] == ["u:route"]
    assert out[0].satisfying_kinds == ("forward_calls",)
    assert out[0].utility_score == pytest.approx(0.95)


def test_publisher_spine_from_intent():
    assert impact_traversal.publisher_spine_from_intent(
        ["routing_surface"],
        intent_similarities={"routing_surface": 0.29, "impact_analysis": 0.288},
    )
    assert not impact_traversal.publisher_spine_from_intent(
        ["routing_surface", "impact_analysis"],
        intent_similarities={"routing_surface": 0.237, "impact_analysis": 0.355},
    )
    assert not impact_traversal.publisher_spine_from_intent(["dependency_solver"])
    assert impact_traversal.publisher_spine_from_intent(
        ["dependency_solver", "routing_surface"],
        intent_similarities={"routing_surface": 0.29, "impact_analysis": 0.288},
    )


def test_dedup_keeps_highest_utility_tag(monkeypatch):
    """A node reached by both reverse_calls and forward_affects keeps the
    stronger (reverse_calls) tag."""
    _install(
        monkeypatch,
        seed_uids=["u:s"],
        by_label={
            "reverse_calls": [_n("u:dup")],
            "forward_affects": [_n("u:dup")],
        },
    )
    out = expand([_seed("u:s")])
    assert len(out) == 1
    assert out[0].satisfying_kinds == ("reverse_calls",)


def test_seeds_never_in_impact_pool(monkeypatch):
    _install(
        monkeypatch,
        seed_uids=["u:s"],
        by_label={
            "reverse_calls": [_n("u:s")],  # seed reaches itself
            "forward_affects": [_n("u:s")],
        },
    )
    assert expand([_seed("u:s")]) == []


def test_explicit_exclude_uids_skipped(monkeypatch):
    _install(
        monkeypatch,
        seed_uids=["u:s"],
        by_label={"reverse_calls": [_n("u:caller"), _n("u:other")]},
    )
    out = expand([_seed("u:s")], exclude_uids=["u:caller"])
    assert [c.uid for c in out] == ["u:other"]


def test_max_impacted_caps_pool_size(monkeypatch):
    _install(
        monkeypatch,
        seed_uids=["u:s"],
        by_label={"reverse_calls": [_n(f"u:c{i}") for i in range(50)]},
    )
    out = expand([_seed("u:s")], max_impacted=10)
    assert len(out) == 10
