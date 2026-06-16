"""Shared graph-walk core — walk_neighbours + cap_by_file."""

from __future__ import annotations

import pytest

from sidecar.axis.graph_walk import (
    EdgeProfile,
    Neighbour,
    _safe_rel_pattern,
    call_fan_in,
    cap_by_file,
    walk_neighbours,
)

WORKSPACE = "qa_repo/test@axis"


class _Result:
    def __init__(self, records):
        self._records = list(records)

    def __iter__(self):
        return iter(self._records)


class _Session:
    def __init__(self, records):
        self._records = list(records)
        self.runs: list[tuple[str, dict]] = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def run(self, query: str, **params):
        self.runs.append((query, dict(params)))
        return _Result(self._records)


class _Driver:
    def __init__(self, session):
        self._session = session

    def session(self):
        return self._session


class _FakeDB:
    def __init__(self, records=None):
        self.session_obj = _Session(records or [])
        self.driver = _Driver(self.session_obj)


def _rec(uid, name="n", file_path="/f.py", depth=1, reach=1):
    return {
        "uid": uid,
        "name": name,
        "file_path": file_path,
        "depth": depth,
        "reach": reach,
    }


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
    assert walk_neighbours(_FakeDB(), WORKSPACE, [], edges=EdgeProfile.AFFECTS) == []


def test_walk_parses_rows_into_neighbours():
    db = _FakeDB([_rec("u:a", depth=2, reach=3)])
    out = walk_neighbours(db, WORKSPACE, ["u:seed"], edges=EdgeProfile.AFFECTS)
    assert len(out) == 1
    assert out[0] == Neighbour("u:a", "n", "/f.py", 2, 3)


def test_walk_driver_error_returns_empty():
    class _BoomSession(_Session):
        def run(self, query, **params):
            raise RuntimeError("boom")

    db = _FakeDB()
    db.driver = _Driver(_BoomSession([]))
    assert walk_neighbours(db, WORKSPACE, ["u:s"], edges=EdgeProfile.AFFECTS) == []


# --- walk_neighbours: direction shapes the Cypher --------------------------


def test_forward_direction_emits_outgoing_pattern():
    db = _FakeDB([])
    walk_neighbours(db, WORKSPACE, ["u:s"], edges=EdgeProfile.AFFECTS, direction="forward")
    q = db.session_obj.runs[0][0]
    assert "(s)-[r:AFFECTS*1..2]->(n:Symbol)" in q


def test_reverse_direction_emits_incoming_pattern():
    db = _FakeDB([])
    walk_neighbours(db, WORKSPACE, ["u:s"], edges=EdgeProfile.CALLS, direction="reverse")
    q = db.session_obj.runs[0][0]
    assert "(n:Symbol)-[r:" in q and "]->(s)" in q


def test_undirected_direction_emits_undirected_pattern():
    db = _FakeDB([])
    walk_neighbours(db, WORKSPACE, ["u:s"], edges=EdgeProfile.AFFECTS, direction="undirected")
    q = db.session_obj.runs[0][0]
    assert "(s)-[r:AFFECTS*1..2]-(n:Symbol)" in q


def test_max_hops_threaded_into_pattern():
    db = _FakeDB([])
    walk_neighbours(db, WORKSPACE, ["u:s"], edges=EdgeProfile.AFFECTS, max_hops=4)
    assert "*1..4" in db.session_obj.runs[0][0]


@pytest.mark.parametrize("bad_hops", [0, -1, 1.5, "2", True])
def test_walk_rejects_unsafe_max_hops(bad_hops):
    with pytest.raises(ValueError, match="max_hops"):
        walk_neighbours(
            _FakeDB(),
            WORKSPACE,
            ["u:s"],
            edges=EdgeProfile.AFFECTS,
            max_hops=bad_hops,  # type: ignore[arg-type]
        )


# --- walk_neighbours: anchor + filters -------------------------------------


def test_file_classes_anchor_starts_at_classes_and_excludes_same_file():
    db = _FakeDB([])
    walk_neighbours(
        db,
        WORKSPACE,
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
    db = _FakeDB([])
    walk_neighbours(
        db,
        WORKSPACE,
        ["u:s"],
        edges=EdgeProfile.INHERITANCE,
        class_targets_only=True,
    )
    assert "n.kind = 'class'" in db.session_obj.runs[0][0]


def test_exclude_tests_injects_fence_clause():
    db = _FakeDB([])
    walk_neighbours(
        db,
        WORKSPACE,
        ["u:s"],
        edges=EdgeProfile.AFFECTS,
        exclude_tests=True,
    )
    q = db.session_obj.runs[0][0]
    assert "/tests/" in q and "fn.path" in q


def test_no_exclude_tests_has_no_fence():
    db = _FakeDB([])
    walk_neighbours(
        db,
        WORKSPACE,
        ["u:s"],
        edges=EdgeProfile.AFFECTS,
        exclude_tests=False,
    )
    assert "/tests/" not in db.session_obj.runs[0][0]


def test_reach_and_depth_aggregation_in_query():
    db = _FakeDB([])
    walk_neighbours(db, WORKSPACE, ["u:s"], edges=EdgeProfile.AFFECTS)
    q = db.session_obj.runs[0][0]
    assert "count(DISTINCT su) AS reach" in q
    assert "min(size(r)) AS depth" in q


def test_walk_filters_traversed_relationships_by_workspace():
    db = _FakeDB([])
    walk_neighbours(db, WORKSPACE, ["u:s"], edges=EdgeProfile.AFFECTS)
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
    db = _FakeDB([])
    assert call_fan_in(db, WORKSPACE, []) == {}
    assert db.session_obj.runs == []


def test_call_fan_in_counts_distinct_callers():
    db = _FakeDB([{"uid": "u:a", "fanin": 7}, {"uid": "u:b", "fanin": 0}])
    out = call_fan_in(db, WORKSPACE, ["u:a", "u:b"])
    assert out == {"u:a": 7, "u:b": 0}


def test_call_fan_in_query_is_workspace_scoped_caller_count():
    db = _FakeDB([])
    call_fan_in(db, WORKSPACE, ["u:a"])
    q = db.session_obj.runs[0][0]
    assert "count(DISTINCT caller) AS fanin" in q
    assert "coalesce(r.workspace_id, $workspace_id) = $workspace_id" in q
    assert "(cf:File {workspace_id: $workspace_id})-[:CONTAINS]->(caller)" in q


def test_call_fan_in_driver_error_returns_empty():
    class _BoomSession(_Session):
        def run(self, query, **params):
            raise RuntimeError("boom")

    db = _FakeDB()
    db.driver = _Driver(_BoomSession([]))
    assert call_fan_in(db, WORKSPACE, ["u:a"]) == {}
