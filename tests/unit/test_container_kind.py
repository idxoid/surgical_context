"""Container kind classifier unit tests.

Each test builds a synthetic ``AxisProfile`` representing the structural
fingerprint of one kind. No real Python source is parsed — the goal is to
prove that the classifier reacts to bit + payload presence, not to test the
extractor.

A failing test means either the kind predicate is too narrow (missing a real
fingerprint), too broad (matches a profile it should not), or the predicate
is reading something other than axis bits / payloads (e.g. a name).
"""

from __future__ import annotations

from sidecar.axis.container_kind import (
    ContainerKindClassifier,
    NullGraphProbe,
)
from sidecar.axis.schema import AxisExtraction, AxisFact, AxisProfile


def _fact(axis: str, bit: str, *, uid="u:x", qn="pkg.X", kind="class", line=1, payload=None) -> AxisFact:
    return AxisFact(
        symbol_uid=uid,
        qualified_name=qn,
        symbol_kind=kind,
        axis=axis,
        bit=bit,
        line=line,
        evidence=f"<{bit}>",
        ast_kind="Synthetic",
        payload=payload or {},
    )


def _profile(facts: list[AxisFact], *, uid="u:x", qn="pkg.X", kind="class") -> AxisProfile:
    ext = AxisExtraction(file_path="<synthetic>", facts=facts)
    profile = ext.profiles.get(uid) or AxisProfile(symbol_uid=uid, qualified_name=qn, symbol_kind=kind)
    return profile


class _StubProbe:
    """Test probe — answers are constructed inline per test."""

    def __init__(self, *, marker_kinds: set[str] | None = None, dispersion: float = 0.0, driver: bool = False, kind_edges: int = 0) -> None:
        self._marker_kinds = marker_kinds or set()
        self._dispersion = dispersion
        self._driver = driver
        self._kind_edges = kind_edges

    def library_marker_kinds(self, symbol_uid: str) -> set[str]:
        return set(self._marker_kinds)

    def caller_package_dispersion(self, symbol_uid: str) -> float:
        return self._dispersion

    def is_cfg_driver(self, symbol_uid: str) -> bool:
        return self._driver

    def outgoing_kind_edges(self, symbol_uid, kinds) -> int:  # type: ignore[no-untyped-def]
        return self._kind_edges


# ---------------------------------------------------------------------------
# data_model
# ---------------------------------------------------------------------------


def test_data_model_matches_class_with_typed_attributes():
    profile = _profile(
        [
            _fact("struct", "class_def", uid="u:M", qn="pkg.Model"),
            _fact("struct", "class_attribute", uid="u:M", qn="pkg.Model"),
            _fact("struct", "class_attribute", uid="u:M", qn="pkg.Model"),
            _fact("struct", "annotation", uid="u:M", qn="pkg.Model"),
            _fact("struct", "generic_shape", uid="u:M", qn="pkg.Model"),
        ],
        uid="u:M",
        qn="pkg.Model",
        kind="class",
    )
    classifier = ContainerKindClassifier(NullGraphProbe())
    matches = classifier.classify(profile)
    assert {m.kind for m in matches} == {"data_model"}
    assert matches[0].payload["generic_shape_count"] == 1


def test_data_model_rejects_non_class():
    profile = _profile(
        [
            _fact("struct", "function_def", uid="u:f", qn="pkg.f", kind="function"),
            _fact("dfg", "constructed_output", uid="u:f", qn="pkg.f", kind="function"),
        ],
        uid="u:f",
        kind="function",
    )
    classifier = ContainerKindClassifier(NullGraphProbe())
    assert classifier.classify(profile) == []


def test_data_model_rejects_class_without_typed_shape():
    profile = _profile(
        [
            _fact("struct", "class_def", uid="u:M", qn="pkg.M"),
            _fact("struct", "class_attribute", uid="u:M", qn="pkg.M"),
            _fact("struct", "class_attribute", uid="u:M", qn="pkg.M"),
        ],
        uid="u:M",
    )
    classifier = ContainerKindClassifier(NullGraphProbe())
    assert classifier.classify(profile) == []


# ---------------------------------------------------------------------------
# metadata_carrier
# ---------------------------------------------------------------------------


def test_metadata_carrier_matches_shared_literal_keys():
    profile = _profile(
        [
            _fact("dfg", "keyed_write", payload={"key": "owner"}),
            _fact("dfg", "keyed_write", payload={"key": "name"}),
            _fact("dfg", "keyed_read", payload={"key": "owner"}),
            _fact("struct", "literal_key", payload={"key": "owner"}),
        ]
    )
    classifier = ContainerKindClassifier(NullGraphProbe())
    matches = {m.kind for m in classifier.classify(profile)}
    assert "metadata_carrier" in matches


