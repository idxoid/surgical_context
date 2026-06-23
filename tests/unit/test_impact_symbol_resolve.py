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


def test_resolve_impact_symbol_uid_prefers_candidate_with_callers():
    db = Neo4jClient.__new__(Neo4jClient)

    def _candidates(name, workspace_id="ws"):
        return [
            {
                "uid": "ctx",
                "path": "/repo/context_engine/axis/graph_walk.py",
                "incoming": 0,
            },
            {
                "uid": "mirror",
                "path": "/repo/sidecar/axis/graph_walk.py",
                "incoming": 2,
            },
        ]

    db.list_symbol_impact_candidates = _candidates  # type: ignore[method-assign]

    uid = db.resolve_impact_symbol_uid(
        "steps_for_mode",
        "ws",
        file_path="/repo/context_engine/axis/graph_walk.py",
    )
    assert uid == "mirror"
