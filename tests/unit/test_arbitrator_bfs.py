"""Unit tests for ContextArbitrator orchestration and BFS traversal."""

from unittest.mock import MagicMock, Mock, patch

import pytest

from sidecar.context.arbitrator import ContextArbitrator
from sidecar.context.graph_expander import GraphExpander
from sidecar.context.types import DocChunk, PromptContext, Subgraph, SubgraphNode
from sidecar.context.unified_ranker import RankerWeights


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

    def test_vector_docs_are_resolved_before_prompt_compilation(self, mock_db):
        """Doc chunks are fetched inside arbitration, before PromptCompiler runs."""

        class FakeVectorDb:
            def __init__(self):
                self.calls = []

            def search(self, query, limit):
                self.calls.append((query, limit))
                return [
                    {
                        "file_path": "docs/spec_payment.md",
                        "chunk": "Payment processing specification",
                    }
                ]

        subgraph = Subgraph(
            primary=SubgraphNode(
                uid="target",
                name="process_payment",
                file_path="<unknown>",
                range=[1, 1],
                token_estimate=8,
                relation="target",
                direction="primary",
                depth=0,
                relevance_score=1.0,
            ),
            nodes=[],
            budget={"limit": 4000, "spent": 108, "reserved": 100, "pruned": 0},
        )
        vector_db = FakeVectorDb()
        arbitrator = ContextArbitrator(mock_db, vector_db=vector_db)

        class FakeRanker:
            PREAMBLE_TOKENS = 100

            def __init__(self, db, vector_searcher, workspace_id=None, weights=None):
                self.vector_searcher = vector_searcher

            def get_target(self, *args, **kwargs):
                return subgraph.primary, {"strategy": "unique_match", "ambiguous": False}

            def rank(self, target, query, intent, budget):
                docs = self.vector_searcher.search_docs(query, limit=3)
                self._docs = docs
                return (
                    [],
                    {"limit": budget, "spent": 108, "reserved": 100, "pool_size": 0},
                    "pool_exhausted",
                    [],
                    [],
                )

            def candidates_to_subgraph(
                self, target, candidates, budget_info, stopped_reason, pruned_details
            ):
                docs = [
                    DocChunk(
                        source_file=item["file_path"],
                        chunk_id=item["chunk_id"],
                        content=item["content"],
                        score=item["score"],
                        semantic_score=item["score"],
                        blended_score=item["score"],
                        provenance=["vector:docs"],
                    )
                    for item in self._docs
                ]
                return (
                    Subgraph(
                        primary=target,
                        nodes=[],
                        budget=budget_info,
                        stopped_reason=stopped_reason,
                        pruned_details=pruned_details,
                    ),
                    docs,
                )

            def _determine_mechanism(self, target, query=""):
                return "generic"

            def _get_required_roles(self, mechanism):
                return ["api_surface", "executor", "runtime_surface", "docs_or_concept"]

        with patch("sidecar.context.arbitrator.UnifiedRanker", FakeRanker):
            ctx = arbitrator.get_context_for_symbol(
                "process_payment",
                question="How should this payment flow work?",
                token_budget=4000,
            )

        assert isinstance(ctx, PromptContext)
        assert vector_db.calls == [("process_payment How should this payment flow work?", 3)]
        assert len(ctx.documentation) == 1
        assert ctx.documentation[0].source_file == "docs/spec_payment.md"
        assert ctx.tier_tokens["specs"] > 0

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

    def test_unified_path_surfaces_ranker_metadata(self, mock_db):
        """Unified path should expose weights and target-selection metadata in the contract."""

        class FakeRanker:
            PREAMBLE_TOKENS = 100

            def __init__(self, *args, **kwargs):
                pass

            def get_target(self, *args, **kwargs):
                return (
                    SubgraphNode(
                        uid="depends-fn",
                        name="Depends",
                        file_path="/repo/fastapi/param_functions.py",
                        range=[2283, 2340],
                        token_estimate=120,
                        relation="target",
                        direction="primary",
                        depth=0,
                        relevance_score=1.0,
                        provenance=["primary:target"],
                    ),
                    {
                        "strategy": "duplicate_resolution",
                        "ambiguous": True,
                        "selected_uid": "depends-fn",
                    },
                )

            def rank(self, target, query, intent, budget):
                return (
                    [],
                    {"limit": budget, "spent": 220, "reserved": 100, "pool_size": 7},
                    "pool_exhausted",
                    [
                        {
                            "kind": "symbol",
                            "uid": "audit-log",
                            "name": "Audit.log",
                            "reason": "over_budget",
                            "blended_score": 0.51,
                            "token_cost": 620,
                        }
                    ],
                    ["dependency_solver"],
                )

            def candidates_to_subgraph(
                self, target, candidates, budget_info, stopped_reason, pruned_details
            ):
                return (
                    Subgraph(
                        primary=target,
                        nodes=[],
                        budget=budget_info,
                        stopped_reason=stopped_reason,
                        pruned_details=pruned_details,
                    ),
                    [
                        DocChunk(
                            source_file="docs/reference/dependencies.md",
                            chunk_id="doc-1",
                            content="Depends wires request-time dependency resolution.",
                            score=0.8,
                            semantic_score=0.8,
                            blended_score=0.9,
                            provenance=["vector:docs"],
                        )
                    ],
                )

            def _determine_mechanism(self, target, query=""):
                return "fastapi_dependency_injection"

            def _get_required_roles(self, mechanism):
                return [
                    "api_surface",
                    "config_surface",
                    "representation_surface",
                    "orchestrator",
                    "runtime_surface",
                    "docs_or_concept",
                ]

        arbitrator = ContextArbitrator(
            mock_db,
            vector_db=Mock(),
            ranker_weights=RankerWeights(alpha=0.9, beta=0.7, gamma=0.4, delta=0.6, epsilon=0.2),
        )

        with (
            patch("sidecar.context.arbitrator.UnifiedRanker", FakeRanker),
            patch(
                "sidecar.context.arbitrator.CodeResolver.resolve",
                return_value=("def Depends(...):\n    pass", False),
            ),
        ):
            ctx = arbitrator.get_context_for_symbol(
                "Depends",
                question="How does dependency injection get resolved before the endpoint function is called?",
                token_budget=4000,
            )

        assert isinstance(ctx, PromptContext)
        payload = ctx.to_dict()
        assert payload["metadata"]["ranker"]["weights"]["alpha"] == 0.9
        assert (
            payload["metadata"]["ranker"]["target_selection"]["strategy"] == "duplicate_resolution"
        )
        assert payload["metadata"]["ranker"]["pruned_total_count"] == 1
        assert payload["intent_details"]["primary"] == "exploration"
        assert payload["pruned"][0]["name"] == "Audit.log"

    def test_explain_behavior_missing_symbol_uses_concept_anchor_fallback(self, mock_db):
        """Missing conceptual symbols can resolve to anchor symbols for explain-style prompts."""
        arbitrator = ContextArbitrator(mock_db, vector_db=Mock())
        target = SubgraphNode(
            uid="anchor-use",
            name="use",
            file_path="lib/application.js",
            range=[1, 10],
            token_estimate=40,
            relation="target",
            direction="primary",
            depth=0,
            relevance_score=1.0,
        )

        class FakeRanker:
            PREAMBLE_TOKENS = 100

            def __init__(self, db, vector_searcher, workspace_id=None, weights=None):
                pass

            def get_target(self, symbol_name, query="", intent=None, with_metadata=False):
                if symbol_name == "middleware":
                    return (None, {"strategy": "not_found"}) if with_metadata else None
                if symbol_name == "use":
                    meta = {"strategy": "unique_match", "selected_uid": "anchor-use"}
                    return (target, meta) if with_metadata else target
                return (None, {"strategy": "not_found"}) if with_metadata else None

            def rank(self, target, query, intent, budget):
                return (
                    [],
                    {"limit": budget, "spent": 140, "reserved": 100, "pool_size": 0},
                    "",
                    [],
                    [],
                )

            def candidates_to_subgraph(
                self, target, candidates, budget_info, stopped_reason, pruned_details
            ):
                return (
                    Subgraph(
                        primary=target,
                        nodes=[],
                        budget=budget_info,
                        stopped_reason=stopped_reason,
                        pruned_details=pruned_details,
                    ),
                    [],
                )

            def _determine_mechanism(self, target, query=""):
                return "generic"

            def _get_required_roles(self, mechanism):
                return []

        with patch("sidecar.context.arbitrator.UnifiedRanker", FakeRanker):
            ctx = arbitrator.get_context_for_symbol(
                "middleware",
                question="How does middleware sequencing work?",
                token_budget=2000,
            )

        assert isinstance(ctx, PromptContext)
        assert ctx.primary_source.symbol == "use"
        target_meta = ctx.ranker_state.get("target_selection", {})
        assert target_meta.get("strategy") == "concept_anchor_fallback"
        assert target_meta.get("missing_symbol") == "middleware"
        assert target_meta.get("anchor_symbol") == "use"
