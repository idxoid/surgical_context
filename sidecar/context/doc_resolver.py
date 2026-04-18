"""DocResolver — LanceDB semantic retrieval."""

from sidecar.context.types import DocChunk


class DocResolver:
    """Wraps LanceDB semantic search."""

    def __init__(self, lancedb_client):
        self.db = lancedb_client

    def search(self, query: str, limit: int = 3) -> list[DocChunk]:
        """Semantic search returning top-k doc chunks."""
        raw = self.db.search(query, limit)
        return [
            DocChunk(
                source_file=d["file_path"],
                chunk_id=f"{d['file_path']}::search",
                content=d["chunk"],
            )
            for d in raw
        ]
