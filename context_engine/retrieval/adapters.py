"""Production adapters that implement retrieval protocols (Neo4j, LanceDB wrappers)."""

from __future__ import annotations

from typing import Any

from context_engine.retrieval.protocols import WorkspaceMetaProvider


class Neo4jWorkspaceMetaAdapter:
    """``WorkspaceMetaProvider`` backed by ``Neo4jClient`` / ``AuraClient`` profile + graph version APIs."""

    def __init__(self, neo4j_client: Any) -> None:
        self._db = neo4j_client

    def repository_profile(self, workspace_id: str) -> dict[str, Any]:
        get_profile = getattr(self._db, "get_repository_profile", None)
        if not callable(get_profile):
            return {}
        try:
            profile = get_profile(workspace_id=workspace_id)
        except Exception:
            return {}
        return profile if isinstance(profile, dict) else {}

    def graph_version(self, workspace_id: str) -> int:
        gv = getattr(self._db, "get_workspace_graph_version", None)
        if not callable(gv):
            return 0
        try:
            v = gv(workspace_id=workspace_id)
        except TypeError:
            v = gv(workspace_id)
        if v is None:
            return 0
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0


def neo4j_workspace_meta(neo4j_client: Any) -> WorkspaceMetaProvider:
    """Factory for type checkers / explicit wiring."""
    return Neo4jWorkspaceMetaAdapter(neo4j_client)
