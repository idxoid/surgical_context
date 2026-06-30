from context_engine.indexer import docs as docs_indexer


def _provider_returning(client):
    """Fake DatabaseProvider whose client_for() yields the given neo4j stub —
    index_docs now mints its client from the shared provider, not a raw
    Neo4jClient."""

    class _Prov:
        def client_for(self, user_id="anonymous"):
            return client

    return _Prov()


def test_index_docs_returns_file_and_chunk_counts(tmp_path, monkeypatch):
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "a.md").write_text("# A\n\nhello world", encoding="utf-8")
    (docs_dir / "b.md").write_text("# B\n\none two three", encoding="utf-8")

    batch_upserts: list[list[tuple[str, int]]] = []
    linked: list[str] = []
    closed: list[str] = []

    class FakeLance:
        def upsert_chunk_batches(self, file_chunks, *, workspace_id="", progress_callback=None):
            assert workspace_id == "acme/repo@main"
            batch_upserts.append([(file_path, len(chunks)) for file_path, chunks in file_chunks])

    class FakeNeo4j:
        def close(self):
            closed.append("neo4j")

    monkeypatch.setattr(docs_indexer, "LanceDBClient", lambda **_: FakeLance())
    monkeypatch.setattr(
        docs_indexer, "get_database_provider", lambda: _provider_returning(FakeNeo4j())
    )
    monkeypatch.setattr(
        docs_indexer,
        "link_docs_to_symbols",
        lambda neo4j, lance, workspace_id="", **_: linked.append(workspace_id),
    )

    result = docs_indexer.index_docs(
        str(docs_dir), workspace_id="acme/repo@main", index_profile="legacy"
    )

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
    assert batch_upserts == [
        [
            (str(docs_dir / "a.md"), 1),
            (str(docs_dir / "b.md"), 1),
        ]
    ]
    assert linked == ["acme/repo@main"]
    assert closed == ["neo4j"]


def test_index_docs_falls_back_to_per_file_upsert_when_bulk_missing(tmp_path, monkeypatch):
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "a.md").write_text("# A\n\nhello world", encoding="utf-8")

    upserts: list[tuple[str, int]] = []

    class FakeLance:
        def upsert_chunks(self, file_path: str, chunks: list[str], *, workspace_id: str = ""):
            assert workspace_id == "acme/repo@main"
            upserts.append((file_path, len(chunks)))

    class FakeNeo4j:
        @staticmethod
        def close():
            return None

    monkeypatch.setattr(docs_indexer, "LanceDBClient", lambda **_: FakeLance())
    monkeypatch.setattr(
        docs_indexer, "get_database_provider", lambda: _provider_returning(FakeNeo4j())
    )
    monkeypatch.setattr(docs_indexer, "link_docs_to_symbols", lambda *args, **kwargs: None)

    docs_indexer.index_docs(str(docs_dir), workspace_id="acme/repo@main", index_profile="legacy")

    assert upserts == [(str(docs_dir / "a.md"), 1)]


def test_index_docs_threads_one_profile_into_client_and_workspace(tmp_path, monkeypatch):
    """Regression: a single resolved profile must drive BOTH the LanceDBClient
    tables and the effective workspace suffix, so docs are not written to a
    table that mismatches the suffixed namespace they are stored under."""
    from context_engine.index_profile import AXIS_PYTHON_V1_PROFILE

    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "a.md").write_text("# A\n\nhello world", encoding="utf-8")

    constructed: dict[str, object] = {}
    upsert_ws: list[str] = []

    class FakeLance:
        def upsert_chunk_batches(self, file_chunks, *, workspace_id="", progress_callback=None):
            upsert_ws.append(workspace_id)

    def fake_client(*, index_profile=None):
        constructed["profile"] = index_profile
        return FakeLance()

    class FakeNeo4j:
        @staticmethod
        def close():
            return None

    monkeypatch.setattr(docs_indexer, "LanceDBClient", fake_client)
    monkeypatch.setattr(
        docs_indexer, "get_database_provider", lambda: _provider_returning(FakeNeo4j())
    )
    monkeypatch.setattr(docs_indexer, "link_docs_to_symbols", lambda *a, **k: None)

    docs_indexer.index_docs(
        str(docs_dir), workspace_id="acme/repo@main", index_profile="axis_python_v1"
    )

    profile = constructed["profile"]
    assert getattr(profile, "name", None) == AXIS_PYTHON_V1_PROFILE
    # The suffix on the upsert workspace id comes from the same profile.
    assert upsert_ws == ["acme/repo@main+axis_python_v1"]
