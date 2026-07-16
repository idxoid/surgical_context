"""Shared graph-walk core — walk_neighbours + cap_by_file."""

from __future__ import annotations

import pytest

from context_engine.axis.graph_walk import (
    EdgeProfile,
    Neighbour,
    _safe_rel_pattern,
    call_fan_in,
    cap_by_file,
    walk_neighbours,
    walk_neighbours_grouped,
)
from context_engine.axis.stage_warnings import collect_stage_warnings, stage_warning_dicts
from tests.unit.axis_helpers import (
    AXIS_TEST_WORKSPACE,
    BAD_MAX_HOPS,
    FakeNeo4jDB,
    Neo4jDriver,
    Neo4jSession,
    graph_row,
)


def _rec(uid, name="n", file_path="/f.py", depth=1, reach=1):
    return graph_row(uid, name, file_path, depth=depth, reach=reach)


def _nb(uid, file_path="/f.py", depth=1, reach=1):
    return Neighbour(uid=uid, name=uid, file_path=file_path, depth=depth, reach=reach)


# --- _safe_rel_pattern -----------------------------------------------------


def test_safe_rel_pattern_joins_valid():
    assert _safe_rel_pattern(["CALLS", "HAS_API"]) == "CALLS|HAS_API"


def test_safe_rel_pattern_rejects_injection():
    with pytest.raises(ValueError, match="unsafe edge type"):
        _safe_rel_pattern(["CALLS", "DROP TABLE"])


# --- walk_neighbours: empty / parsing --------------------------------------


def test_walk_empty_seeds_returns_empty():
    assert walk_neighbours(FakeNeo4jDB(), AXIS_TEST_WORKSPACE, [], edges=EdgeProfile.AFFECTS) == []


def test_walk_parses_rows_into_neighbours():
    db = FakeNeo4jDB([_rec("u:a", depth=2, reach=3)])
    out = walk_neighbours(db, AXIS_TEST_WORKSPACE, ["u:seed"], edges=EdgeProfile.AFFECTS)
    assert len(out) == 1
    assert out[0] == Neighbour("u:a", "n", "/f.py", 2, 3)


def test_walk_driver_error_returns_empty():
    class _BoomSession(Neo4jSession):
        def run(self, query, **params):
            raise RuntimeError("boom")

    db = FakeNeo4jDB()
    db.driver = Neo4jDriver(_BoomSession([]))
    with collect_stage_warnings() as warnings:
        assert walk_neighbours(db, AXIS_TEST_WORKSPACE, ["u:s"], edges=EdgeProfile.AFFECTS) == []

    payload = stage_warning_dicts(warnings)
    assert payload[0]["code"] == "graph_walk_cypher_failed"
    assert payload[0]["error_type"] == "RuntimeError"
    assert payload[0]["details"]["seed_count"] == 1


def test_grouped_walk_driver_error_returns_warning():
    class _BoomSession(Neo4jSession):
        def run(self, query, **params):
            raise RuntimeError("boom")

    db = FakeNeo4jDB()
    db.driver = Neo4jDriver(_BoomSession([]))

    with collect_stage_warnings() as warnings:
        assert (
            walk_neighbours_grouped(
                db,
                AXIS_TEST_WORKSPACE,
                ["u:s"],
                edges=EdgeProfile.AFFECTS,
            )
            == {}
        )

    payload = stage_warning_dicts(warnings)
    assert payload[0]["code"] == "graph_walk_grouped_cypher_failed"


def test_grouped_walk_threads_variable_per_seed_limits_into_one_query():
    db = FakeNeo4jDB([])

    walk_neighbours_grouped(
        db,
        AXIS_TEST_WORKSPACE,
        ["u:head", "u:tail"],
        edges=EdgeProfile.CALLS,
        limit_per_seed=24,
        limit_per_seed_by_uid={"u:head": 24, "u:tail": 12},
    )

    query, params = db.session_obj.runs[0]
    assert "coalesce($limit_per_seed_by_uid[su], $limit_per_seed)" in query
    assert params["limit_per_seed"] == 24
    assert params["limit_per_seed_by_uid"] == {"u:head": 24, "u:tail": 12}


def test_grouped_walk_rejects_invalid_variable_limit():
    with pytest.raises(ValueError, match="limit_per_seed_by_uid"):
        walk_neighbours_grouped(
            FakeNeo4jDB(),
            AXIS_TEST_WORKSPACE,
            ["u:tail"],
            edges=EdgeProfile.CALLS,
            limit_per_seed_by_uid={"u:tail": 0},
        )


