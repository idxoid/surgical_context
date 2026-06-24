"""Impact symbol uid resolution against duplicate indexed paths."""

from context_engine.database.neo4j_client import Neo4jClient


def test_path_matches_file():
    assert Neo4jClient._path_matches_file(
        "/repo/context_engine/axis/graph_walk.py",
        "/repo/context_engine/axis/graph_walk.py",
    )
    assert Neo4jClient._path_matches_file(
        "/repo/context_engine/axis/graph_walk.py",
        "context_engine/axis/graph_walk.py",
    )
    assert not Neo4jClient._path_matches_file(
        "/repo/context_engine/axis/graph_walk.py",
        "/repo/context_engine/axis/graph_walk.py",
    )


def test_resolve_impact_symbol_uid_prefers_requested_file_over_unrelated_callers():
    db = Neo4jClient.__new__(Neo4jClient)

    def _candidates(name, workspace_id="ws"):
        return [
            {
                "uid": "typescript-ask",
                "path": "/repo/extension/src/context_engineClient.ts",
                "incoming": 0,
            },
            {
                "uid": "python-ask",
                "path": "/repo/context_engine/api/routes/ask.py",
                "incoming": 5,
            },
        ]

    db.list_symbol_impact_candidates = _candidates  # type: ignore[method-assign]

    uid = db.resolve_impact_symbol_uid(
        "ask",
        "ws",
        file_path="/repo/extension/src/context_engineClient.ts",
    )
    assert uid == "typescript-ask"


def test_resolve_impact_symbol_uid_uses_callers_when_requested_file_is_not_indexed():
    db = Neo4jClient.__new__(Neo4jClient)

    def _candidates(name, workspace_id="ws"):
        return [
            {
                "uid": "quiet",
                "path": "/repo/context_engine/axis/graph_walk.py",
                "incoming": 0,
            },
            {
                "uid": "active",
                "path": "/repo/context_engine/axis/graph_walk.py",
                "incoming": 2,
            },
        ]

    db.list_symbol_impact_candidates = _candidates  # type: ignore[method-assign]

    uid = db.resolve_impact_symbol_uid(
        "steps_for_mode",
        "ws",
        file_path="/repo/not-yet-indexed/graph_steps.py",
    )
    assert uid == "active"
