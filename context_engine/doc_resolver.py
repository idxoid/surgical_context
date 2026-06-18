"""DocResolver — LanceDB semantic retrieval."""

from context_engine.context_types import DocChunk
from context_engine.workspace import DEFAULT_WORKSPACE_ID


class DocResolver:
    """Wraps LanceDB semantic search."""

    def __init__(self, lancedb_client):
        self.db = lancedb_client

    def search(
        self, query: str, limit: int = 3, *, workspace_id: str = DEFAULT_WORKSPACE_ID
    ) -> list[DocChunk]:
        """Semantic search returning top-k doc chunks."""
        try:
            raw = self.db.search(query, limit, workspace_id=workspace_id)
        except TypeError:
            # Backward compatibility for test fakes/older clients.
            raw = self.db.search(query, limit)
        return [
            DocChunk(
                source_file=d["file_path"],
                chunk_id=d.get("id", f"{d['file_path']}::search"),
                content=d["chunk"],
                score=d.get("score"),
                provenance=["vector:docs"],
            )
            for d in raw
        ]
