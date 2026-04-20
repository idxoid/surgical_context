"""Unit tests for incremental indexing with hash-based skip logic."""

from unittest.mock import MagicMock, patch

from sidecar.database.neo4j_client import Neo4jClient


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
            assert call_args[1] == {"path": "/test.py"}

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
