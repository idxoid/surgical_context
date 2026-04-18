"""Unit tests for ContextArbitrator BFS traversal logic."""

import pytest
from unittest.mock import Mock, MagicMock, patch
from pathlib import Path
from sidecar.context.arbitrator import ContextArbitrator, SymbolContext, PromptContext


class TestContextArbitratorBFS:
    """Test the BFS graph traversal in ContextArbitrator."""

    @pytest.fixture
    def mock_db(self):
        """Create a mock Neo4j client."""
        db = Mock()
        return db

    @pytest.fixture
    def arbitrator(self, mock_db):
        """Create arbitrator with mocked db."""
        return ContextArbitrator(mock_db)

    def test_get_context_for_symbol_found(self, arbitrator, mock_db):
        """Test retrieving context for a symbol that exists in the graph."""
        target_node = {
            "uid": "abc123",
            "name": "process_payment",
            "range": [10, 25],
        }
        call_dep = {
            "uid": "def456",
            "name": "validate_amount",
            "range": [5, 8],
        }
        type_dep = {
            "uid": "ghi789",
            "name": "PaymentError",
            "range": [2, 4],
        }

        mock_session = MagicMock()
        mock_db.driver.session.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_db.driver.session.return_value.__exit__ = MagicMock(return_value=None)

        mock_session.run.return_value.single.side_effect = [
            {
                "target": target_node,
                "calls": [call_dep],
                "depends_on": [type_dep],
                "imports": [],
            },
            {"path": "/payments/processor.py"},
            {"path": "/payments/processor.py"},
            {"path": "/payments/validators.py"},
        ]

        with patch("builtins.open", create=True) as mock_open:
            mock_open.return_value.__enter__.return_value.readlines.return_value = [
                "def process_payment(...):\n",
                "    validate_amount(amount)\n",
            ]

            ctx = arbitrator.get_context_for_symbol("process_payment")

        assert isinstance(ctx, PromptContext)
        assert ctx.primary_source.symbol == "process_payment"
        assert len(ctx.graph_context) >= 2

    def test_get_context_for_symbol_not_found(self, arbitrator, mock_db):
        """Test that a non-existent symbol returns an error string."""
        mock_session = MagicMock()
        mock_db.driver.session.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_db.driver.session.return_value.__exit__ = MagicMock(return_value=None)

        mock_session.run.return_value.single.return_value = None

        result = arbitrator.get_context_for_symbol("nonexistent_symbol")

        assert isinstance(result, str)
        assert "Error:" in result
        assert "not found" in result

    def test_get_context_splits_edge_types(self, arbitrator, mock_db):
        """Test that context correctly splits CALLS, DEPENDS_ON, and IMPORTS edges."""
        target = {"uid": "t1", "name": "main", "range": [1, 10]}
        call1 = {"uid": "c1", "name": "helper", "range": [1, 5]}
        type1 = {"uid": "d1", "name": "BaseClass", "range": [1, 8]}

        mock_session = MagicMock()
        mock_db.driver.session.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_db.driver.session.return_value.__exit__ = MagicMock(return_value=None)

        mock_session.run.return_value.single.side_effect = [
            {
                "target": target,
                "calls": [call1],
                "depends_on": [type1],
                "imports": [],
            },
            {"path": "/app/main.py"},
            {"path": "/app/main.py"},
            {"path": "/app/base.py"},
        ]

        with patch("builtins.open", create=True):
            ctx = arbitrator.get_context_for_symbol("main")

        assert isinstance(ctx, PromptContext)
        calls = [d for d in ctx.graph_context if d.relation == "CALLS"]
        depends_on = [d for d in ctx.graph_context if d.relation == "DEPENDS_ON"]

        assert any(d.symbol == "helper" for d in calls)
        assert any(d.symbol == "BaseClass" for d in depends_on)

    def test_get_context_marks_dirty_state(self, arbitrator, mock_db):
        """Test that is_dirty flag is set when overlay has the file."""
        target = {"uid": "t1", "name": "foo", "range": [1, 5]}

        mock_session = MagicMock()
        mock_db.driver.session.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_db.driver.session.return_value.__exit__ = MagicMock(return_value=None)

        mock_session.run.return_value.single.side_effect = [
            {"target": target, "calls": [], "depends_on": [], "imports": []},
            {"path": "/app/foo.py"},
        ]

        from sidecar.context.overlay import InMemoryOverlay
        overlay = InMemoryOverlay()
        overlay.update("/app/foo.py", "def foo(): pass\n")
        arbitrator_with_overlay = ContextArbitrator(mock_db, overlay=overlay)

        with patch("builtins.open", create=True):
            ctx = arbitrator_with_overlay.get_context_for_symbol("foo")

        assert ctx.primary_source.is_dirty is True

    def test_build_symbol_context_reads_correct_lines(self, arbitrator, mock_db):
        """Test that _build_symbol_context reads the correct line range."""
        symbol_node = {"uid": "s1", "name": "my_function", "range": [2, 4]}

        mock_session = MagicMock()
        mock_db.driver.session.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_db.driver.session.return_value.__exit__ = MagicMock(return_value=None)

        mock_session.run.return_value.single.return_value = {"path": "/test.py"}

        source_lines = ["# line 1\n", "def my_function():\n", "    return 42\n", "# line 4\n", "# line 5\n"]

        with patch("builtins.open", create=True) as mock_open:
            mock_open.return_value.__enter__.return_value.readlines.return_value = source_lines

            ctx = arbitrator._build_symbol_context(symbol_node, "test_relation")

        assert "def my_function" in ctx.code
        assert "return 42" in ctx.code
