"""Trace-dependency traversal -- CALLS-only call-chain walk."""

from __future__ import annotations

from context_engine.axis.trace_traversal import expand_trace_neighbourhood
from tests.unit.axis_helpers import (
    AXIS_TEST_WORKSPACE,
    FakeNeo4jDB,
    axis_test_file_path,
    graph_row,
    make_role_candidate,
)


def _record(uid: str, *, name: str = "x", path: str | None = None) -> dict:
    return graph_row(uid, name, path or axis_test_file_path("x"))


def test_no_seeds_returns_empty():
    assert expand_trace_neighbourhood([], db=FakeNeo4jDB(), workspace_id=AXIS_TEST_WORKSPACE) == []


def test_reverse_calls_emit_trace_callers():
    db = FakeNeo4jDB(
        [
            [_record("u:caller", name="caller")],
            [],
        ],
        queued=True,
    )

    out = expand_trace_neighbourhood(
        [make_role_candidate("u:target")],
        db=db,
        workspace_id=AXIS_TEST_WORKSPACE,
    )

    assert len(out) == 1
    assert out[0].uid == "u:caller"
    assert out[0].role == "trace_dependency"
    assert out[0].satisfying_kinds == ("trace_callers",)


def test_forward_calls_emit_trace_callees():
    db = FakeNeo4jDB(
        [
            [],
            [_record("u:callee", name="callee")],
        ],
        queued=True,
    )

    out = expand_trace_neighbourhood(
        [make_role_candidate("u:target")],
        db=db,
        workspace_id=AXIS_TEST_WORKSPACE,
    )

    assert len(out) == 1
    assert out[0].uid == "u:callee"
    assert out[0].role == "trace_dependency"
    assert out[0].satisfying_kinds == ("trace_callees",)


def test_trace_walks_only_calls_edges():
    db = FakeNeo4jDB([[], []], queued=True)
    expand_trace_neighbourhood(
        [make_role_candidate("u:target")],
        db=db,
        workspace_id=AXIS_TEST_WORKSPACE,
    )

    assert len(db._session.runs) == 2
    joined = "\n".join(query for query, _params in db._session.runs)
    assert "CALLS_DIRECT" in joined
    assert "CALLS_SCOPED" in joined
    assert "AFFECTS" not in joined
    assert "HAS_API" not in joined
    assert "INHERITED_API" not in joined


def test_duplicate_uids_keep_caller_tag_first():
    db = FakeNeo4jDB(
        [
            [_record("u:both")],
            [_record("u:both")],
        ],
        queued=True,
    )

    out = expand_trace_neighbourhood(
        [make_role_candidate("u:target")],
        db=db,
        workspace_id=AXIS_TEST_WORKSPACE,
    )

    assert len(out) == 1
    assert out[0].satisfying_kinds == ("trace_callers",)


def test_seed_and_excluded_uids_are_skipped():
    db = FakeNeo4jDB(
        [
            [_record("u:target"), _record("u:skip"), _record("u:keep")],
            [],
        ],
        queued=True,
    )

    out = expand_trace_neighbourhood(
        [make_role_candidate("u:target")],
        db=db,
        workspace_id=AXIS_TEST_WORKSPACE,
        exclude_uids=["u:skip"],
    )

    assert [c.uid for c in out] == ["u:keep"]


def test_max_traced_caps_pool_size():
    db = FakeNeo4jDB(
        [
            [_record(f"u:c{i}") for i in range(20)],
            [],
        ],
        queued=True,
    )

    out = expand_trace_neighbourhood(
        [make_role_candidate("u:target")],
        db=db,
        workspace_id=AXIS_TEST_WORKSPACE,
        max_traced=5,
    )

    assert len(out) == 5
