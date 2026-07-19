"""Cross-role lookahead — graph-evidenced candidate expansion."""

from __future__ import annotations

import json

import pytest

from context_engine.axis.role_lookahead import expand_candidates_via_neighbourhood
from context_engine.axis.role_retrieval import RoleCandidate
from tests.unit.axis_helpers import (
    AXIS_TEST_WORKSPACE,
    FakeLanceDB,
    FakeNeo4jDB,
    axis_test_file_path,
    lance_kind_row,
    make_role_candidate,
    walk_rows,
)


def _candidate(
    uid: str,
    *,
    role: str,
    name: str | None = None,
    score: float = 0.6,
) -> RoleCandidate:
    return make_role_candidate(uid, name=name, role=role, score=score)


def _queued_db(records_by_call):
    return FakeNeo4jDB(records_by_call, queued=True)


def test_no_intent_roles_returns_unchanged():
    """Edge case: an intent ranking with no known roles produces no
    kind-to-role mapping, so there is nothing the lookahead can attribute
    a neighbour to."""
    cands = {"unknown_role": [_candidate("u:a", role="unknown_role")]}
    out = expand_candidates_via_neighbourhood(
        ["unknown_role"],
        cands,
        db=FakeNeo4jDB(),
        lance=FakeLanceDB([]),
        workspace_id=AXIS_TEST_WORKSPACE,
    )
    assert out == cands


def test_neighbour_backing_other_role_is_injected():
    """The headline case. ``proxy_mechanism`` seed's K-hop neighbour
    carries ``keyed_dispatch_callable`` (which backs ``dispatch_surface``);
    the lookahead injects it into the ``dispatch_surface`` pool."""
    seed = _candidate("u:proxy", role="proxy_mechanism")
    cands = {"proxy_mechanism": [seed], "dispatch_surface": []}

    db = _queued_db([walk_rows(["u:dispatcher"])])
    lance = FakeLanceDB(
        [
            lance_kind_row(
                "u:dispatcher",
                kinds=["keyed_dispatch_callable"],
                name="dispatch_request",
            )
        ]
    )

    out = expand_candidates_via_neighbourhood(
        ["proxy_mechanism", "dispatch_surface"],
        cands,
        db=db,
        lance=lance,
        workspace_id=AXIS_TEST_WORKSPACE,
    )

    injected = out["dispatch_surface"]
    assert len(injected) == 1
    assert injected[0].uid == "u:dispatcher"
    assert injected[0].role == "dispatch_surface"
    assert "keyed_dispatch_callable" in injected[0].satisfying_kinds
    assert injected[0].kind_count == 1
    assert [c.uid for c in out["proxy_mechanism"]] == ["u:proxy"]


def test_injected_candidate_blends_query_semantic_from_prescanned():
    """The 0.55-wall fix: with a query + prescanned vectors, an injected
    neighbour scores ``0.5*base + 0.5*semantic`` instead of the flat
    ``base_score`` constant — a semantically-close neighbour outranks a
    far one instead of tying with it mid-way through the scored range."""
    import numpy as np

    from context_engine.axis.role_retrieval import WorkspaceScan

    seed = _candidate("u:proxy", role="proxy_mechanism")
    cands = {"proxy_mechanism": [seed], "dispatch_surface": []}
    db = _queued_db([walk_rows(["u:near", "u:far"])])
    rows = [
        {
            "uid": "u:near",
            "name": "near",
            "file_path": axis_test_file_path("near"),
            "_contracts": set(),
            "_kinds": {"keyed_dispatch_callable"},
            "_idx": 0,
        },
        {
            "uid": "u:far",
            "name": "far",
            "file_path": axis_test_file_path("far"),
            "_contracts": set(),
            "_kinds": {"keyed_dispatch_callable"},
            "_idx": 1,
        },
    ]
    scan = WorkspaceScan(rows=rows, vectors=np.array([[1.0, 0.0], [0.0, 5.0]], dtype=float))

    out = expand_candidates_via_neighbourhood(
        ["proxy_mechanism", "dispatch_surface"],
        cands,
        db=db,
        lance=FakeLanceDB([]),
        workspace_id=AXIS_TEST_WORKSPACE,
        prescanned=scan,
        query_text="how is dispatch keyed?",
        embed_fn=lambda _text: [1.0, 0.0],
    )

    injected = {c.uid: c for c in out["dispatch_surface"]}
    near, far = injected["u:near"], injected["u:far"]
    assert near.vector_distance == pytest.approx(0.0)
    assert near.score == pytest.approx(0.5 * 0.4 + 0.5 * 1.0)
    assert far.vector_distance == pytest.approx((1 + 25) ** 0.5)
    assert far.score < near.score


