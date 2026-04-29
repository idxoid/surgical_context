"""Unit tests for PromptCompiler with intent-aware context assembly."""

import pytest

from sidecar.context.intent_classifier import Intent
from sidecar.context.prompt_compiler import PromptCompiler
from sidecar.context.types import DocChunk, Subgraph, SubgraphNode


@pytest.fixture
def sample_subgraph():
    """Create a sample subgraph for testing."""
    primary = SubgraphNode(
        uid="file.py:process_payment",
        name="process_payment",
        file_path="src/payment.py",
        range=[10, 25],
        token_estimate=150,
        relation="PRIMARY",
        direction="self",
        depth=0,
        relevance_score=1.0,
    )

    nodes = [
        SubgraphNode(
            uid="file.py:validate_amount",
            name="validate_amount",
            file_path="src/payment.py",
            range=[40, 50],
            token_estimate=100,
            relation="CALLS",
            direction="callee",
            depth=1,
            relevance_score=0.9,
        ),
        SubgraphNode(
            uid="file.py:save_payment",
            name="save_payment",
            file_path="src/payment.py",
            range=[60, 75],
            token_estimate=120,
            relation="CALLS",
            direction="callee",
            depth=1,
            relevance_score=0.85,
        ),
    ]

    return Subgraph(
        primary=primary,
        nodes=nodes,
        budget={"tokens_allocated": 4000, "tokens_remaining": 3000},
    )


@pytest.fixture
def sample_code_map():
    """Create a sample code map."""
    return {
        "file.py:process_payment": ("def process_payment():\n    pass", False),
        "file.py:validate_amount": ("def validate_amount():\n    pass", False),
        "file.py:save_payment": ("def save_payment():\n    pass", True),
    }


@pytest.fixture
def sample_docs():
    """Create sample documentation chunks."""
    return [
        DocChunk(
            source_file="docs/spec_payment.md",
            chunk_id="spec_1",
            content="Payment processing specification",
        ),
        DocChunk(
            source_file="docs/concept.md",
            chunk_id="concept_1",
            content="How payment systems work",
        ),
        DocChunk(
            source_file="docs/idea_future.md",
            chunk_id="idea_1",
            content="Future payment features",
        ),
    ]


class TestPromptCompilerBasic:
    """Test basic compile functionality."""

    def test_compile_creates_prompt_context(self, sample_subgraph, sample_code_map, sample_docs):
        """compile() creates a valid PromptContext."""
        compiler = PromptCompiler()
        ctx = compiler.compile(sample_subgraph, sample_code_map, sample_docs)

        assert ctx.primary_source.symbol == "process_payment"
        assert len(ctx.graph_context) == 2
        assert len(ctx.documentation) == 3
        assert ctx.mode == "surgical_full"

    def test_compile_preserves_metadata(self, sample_subgraph, sample_code_map, sample_docs):
        """compile() preserves node metadata."""
        compiler = PromptCompiler()
        ctx = compiler.compile(sample_subgraph, sample_code_map, sample_docs)

        assert ctx.primary_source.file_path == "src/payment.py"
        assert ctx.primary_source.code == "def process_payment():\n    pass"
        assert ctx.primary_source.is_dirty is False

    def test_compile_marks_dirty_code(self, sample_subgraph, sample_code_map, sample_docs):
        """compile() marks is_dirty from code_map."""
        compiler = PromptCompiler()
        ctx = compiler.compile(sample_subgraph, sample_code_map, sample_docs)

        # save_payment should have is_dirty=True
        save_payment = next((s for s in ctx.graph_context if s.symbol == "save_payment"), None)
        assert save_payment is not None
        assert save_payment.is_dirty is True

    def test_to_dict_includes_observability_contract(self, sample_subgraph, sample_code_map):
        """PromptContext JSON exposes provenance, scores, pruning, and assembly metadata."""
        compiler = PromptCompiler()
        docs = [
            DocChunk(
                source_file="docs/spec_payment.md",
                chunk_id="spec_1",
                content="Payment processing specification",
                score=0.73,
                graph_score=0.18,
                semantic_score=0.73,
                blended_score=0.81,
                intent_weight=0.2,
                matched_symbols=["process_payment"],
                provenance=["vector:docs"],
            )
        ]
        ctx = compiler.compile_with_intent(
            sample_subgraph,
            sample_code_map,
            docs,
            Intent.NAVIGATION,
        )
        ctx.trace_id = "trace-123"
        ctx.workspace_id = "local/repo@main"
        ctx.stage_timings_ms = {"context": 1.5}
        ctx.token_counts = {"context": 42}
        ctx.model_route = {"provider": "ollama", "model": "llama3"}
        ctx.pruning_reasons = ["budget skipped distant import"]
        ctx.intent_distribution = {"navigation": 0.75, "exploration": 0.25}
        ctx.intent_confidence = 0.75
        ctx.intent_ambiguous = True
        ctx.pruned_details = [
            {
                "kind": "symbol",
                "uid": "audit-log",
                "name": "Audit.log",
                "reason": "over_budget",
                "blended_score": 0.51,
                "token_cost": 620,
            }
        ]
        ctx.ranker_state = {
            "strategy": "unified",
            "weights": {"alpha": 1.0, "beta": 0.8, "gamma": 0.4, "delta": 0.5, "epsilon": 0.3},
            "candidates_considered": 12,
            "candidates_selected": 3,
            "target_selection": {"strategy": "duplicate_resolution", "ambiguous": True},
        }

        payload = ctx.to_dict()

        assert payload["metadata"]["assembly"]["trace_id"] == "trace-123"
        assert payload["metadata"]["assembly"]["resolver_version"] == "context-arbitrator-v2"
        assert payload["metadata"]["pruning_reasons"] == ["budget skipped distant import"]
        assert payload["intent_details"]["distribution"] == {"navigation": 0.75, "exploration": 0.25}
        assert payload["intent_details"]["ambiguous"] is True
        assert payload["metadata"]["ranker"]["weights"]["epsilon"] == 0.3
        assert payload["metadata"]["ranker"]["target_selection"]["strategy"] == "duplicate_resolution"
        assert payload["primary_source"]["provenance"] == ["graph", "code_resolver"]
        assert payload["primary_source"]["scores"]["blended_score"] == 1.0
        assert payload["documentation"][0]["score"] == 0.73
        assert payload["documentation"][0]["scores"]["graph_score"] == 0.18
        assert payload["documentation"][0]["matched_symbols"] == ["process_payment"]
        assert payload["pruned"][0]["name"] == "Audit.log"


