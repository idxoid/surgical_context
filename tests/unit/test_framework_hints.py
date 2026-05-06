from sidecar.context.framework_hints import _matches_callee_qualified_gate


class TestCalleeQualifiedGate:
    def test_prefix_gate_accepts_fastapi_depends(self):
        rule = {
            "trigger_call": "Depends",
            "require_callee_qualified_prefix": "fastapi",
        }
        assert _matches_callee_qualified_gate({"callee_qualified_name": "fastapi.Depends"}, rule)
        assert _matches_callee_qualified_gate(
            {"callee_qualified_name": "fastapi.dependencies.Depends"}, rule
        )

    def test_prefix_gate_rejects_unqualified_or_wrong_module(self):
        rule = {
            "trigger_call": "Depends",
            "require_callee_qualified_prefix": "fastapi",
        }
        assert not _matches_callee_qualified_gate({}, rule)
        assert not _matches_callee_qualified_gate({"callee_qualified_name": None}, rule)
        assert not _matches_callee_qualified_gate({"callee_qualified_name": "Depends"}, rule)

    def test_no_prefix_always_passes(self):
        rule = {"trigger_call": "Depends"}
        assert _matches_callee_qualified_gate({}, rule)
