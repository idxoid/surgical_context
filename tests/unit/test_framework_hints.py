from sidecar.indexer.framework_hints import (
    FrameworkHintsIndexer,
    _matches_call_argument_rule,
    _matches_callee_qualified_gate,
)


class TestCalleeQualifiedGate:
    def test_prefix_gate_accepts_qualified_trigger(self):
        rule = {
            "trigger_call": "Marker",
            "require_callee_qualified_prefix": "framework",
        }
        assert _matches_callee_qualified_gate({"callee_qualified_name": "framework.Marker"}, rule)
        assert _matches_callee_qualified_gate(
            {"callee_qualified_name": "framework.subsystem.Marker"}, rule
        )

    def test_prefix_gate_rejects_unqualified_or_wrong_module(self):
        rule = {
            "trigger_call": "Marker",
            "require_callee_qualified_prefix": "framework",
        }
        assert not _matches_callee_qualified_gate({}, rule)
        assert not _matches_callee_qualified_gate({"callee_qualified_name": None}, rule)
        assert not _matches_callee_qualified_gate({"callee_qualified_name": "Marker"}, rule)

    def test_no_prefix_always_passes(self):
        rule = {"trigger_call": "Marker"}
        assert _matches_callee_qualified_gate({}, rule)


class TestCallArgumentRule:
    def test_bundled_dependency_rule_is_generic(self):
        rules = FrameworkHintsIndexer(db=None).rules
        rule = next(r for r in rules if r["id"] == "dependency_call_argument_link")

        assert rule["subtype"] == "dependency_injection"
        assert "trigger_call" not in rule
        assert "require_callee_qualified_prefix" not in rule
        assert _matches_call_argument_rule({"callee_name": "DependencyMarker"}, rule)

    def test_exact_trigger_still_supported_for_custom_rules(self):
        rule = {"type": "call_argument_link", "trigger_call": "Marker"}
        assert _matches_call_argument_rule({"callee_name": "Marker"}, rule)
        assert not _matches_call_argument_rule({"callee_name": "Other"}, rule)

    def test_dependency_token_rule_matches_name_or_qualified_name(self):
        rule = {
            "type": "call_argument_link",
            "trigger_call_tokens": ["depend", "inject", "provider"],
        }
        assert _matches_call_argument_rule({"callee_name": "DependencyMarker"}, rule)
        assert _matches_call_argument_rule(
            {
                "callee_name": "marker",
                "callee_qualified_name": "container.inject.marker",
            },
            rule,
        )

    def test_dependency_token_rule_avoids_substring_false_positive(self):
        rule = {
            "type": "call_argument_link",
            "trigger_call_tokens": ["depend"],
        }
        assert not _matches_call_argument_rule({"callee_name": "IndependentMarker"}, rule)

    def test_wrong_rule_type_or_missing_tokens_do_not_match(self):
        assert not _matches_call_argument_rule(
            {"callee_name": "DependencyMarker"},
            {"type": "decorator_bridge", "trigger_call_tokens": ["depend"]},
        )
        assert not _matches_call_argument_rule(
            {"callee_name": "plain_call"},
            {"type": "call_argument_link", "trigger_call_tokens": ["depend"]},
        )
