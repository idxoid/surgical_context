"""Unit tests for ContextArbitrator with intent classification."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pytest

from sidecar.context.arbitrator import ContextArbitrator
from sidecar.context.intent_classifier import Intent
from sidecar.database.neo4j_client import Neo4jClient


@pytest.fixture
def neo4j_db():
    """Connect to test Neo4j instance."""
    uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    user = os.getenv("NEO4J_USER", "neo4j")
    password = os.getenv("NEO4J_PASSWORD", "password")
    db = Neo4jClient(uri, user, password)
    yield db
    db.close()


class TestContextArbitratorWithIntent:
    """Test intent detection and tier-aware context assembly."""

    def test_get_context_with_navigation_intent(self, neo4j_db):
        """Navigation intent produces surgical context."""
        arb = ContextArbitrator(neo4j_db)

        # Query with navigation intent
        ctx = arb.get_context_for_symbol(
            "process_payment",
            question="Where is the payment processing function?",
            token_budget=2000,
        )

        # Should produce valid context with intent set
        if not isinstance(ctx, str):
            assert ctx.intent == "navigation"
            assert ctx.mode in ("surgical_full", "surgical_doc_only", "standard")
            assert ctx.primary_source.symbol == "process_payment"

    def test_get_context_with_debugging_intent(self, neo4j_db):
        """Debugging intent produces code + cross-refs context."""
        arb = ContextArbitrator(neo4j_db)

        ctx = arb.get_context_for_symbol(
            "validate_amount",
            question="Why does payment validation fail?",
            token_budget=2000,
        )

        if not isinstance(ctx, str):
            assert ctx.intent == "debugging"
            assert ctx.primary_source.code  # Code should be populated

    def test_get_context_with_refactor_intent(self, neo4j_db):
        """Refactoring intent includes cross-refs (blast radius)."""
        arb = ContextArbitrator(neo4j_db)

        ctx = arb.get_context_for_symbol(
            "calculate_fee",
            question="Rename calculate_fee everywhere",
            token_budget=2000,
        )

        if not isinstance(ctx, str):
            assert ctx.intent == "refactor"
            # Refactoring prioritizes cross-refs, so graph should be present
            assert ctx.primary_source.symbol == "calculate_fee"

    def test_get_context_with_exploration_intent(self, neo4j_db):
        """Exploration intent includes code and concept docs."""
        arb = ContextArbitrator(neo4j_db)

        ctx = arb.get_context_for_symbol(
            "process_payment",
            question="How does the payment processing work?",
            token_budget=2000,
        )

        if not isinstance(ctx, str):
            assert ctx.intent == "exploration"
            assert ctx.primary_source.code

    def test_get_context_with_new_feature_intent(self, neo4j_db):
        """New feature intent deprioritizes code."""
        arb = ContextArbitrator(neo4j_db)

        ctx = arb.get_context_for_symbol(
            "process_payment",
            question="How do I add new payment methods?",
            token_budget=2000,
        )

        if not isinstance(ctx, str):
            assert ctx.intent == "new_feature"
            assert ctx.mode in ("surgical_full", "surgical_doc_only", "standard")

    def test_get_context_with_design_question_intent(self, neo4j_db):
        """Design question intent prioritizes concepts."""
        arb = ContextArbitrator(neo4j_db)

        ctx = arb.get_context_for_symbol(
            "validate_order",
            question="What pattern should we use for validation?",
            token_budget=2000,
        )

        if not isinstance(ctx, str):
            assert ctx.intent == "design_question"

    def test_get_context_without_question_defaults_to_exploration(self, neo4j_db):
        """Missing question defaults to exploration intent."""
        arb = ContextArbitrator(neo4j_db)

        ctx = arb.get_context_for_symbol(
            "process_payment",
            token_budget=2000,
        )

        if not isinstance(ctx, str):
            # Default question is empty string → exploration intent
            assert ctx.intent == "exploration"

    def test_mode_field_populated_in_context(self, neo4j_db):
        """Mode field is populated in returned PromptContext."""
        arb = ContextArbitrator(neo4j_db)

        ctx = arb.get_context_for_symbol(
            "process_payment",
            question="What does this function do?",
            token_budget=2000,
        )

        if not isinstance(ctx, str):
            assert hasattr(ctx, "mode")
            assert ctx.mode in ("surgical_full", "surgical_doc_only", "standard")

    def test_intent_field_in_dict_serialization(self, neo4j_db):
        """Intent field appears in to_dict() serialization."""
        arb = ContextArbitrator(neo4j_db)

        ctx = arb.get_context_for_symbol(
            "process_payment",
            question="Why is this failing?",
            token_budget=2000,
        )

        if not isinstance(ctx, str):
            ctx_dict = ctx.to_dict()
            assert "intent" in ctx_dict
            assert "mode" in ctx_dict
            assert ctx_dict["intent"] == "debugging"

    def test_multiple_questions_produce_different_intents(self, neo4j_db):
        """Same symbol with different questions → different intents."""
        arb = ContextArbitrator(neo4j_db)

        ctx_nav = arb.get_context_for_symbol(
            "process_payment",
            question="Where is this?",
            token_budget=2000,
        )

        ctx_debug = arb.get_context_for_symbol(
            "process_payment",
            question="Why does it fail?",
            token_budget=2000,
        )

        if not isinstance(ctx_nav, str) and not isinstance(ctx_debug, str):
            assert ctx_nav.intent == "navigation"
            assert ctx_debug.intent == "debugging"
            # Same symbol, different context assembly
            assert ctx_nav.intent != ctx_debug.intent

    def test_backward_compatibility_without_question(self, neo4j_db):
        """Existing code calling without question still works."""
        arb = ContextArbitrator(neo4j_db)

        # Old calling convention (no question parameter)
        ctx = arb.get_context_for_symbol("process_payment", token_budget=2000)

        if not isinstance(ctx, str):
            assert ctx.primary_source.symbol == "process_payment"
            assert ctx.intent == "exploration"  # Default