def test_inproc_grouped_walk_stops_before_deeper_hops_once_limit_is_full():
    from context_engine.axis.graph_walk_inproc import _grouped_neighbours_by_seed

    calls: list[str] = []
    edges = {
        "u:seed": frozenset({"u:c", "u:a", "u:b"}),
        "u:a": frozenset({"u:deep"}),
    }

    def neighbours(uid: str):
        calls.append(uid)
        return edges.get(uid, frozenset())

    meta = {
        uid: (uid, f"/{uid}.py", "function") for uid in ("u:seed", "u:a", "u:b", "u:c", "u:deep")
    }
    grouped = _grouped_neighbours_by_seed(
        ["u:seed"],
        neigh=neighbours,
        meta=meta,
        max_hops=2,
        limit_per_seed=4,
        limit_per_seed_by_uid={"u:seed": 2},
    )

    assert [row.uid for row in grouped["u:seed"]] == ["u:a", "u:b"]
    assert calls == ["u:seed"]


def test_empty_inproc_walk_falls_back_to_neo4j(monkeypatch):
    from context_engine.axis import graph_walk_inproc

    monkeypatch.setattr(graph_walk_inproc, "should_use", lambda workspace_id: True)
    monkeypatch.setattr(graph_walk_inproc, "walk_neighbours", lambda *args, **kwargs: [])
    db = FakeNeo4jDB([_rec("u:caller", name="caller")])

    out = walk_neighbours(
        db,
        AXIS_TEST_WORKSPACE,
        ["u:seed"],
        edges=EdgeProfile.CALLS,
        direction="reverse",
    )

    assert [row.uid for row in out] == ["u:caller"]
    assert "CALLS_IMPORTED" in db.session_obj.runs[0][0]


# --- walk_neighbours: direction shapes the Cypher --------------------------


def test_forward_direction_emits_outgoing_pattern():
    db = FakeNeo4jDB([])
    walk_neighbours(
        db, AXIS_TEST_WORKSPACE, ["u:s"], edges=EdgeProfile.AFFECTS, direction="forward"
    )
    q = db.session_obj.runs[0][0]
    assert "(s)-[r:AFFECTS*1..2]->(n:Symbol)" in q


def test_reverse_direction_emits_incoming_pattern():
    db = FakeNeo4jDB([])
    walk_neighbours(db, AXIS_TEST_WORKSPACE, ["u:s"], edges=EdgeProfile.CALLS, direction="reverse")
    q = db.session_obj.runs[0][0]
    assert "(n:Symbol)-[r:" in q and "]->(s)" in q


def test_undirected_direction_emits_undirected_pattern():
    db = FakeNeo4jDB([])
    walk_neighbours(
        db, AXIS_TEST_WORKSPACE, ["u:s"], edges=EdgeProfile.AFFECTS, direction="undirected"
    )
    q = db.session_obj.runs[0][0]
    assert "(s)-[r:AFFECTS*1..2]-(n:Symbol)" in q


def test_max_hops_threaded_into_pattern():
    db = FakeNeo4jDB([])
    walk_neighbours(db, AXIS_TEST_WORKSPACE, ["u:s"], edges=EdgeProfile.AFFECTS, max_hops=4)
    assert "*1..4" in db.session_obj.runs[0][0]


@pytest.mark.parametrize("bad_hops", BAD_MAX_HOPS)
def test_walk_rejects_unsafe_max_hops(bad_hops):
    with pytest.raises(ValueError, match="max_hops"):
        walk_neighbours(
            FakeNeo4jDB(),
            AXIS_TEST_WORKSPACE,
            ["u:s"],
            edges=EdgeProfile.AFFECTS,
            max_hops=bad_hops,  # type: ignore[arg-type]
        )


# --- walk_neighbours: anchor + filters -------------------------------------


def test_file_classes_anchor_starts_at_classes_and_excludes_same_file():
    db = FakeNeo4jDB([])
    walk_neighbours(
        db,
        AXIS_TEST_WORKSPACE,
        ["u:s"],
        edges=EdgeProfile.INHERITANCE,
        direction="forward",
        anchor="file_classes",
    )
    q = db.session_obj.runs[0][0]
    assert "(seed_file)-[:CONTAINS]->(cls:Symbol)" in q
    assert "cls.kind = 'class'" in q
    assert "fn.path <> seed_file.path" in q


def test_class_targets_only_filters_neighbour_kind():
    db = FakeNeo4jDB([])
    walk_neighbours(
        db,
        AXIS_TEST_WORKSPACE,
        ["u:s"],
        edges=EdgeProfile.INHERITANCE,
        class_targets_only=True,
    )
    assert "n.kind = 'class'" in db.session_obj.runs[0][0]


def test_exclude_tests_injects_fence_clause():
    db = FakeNeo4jDB([])
    walk_neighbours(
        db,
        AXIS_TEST_WORKSPACE,
        ["u:s"],
        edges=EdgeProfile.AFFECTS,
        exclude_tests=True,
    )
    q = db.session_obj.runs[0][0]
    assert "/tests/" in q and "fn.path" in q


