"""Unit tests for InMemoryOverlay — dirty state handling."""

import time

import pytest

from context_engine.observability.metrics import MetricsRegistry
from context_engine.overlay import InMemoryOverlay


class TestInMemoryOverlay:
    """Test the in-memory overlay for unsaved code changes."""

    @pytest.fixture
    def overlay(self):
        """Create a fresh overlay instance."""
        return InMemoryOverlay(max_entries=0, ttl_seconds=0, metrics=MetricsRegistry())

    def test_update_and_has(self, overlay):
        """Test updating a file and checking existence."""
        content = "def foo():\n    return 42\n"
        overlay.update("test.py", content)
        assert overlay.has("test.py")

    def test_has_returns_false_for_missing(self, overlay):
        """Test that has() returns False for non-existent files."""
        assert not overlay.has("nonexistent.py")

    def test_read_lines_roundtrip(self, overlay):
        """Test reading back exact lines (1-based, inclusive)."""
        content = "line1\nline2\nline3\nline4\nline5\n"
        overlay.update("test.py", content)

        lines_1_3 = overlay.read_lines("test.py", 1, 3)
        assert lines_1_3 == "line1\nline2\nline3\n"

        lines_2_4 = overlay.read_lines("test.py", 2, 4)
        assert lines_2_4 == "line2\nline3\nline4\n"

        single_line = overlay.read_lines("test.py", 2, 2)
        assert single_line == "line2\n"

    def test_read_lines_out_of_range(self, overlay):
        """Test read_lines with out-of-range indices (should be safe)."""
        content = "line1\nline2\nline3\n"
        overlay.update("test.py", content)

        # Reading beyond file length should not crash
        lines = overlay.read_lines("test.py", 1, 100)
        assert "line1" in lines
        assert "line2" in lines
        assert "line3" in lines

    def test_clear_removes_entry(self, overlay):
        """Test that clear() removes a file entry."""
        overlay.update("test.py", "content")
        assert overlay.has("test.py")

        overlay.clear("test.py")
        assert not overlay.has("test.py")

    def test_clear_idempotent(self, overlay):
        """Test that clearing a non-existent file doesn't crash."""
        # Should not raise an error
        overlay.clear("nonexistent.py")

    def test_get_symbols_roundtrip(self, overlay):
        """Test extracting symbols from in-memory content."""
        content = """
def function_one():
    pass

def function_two(x, y):
    return x + y

class MyClass:
    def method(self):
        pass
"""
        overlay.update("test.py", content)
        symbols = overlay.get_symbols("test.py")

        # Check that we extracted functions and class
        assert "function_one" in symbols
        assert "function_two" in symbols
        assert "MyClass" in symbols

    def test_get_symbols_returns_line_ranges(self, overlay):
        """Test that get_symbols returns (start, end) line tuples."""
        content = """def foo():
    return 42
"""
        overlay.update("test.py", content)
        symbols = overlay.get_symbols("test.py")

        assert "foo" in symbols
        start, end = symbols["foo"]
        assert isinstance(start, int)
        assert isinstance(end, int)
        assert start > 0 and end > 0

    def test_multiple_files(self, overlay):
        """Test handling multiple files simultaneously."""
        overlay.update("file1.py", "def foo(): pass\n")
        overlay.update("file2.py", "def bar(): pass\n")

        assert overlay.has("file1.py")
        assert overlay.has("file2.py")

        lines1 = overlay.read_lines("file1.py", 1, 1)
        assert "foo" in lines1

        lines2 = overlay.read_lines("file2.py", 1, 1)
        assert "bar" in lines2

    def test_users_in_same_workspace_do_not_share_overlay_buffers(self, overlay):
        """Unsaved buffers are isolated per (workspace_id, user_id, file_path)."""
        ws = "acme/repo@main"
        overlay.update("test.py", "alice draft\n", workspace_id=ws, user_id="alice")
        overlay.update("test.py", "bob draft\n", workspace_id=ws, user_id="bob")

        assert (
            overlay.read_lines("test.py", 1, 1, workspace_id=ws, user_id="alice") == "alice draft\n"
        )
        assert overlay.read_lines("test.py", 1, 1, workspace_id=ws, user_id="bob") == "bob draft\n"

        overlay.clear("test.py", workspace_id=ws, user_id="alice")
        assert not overlay.has("test.py", workspace_id=ws, user_id="alice")
        assert overlay.has("test.py", workspace_id=ws, user_id="bob")

    def test_dirty_defaults_true(self, overlay):
        overlay.update("test.py", "draft\n")
        assert overlay.is_dirty("test.py")

    def test_saved_overlay_is_not_dirty(self, overlay):
        overlay.update("test.py", "saved\n", dirty=False)
        assert overlay.has("test.py")
        assert not overlay.is_dirty("test.py")

    def test_update_can_flip_dirty_state(self, overlay):
        overlay.update("test.py", "v1\n", dirty=True)
        overlay.update("test.py", "v2\n", dirty=False)
        assert not overlay.is_dirty("test.py")
        assert overlay.read_lines("test.py", 1, 1) == "v2\n"

    def test_cap_evicts_oldest_entry(self):
        metrics = MetricsRegistry()
        overlay = InMemoryOverlay(max_entries=2, ttl_seconds=0, metrics=metrics)
        overlay.update("a.py", "a\n")
        overlay.update("b.py", "b\n")
        overlay.update("c.py", "c\n")

        assert not overlay.has("a.py")
        assert overlay.has("b.py")
        assert overlay.has("c.py")
        assert overlay.stats() == {"entries": 2, "bytes": 4}

        rendered = metrics.render_prometheus()
        assert 'sidecar_overlay_evictions_total{reason="cap"} 1' in rendered
        assert "sidecar_overlay_entries 2" in rendered

    def test_ttl_evicts_stale_entry(self, monkeypatch):
        metrics = MetricsRegistry()
        overlay = InMemoryOverlay(max_entries=0, ttl_seconds=60, metrics=metrics)
        now = 1000.0
        monkeypatch.setattr(time, "monotonic", lambda: now)

        overlay.update("stale.py", "old\n")
        now += 61.0
        assert not overlay.has("stale.py")

        rendered = metrics.render_prometheus()
        assert 'sidecar_overlay_evictions_total{reason="ttl"} 1' in rendered

    def test_clear_increments_eviction_metric(self, overlay):
        overlay.update("test.py", "content\n")
        overlay.clear("test.py")

        rendered = overlay._metrics.render_prometheus()
        assert 'sidecar_overlay_evictions_total{reason="clear"} 1' in rendered
        assert "sidecar_overlay_entries 0" in rendered

    def test_read_refreshes_ttl(self, monkeypatch):
        metrics = MetricsRegistry()
        overlay = InMemoryOverlay(max_entries=0, ttl_seconds=60, metrics=metrics)
        now = 1000.0
        monkeypatch.setattr(time, "monotonic", lambda: now)

        overlay.update("fresh.py", "keep\n")
        now += 50.0
        assert overlay.read_lines("fresh.py", 1, 1) == "keep\n"
        now += 50.0
        assert overlay.has("fresh.py")