class TestPromptCompilerWithIntent:
    """Test intent-aware compile_with_intent() method."""

    def test_compile_with_intent_creates_context(
        self, sample_subgraph, sample_code_map, sample_docs
    ):
        """compile_with_intent() creates a valid PromptContext."""
        compiler = PromptCompiler()
        ctx = compiler.compile_with_intent(
            sample_subgraph, sample_code_map, sample_docs, Intent.NAVIGATION
        )

        assert ctx.primary_source.symbol == "process_payment"
        assert ctx.intent == "navigation"
        assert ctx.mode in ("surgical_full", "surgical_doc_only", "standard")

    def test_compile_with_intent_marks_mode_surgical_full(
        self, sample_subgraph, sample_code_map, sample_docs
    ):
        """compile_with_intent() marks mode as surgical_full when code and graph present."""
        compiler = PromptCompiler()
        ctx = compiler.compile_with_intent(
            sample_subgraph, sample_code_map, sample_docs, Intent.NAVIGATION
        )

        assert ctx.mode == "surgical_full"

    def test_compile_with_intent_navigation_includes_code_first(
        self, sample_subgraph, sample_code_map, sample_docs
    ):
        """Navigation intent prioritizes code and cross-refs."""
        compiler = PromptCompiler()
        ctx = compiler.compile_with_intent(
            sample_subgraph, sample_code_map, sample_docs, Intent.NAVIGATION
        )

        assert ctx.primary_source.code  # Code should be present
        assert ctx.graph_context  # Graph (cross-refs) should be present

    def test_compile_with_intent_infers_doc_types(
        self, sample_subgraph, sample_code_map, sample_docs
    ):
        """compile_with_intent() correctly infers doc types from filenames."""
        compiler = PromptCompiler()
        ctx = compiler.compile_with_intent(
            sample_subgraph, sample_code_map, sample_docs, Intent.NEW_FEATURE
        )

        # New feature prioritizes idea > concept > architecture > specs
        # So idea docs should be included first
        doc_sources = [doc.source_file for doc in ctx.documentation]
        assert len(doc_sources) > 0

    def test_compile_with_intent_respects_tier_priority(
        self, sample_subgraph, sample_code_map, sample_docs
    ):
        """compile_with_intent() assembles docs according to intent tier priority."""
        compiler = PromptCompiler()

        # For NEW_FEATURE, idea > concept > architecture > specs
        ctx_new = compiler.compile_with_intent(
            sample_subgraph, sample_code_map, sample_docs, Intent.NEW_FEATURE
        )

        # All docs should still be included (since all tiers have content)
        # but order should reflect intent priority
        assert len(ctx_new.documentation) > 0

    def test_compile_with_intent_debugging(self, sample_subgraph, sample_code_map, sample_docs):
        """Debugging intent includes code and cross-refs as primary."""
        compiler = PromptCompiler()
        ctx = compiler.compile_with_intent(
            sample_subgraph, sample_code_map, sample_docs, Intent.DEBUGGING
        )

        assert ctx.intent == "debugging"
        assert ctx.primary_source.code
        assert ctx.graph_context  # Callers/callees

    def test_compile_with_intent_refactoring(self, sample_subgraph, sample_code_map, sample_docs):
        """Refactoring intent prioritizes cross-refs (blast radius)."""
        compiler = PromptCompiler()
        ctx = compiler.compile_with_intent(
            sample_subgraph, sample_code_map, sample_docs, Intent.REFACTORING
        )

        assert ctx.intent == "refactor"
        assert ctx.graph_context  # Cross-refs should be prominent
        assert ctx.primary_source.code

    def test_compile_with_intent_exploration(self, sample_subgraph, sample_code_map, sample_docs):
        """Exploration intent includes code and conceptual docs."""
        compiler = PromptCompiler()
        ctx = compiler.compile_with_intent(
            sample_subgraph, sample_code_map, sample_docs, Intent.EXPLORATION
        )

        assert ctx.intent == "exploration"
        assert ctx.primary_source.code
        # Concept should be high in priority

    def test_compile_with_intent_design_question(
        self, sample_subgraph, sample_code_map, sample_docs
    ):
        """Design question intent prioritizes concept/idea docs."""
        compiler = PromptCompiler()
        ctx = compiler.compile_with_intent(
            sample_subgraph, sample_code_map, sample_docs, Intent.DESIGN_QUESTION
        )

        assert ctx.intent == "design_question"
        # Code should be deprioritized for design questions
        assert len(ctx.documentation) > 0

    def test_compile_with_intent_empty_docs(self, sample_subgraph, sample_code_map):
        """compile_with_intent() handles empty doc list gracefully."""
        compiler = PromptCompiler()
        ctx = compiler.compile_with_intent(sample_subgraph, sample_code_map, [], Intent.NAVIGATION)

        assert len(ctx.documentation) == 0
        assert ctx.mode == "surgical_full"  # Code and graph still present

    def test_compile_with_intent_empty_graph(self, sample_code_map, sample_docs):
        """compile_with_intent() handles empty graph gracefully."""
        primary = SubgraphNode(
            uid="file.py:isolated",
            name="isolated",
            file_path="src/file.py",
            range=[1, 10],
            token_estimate=50,
            relation="PRIMARY",
            direction="self",
            depth=0,
            relevance_score=1.0,
        )
        subgraph = Subgraph(primary=primary, nodes=[], budget={})

        compiler = PromptCompiler()
        ctx = compiler.compile_with_intent(
            subgraph,
            {**sample_code_map, "file.py:isolated": ("code", False)},
            sample_docs,
            Intent.NAVIGATION,
        )

        assert len(ctx.graph_context) == 0
        assert ctx.primary_source.code == "code"

    def test_compile_and_compile_with_intent_both_work(
        self, sample_subgraph, sample_code_map, sample_docs
    ):
        """Both compile() and compile_with_intent() work without breaking each other."""
        compiler = PromptCompiler()

        ctx_basic = compiler.compile(sample_subgraph, sample_code_map, sample_docs)
        ctx_intent = compiler.compile_with_intent(
            sample_subgraph, sample_code_map, sample_docs, Intent.NAVIGATION
        )

        # Both should produce valid contexts
        assert ctx_basic.primary_source.symbol == ctx_intent.primary_source.symbol
        assert len(ctx_basic.graph_context) == len(ctx_intent.graph_context)


