"""Unit tests for intent classification."""


from sidecar.context.intent_classifier import Intent, IntentClassifier, IntentConfig


class TestIntentClassifier:
    """Test intent detection accuracy."""

    def test_navigation_intent(self):
        """Detect navigation queries."""
        queries = [
            "Where is the process_payment function?",
            "What calls validate_amount?",
            "Find the definition of calculate_fee",
            "Where is this imported?",
        ]
        for q in queries:
            assert IntentClassifier.classify_intent(q) == Intent.NAVIGATION

    def test_debugging_intent(self):
        """Detect debugging queries."""
        queries = [
            "Why does this fail?",
            "What's wrong with the payment logic?",
            "Debug the symbol extraction error",
            "Why is this breaking?",
        ]
        for q in queries:
            assert IntentClassifier.classify_intent(q) == Intent.DEBUGGING

    def test_refactoring_intent(self):
        """Detect refactoring queries."""
        queries = [
            "Rename CALLS to CALLS_DIRECT everywhere",
            "Move this function to another module",
            "Change the database query pattern",
            "Remove deprecated code",
        ]
        for q in queries:
            assert IntentClassifier.classify_intent(q) == Intent.REFACTORING

    def test_exploration_intent(self):
        """Detect exploration queries."""
        queries = [
            "What does this function do?",
            "How does the graph expansion work?",
            "Explain the payment flow",
            "What's the purpose of this code?",
        ]
        for q in queries:
            assert IntentClassifier.classify_intent(q) == Intent.EXPLORATION

    def test_new_feature_intent(self):
        """Detect new feature queries."""
        queries = [
            "Add support for TypeScript class properties",
            "How do I implement a new symbol extractor?",
            "Build a new caching layer",
            "Create a metrics endpoint",
        ]
        for q in queries:
            assert IntentClassifier.classify_intent(q) == Intent.NEW_FEATURE

    def test_design_question_intent(self):
        """Detect design question queries."""
        queries = [
            "How should we approach caching?",
            "What's the best pattern for this?",
            "Should we use a decorator or middleware?",
            "What architecture would you recommend?",
        ]
        for q in queries:
            assert IntentClassifier.classify_intent(q) == Intent.DESIGN_QUESTION

    def test_empty_query_defaults_to_exploration(self):
        """Empty query defaults to exploration."""
        assert IntentClassifier.classify_intent("") == Intent.EXPLORATION

    def test_ambiguous_query_follows_priority(self):
        """Ambiguous queries match first in priority order."""
        # "why" is debugging, "add" is new_feature
        # Debugging is checked before new_feature, so "why add" should be debugging
        query = "Why should I add this feature?"
        assert IntentClassifier.classify_intent(query) == Intent.DEBUGGING

    def test_case_insensitive_matching(self):
        """Intent matching is case-insensitive."""
        assert IntentClassifier.classify_intent("WHERE IS THIS?") == Intent.NAVIGATION
        assert IntentClassifier.classify_intent("Why DOES this fail?") == Intent.DEBUGGING


class TestIntentConfig:
    """Test intent priority configuration."""

    def test_all_intents_have_priorities(self):
        """Every intent has a priority ordering."""
        for intent in Intent:
            assert intent in IntentConfig.PRIORITY
            assert len(IntentConfig.PRIORITY[intent]) == 6

    def test_priority_orders_contain_all_tiers(self):
        """Each priority order contains all 6 tiers exactly once."""
        for intent, tiers in IntentConfig.PRIORITY.items():
            assert len(tiers) == 6
            assert set(tiers) == set(IntentConfig.TIERS)

    def test_navigation_prioritizes_code(self):
        """Navigation intent prioritizes code over docs."""
        priority = IntentConfig.PRIORITY[Intent.NAVIGATION]
        assert priority[0] == "code"
        assert "code" in priority[:2]

    def test_new_feature_deprioritizes_code(self):
        """New feature intent deprioritizes code."""
        priority = IntentConfig.PRIORITY[Intent.NEW_FEATURE]
        assert priority[-2:] == ["cross_refs", "code"]

    def test_design_question_prioritizes_concept(self):
        """Design question intent prioritizes concept/idea."""
        priority = IntentConfig.PRIORITY[Intent.DESIGN_QUESTION]
        assert priority[0] == "concept"
        assert priority[1] == "idea"

    def test_refactor_prioritizes_cross_refs(self):
        """Refactoring intent prioritizes cross-refs (blast radius)."""
        priority = IntentConfig.PRIORITY[Intent.REFACTORING]
        assert priority[0] == "cross_refs"


class TestIntentGetTierPriority:
    """Test tier priority retrieval."""

    def test_get_tier_priority_returns_list(self):
        """get_tier_priority returns a list of tiers."""
        result = IntentClassifier.get_tier_priority(Intent.NAVIGATION)
        assert isinstance(result, list)
        assert len(result) == 6

    def test_get_tier_priority_matches_config(self):
        """get_tier_priority matches the IntentConfig."""
        for intent in Intent:
            priority = IntentClassifier.get_tier_priority(intent)
            expected = IntentConfig.PRIORITY[intent]
            assert priority == expected
