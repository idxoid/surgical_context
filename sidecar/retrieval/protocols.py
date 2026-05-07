"""Storage-facing protocols for retrieval (default adapters: Neo4j, LanceDB).

Implementations can be swapped in tests via `sidecar.retrieval.fakes`.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from sidecar.workspace import DEFAULT_WORKSPACE_ID


@runtime_checkable
class VectorSearchProvider(Protocol):
    """Doc + symbol embedding search (LanceDB-shaped surface).

    Matches `VectorSearcher` method names so production wrapper satisfies the protocol.
    """

    def search_docs(
        self,
        query: str,
        limit: int = 30,
        *,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
    ) -> list[dict[str, Any]]: ...

    def search_symbols(
        self,
        query: str,
        limit: int = 30,
        *,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
    ) -> list[dict[str, Any]]: ...


@runtime_checkable
class WorkspaceMetaProvider(Protocol):
    """Repository profile + graph generation metadata per workspace."""

    def repository_profile(self, workspace_id: str) -> dict[str, Any]: ...

    def graph_version(self, workspace_id: str) -> int: ...


@runtime_checkable
class GraphDriverProvider(Protocol):
    """Neo4j driver holder (`driver.session()` for Cypher)."""

    @property
    def driver(self) -> Any: ...
