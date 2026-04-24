"""Unit tests for incremental indexing with hash-based skip logic."""

from unittest.mock import MagicMock, patch

from sidecar.database.neo4j_client import Neo4jClient
from sidecar.indexer.code import index_file
from sidecar.parser.protocol import ImportEdge, SymbolMetadata


def _symbol(uid: str, content_hash: str, start_line: int = 1, end_line: int = 2):
    return SymbolMetadata(
        uid=uid,
        name=uid,
        kind="function",
        start_line=start_line,
        end_line=end_line,
        content_hash=content_hash,
        file_path="/test.py",
        qualified_name=uid,
        signature=f"def {uid}()",
        signature_hash=f"sig-{uid}",
        signature_status="resolved",
        language="python",
    )


class TestIncrementalIndexing:
    """Test hash-based file skipping and incremental re-indexing."""

    def test_get_file_hashes_returns_empty_for_empty_input(self):
        """Test that empty file list returns empty dict."""
        db = Neo4jClient("bolt://localhost:7687", "neo4j", "password")
        result = db.get_file_hashes([])
        assert result == {}

    def test_get_file_hashes_with_mock_session(self):
        """Test get_file_hashes logic with mocked session."""
        db = Neo4jClient("bolt://localhost:7687", "neo4j", "password")
        mock_session = MagicMock()

        # Create mock records that support dictionary-style access
        mock_records = [
            {"path": "/file1.py", "hash": "hash1"},
            {"path": "/file2.py", "hash": "hash2"},
        ]

        with patch.object(db.driver, "session") as mock_ctx:
            mock_ctx.return_value.__enter__.return_value = mock_session
            mock_session.run.return_value = mock_records
            result = db.get_file_hashes(["/file1.py", "/file2.py"])
            assert result == {"/file1.py": "hash1", "/file2.py": "hash2"}

    def test_delete_symbols_for_file_with_mock_session(self):
        """Test that delete_symbols_for_file sends correct Cypher."""
        db = Neo4jClient("bolt://localhost:7687", "neo4j", "password")
        mock_session = MagicMock()

        with patch.object(db.driver, "session") as mock_ctx:
            mock_ctx.return_value.__enter__.return_value = mock_session
            db.delete_symbols_for_file("/test.py")
            mock_session.run.assert_called_once()
            call_args = mock_session.run.call_args
            assert "DETACH DELETE s" in call_args[0][0]
            assert call_args[1] == {
                "path": "/test.py",
                "workspace_id": "local/surgical_context@main",
            }

    def test_upsert_nodes_batches_symbols_with_unwind(self):
        tx = MagicMock()

        Neo4jClient._upsert_nodes(
            tx,
            "/test.py",
            "file-hash",
            [_symbol("one", "hash-1"), _symbol("two", "hash-2")],
            "acme/repo@main",
        )

        assert tx.run.call_count == 2
        query = tx.run.call_args.args[0]
        params = tx.run.call_args.kwargs
        assert "UNWIND $symbols AS symbol" in query
        assert params["workspace_id"] == "acme/repo@main"
        assert len(params["symbols"]) == 2

    def test_create_import_relations_batches_rows_with_unwind(self):
        tx = MagicMock()

        Neo4jClient._create_import_relations(
            tx,
            [
                ImportEdge("/repo/a.py", "pkg.module_a", "direct"),
                ImportEdge("/repo/a.py", "pkg.module_b", "direct"),
            ],
            "acme/repo@main",
        )

        tx.run.assert_called_once()
        query = tx.run.call_args.args[0]
        params = tx.run.call_args.kwargs
        assert "UNWIND $imports AS imp" in query
        assert params["workspace_id"] == "acme/repo@main"
        assert len(params["imports"]) == 2

    def test_hash_skip_gate_with_unchanged_file(self):
        """Test that unchanged files are correctly identified."""
        stored_hashes = {"/file1.py": "abc123", "/file2.py": "def456"}
        current_hashes = {"/file1.py": "abc123", "/file2.py": "def456"}

        changed_files = [p for p in current_hashes if current_hashes[p] != stored_hashes.get(p)]
        assert changed_files == []

    def test_hash_skip_gate_with_changed_file(self):
        """Test that changed files are correctly identified."""
        stored_hashes = {"/file1.py": "abc123", "/file2.py": "def456"}
        current_hashes = {"/file1.py": "xyz789", "/file2.py": "def456"}

        changed_files = [p for p in current_hashes if current_hashes[p] != stored_hashes.get(p)]
        assert changed_files == ["/file1.py"]

    def test_hash_skip_gate_with_new_file(self):
        """Test that new files (not in stored hashes) are included in changed."""
        stored_hashes = {"/file1.py": "abc123"}
        current_hashes = {"/file1.py": "abc123", "/file2.py": "new_hash"}

        changed_files = [p for p in current_hashes if current_hashes[p] != stored_hashes.get(p)]
        assert changed_files == ["/file2.py"]

    def test_hash_skip_gate_with_mixed_scenario(self):
        """Test skip gate with unchanged, changed, and new files."""
        stored_hashes = {
            "/file1.py": "hash1",
            "/file2.py": "hash2",
            "/file3.py": "hash3",
        }
        current_hashes = {
            "/file1.py": "hash1",  # unchanged
            "/file2.py": "hash2_new",  # changed
            "/file4.py": "hash4",  # new (not in stored)
        }

        changed_files = [p for p in current_hashes if current_hashes[p] != stored_hashes.get(p)]
        assert set(changed_files) == {"/file2.py", "/file4.py"}

    def test_sha256_hash_is_deterministic(self):
        """Test that sha256 hash is deterministic for same content."""
        import hashlib

        content = b"def test(): pass"
        hash1 = hashlib.sha256(content).hexdigest()
        hash2 = hashlib.sha256(content).hexdigest()
        assert hash1 == hash2
        # Should be a 64-character hex string (256 bits / 4 bits per hex char)
        assert len(hash1) == 64

    def test_sha256_hash_differs_for_different_content(self):
        """Test that different content produces different hashes."""
        import hashlib

        hash1 = hashlib.sha256(b"content1").hexdigest()
        hash2 = hashlib.sha256(b"content2").hexdigest()
        assert hash1 != hash2

    def test_index_file_upserts_only_changed_symbols(self, tmp_path, monkeypatch):
        """Changed/new symbols are upserted; unchanged symbols are preserved."""

        class FakeDb:
            def __init__(self):
                self.upserted = []
                self.pruned_keep = []
                self.cleared_edges = []
                self.linked_calls = []
                self.deleted_imports = []

            def get_symbol_index_for_file(self, file_path, workspace_id):
                return {
                    "unchanged": {"hash": "same", "start_line": 1, "end_line": 2},
                    "changed": {"hash": "old", "start_line": 3, "end_line": 4},
                    "removed": {"hash": "gone", "start_line": 5, "end_line": 6},
                }

            def upsert_file_structure(self, file_path, file_hash, symbols, workspace_id):
                self.upserted = [s.uid for s in symbols]

            def prune_symbols_for_file(self, file_path, keep_uids, workspace_id):
                self.pruned_keep = keep_uids

            def clear_outgoing_symbol_edges(self, symbol_uids, workspace_id):
                self.cleared_edges = symbol_uids

            def link_calls(self, calls, workspace_id):
                self.linked_calls = calls

            def delete_imports_for_file(self, file_path, workspace_id):
                self.deleted_imports.append(file_path)

            def link_imports(self, imports, workspace_id):
                raise AssertionError("No imports expected")

            def link_inheritance(self, inheritance_edges, workspace_id):
                raise AssertionError("No inheritance expected")

        class FakeLance:
            def __init__(self):
                self.upserted = []
                self.deleted = []

            def upsert_symbol_embeddings(self, symbols):
                self.upserted = [s["uid"] for s in symbols]

            def delete_symbol_embeddings(self, uids):
                self.deleted = uids

        class FakeExtractor:
            def extract(self, file_path):
                return [
                    _symbol("unchanged", "same", 1, 2),
                    _symbol("changed", "new", 3, 4),
                    _symbol("new", "brand-new", 5, 6),
                ]

            def extract_calls(self, file_path):
                return [{"caller_uid": "changed", "callee_name": "helper"}]

            def extract_imports(self, file_path):
                return []

            def extract_inheritance(self, file_path):
                return []

        rebuilt = []

        class FakeAffectsIndexer:
            def __init__(self, db):
                self.db = db

            def rebuild_affects(self, uids, workspace_id):
                rebuilt.extend(uids)

        monkeypatch.setattr("sidecar.indexer.affects.AFFECTSIndexer", FakeAffectsIndexer)
        source_file = tmp_path / "test.py"
        source_file.write_text(
            "def unchanged():\n    pass\ndef changed():\n    return 1\ndef new():\n    return 2\n",
            encoding="utf-8",
        )
        db = FakeDb()
        lance = FakeLance()

        index_file(str(source_file), db, lance, FakeExtractor(), workspace_id="acme/repo@main")

        assert db.upserted == ["changed", "new"]
        assert db.pruned_keep == ["unchanged", "changed", "new"]
        assert db.cleared_edges == ["changed", "new"]
        assert db.linked_calls == [{"caller_uid": "changed", "callee_name": "helper"}]
        assert db.deleted_imports == [str(source_file)]
        assert lance.upserted == ["changed", "new"]
        assert lance.deleted == ["removed"]
        assert rebuilt == ["changed", "new"]

    def test_index_file_skips_embeddings_when_symbols_unchanged(self, tmp_path, monkeypatch):
        """Unchanged symbols skip row/vector writes but refresh file-scoped edges."""

        class FakeDb:
            def __init__(self):
                self.upserted = None
                self.cleared_edges = None

            def get_symbol_index_for_file(self, file_path, workspace_id):
                return {"unchanged": {"hash": "same", "start_line": 1, "end_line": 2}}

            def upsert_file_structure(self, file_path, file_hash, symbols, workspace_id):
                self.upserted = [s.uid for s in symbols]

            def prune_symbols_for_file(self, file_path, keep_uids, workspace_id):
                self.keep_uids = keep_uids

            def clear_outgoing_symbol_edges(self, symbol_uids, workspace_id):
                self.cleared_edges = symbol_uids

            def link_calls(self, calls, workspace_id):
                self.linked_calls = calls

            def delete_imports_for_file(self, file_path, workspace_id):
                self.deleted_imports = True

        class FakeLance:
            def upsert_symbol_embeddings(self, symbols):
                self.upserted = symbols

            def delete_symbol_embeddings(self, uids):
                self.deleted = uids

        class FakeExtractor:
            def extract(self, file_path):
                return [_symbol("unchanged", "same", 1, 2)]

            def extract_calls(self, file_path):
                return [{"caller_uid": "unchanged", "callee_name": "helper"}]

            def extract_imports(self, file_path):
                return []

            def extract_inheritance(self, file_path):
                return []

        monkeypatch.setattr(
            "sidecar.indexer.affects.AFFECTSIndexer",
            lambda db: (_ for _ in ()).throw(AssertionError("AFFECTS should not rebuild")),
        )
        source_file = tmp_path / "test.py"
        source_file.write_text("def unchanged():\n    pass\n", encoding="utf-8")
        db = FakeDb()
        lance = FakeLance()

        index_file(str(source_file), db, lance, FakeExtractor(), workspace_id="acme/repo@main")

        assert db.upserted == []
        assert db.keep_uids == ["unchanged"]
        assert db.cleared_edges == ["unchanged"]
        assert db.linked_calls == [{"caller_uid": "unchanged", "callee_name": "helper"}]
        assert db.deleted_imports is True
        assert lance.upserted == []
        assert lance.deleted == []
