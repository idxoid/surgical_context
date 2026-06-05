"""Unit tests for intent classification."""

from sidecar.context.intent_classifier import (
    Intent,
    IntentClassifier,
    IntentConfig,
    IntentSignal,
)


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

    def test_impact_intent_for_affected_tests_and_examples(self):
        """Detect benchmark-style blast-radius questions without requiring 'break'."""
        queries = [
            "If Flask's routing dispatch mechanism changes, what test suites and example code would be affected?",
            "What tests are affected if alias handling changes?",
            "If the ORM field descriptor protocol changes, which parts of Django tests and public APIs would break?",
        ]
        for q in queries:
            assert IntentClassifier.classify_intent(q) == Intent.IMPACT_ANALYSIS

    def test_impact_primary_uses_score_before_precedence(self):
        """A strong impact phrase should beat weaker earlier debugging/refactor tokens."""
        signal = IntentClassifier.classify_with_metadata(
            "If I change this generated action type, what parts are most likely to break?"
        )

        assert signal.primary == Intent.IMPACT_ANALYSIS
        assert signal.distribution["impact_analysis"] > signal.distribution["debugging"]

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

    def test_classify_with_metadata_returns_distribution(self):
        """Keyword scoring exposes a normalized distribution and confidence."""
        signal = IntentClassifier.classify_with_metadata(
            "How does dependency injection work before the endpoint function is called?"
        )

        assert signal.primary == Intent.EXPLORATION
        assert abs(sum(signal.distribution.values()) - 1.0) < 1e-9
        assert signal.confidence > 0
        assert signal.matched_keywords["exploration"]

    def test_classify_with_metadata_marks_ambiguous_queries(self):
        """Mixed-intent prompts should surface ambiguity instead of hiding it."""
        signal = IntentClassifier.classify_with_metadata("Why should I add this feature?")

        assert signal.primary == Intent.DEBUGGING
        assert signal.ambiguous is True
        assert "debugging" in signal.distribution
        assert "new_feature" in signal.distribution

    def test_policy_from_signal_promotes_strong_secondary_intent(self):
        """Budget policy consumes distribution instead of collapsing to primary only."""
        signal = IntentSignal(
            primary=Intent.DEBUGGING,
            distribution={
                "debugging": 0.55,
                "refactor": 0.30,
                "exploration": 0.15,
            },
            confidence=0.55,
            ambiguous=True,
        )

        policy = IntentClassifier.policy_from_signal(signal)

        assert policy.active_intents == (
            Intent.DEBUGGING,
            Intent.REFACTORING,
        )
        assert policy.secondary_intents == (Intent.REFACTORING,)
        assert "impact_runtime" in policy.supplemental_roles
        assert "impact_public_api" in policy.supplemental_roles
        assert policy.tier_order[:2] == ("code", "cross_refs")
        assert policy.budget_share["debugging"] > policy.budget_share["refactor"]

    def test_policy_from_signal_uses_ambiguous_runner_up_below_threshold(self):
        """Ambiguous queries keep the runner-up even when it is below the hard threshold."""
        signal = IntentSignal(
            primary=Intent.REFACTORING,
            distribution={"refactor": 0.62, "debugging": 0.22, "exploration": 0.16},
            confidence=0.62,
            ambiguous=True,
        )

        policy = IntentClassifier.policy_from_signal(signal)

        assert policy.active_intents == (Intent.REFACTORING, Intent.DEBUGGING)
        assert "runtime_surface" in policy.supplemental_roles

    def test_empty_query_metadata_defaults_to_exploration(self):
        """Empty queries keep the old default and expose a simple distribution."""
        signal = IntentClassifier.classify_with_metadata("")

        assert signal.primary == Intent.EXPLORATION
        assert signal.distribution == {"exploration": 1.0}
        assert signal.confidence == 0.0
        assert signal.ambiguous is False

    def test_intent_resolution_degrades_impact_against_shallow_profile(self):
        """Impact intent becomes a reachability mode when repo impact is shallow."""
        resolution = IntentClassifier.resolve_with_profile(
            "What breaks if I change relationship()?",
            {
                "indexability": "medium",
                "retrieval_readiness": "partial",
                "capabilities": {
                    "impact_analysis": "shallow_partial",
                    "static_call_reasoning": "medium",
                    "runtime_registry_semantics": "low",
                },
                "reasoning_contract": {
                    "allowed": ["limited reachability-based impact candidates"],
                    "risky": ["impact is shallow"],
                },
            },
        )

        assert resolution.desired_intent == "impact_analysis"
        assert resolution.effective_mode == "shallow_reachability_impact"
        assert resolution.degraded is True
        assert resolution.available_capabilities["impact_analysis"] == "shallow_partial"
        assert "impact is shallow" in resolution.risks

    def test_intent_resolution_marks_missing_profile(self):
        """Without a repository profile, intent is only a text-routing hint."""
        resolution = IntentClassifier.resolve_with_profile("How does this work?", None)

        assert resolution.desired_intent == "exploration"
        assert resolution.effective_mode == "unprofiled_intent_routing"
        assert resolution.degraded is True
        assert resolution.repository_readiness == ""


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


