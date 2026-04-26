from unittest.mock import MagicMock

from sidecar.indexer import anchor
from sidecar.indexer.anchor import (
    _add_covers_edges,
    _add_covers_edges_batch,
    _matches_allowed_prefix,
    _normalize_allowed_prefixes,
    _write_anchors,
)


def test_normalize_allowed_prefixes_resolves_and_deduplicates(tmp_path):
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()

    prefixes = _normalize_allowed_prefixes(
        [str(docs_dir), str(docs_dir / ".." / "docs"), None, ""]
    )

    assert prefixes == [str(docs_dir.resolve())]


def test_matches_allowed_prefix_accepts_nested_file_only(tmp_path):
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    allowed = [str(docs_dir.resolve())]

    assert _matches_allowed_prefix(str(docs_dir / "a.md"), allowed) is True
    assert _matches_allowed_prefix(str(tmp_path / "other" / "a.md"), allowed) is False


def test_add_covers_edges_uses_unwind_for_bulk_uids():
    tx = MagicMock()

    _add_covers_edges(tx, "chunk-1", ["uid-a", "uid-b"], "acme/repo@main")

    query = tx.run.call_args.args[0]
    params = tx.run.call_args.kwargs
    assert "UNWIND $uids AS uid" in query
    assert params["chunk_id"] == "chunk-1"
    assert params["uids"] == ["uid-a", "uid-b"]
    assert params["workspace_id"] == "acme/repo@main"


def test_add_covers_edges_batch_uses_unwind_for_bulk_chunks():
    tx = MagicMock()

    _add_covers_edges_batch(
        tx,
        [
            {"chunk_id": "chunk-1", "uids": ["uid-a", "uid-b"]},
            {"chunk_id": "chunk-2", "uids": ["uid-c"]},
        ],
        "acme/repo@main",
    )

    query = tx.run.call_args.args[0]
    params = tx.run.call_args.kwargs
    assert "UNWIND $rows AS row" in query
    assert "UNWIND row.uids AS uid" in query
    assert params["rows"] == [
        {"chunk_id": "chunk-1", "uids": ["uid-a", "uid-b"]},
        {"chunk_id": "chunk-2", "uids": ["uid-c"]},
    ]
    assert params["workspace_id"] == "acme/repo@main"


def test_write_anchors_uses_unwind_for_bulk_chunks():
    tx = MagicMock()

    _write_anchors(
        tx,
        [
            {
                "chunk_id": "chunk-1",
                "file_path": "/docs/a.md",
                "doc_type": "documentation",
            }
        ],
        "acme/repo@main",
    )

    query = tx.run.call_args.args[0]
    params = tx.run.call_args.kwargs
    assert "UNWIND $rows AS row" in query
    assert params["rows"][0]["chunk_id"] == "chunk-1"
    assert params["workspace_id"] == "acme/repo@main"


def test_link_docs_to_symbols_skips_semantic_search_when_identifier_matches_are_enough(monkeypatch):
    class FakeRows:
        empty = False

        @staticmethod
        def to_dict(mode):
            assert mode == "records"
            return [
                {
                    "id": "chunk-1",
                    "chunk": "# Title\n\nFastAPI APIRoute something",
                    "file_path": "/docs/a.md",
                    "pending": [],
                    "vector": [0.0],
                    "embedding_metadata": "{}",
                }
            ]

    class FakeTable:
        @staticmethod
        def to_pandas():
            return FakeRows()

    class FakeLance:
        def __init__(self):
            self._table = FakeTable()
            self.search_called = False
            self.pending_updates = []

        def search_symbols(self, query, limit=5, threshold=1.5):
            self.search_called = True
            return []

        def set_pending_row(self, row, pending):
            self.pending_updates.append((row["id"], pending))

    neo4j = MagicMock()
    neo4j.driver.session.return_value.__enter__.return_value.run.return_value = [
        {"uid": "uid-fastapi", "name": "FastAPI"},
        {"uid": "uid-route", "name": "APIRoute"},
    ]
    lance = FakeLance()

    monkeypatch.setattr(anchor, "_extract_identifiers", lambda text: ["FastAPI", "APIRoute"])
    monkeypatch.setattr(anchor, "_make_progress", lambda total, desc, unit="item": MagicMock(update=lambda n=1: None, close=lambda: None))

    anchor.link_docs_to_symbols(neo4j, lance, workspace_id="acme/repo@main")

    assert lance.search_called is False
    assert lance.pending_updates == []


