import pytest

from sidecar.axis.graph_traversal import AxisGraphTraversal, render_axis_expansion_query
from sidecar.axis.query_plan import (
    AxisQueryRequest,
    GraphExpansionStep,
    compile_axis_query,
)


class _Session:
    def __init__(self, rows_by_call):
        self.rows_by_call = list(rows_by_call)
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def run(self, query, **params):
        self.calls.append((query, params))
        return self.rows_by_call.pop(0)


class _Driver:
    def __init__(self, rows_by_call):
        self.session_obj = _Session(rows_by_call)

    def session(self):
        return self.session_obj


class _Db:
    def __init__(self, rows_by_call):
        self.driver = _Driver(rows_by_call)


def test_render_axis_expansion_query_uses_direction_and_workspace_filter():
    query = render_axis_expansion_query(
        GraphExpansionStep(
            name="control",
            edge_types=("CALLS", "CALLS_DYNAMIC"),
            direction="out",
            max_depth=2,
        )
    )

    assert "(seed)-[rels:CALLS|CALLS_DYNAMIC*1..2]->(n:Symbol)" in query
    assert "coalesce(rel.workspace_id, $workspace_id) = $workspace_id" in query
    assert "MATCH (file:File {workspace_id: $workspace_id})-[:CONTAINS]->(n)" in query


def test_render_axis_expansion_query_rejects_unsafe_edge_type():
    with pytest.raises(ValueError, match="Unsafe edge type"):
        render_axis_expansion_query(
            GraphExpansionStep(
                name="bad",
                edge_types=("CALLS) DELETE n //",),
                direction="out",
            )
        )


def test_axis_graph_traversal_executes_plan_steps_and_deduplicates_hits():
    plan = compile_axis_query(
        AxisQueryRequest(traversal_mode="deferred_binding_flow"),
        workspace_id="ws",
    )
    db = _Db(
        [
            [
                {
                    "seed_uid": "seed",
                    "uid": "a",
                    "name": "A",
                    "qualified_name": "pkg.A",
                    "file_path": "/repo/a.py",
                    "depth": 1,
                },
                {
                    "seed_uid": "seed",
                    "uid": "a",
                    "name": "A",
                    "qualified_name": "pkg.A",
                    "file_path": "/repo/a.py",
                    "depth": 1,
                },
            ],
            [
                {
                    "seed_uid": "seed",
                    "uid": "b",
                    "name": "B",
                    "qualified_name": "pkg.B",
                    "file_path": "/repo/b.py",
                    "depth": 2,
                }
            ],
        ]
    )

    hits = AxisGraphTraversal(db, "ws").expand(["seed"], plan)

    assert [hit.to_dict() for hit in hits] == [
        {
            "seed_uid": "seed",
            "uid": "a",
            "name": "A",
            "qualified_name": "pkg.A",
            "file_path": "/repo/a.py",
            "step": "binding_structure_expansion",
            "depth": 1,
        },
        {
            "seed_uid": "seed",
            "uid": "b",
            "name": "B",
            "qualified_name": "pkg.B",
            "file_path": "/repo/b.py",
            "step": "deferred_runtime_dispatch",
            "depth": 2,
        },
    ]
    assert len(db.driver.session_obj.calls) == 2
    assert db.driver.session_obj.calls[0][1] == {
        "seed_uids": ["seed"],
        "workspace_id": "ws",
    }


def test_axis_graph_traversal_returns_empty_for_no_seeds():
    plan = compile_axis_query(
        AxisQueryRequest(traversal_mode="immediate_control_flow"),
        workspace_id="ws",
    )
    db = _Db([])

    assert AxisGraphTraversal(db, "ws").expand([], plan) == []
    assert db.driver.session_obj.calls == []
