"""Unit tests for ContextArbitrator orchestration and BFS traversal."""

from unittest.mock import Mock, patch

import pytest

from sidecar.context.arbitrator import ContextArbitrator
from sidecar.context.graph_expander import GraphExpander
from sidecar.context.types import DocChunk, PromptContext, Subgraph, SubgraphNode
from sidecar.context.unified_ranker import RankerWeights


def _make_fake_ranker(target_node: SubgraphNode | None, budget_override: int | None = None):
    """Return a FakeRanker class that serves a fixed target node."""

    class FakeRanker:
        PREAMBLE_TOKENS = 100

        def __init__(self, *args, **kwargs):
            pass

        def get_target(self, *args, **kwargs):
            if target_node is None:
                return (None, {"strategy": "not_found"})
            return (target_node, {"strategy": "unique_match", "ambiguous": False})

        def rank(self, target, query, intent, budget, **_kw):
            spent = budget_override if budget_override is not None else target.token_estimate + 100
            return (
                [],
                {"limit": budget, "spent": spent, "reserved": 100, "pool_size": 0},
                "pool_exhausted",
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

        def _get_required_roles(self, mechanism, *, target=None):
            return ["api_surface", "executor"]

    return FakeRanker


class TestContextArbitratorBFS:
    """Test the BFS graph traversal via ContextArbitrator orchestrator."""

    @pytest.fixture
    def mock_db(self):
        """Create a mock Neo4j client."""
        db = Mock()
        return db

    @pytest.fixture
    def arbitrator(self, mock_db):
        """Create arbitrator with mocked db (no vector_db — unified path with zero vector scores)."""
        return ContextArbitrator(mock_db)

    def test_get_context_for_symbol_found(self, arbitrator, mock_db):
        """Test retrieving context for a symbol that exists in the graph."""
        target = SubgraphNode(
            uid="abc123",
            name="process_payment",
            file_path="/payments/processor.py",
            range=[10, 25],
            token_estimate=120,
            relation="target",
            direction="primary",
            depth=0,
            relevance_score=1.0,
        )
        with (
            patch("sidecar.context.arbitrator.UnifiedRanker", _make_fake_ranker(target)),
            patch(
                "sidecar.context.arbitrator.CodeResolver.resolve",
                return_value=("def process_payment(): pass", False),
            ),
        ):
            ctx = arbitrator.get_context_for_symbol("process_payment", token_budget=4000)

        assert isinstance(ctx, PromptContext)
        assert ctx.primary_source.symbol == "process_payment"
        assert ctx.budget["limit"] == 4000

    def test_expand_for_symbol_not_found(self, arbitrator, mock_db):
        """Test that a non-existent symbol returns an error string."""
        with patch("sidecar.context.arbitrator.UnifiedRanker", _make_fake_ranker(None)):
            result = arbitrator.get_context_for_symbol("nonexistent_symbol")

        assert isinstance(result, str)
        assert "Error:" in result
        assert "not found" in result

    def test_budget_too_small(self, arbitrator, mock_db):
        """Budget smaller than signature-only estimate (>500 tokens) returns an error string."""
        # token_estimate=6000 → signature cap = min(500, 600) = 500
        # PREAMBLE_TOKENS=100, so reserved = 600 > budget=400 → error
        target = SubgraphNode(
            uid="huge",
            name="huge_function",
            file_path="/test.py",
            range=[1, 500],
            token_estimate=6000,
            relation="target",
            direction="primary",
            depth=0,
            relevance_score=1.0,
        )
        with patch("sidecar.context.arbitrator.UnifiedRanker", _make_fake_ranker(target)):
            result = arbitrator.get_context_for_symbol("huge_function", token_budget=400)

        assert isinstance(result, str)
        assert "Error:" in result
        assert "too small" in result.lower()

    def test_subgraph_has_budget_info(self, arbitrator, mock_db):
        """Test that returned context includes budget tracking."""
        target = SubgraphNode(
            uid="t1",
            name="target",
            file_path="/app/target.py",
            range=[1, 5],
            token_estimate=40,
            relation="target",
            direction="primary",
            depth=0,
            relevance_score=1.0,
        )
        with (
            patch("sidecar.context.arbitrator.UnifiedRanker", _make_fake_ranker(target)),
            patch(
                "sidecar.context.arbitrator.CodeResolver.resolve",
                return_value=("def target(): pass", False),
            ),
        ):
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

            def rank(self, target, query, intent, budget, **_kw):
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

            def _get_required_roles(self, mechanism, *, target=None):
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
        target = SubgraphNode(
            uid="t1",
            name="target",
            file_path="/app/target.py",
            range=[1, 5],
            token_estimate=40,
            relation="target",
            direction="primary",
            depth=0,
            relevance_score=1.0,
        )
        with (
            patch("sidecar.context.arbitrator.UnifiedRanker", _make_fake_ranker(target)),
            patch(
                "sidecar.context.arbitrator.CodeResolver.resolve",
                return_value=("def target(): pass", False),
            ),
        ):
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

            def rank(self, target, query, intent, budget, **_kw):
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

            def _get_required_roles(self, mechanism, *, target=None):
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

    def test_unified_signature_only_uses_effective_range_for_payload_and_cache(self, mock_db):
        """Massive targets should expose/cache the resolved head range, not the full span."""

        class FakeRanker:
            PREAMBLE_TOKENS = 100

            def __init__(self, *args, **kwargs):
                pass

            def get_target(self, *args, **kwargs):
                return (
                    SubgraphNode(
                        uid="large-class",
                        name="LargeClass",
                        file_path="/repo/large.py",
                        range=[10, 500],
                        token_estimate=5000,
                        relation="target",
                        direction="primary",
                        depth=0,
                        relevance_score=1.0,
                        file_hash="hash-large",
                    ),
                    {"strategy": "unique_match", "ambiguous": False},
                )

            def rank(self, target, query, intent, budget, **_kw):
                return (
                    [],
                    {"limit": budget, "spent": 600, "reserved": 100, "pool_size": 0},
                    "role_complete",
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

            def _get_required_roles(self, mechanism, *, target=None):
                return ["api_surface"]

        arbitrator = ContextArbitrator(mock_db, vector_db=Mock())
        with (
            patch("sidecar.context.arbitrator.UnifiedRanker", FakeRanker),
            patch(
                "sidecar.context.arbitrator.CodeResolver.resolve",
                return_value=('class LargeClass:\n    """head only"""\n', False),
            ) as resolve,
        ):
            ctx = arbitrator.get_context_for_symbol(
                "LargeClass",
                question="How does this class work?",
                token_budget=4000,
            )

        assert isinstance(ctx, PromptContext)
        resolve.assert_called_once_with("/repo/large.py", 10, 25)
        assert ctx.primary_source.range == [10, 25]
        assert ctx.primary_source.render_mode == "signature_only"
        payload = ctx.to_dict()["primary_source"]
        assert payload["range"] == [10, 25]
        assert payload["render_mode"] == "signature_only"
        cached = arbitrator.cache.get_body("/repo/large.py", (10, 25), "hash-large")
        assert cached is not None
        assert arbitrator.cache.get_body("/repo/large.py", (10, 500), "hash-large") is None

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

            def concept_anchor_candidates(self, symbol_name, query=""):
                return ["use"] if symbol_name == "middleware" else []

            def get_target(self, symbol_name, query="", intent=None, with_metadata=False):
                if symbol_name == "middleware":
                    return (None, {"strategy": "not_found"}) if with_metadata else None
                if symbol_name == "use":
                    meta = {"strategy": "unique_match", "selected_uid": "anchor-use"}
                    return (target, meta) if with_metadata else target
                return (None, {"strategy": "not_found"}) if with_metadata else None

            def rank(self, target, query, intent, budget, **_kw):
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

            def _get_required_roles(self, mechanism, *, target=None):
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

    def test_low_quality_concept_target_uses_anchor_fallback(self, mock_db):
        """Noisy duplicate targets can be replaced by a production concept anchor."""
        arbitrator = ContextArbitrator(mock_db, vector_db=Mock())
        noisy_target = SubgraphNode(
            uid="test-app",
            name="app",
            file_path="test/app.js",
            range=[1, 1],
            token_estimate=8,
            relation="target",
            direction="primary",
            depth=0,
            relevance_score=1.0,
        )
        anchor_target = SubgraphNode(
            uid="create-application",
            name="createApplication",
            file_path="lib/express.js",
            range=[20, 50],
            token_estimate=80,
            relation="target",
            direction="primary",
            depth=0,
            relevance_score=1.0,
        )

        class FakeRanker:
            PREAMBLE_TOKENS = 100

            def __init__(self, db, vector_searcher, workspace_id=None, weights=None):
                pass

            def concept_anchor_candidates(self, symbol_name, query=""):
                return ["createApplication"] if symbol_name == "app" else []

            def get_target(self, symbol_name, query="", intent=None, with_metadata=False):
                if symbol_name == "app":
                    meta = {
                        "strategy": "duplicate_resolution",
                        "selected_uid": "test-app",
                        "selected_score": -0.2,
                    }
                    return (noisy_target, meta) if with_metadata else noisy_target
                if symbol_name == "createApplication":
                    meta = {"strategy": "unique_match", "selected_uid": "create-application"}
                    return (anchor_target, meta) if with_metadata else anchor_target
                return (None, {"strategy": "not_found"}) if with_metadata else None

            def rank(self, target, query, intent, budget, **_kw):
                return (
                    [],
                    {"limit": budget, "spent": 180, "reserved": 100, "pool_size": 0},
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

            def _get_required_roles(self, mechanism, *, target=None):
                return []

        with patch("sidecar.context.arbitrator.UnifiedRanker", FakeRanker):
            ctx = arbitrator.get_context_for_symbol(
                "app",
                question="How does an app register middleware?",
                token_budget=2000,
            )

        assert isinstance(ctx, PromptContext)
        assert ctx.primary_source.symbol == "createApplication"
        target_meta = ctx.ranker_state.get("target_selection", {})
        assert target_meta.get("strategy") == "concept_anchor_fallback"
        assert target_meta.get("anchor_symbol") == "createApplication"
        assert target_meta.get("replaced_target_selection", {}).get("selected_uid") == "test-app"