def test_metadata_carrier_rejects_when_keys_do_not_overlap():
    profile = _profile(
        [
            _fact("dfg", "keyed_write", payload={"key": "owner"}),
            _fact("dfg", "keyed_read", payload={"key": "name"}),
        ]
    )
    classifier = ContainerKindClassifier(NullGraphProbe())
    assert "metadata_carrier" not in {m.kind for m in classifier.classify(profile)}


def test_metadata_carrier_ignores_missing_key_payload():
    profile = _profile(
        [
            _fact("dfg", "keyed_write"),
            _fact("dfg", "keyed_read"),
        ]
    )
    classifier = ContainerKindClassifier(NullGraphProbe())
    assert "metadata_carrier" not in {m.kind for m in classifier.classify(profile)}


# ---------------------------------------------------------------------------
# middleware_chain
# ---------------------------------------------------------------------------


def test_middleware_chain_requires_append_iterate_call():
    profile = _profile(
        [
            _fact("dfg", "callable_value"),
            _fact("dfg", "container_write_value"),
            _fact("dfg", "iteration_source"),
            _fact("cfg", "value_call"),
        ]
    )
    classifier = ContainerKindClassifier(NullGraphProbe())
    assert "middleware_chain" in {m.kind for m in classifier.classify(profile)}


def test_middleware_chain_rejects_when_no_iteration():
    profile = _profile(
        [
            _fact("dfg", "callable_value"),
            _fact("dfg", "container_write_value"),
            _fact("cfg", "value_call"),
        ]
    )
    classifier = ContainerKindClassifier(NullGraphProbe())
    assert "middleware_chain" not in {m.kind for m in classifier.classify(profile)}


def test_middleware_chain_rejects_when_iterated_but_never_called():
    profile = _profile(
        [
            _fact("dfg", "callable_value"),
            _fact("dfg", "container_write_value"),
            _fact("dfg", "iteration_source"),
            # no value_call: stored callable is never invoked
        ]
    )
    classifier = ContainerKindClassifier(NullGraphProbe())
    assert "middleware_chain" not in {m.kind for m in classifier.classify(profile)}


# ---------------------------------------------------------------------------
# signal_register
# ---------------------------------------------------------------------------


def test_signal_register_has_no_axis_only_fallback():
    profile = _profile(
        [
            _fact("dfg", "callable_value"),
            _fact("dfg", "container_write_value"),
            _fact("dfg", "iteration_source"),
            _fact("cfg", "value_call"),
        ]
    )
    classifier = ContainerKindClassifier(NullGraphProbe())
    kinds = {m.kind for m in classifier.classify(profile)}
    # The axis-only shape is too broad and belongs to middleware/callback
    # chains. signal_register needs graph/marker proof.
    assert "signal_register" not in kinds
    assert "middleware_chain" in kinds


def test_signal_register_via_library_marker():
    profile = _profile(
        [
            _fact("dfg", "callable_value"),
            _fact("dfg", "container_write_value"),
            _fact("dfg", "iteration_source"),
            _fact("cfg", "value_call"),
        ]
    )
    probe = _StubProbe(marker_kinds={"signal_register"})
    classifier = ContainerKindClassifier(probe)
    kinds = {m.kind for m in classifier.classify(profile)}
    assert "signal_register" in kinds


# ---------------------------------------------------------------------------
# config_carrier
# ---------------------------------------------------------------------------


def test_config_carrier_matches_class_with_annotated_defaults():
    profile = _profile(
        [
            _fact("struct", "class_def", uid="u:C", qn="pkg.Config"),
            _fact(
                "struct",
                "class_attribute",
                uid="u:C",
                qn="pkg.Config",
                payload={"target": "host", "annotation": "str", "value": "'localhost'"},
            ),
            _fact(
                "struct",
                "class_attribute",
                uid="u:C",
                qn="pkg.Config",
                payload={"target": "port", "annotation": "int", "value": "8080"},
            ),
        ],
        uid="u:C",
        qn="pkg.Config",
        kind="class",
    )
    classifier = ContainerKindClassifier(NullGraphProbe())
    assert "config_carrier" in {m.kind for m in classifier.classify(profile)}


