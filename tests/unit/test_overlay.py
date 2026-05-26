"""Unit tests for InMemoryOverlay — dirty state handling."""

import pytest

from sidecar.context.overlay import InMemoryOverlay


class TestInMemoryOverlay:
    """Test the in-memory overlay for unsaved code changes."""

    @pytest.fixture
    def overlay(self):
        """Create a fresh overlay instance."""
        return InMemoryOverlay()

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

        assert overlay.read_lines("test.py", 1, 1, workspace_id=ws, user_id="alice") == "alice draft\n"
        assert overlay.read_lines("test.py", 1, 1, workspace_id=ws, user_id="bob") == "bob draft\n"

        overlay.clear("test.py", workspace_id=ws, user_id="alice")
        assert not overlay.has("test.py", workspace_id=ws, user_id="alice")
        assert overlay.has("test.py", workspace_id=ws, user_id="bob")
