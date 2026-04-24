from sidecar.indexer import docs as docs_indexer


def test_index_docs_returns_file_and_chunk_counts(tmp_path, monkeypatch):
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "a.md").write_text("# A\n\nhello world", encoding="utf-8")
    (docs_dir / "b.md").write_text("# B\n\none two three", encoding="utf-8")

    batch_upserts: list[list[tuple[str, int]]] = []
    linked: list[str] = []
    closed: list[str] = []

    class FakeLance:
        def upsert_chunk_batches(self, file_chunks, progress_callback=None):
            batch_upserts.append([(file_path, len(chunks)) for file_path, chunks in file_chunks])

    class FakeNeo4j:
        def close(self):
            closed.append("neo4j")

    monkeypatch.setattr(docs_indexer, "LanceDBClient", lambda: FakeLance())
    monkeypatch.setattr(docs_indexer, "Neo4jClient", lambda *args, **kwargs: FakeNeo4j())
    monkeypatch.setattr(
        docs_indexer,
        "link_docs_to_symbols",
        lambda neo4j, lance, workspace_id="": linked.append(workspace_id),
    )

    result = docs_indexer.index_docs(str(docs_dir), workspace_id="acme/repo@main")

    assert result["files_indexed"] == 2
    assert result["chunks_indexed"] == 2
    assert result["docs_path"] == str(docs_dir)
    assert set(result["timings_sec"]) == {
        "chunking",
        "upsert",
        "link_prepare",
        "link_neo_write",
        "link",
        "total",
    }
    assert result["link_stats"] == {}
    assert batch_upserts == [[
        (str(docs_dir / "a.md"), 1),
        (str(docs_dir / "b.md"), 1),
    ]]
    assert linked == ["acme/repo@main"]
    assert closed == ["neo4j"]


def test_index_docs_falls_back_to_per_file_upsert_when_bulk_missing(tmp_path, monkeypatch):
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "a.md").write_text("# A\n\nhello world", encoding="utf-8")

    upserts: list[tuple[str, int]] = []

    class FakeLance:
        def upsert_chunks(self, file_path: str, chunks: list[str]):
            upserts.append((file_path, len(chunks)))

    class FakeNeo4j:
        @staticmethod
        def close():
            return None

    monkeypatch.setattr(docs_indexer, "LanceDBClient", lambda: FakeLance())
    monkeypatch.setattr(docs_indexer, "Neo4jClient", lambda *args, **kwargs: FakeNeo4j())
    monkeypatch.setattr(docs_indexer, "link_docs_to_symbols", lambda *args, **kwargs: None)

    docs_indexer.index_docs(str(docs_dir), workspace_id="acme/repo@main")

    assert upserts == [(str(docs_dir / "a.md"), 1)]