class TestQuestionShape:
    """Plain-text question features — high-precision orthogonal signals."""

    def test_definition_hint_narrows_direction_to_self(self):
        from sidecar.context.intent_classifier import (
            extract_question_shape,
            modulate_shape,
        )

        qs = extract_question_shape("Where is HttpRouter defined?")
        assert qs.wh_word == "where"
        assert qs.direction_hint == "definition"
        assert qs.entity_count == 1

        shape = modulate_shape(Intent.NAVIGATION, qs)
        assert shape.direction == ("self",)

    def test_usage_hint_keeps_navigation_backward(self):
        from sidecar.context.intent_classifier import (
            extract_question_shape,
            modulate_shape,
        )

        qs = extract_question_shape("Where is CacheManager used by other modules?")
        assert qs.direction_hint == "usage"
        shape = modulate_shape(Intent.NAVIGATION, qs)
        assert shape.direction == ("backward",)

    def test_bare_uses_no_longer_triggers_usage_hint(self):
        """Bare `uses` / `use` are ambiguous (``X uses Y`` is forward;
        ``what uses X`` is backward) — they no longer set the direction
        hint. Only unambiguous passive-voice phrases (``used by``,
        ``called by``, ``callers of``) do."""
        from sidecar.context.intent_classifier import extract_question_shape

        # "use decorators" is `X uses Y`-shape, not a callers query.
        assert extract_question_shape(
            "How does NestJS use decorators to map routes?"
        ).direction_hint == ""

    def test_flow_verb_turns_chase_chains_on_without_narrowing_direction(self):
        from sidecar.context.intent_classifier import (
            INTENT_TRAVERSAL,
            extract_question_shape,
            modulate_shape,
        )

        qs = extract_question_shape(
            "How does dependency injection get resolved before the endpoint runs?"
        )
        assert qs.has_flow_verb is True

        # DEBUGGING has chase_chains=False at base; flow verb turns it on
        # but keeps the base direction (collapsing to forward-only here
        # loses the registration / handler chain that gave the question
        # its meaning, so the modulator stays additive).
        base = INTENT_TRAVERSAL[Intent.DEBUGGING]
        shape = modulate_shape(Intent.DEBUGGING, qs)
        assert shape.chase_chains is True
        assert shape.direction == base.direction
        assert base.chase_chains is False

    def test_chained_definition_question_keeps_graph_walk(self):
        from sidecar.context.intent_classifier import (
            INTENT_TRAVERSAL,
            extract_question_shape,
            modulate_shape,
        )

        qs = extract_question_shape(
            "How are response methods like send() and json() implemented and chained?"
        )
        assert qs.direction_hint == "definition"
        assert qs.has_flow_verb is True

        shape = modulate_shape(Intent.EXPLORATION, qs)
        base = INTENT_TRAVERSAL[Intent.EXPLORATION]
        assert shape.chase_chains is True
        assert shape.direction == base.direction

    def test_state_verb_alone_does_not_modulate(self):
        from sidecar.context.intent_classifier import (
            INTENT_TRAVERSAL,
            extract_question_shape,
            modulate_shape,
        )

        qs = extract_question_shape("How does Context manage state?")
        assert qs.has_state_verb is True
        assert qs.has_flow_verb is False
        # Single-mechanism explain — base EXPLORATION shape stays put.
        shape = modulate_shape(Intent.EXPLORATION, qs)
        base = INTENT_TRAVERSAL[Intent.EXPLORATION]
        assert shape.direction == base.direction
        assert shape.max_depth == base.max_depth
        assert shape.chase_chains == base.chase_chains

    def test_multiple_entities_widen_depth_and_chain(self):
        from sidecar.context.intent_classifier import (
            INTENT_TRAVERSAL,
            extract_question_shape,
            modulate_shape,
        )

        qs = extract_question_shape(
            "How does Controller turn DefaultRouter routes into HttpResponses?"
        )
        assert qs.entity_count >= 2
        base = INTENT_TRAVERSAL[Intent.EXPLORATION]
        shape = modulate_shape(Intent.EXPLORATION, qs)
        assert shape.chase_chains is True
        assert shape.max_depth >= base.max_depth + 2

    def test_wide_scope_pushes_to_transitive(self):
        from sidecar.context.intent_classifier import (
            extract_question_shape,
            modulate_shape,
        )

        qs = extract_question_shape(
            "How does FastAPI handle errors everywhere in the codebase?"
        )
        assert qs.scope == "wide"
        shape = modulate_shape(Intent.EXPLORATION, qs)
        assert shape.max_depth >= 10

    def test_no_query_yields_blank_shape(self):
        from sidecar.context.intent_classifier import extract_question_shape

        qs = extract_question_shape("")
        assert qs.entity_count == 0
        assert qs.wh_word == ""
        assert qs.direction_hint == ""