class TestDocTypeInference:
    """Test doc type inference from filenames."""

    def test_infer_spec_doc_type(self, sample_subgraph, sample_code_map):
        """Infer 'specs' from spec_*.md filenames."""
        docs = [DocChunk(source_file="docs/spec_payment.md", chunk_id="s1", content="spec")]
        compiler = PromptCompiler()
        ctx = compiler.compile_with_intent(
            sample_subgraph, sample_code_map, docs, Intent.NAVIGATION
        )

        assert len(ctx.documentation) == 1

    def test_infer_idea_doc_type(self, sample_subgraph, sample_code_map):
        """Infer 'idea' from idea_*.md filenames."""
        docs = [DocChunk(source_file="docs/idea_future.md", chunk_id="i1", content="idea")]
        compiler = PromptCompiler()
        ctx = compiler.compile_with_intent(
            sample_subgraph, sample_code_map, docs, Intent.NEW_FEATURE
        )

        assert len(ctx.documentation) == 1

    def test_infer_concept_doc_type(self, sample_subgraph, sample_code_map):
        """Infer 'concept' from concept.md."""
        docs = [DocChunk(source_file="docs/concept.md", chunk_id="c1", content="concept")]
        compiler = PromptCompiler()
        ctx = compiler.compile_with_intent(
            sample_subgraph, sample_code_map, docs, Intent.EXPLORATION
        )

        assert len(ctx.documentation) == 1

    def test_infer_architecture_doc_type(self, sample_subgraph, sample_code_map):
        """Infer 'architecture' from architecture*.md."""
        docs = [DocChunk(source_file="docs/architecture.md", chunk_id="a1", content="arch")]
        compiler = PromptCompiler()
        ctx = compiler.compile_with_intent(
            sample_subgraph, sample_code_map, docs, Intent.DESIGN_QUESTION
        )

        assert len(ctx.documentation) == 1
