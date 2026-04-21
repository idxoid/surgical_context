"""Unit tests for audit logging."""

import os
import tempfile
import pytest

from sidecar.auth import AuditLog


class TestAuditLog:
    """Test audit logging."""

    @pytest.fixture
    def temp_log_file(self):
        """Create temporary audit log file."""
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".jsonl") as f:
            temp_path = f.name
        yield temp_path
        # Cleanup
        if os.path.exists(temp_path):
            os.unlink(temp_path)

    def test_log_action(self, temp_log_file):
        """Log an action."""
        audit = AuditLog(log_file=temp_log_file)
        audit.log_action("alice", "query", "symbol", {"symbol": "process_payment"})

        assert os.path.exists(temp_log_file)
        with open(temp_log_file) as f:
            lines = f.readlines()
            assert len(lines) == 1

    def test_log_index(self, temp_log_file):
        """Log indexing action."""
        audit = AuditLog(log_file=temp_log_file)
        audit.log_index("alice", "/project", 10, 50)

        entries = audit.get_recent_actions()
        assert len(entries) == 1
        assert entries[0]["action"] == "index"
        assert entries[0]["user_id"] == "alice"

    def test_log_query(self, temp_log_file):
        """Log query action."""
        audit = AuditLog(log_file=temp_log_file)
        audit.log_query("bob", "process_payment", "What does this do?", "exploration", "surgical_full")

        entries = audit.get_recent_actions()
        assert len(entries) == 1
        assert entries[0]["action"] == "query"
        assert entries[0]["details"]["symbol"] == "process_payment"

    def test_log_overlay_sync(self, temp_log_file):
        """Log overlay sync action."""
        audit = AuditLog(log_file=temp_log_file)
        audit.log_overlay_sync("charlie", "src/payment.py", 5)

        entries = audit.get_recent_actions()
        assert len(entries) == 1
        assert entries[0]["action"] == "sync"
        assert entries[0]["details"]["file_path"] == "src/payment.py"

    def test_log_error(self, temp_log_file):
        """Log error action."""
        audit = AuditLog(log_file=temp_log_file)
        audit.log_error("dave", "query", "Symbol not found")

        entries = audit.get_recent_actions()
        assert len(entries) == 1
        assert entries[0]["status"] == "error"
        assert "Symbol not found" in entries[0]["details"]["error"]

    def test_get_recent_actions_limit(self, temp_log_file):
        """Get recent actions with limit."""
        audit = AuditLog(log_file=temp_log_file)
        for i in range(10):
            audit.log_action("alice", "query", "symbol", {"index": i})

        entries = audit.get_recent_actions(limit=5)
        assert len(entries) == 5

    def test_get_recent_actions_by_user(self, temp_log_file):
        """Filter recent actions by user."""
        audit = AuditLog(log_file=temp_log_file)
        audit.log_action("alice", "query", "symbol")
        audit.log_action("bob", "query", "symbol")
        audit.log_action("alice", "sync", "overlay")

        alice_actions = audit.get_recent_actions(user_id="alice")
        assert len(alice_actions) == 2
        assert all(a["user_id"] == "alice" for a in alice_actions)

    def test_get_recent_actions_empty(self, temp_log_file):
        """Get recent actions when log is empty."""
        audit = AuditLog(log_file=temp_log_file)
        entries = audit.get_recent_actions()
        assert len(entries) == 0

    def test_log_file_directory_creation(self):
        """Create log file directory if it doesn't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = os.path.join(tmpdir, "subdir", "logs", "audit.jsonl")
            audit = AuditLog(log_file=log_path)
            audit.log_action("alice", "query", "symbol")

            assert os.path.exists(log_path)
