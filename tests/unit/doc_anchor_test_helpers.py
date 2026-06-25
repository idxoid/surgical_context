"""Shared fakes for doc-anchor linker integration tests."""

from __future__ import annotations

from unittest.mock import MagicMock

from context_engine.indexer import anchor

WORKSPACE_ID = "acme/repo@main"
DEFAULT_DOC_ROW = {
    "id": "chunk-1",
    "file_path": "/docs/a.md",
    "pending": [],
    "embedding_metadata": "{}",
}


class FakeDocRows:
    def __init__(self, rows: list[dict]):
        self.empty = not rows
        self._rows = rows

    def to_dict(self, mode: str):
        assert mode == "records"
        return self._rows


def make_doc_row(*, chunk: str, vector: list[float], pending=(), **overrides) -> dict:
    return {**DEFAULT_DOC_ROW, "chunk": chunk, "vector": vector, "pending": pending, **overrides}


class FakeAnchorLance:
    def __init__(self, rows: list[dict]):
        self._rows = FakeDocRows(rows)
        self.pending_updates: list[tuple[str, object]] = []
        self.search_called = False
        self.text_search_called = False
        self.vector_queries: list[list[float]] = []

    def scan_docs_workspace(self, workspace_id):
        return self._rows.to_dict("records")

    def search_symbols(self, query, limit=5, threshold=1.5):
        self.search_called = True
        self.text_search_called = True
        return []

    def search_symbols_by_vector(self, vector, limit=5, threshold=1.5):
        self.vector_queries.append(vector)
        return []

    def set_pending_row(self, row, pending):
        self.pending_updates.append((row["id"], pending))


def null_progress(_total, _desc, unit="item"):
    return MagicMock(update=lambda n=1: None, close=lambda: None)


def run_link_docs_to_symbols(
    monkeypatch,
    *,
    rows: list[dict],
    identifiers: list[str] | None = None,
    neo4j_run_return: list | None = None,
    lance_factory=FakeAnchorLance,
    workspace_id: str = WORKSPACE_ID,
):
    neo4j = MagicMock()
    neo4j.driver.session.return_value.__enter__.return_value.run.return_value = (
        neo4j_run_return if neo4j_run_return is not None else []
    )
    lance = lance_factory(rows)
    monkeypatch.setattr(anchor, "_extract_identifiers", lambda text: identifiers or [])
    monkeypatch.setattr(anchor, "_make_progress", null_progress)
    anchor.link_docs_to_symbols(neo4j, lance, workspace_id=workspace_id)
    return neo4j, lance
