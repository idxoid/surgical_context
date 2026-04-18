"""Unit tests for ContextArbitrator orchestration and BFS traversal."""

import pytest
from unittest.mock import Mock, MagicMock, patch
from pathlib import Path
from sidecar.context.arbitrator import ContextArbitrator
from sidecar.context.graph_expander import GraphExpander
from sidecar.context.types import SymbolContext, PromptContext, BudgetTooSmall


class TestContextArbitratorBFS:
    """Test the BFS graph traversal via ContextArbitrator orchestrator."""

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
            "token_estimate": 120,
        }

        mock_session = MagicMock()
        mock_db.driver.session.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_db.driver.session.return_value.__exit__ = MagicMock(return_value=None)

        mock_session.run.return_value.single.return_value = {
            "s": target_node,
            "file_path": "/payments/processor.py",
        }

        with patch("builtins.open", create=True):
            ctx = arbitrator.get_context_for_symbol("process_payment", token_budget=4000)

        assert isinstance(ctx, PromptContext)
        assert ctx.primary_source.symbol == "process_payment"
        assert ctx.budget["limit"] == 4000

    def test_expand_for_symbol_not_found(self, arbitrator, mock_db):
        """Test that a non-existent symbol returns an error string."""
        mock_session = MagicMock()
        mock_db.driver.session.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_db.driver.session.return_value.__exit__ = MagicMock(return_value=None)

        mock_session.run.return_value.single.return_value = None

        result = arbitrator.get_context_for_symbol("nonexistent_symbol")

        assert isinstance(result, str)
        assert "Error:" in result
        assert "not found" in result

    def test_budget_too_small(self, arbitrator, mock_db):
        """Test that oversized target raises BudgetTooSmall error."""
        target_node = {
            "uid": "huge",
            "name": "huge_function",
            "range": [1, 500],
            "token_estimate": 4500,
        }

        mock_session = MagicMock()
        mock_db.driver.session.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_db.driver.session.return_value.__exit__ = MagicMock(return_value=None)

        mock_session.run.return_value.single.return_value = {
            "s": target_node,
            "file_path": "/test.py",
        }

        result = arbitrator.get_context_for_symbol("huge_function", token_budget=4000)

        assert isinstance(result, str)
        assert "Error:" in result
        assert "too small" in result.lower()

    def test_subgraph_has_budget_info(self, arbitrator, mock_db):
        """Test that returned context includes budget tracking."""
        target_node = {
            "uid": "t1",
            "name": "target",
            "range": [1, 5],
            "token_estimate": 40,
        }

        mock_session = MagicMock()
        mock_db.driver.session.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_db.driver.session.return_value.__exit__ = MagicMock(return_value=None)

        mock_session.run.return_value.single.return_value = {
            "s": target_node,
            "file_path": "/app/target.py",
        }

        with patch("builtins.open", create=True):
            ctx = arbitrator.get_context_for_symbol("target", token_budget=1000)

        assert isinstance(ctx, PromptContext)
        assert "limit" in ctx.budget
        assert ctx.budget["limit"] == 1000
        assert "spent" in ctx.budget
        assert ctx.budget["spent"] > 0

    def test_subgraph_node_has_depth_direction_score(self, arbitrator, mock_db):
        """Test that SymbolContext includes depth, direction, and relevance_score."""
        target_node = {
            "uid": "t1",
            "name": "target",
            "range": [1, 5],
            "token_estimate": 40,
        }

        mock_session = MagicMock()
        mock_db.driver.session.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_db.driver.session.return_value.__exit__ = MagicMock(return_value=None)

        mock_session.run.return_value.single.return_value = {
            "s": target_node,
            "file_path": "/app/target.py",
        }

        with patch("builtins.open", create=True):
            ctx = arbitrator.get_context_for_symbol("target")

        primary = ctx.primary_source
        assert hasattr(primary, "depth")
        assert hasattr(primary, "direction")
        assert hasattr(primary, "relevance_score")
        assert primary.depth == 0
        assert primary.direction == "primary"
        assert primary.relevance_score == 1.0

    def test_scoring_function_prefers_callers(self):
        """Test that incoming CALLS (callers) score higher than outgoing."""
        expander = GraphExpander(Mock())

        # Caller (incoming call)
        score_caller = expander._score(
            rel_type="CALLS",
            outgoing=False,
            caller_count=5,
            token_estimate=100,
            distance=1,
        )

        # Callee (outgoing call)
        score_callee = expander._score(
            rel_type="CALLS",
            outgoing=True,
            caller_count=5,
            token_estimate=100,
            distance=1,
        )

        assert score_caller > score_callee

    def test_direction_mapping(self):
        """Test that relation types map to correct direction strings."""
        expander = GraphExpander(Mock())

        assert expander._direction("CALLS", outgoing=True) == "callee"
        assert expander._direction("CALLS", outgoing=False) == "caller"
        assert expander._direction("DEPENDS_ON", outgoing=True) == "type"
        assert expander._direction("IMPORTS", outgoing=True) == "import"

    def test_estimate_tokens(self):
        """Test cold-path token estimation."""
        expander = GraphExpander(Mock())

        node = {"range": [1, 10]}
        estimate = expander._estimate_tokens(node)
        # (10 - 1 + 1) * 8 = 80
        assert estimate == 80

        node = {"range": [1, 1]}
        estimate = expander._estimate_tokens(node)
        assert estimate == 8

        node = {}
        estimate = expander._estimate_tokens(node)
        assert estimate == 0