def test_no_exclude_tests_has_no_fence():
    db = FakeNeo4jDB([])
    walk_neighbours(
        db,
        AXIS_TEST_WORKSPACE,
        ["u:s"],
        edges=EdgeProfile.AFFECTS,
        exclude_tests=False,
    )
    assert "/tests/" not in db.session_obj.runs[0][0]


def test_reach_and_depth_aggregation_in_query():
    db = FakeNeo4jDB([])
    walk_neighbours(db, AXIS_TEST_WORKSPACE, ["u:s"], edges=EdgeProfile.AFFECTS)
    q = db.session_obj.runs[0][0]
    assert "count(DISTINCT su) AS reach" in q
    assert "min(size(r)) AS depth" in q


def test_walk_filters_traversed_relationships_by_workspace():
    db = FakeNeo4jDB([])
    walk_neighbours(db, AXIS_TEST_WORKSPACE, ["u:s"], edges=EdgeProfile.AFFECTS)
    q = db.session_obj.runs[0][0]
    assert "all(rel IN r WHERE coalesce(rel.workspace_id, $workspace_id) = $workspace_id)" in q


# --- cap_by_file -----------------------------------------------------------


def test_cap_skips_seed_files():
    nbs = [_nb("u:a", "/seed.py"), _nb("u:b", "/other.py")]
    out = cap_by_file(nbs, seed_files={"/seed.py"})
    assert [n.uid for n in out] == ["u:b"]


def test_cap_excludes_uids():
    nbs = [_nb("u:a", "/a.py"), _nb("u:b", "/b.py")]
    out = cap_by_file(nbs, exclude_uids={"u:a"})
    assert [n.uid for n in out] == ["u:b"]


def test_cap_dedupes_uid():
    nbs = [_nb("u:a", "/a.py"), _nb("u:a", "/a.py")]
    out = cap_by_file(nbs, max_per_file=5)
    assert [n.uid for n in out] == ["u:a"]


def test_cap_max_per_file():
    nbs = [_nb(f"u:{i}", "/shared.py") for i in range(5)]
    out = cap_by_file(nbs, max_per_file=2, max_total=99)
    assert len(out) == 2


def test_cap_max_files():
    nbs = [_nb(f"u:{i}", f"/f{i}.py") for i in range(8)]
    out = cap_by_file(nbs, max_files=3, max_per_file=1, max_total=99)
    assert len({n.file_path for n in out}) == 3


def test_cap_max_total():
    nbs = [_nb(f"u:{i}", f"/f{i}.py") for i in range(20)]
    out = cap_by_file(nbs, max_files=99, max_per_file=1, max_total=5)
    assert len(out) == 5


def test_cap_preserves_input_order():
    nbs = [_nb("u:hi", "/a.py", reach=9), _nb("u:lo", "/b.py", reach=1)]
    out = cap_by_file(nbs, max_per_file=1, max_total=99)
    assert [n.uid for n in out] == ["u:hi", "u:lo"]


# --- call_fan_in -----------------------------------------------------------


def test_call_fan_in_empty_uids_skips_query():
    db = FakeNeo4jDB([])
    assert call_fan_in(db, AXIS_TEST_WORKSPACE, []) == {}
    assert db.session_obj.runs == []


def test_call_fan_in_counts_distinct_callers():
    db = FakeNeo4jDB([{"uid": "u:a", "fanin": 7}, {"uid": "u:b", "fanin": 0}])
    out = call_fan_in(db, AXIS_TEST_WORKSPACE, ["u:a", "u:b"])
    assert out == {"u:a": 7, "u:b": 0}


def test_call_fan_in_query_is_workspace_scoped_caller_count():
    db = FakeNeo4jDB([])
    call_fan_in(db, AXIS_TEST_WORKSPACE, ["u:a"])
    q = db.session_obj.runs[0][0]
    assert "count(DISTINCT caller) AS fanin" in q
    assert "coalesce(r.workspace_id, $workspace_id) = $workspace_id" in q
    assert "(cf:File {workspace_id: $workspace_id})-[:CONTAINS]->(caller)" in q


def test_call_fan_in_driver_error_returns_empty():
    class _BoomSession(Neo4jSession):
        def run(self, query, **params):
            raise RuntimeError("boom")

    db = FakeNeo4jDB()
    db.driver = Neo4jDriver(_BoomSession([]))
    with collect_stage_warnings() as warnings:
        assert call_fan_in(db, AXIS_TEST_WORKSPACE, ["u:a"]) == {}

    payload = stage_warning_dicts(warnings)
    assert payload[0]["code"] == "graph_walk_fan_in_cypher_failed"