def test_injected_candidate_without_query_keeps_flat_base_score():
    seed = _candidate("u:proxy", role="proxy_mechanism")
    cands = {"proxy_mechanism": [seed], "dispatch_surface": []}
    db = _queued_db([walk_rows(["u:dispatcher"])])
    lance = FakeLanceDB([lance_kind_row("u:dispatcher", kinds=["keyed_dispatch_callable"])])

    out = expand_candidates_via_neighbourhood(
        ["proxy_mechanism", "dispatch_surface"],
        cands,
        db=db,
        lance=lance,
        workspace_id=AXIS_TEST_WORKSPACE,
    )

    injected = out["dispatch_surface"][0]
    assert injected.vector_distance is None
    assert injected.score == pytest.approx(0.4)


def test_neighbour_already_in_target_role_is_not_duplicated():
    seed = _candidate("u:proxy", role="proxy_mechanism")
    pre_existing = _candidate("u:dispatcher", role="dispatch_surface", score=0.9)
    cands = {
        "proxy_mechanism": [seed],
        "dispatch_surface": [pre_existing],
    }
    db = _queued_db([walk_rows(["u:dispatcher"])])
    lance = FakeLanceDB([lance_kind_row("u:dispatcher", kinds=["keyed_dispatch_callable"])])

    out = expand_candidates_via_neighbourhood(
        ["proxy_mechanism", "dispatch_surface"],
        cands,
        db=db,
        lance=lance,
        workspace_id=AXIS_TEST_WORKSPACE,
    )

    assert [c.uid for c in out["dispatch_surface"]] == ["u:dispatcher"]
    assert out["dispatch_surface"][0].score == pytest.approx(0.9)


def test_neighbour_that_is_another_roles_seed_is_not_injected():
    proxy_seed = _candidate("u:proxy", role="proxy_mechanism")
    dispatch_seed = _candidate("u:dispatcher", role="dispatch_surface")
    cands = {
        "proxy_mechanism": [proxy_seed],
        "dispatch_surface": [dispatch_seed],
    }
    db = _queued_db(
        [
            walk_rows(["u:dispatcher", "u:other"]),
            [],
        ]
    )
    lance = FakeLanceDB(
        [
            lance_kind_row("u:dispatcher", kinds=["keyed_dispatch_callable"]),
            lance_kind_row("u:other", kinds=["middleware_chain"]),
        ]
    )

    out = expand_candidates_via_neighbourhood(
        ["proxy_mechanism", "dispatch_surface"],
        cands,
        db=db,
        lance=lance,
        workspace_id=AXIS_TEST_WORKSPACE,
    )

    uids = [c.uid for c in out["dispatch_surface"]]
    assert uids == ["u:dispatcher", "u:other"]


def test_neighbour_kind_not_in_intent_is_ignored():
    seed = _candidate("u:proxy", role="proxy_mechanism")
    cands = {"proxy_mechanism": [seed], "dispatch_surface": []}
    db = _queued_db([walk_rows(["u:cfg"])])
    lance = FakeLanceDB([lance_kind_row("u:cfg", kinds=["config_carrier"])])

    out = expand_candidates_via_neighbourhood(
        ["proxy_mechanism", "dispatch_surface"],
        cands,
        db=db,
        lance=lance,
        workspace_id=AXIS_TEST_WORKSPACE,
    )

    assert out["dispatch_surface"] == []


def test_max_injected_per_role_cap_respected():
    seed = _candidate("u:proxy", role="proxy_mechanism")
    cands = {"proxy_mechanism": [seed], "dispatch_surface": []}
    neighbours = [f"u:n{i}" for i in range(20)]
    db = _queued_db([walk_rows(neighbours)])
    lance = FakeLanceDB([lance_kind_row(u, kinds=["middleware_chain"]) for u in neighbours])

    out = expand_candidates_via_neighbourhood(
        ["proxy_mechanism", "dispatch_surface"],
        cands,
        db=db,
        lance=lance,
        workspace_id=AXIS_TEST_WORKSPACE,
        max_injected_per_role=5,
    )

    assert len(out["dispatch_surface"]) == 5