def test_config_carrier_rejects_bare_annotations_without_defaults():
    profile = _profile(
        [
            _fact("struct", "class_def", uid="u:C", qn="pkg.C"),
            _fact(
                "struct",
                "class_attribute",
                uid="u:C",
                qn="pkg.C",
                payload={"target": "host", "annotation": "str", "value": ""},
            ),
            _fact(
                "struct",
                "class_attribute",
                uid="u:C",
                qn="pkg.C",
                payload={"target": "port", "annotation": "int", "value": ""},
            ),
        ],
        uid="u:C",
        kind="class",
    )
    classifier = ContainerKindClassifier(NullGraphProbe())
    assert "config_carrier" not in {m.kind for m in classifier.classify(profile)}


# ---------------------------------------------------------------------------
# web_route_register
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# proxy_object
# ---------------------------------------------------------------------------


def test_proxy_object_via_library_marker():
    profile = _profile([_fact("struct", "class_def")])
    classifier = ContainerKindClassifier(_StubProbe(marker_kinds={"proxy_object"}))
    matches = [m for m in classifier.classify(profile) if m.kind == "proxy_object"]
    assert matches
    assert matches[0].payload.get("via") == "library_marker"


def test_proxy_object_has_no_axis_only_fallback():
    """Without a library marker / graph-level proxy_binding flag, proxy_object
    is intentionally unprovable. The fingerprint (``__getattr__`` overrides
    plus context lookup) crosses symbol boundaries — methods live as separate
    profiles — so the proof requires graph context.
    """
    profile = _profile(
        [
            _fact("struct", "class_def"),
            _fact("dfg", "context_resource"),
            _fact("dfg", "attr_read"),
            _fact("cfg", "context_enter_exit"),
        ]
    )
    classifier = ContainerKindClassifier(NullGraphProbe())
    assert "proxy_object" not in {m.kind for m in classifier.classify(profile)}


def test_proxy_object_marker_does_not_drag_into_signal_or_data():
    """A proxy_object marker on a symbol with overlapping bits must not also
        pick up signal_register or data_model."""
    profile = _profile(
        [
            _fact("struct", "class_def", uid="u:LP", qn="pkg.LocalProxy"),
            _fact("struct", "class_attribute", uid="u:LP", qn="pkg.LocalProxy"),
            _fact("struct", "class_attribute", uid="u:LP", qn="pkg.LocalProxy"),
            _fact(
                "dfg",
                "constructed_output",
                uid="u:LP",
                qn="pkg.LocalProxy",
                payload={"callee": "LocalProxy"},
            ),
            _fact("dfg", "callable_value", uid="u:LP", qn="pkg.LocalProxy"),
        ],
        uid="u:LP",
        qn="pkg.LocalProxy",
        kind="class",
    )
    classifier = ContainerKindClassifier(_StubProbe(marker_kinds={"proxy_object"}))
    kinds = {m.kind for m in classifier.classify(profile)}
    assert "proxy_object" in kinds
    assert "signal_register" not in kinds
    assert "data_model" not in kinds


# ---------------------------------------------------------------------------
# error_dispatch
# ---------------------------------------------------------------------------


def test_error_dispatch_via_library_marker():
    profile = _profile([_fact("struct", "class_def")])
    classifier = ContainerKindClassifier(_StubProbe(marker_kinds={"error_dispatch"}))
    matches = [m for m in classifier.classify(profile) if m.kind == "error_dispatch"]
    assert matches
    assert matches[0].payload.get("via") == "library_marker"


def test_error_dispatch_has_no_axis_only_fallback():
    """Without a library marker, error_dispatch is intentionally unprovable.
    Local axis bits cannot tell apart a class-keyed exception registry from
    a generic class-keyed registry — the discriminator is the type
    hierarchy of the keys, which is graph context, not axis content.
    """
    profile = _profile(
        [
            _fact(
                "dfg",
                "keyed_write",
                payload={
                    "container": "self._handlers",
                    "key": "HTTPException",
                    "key_kind": "Name",
                    "value_kind": "Name",
                },
            ),
            _fact("dfg", "callable_value"),
            _fact("cfg", "exception_handler_type"),
        ]
    )
    classifier = ContainerKindClassifier(NullGraphProbe())
    assert "error_dispatch" not in {m.kind for m in classifier.classify(profile)}


