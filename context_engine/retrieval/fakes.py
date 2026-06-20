"""In-memory / stub providers for contract tests (no Neo4j / LanceDB)."""

from __future__ import annotations

from typing import Any

from context_engine.workspace import DEFAULT_WORKSPACE_ID


class FakeVectorSearchProvider:
    """Returns canned doc/symbol rows per workspace_id."""

    def __init__(
        self,
        *,
        docs_by_workspace: dict[str, list[dict[str, Any]]] | None = None,
        symbols_by_workspace: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        self.docs_by_workspace = docs_by_workspace or {}
        self.symbols_by_workspace = symbols_by_workspace or {}

    def search_docs(
        self,
        query: str,
        limit: int = 30,
        *,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
    ) -> list[dict[str, Any]]:
        rows = list(self.docs_by_workspace.get(workspace_id, []))
        return rows[:limit]

    def search_symbols(
        self,
        query: str,
        limit: int = 30,
        *,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
    ) -> list[dict[str, Any]]:
        rows = list(self.symbols_by_workspace.get(workspace_id, []))
        return rows[:limit]


class FakeWorkspaceMetaProvider:
    """Fixed profile dict and graph version per workspace."""

    def __init__(
        self,
        *,
        profiles: dict[str, dict[str, Any]] | None = None,
        graph_versions: dict[str, int] | None = None,
    ) -> None:
        self._profiles = profiles or {}
        self._graph_versions = graph_versions or {}

    def repository_profile(self, workspace_id: str) -> dict[str, Any]:
        return dict(self._profiles.get(workspace_id, {}))

    def graph_version(self, workspace_id: str) -> int:
        return int(self._graph_versions.get(workspace_id, 0))


class FakeGraphDriverProvider:
    """Wraps any object with a ``driver`` attribute (e.g. MagicMock)."""

    def __init__(self, driver: Any) -> None:
        self._driver = driver

    @property
    def driver(self) -> Any:
        return self._driver