def test_link_docs_to_symbols_uses_precomputed_vector_for_semantic_fallback(monkeypatch):
    class FakeRows:
        empty = False

        @staticmethod
        def to_dict(mode):
            assert mode == "records"
            return [
                {
                    "id": "chunk-1",
                    "chunk": "# Title\n\nSome descriptive prose",
                    "file_path": "/docs/a.md",
                    "pending": [],
                    "vector": [0.25, 0.75],
                    "embedding_metadata": "{}",
                }
            ]

    class FakeTable:
        @staticmethod
        def to_pandas():
            return FakeRows()

    class FakeLance:
        def __init__(self):
            self._table = FakeTable()
            self.vector_queries: list[list[float]] = []
            self.text_search_called = False
            self.pending_updates = []

        def search_symbols_by_vector(self, vector, limit=5, threshold=1.5):
            self.vector_queries.append(vector)
            return [{"uid": "uid-hit", "name": "Hit", "file_path": "/repo/hit.py"}]

        def search_symbols(self, query, limit=5, threshold=1.5):
            self.text_search_called = True
            return []

        def set_pending_row(self, row, pending):
            self.pending_updates.append((row["id"], pending))

    neo4j = MagicMock()
    neo4j.driver.session.return_value.__enter__.return_value.run.return_value = []
    lance = FakeLance()

    monkeypatch.setattr(anchor, "_extract_identifiers", lambda text: [])
    monkeypatch.setattr(
        anchor,
        "_make_progress",
        lambda total, desc, unit="item": MagicMock(
            update=lambda n=1: None,
            close=lambda: None,
        ),
    )

    anchor.link_docs_to_symbols(neo4j, lance, workspace_id="acme/repo@main")

    assert lance.vector_queries == [[0.25, 0.75]]
    assert lance.text_search_called is False
    assert lance.pending_updates == []


def test_link_docs_to_symbols_normalizes_array_like_pending(monkeypatch):
    class PendingArray:
        def __bool__(self):
            raise ValueError("ambiguous")

        @staticmethod
        def tolist():
            return []

    class FakeRows:
        empty = False

        @staticmethod
        def to_dict(mode):
            assert mode == "records"
            return [
                {
                    "id": "chunk-1",
                    "chunk": "No identifiers here",
                    "file_path": "/docs/a.md",
                    "pending": PendingArray(),
                    "vector": [0.1, 0.2],
                    "embedding_metadata": "{}",
                }
            ]

    class FakeTable:
        @staticmethod
        def to_pandas():
            return FakeRows()

    class FakeLance:
        def __init__(self):
            self._table = FakeTable()
            self.pending_updates = []

        @staticmethod
        def search_symbols_by_vector(vector, limit=5, threshold=1.5):
            return []

        def set_pending_row(self, row, pending):
            self.pending_updates.append((row["id"], pending))

    neo4j = MagicMock()
    neo4j.driver.session.return_value.__enter__.return_value.run.return_value = []
    lance = FakeLance()

    monkeypatch.setattr(anchor, "_extract_identifiers", lambda text: [])
    monkeypatch.setattr(
        anchor,
        "_make_progress",
        lambda total, desc, unit="item": MagicMock(
            update=lambda n=1: None,
            close=lambda: None,
        ),
    )

    anchor.link_docs_to_symbols(neo4j, lance, workspace_id="acme/repo@main")

    assert lance.pending_updates == []


def test_link_docs_to_symbols_uses_local_symbol_index_when_available(monkeypatch):
    class FakeRows:
        empty = False

        @staticmethod
        def to_dict(mode):
            assert mode == "records"
            return [
                {
                    "id": "chunk-1",
                    "chunk": "Descriptive prose with no identifiers",
                    "file_path": "/docs/a.md",
                    "pending": [],
                    "vector": [0.0, 0.0],
                    "embedding_metadata": "{}",
                }
            ]

    class FakeDocTable:
        @staticmethod
        def to_pandas():
            return FakeRows()

    class FakeSymbolDataFrame:
        empty = False

        @staticmethod
        def iterrows():
            yield 0, {
                "uid": "uid-hit",
                "name": "Hit",
                "file_path": "/repo/hit.py",
                "vector": [0.0, 0.0],
            }

    class FakeSymTable:
        @staticmethod
        def to_pandas():
            return FakeSymbolDataFrame()

    class FakeLance:
        def __init__(self):
            self._table = FakeDocTable()
            self._sym_table = FakeSymTable()
            self.vector_queries: list[list[float]] = []
            self.text_search_called = False
            self.pending_updates = []

        def search_symbols_by_vector(self, vector, limit=5, threshold=1.5):
            self.vector_queries.append(vector)
            return []

        def search_symbols(self, query, limit=5, threshold=1.5):
            self.text_search_called = True
            return []

        def set_pending_row(self, row, pending):
            self.pending_updates.append((row["id"], pending))

    neo4j = MagicMock()
    neo4j.driver.session.return_value.__enter__.return_value.run.return_value = []
    lance = FakeLance()

    monkeypatch.setattr(anchor, "_extract_identifiers", lambda text: [])
    monkeypatch.setattr(
        anchor,
        "_make_progress",
        lambda total, desc, unit="item": MagicMock(
            update=lambda n=1: None,
            close=lambda: None,
        ),
    )

    anchor.link_docs_to_symbols(neo4j, lance, workspace_id="acme/repo@main")

    assert lance.vector_queries == []
    assert lance.text_search_called is False
    assert lance.pending_updates == []