def test_error_dispatch_marker_excludes_signal_register():
    """error_dispatch is in signal_register's yield list — a symbol with the
    shared four-bit fingerprint that also carries error_dispatch marker must
    not also pick up signal_register."""
    profile = _profile(
        [
            _fact("dfg", "callable_value"),
            _fact("dfg", "container_write_value"),
            _fact("dfg", "iteration_source"),
            _fact("cfg", "value_call"),
        ]
    )
    classifier = ContainerKindClassifier(_StubProbe(marker_kinds={"error_dispatch"}))
    kinds = {m.kind for m in classifier.classify(profile)}
    assert "error_dispatch" in kinds
    assert "signal_register" not in kinds


# ---------------------------------------------------------------------------
# di_container
# ---------------------------------------------------------------------------


def test_di_container_matches_call_expression_default():
    """FastAPI Depends pattern: parameter default is a call expression that
    produces a callable. ``def endpoint(db = Depends(get_db)): ...``"""
    profile = _profile(
        [
            _fact("struct", "function_def", uid="u:ep", qn="pkg.endpoint", kind="function"),
            _fact(
                "struct",
                "parameter_default",
                uid="u:ep",
                qn="pkg.endpoint",
                kind="function",
                payload={"name": "db", "default": "Depends(get_db)", "default_kind": "Call"},
            ),
            _fact("dfg", "parameter_default_value", uid="u:ep", qn="pkg.endpoint", kind="function"),
            _fact("dfg", "callable_value", uid="u:ep", qn="pkg.endpoint", kind="function"),
        ],
        uid="u:ep",
        qn="pkg.endpoint",
        kind="function",
    )
    classifier = ContainerKindClassifier(NullGraphProbe())
    matches = [m for m in classifier.classify(profile) if m.kind == "di_container"]
    assert matches
    assert "db" in matches[0].payload["call_default_parameters"]


def test_di_container_rejects_literal_default():
    """Plain literal defaults (`x: int = 5`) are not DI providers."""
    profile = _profile(
        [
            _fact("struct", "function_def", uid="u:f", qn="pkg.f", kind="function"),
            _fact(
                "struct",
                "parameter_default",
                uid="u:f",
                qn="pkg.f",
                kind="function",
                payload={"name": "x", "default": "5", "default_kind": "Constant"},
            ),
            _fact("dfg", "parameter_default_value", uid="u:f", qn="pkg.f", kind="function"),
        ],
        uid="u:f",
        kind="function",
    )
    classifier = ContainerKindClassifier(NullGraphProbe())
    assert "di_container" not in {m.kind for m in classifier.classify(profile)}


def test_di_container_rejects_name_default():
    """Default of a bare name (`x = some_var`) is not a DI provider."""
    profile = _profile(
        [
            _fact("struct", "function_def", uid="u:f", qn="pkg.f", kind="function"),
            _fact(
                "struct",
                "parameter_default",
                uid="u:f",
                qn="pkg.f",
                kind="function",
                payload={"name": "x", "default": "some_var", "default_kind": "Name"},
            ),
            _fact("dfg", "parameter_default_value", uid="u:f", qn="pkg.f", kind="function"),
            _fact("dfg", "callable_value", uid="u:f", qn="pkg.f", kind="function"),
        ],
        uid="u:f",
        kind="function",
    )
    classifier = ContainerKindClassifier(NullGraphProbe())
    assert "di_container" not in {m.kind for m in classifier.classify(profile)}


def test_di_container_rejects_pytest_fixture_style():
    """Pytest-style ``def test_x(db):`` has no parameter default, so no axis
    fingerprint. The kind correctly does not fire — that pattern resolves by
    name elsewhere, outside axis bits."""
    profile = _profile(
        [
            _fact("struct", "function_def", uid="u:t", qn="pkg.test_x", kind="function"),
            _fact("struct", "parameter_decl", uid="u:t", qn="pkg.test_x", kind="function", payload={"name": "db"}),
            _fact("dfg", "parameter_input", uid="u:t", qn="pkg.test_x", kind="function"),
        ],
        uid="u:t",
        kind="function",
    )
    classifier = ContainerKindClassifier(NullGraphProbe())
    assert "di_container" not in {m.kind for m in classifier.classify(profile)}


def test_di_container_does_not_match_class():
    """di_container lives on the consumer function, not on a surrounding
    class. A class with the same fingerprint applied through its members
    should not pick it up at the class level."""
    profile = _profile(
        [
            _fact("struct", "class_def", uid="u:C", qn="pkg.C", kind="class"),
            _fact("dfg", "callable_value", uid="u:C", qn="pkg.C", kind="class"),
        ],
        uid="u:C",
        kind="class",
    )
    classifier = ContainerKindClassifier(NullGraphProbe())
    assert "di_container" not in {m.kind for m in classifier.classify(profile)}