def test_workspace_isolation_blocks_neighbour_kind_lookup():
    seed = _candidate("u:proxy", role="proxy_mechanism")
    cands = {"proxy_mechanism": [seed], "dispatch_surface": []}
    db = _queued_db([walk_rows(["u:other_ws"])])
    row = {
        "uid": "u:other_ws",
        "name": "x",
        "file_path": axis_test_file_path("x"),
        "axis_container_kinds_json": json.dumps([{"kind": "keyed_dispatch_callable"}]),
        "workspace_id": "some_other_workspace",
    }
    lance = FakeLanceDB([row])

    out = expand_candidates_via_neighbourhood(
        ["proxy_mechanism", "dispatch_surface"],
        cands,
        db=db,
        lance=lance,
        workspace_id=AXIS_TEST_WORKSPACE,
    )

    assert out["dispatch_surface"] == []


def test_auto_promote_creates_non_intent_role_pool():
    seed = _candidate("u:proxy", role="proxy_mechanism")
    cands = {"proxy_mechanism": [seed]}
    db = _queued_db([walk_rows(["u:d1", "u:d2", "u:d3", "u:other"])])
    lance = FakeLanceDB(
        [
            lance_kind_row("u:d1", kinds=["keyed_dispatch_callable"], name="dispatch_request"),
            lance_kind_row("u:d2", kinds=["keyed_dispatch_callable"], name="wsgi_app"),
            lance_kind_row("u:d3", kinds=["keyed_dispatch_callable"], name="url_for"),
            lance_kind_row("u:other", kinds=["config_carrier"], name="cfg"),
        ]
    )

    out = expand_candidates_via_neighbourhood(
        ["proxy_mechanism"],
        cands,
        db=db,
        lance=lance,
        workspace_id=AXIS_TEST_WORKSPACE,
        auto_promote_min_hits=3,
    )

    assert "dispatch_surface" in out
    promoted_uids = sorted(c.uid for c in out["dispatch_surface"])
    assert promoted_uids == ["u:d1", "u:d2", "u:d3"]
    assert "configuration_surface" not in out


def test_auto_promote_threshold_blocks_weak_evidence():
    seed = _candidate("u:proxy", role="proxy_mechanism")
    cands = {"proxy_mechanism": [seed]}
    db = _queued_db([walk_rows(["u:single"])])
    lance = FakeLanceDB([lance_kind_row("u:single", kinds=["keyed_dispatch_callable"])])

    out = expand_candidates_via_neighbourhood(
        ["proxy_mechanism"],
        cands,
        db=db,
        lance=lance,
        workspace_id=AXIS_TEST_WORKSPACE,
        auto_promote_min_hits=3,
    )

    assert "dispatch_surface" not in out


def test_auto_promote_pool_filter_restricts_eligible_roles():
    seed = _candidate("u:proxy", role="proxy_mechanism")
    cands = {"proxy_mechanism": [seed]}
    db = _queued_db([walk_rows(["u:d1", "u:d2", "u:d3"])])
    lance = FakeLanceDB(
        [lance_kind_row(u, kinds=["keyed_dispatch_callable"]) for u in ("u:d1", "u:d2", "u:d3")]
    )

    out = expand_candidates_via_neighbourhood(
        ["proxy_mechanism"],
        cands,
        db=db,
        lance=lance,
        workspace_id=AXIS_TEST_WORKSPACE,
        auto_promote_min_hits=3,
        auto_promote_role_pool=["configuration_surface"],
    )

    assert "dispatch_surface" not in out
    assert "configuration_surface" not in out


def test_auto_promoted_role_order_is_independent_of_pool_input_order():
    seed = _candidate("u:proxy", role="proxy_mechanism")
    neighbours = ["u:r1", "u:r2", "u:r3"]
    rows = [lance_kind_row(uid, kinds=["keyed_register_callable"]) for uid in neighbours]

    def expand(role_pool: list[str]) -> dict[str, list[RoleCandidate]]:
        return expand_candidates_via_neighbourhood(
            ["proxy_mechanism"],
            {"proxy_mechanism": [seed]},
            db=_queued_db([walk_rows(neighbours)]),
            lance=FakeLanceDB(rows),
            workspace_id=AXIS_TEST_WORKSPACE,
            auto_promote_min_hits=3,
            auto_promote_role_pool=role_pool,
        )

    forward = expand(["task_surface", "routing_surface", "binding_surface"])
    reverse = expand(["binding_surface", "routing_surface", "task_surface"])

    expected_order = [
        "proxy_mechanism",
        "binding_surface",
        "routing_surface",
        "task_surface",
    ]
    assert list(forward) == expected_order
    assert list(reverse) == expected_order