# ---------------------------------------------------------------------------
# task_register
# ---------------------------------------------------------------------------


def test_task_register_via_library_marker():
    profile = _profile([_fact("struct", "class_def")])
    classifier = ContainerKindClassifier(_StubProbe(marker_kinds={"task_register"}))
    matches = [m for m in classifier.classify(profile) if m.kind == "task_register"]
    assert matches
    assert matches[0].payload.get("via") == "library_marker"


def test_task_register_has_no_axis_only_fallback():
    """Without a library marker, task_register is intentionally unprovable —
    its discriminator from other registry kinds lives in graph context, and
    inferring it from axis bits alone would only succeed by name-matching.
    The honest result is no match, surfaced as 'task_register unproven' at L3.
    """
    profile = _profile(
        [
            _fact("dfg", "callable_value"),
            _fact("dfg", "container_write_value"),
            _fact("dfg", "iteration_source"),
            _fact("cfg", "value_call"),
            _fact("struct", "decorator_shape"),
        ]
    )
    classifier = ContainerKindClassifier(NullGraphProbe())
    assert "task_register" not in {m.kind for m in classifier.classify(profile)}


def test_task_register_marker_does_not_imply_web_or_signal():
    """Library markers don't double-classify — a task_register marker should
    yield task_register but not pull the symbol into web/signal predicates."""
    profile = _profile(
        [
            _fact("dfg", "callable_value"),
            _fact("dfg", "container_write_value"),
            _fact("dfg", "iteration_source"),
            _fact("cfg", "value_call"),
        ]
    )
    classifier = ContainerKindClassifier(_StubProbe(marker_kinds={"task_register"}))
    kinds = {m.kind for m in classifier.classify(profile)}
    assert "task_register" in kinds
    assert "signal_register" not in kinds


def test_web_route_register_via_library_marker():
    profile = _profile(
        [_fact("struct", "class_def")],
    )
    classifier = ContainerKindClassifier(_StubProbe(marker_kinds={"web_route_register"}))
    matches = [m for m in classifier.classify(profile) if m.kind == "web_route_register"]
    assert matches
    assert matches[0].payload.get("via") == "library_marker"


def test_web_route_register_has_no_axis_only_callable_table_fallback():
    profile = _profile(
        [
            _fact("dfg", "keyed_write", payload={"key": "/users", "value_kind": "callable"}),
            _fact("dfg", "keyed_write", payload={"key": "/items", "value_kind": "callable"}),
            _fact("dfg", "callable_value", line=1),
        ]
    )
    classifier = ContainerKindClassifier(NullGraphProbe())
    assert "web_route_register" not in {m.kind for m in classifier.classify(profile)}


def test_web_route_register_rejects_lone_keyed_write():
    profile = _profile(
        [
            _fact("dfg", "keyed_write", payload={"key": "/x", "value_kind": "callable"}),
        ]
    )
    classifier = ContainerKindClassifier(NullGraphProbe())
    assert "web_route_register" not in {m.kind for m in classifier.classify(profile)}


# ---------------------------------------------------------------------------
# Classifier batch + introspection
# ---------------------------------------------------------------------------


def test_registered_kinds_lists_every_predicate():
    classifier = ContainerKindClassifier(NullGraphProbe())
    kinds = classifier.registered_kinds()
    assert "data_model" in kinds
    assert "metadata_carrier" in kinds
    assert "middleware_chain" in kinds
    assert "signal_register" in kinds
    assert "config_carrier" in kinds
    assert "web_route_register" in kinds


def test_classify_many_returns_only_matched_symbols():
    matching = _profile(
        [
            _fact("struct", "class_def", uid="u:M", qn="pkg.M"),
            _fact("struct", "class_attribute", uid="u:M", qn="pkg.M"),
            _fact("struct", "class_attribute", uid="u:M", qn="pkg.M"),
            _fact("struct", "annotation", uid="u:M", qn="pkg.M"),
        ],
        uid="u:M",
        qn="pkg.M",
        kind="class",
    )
    nonmatching = _profile(
        [_fact("struct", "function_def", uid="u:f", qn="pkg.f", kind="function")],
        uid="u:f",
        kind="function",
    )
    classifier = ContainerKindClassifier(NullGraphProbe())
    out = classifier.classify_many([matching, nonmatching])
    assert "u:M" in out
    assert "u:f" not in out
