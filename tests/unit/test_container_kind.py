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

    def __init__(
        self,
        *,
        marker_kinds: set[str] | None = None,
        dispersion: float = 0.0,
        driver: bool = False,
        kind_edges: int = 0,
        peer_kinds_by_prefix: dict[str, set[str]] | None = None,
    ) -> None:
        self._marker_kinds = marker_kinds or set()
        self._dispersion = dispersion
        self._driver = driver
        self._kind_edges = kind_edges
        self._peer_kinds_by_prefix = peer_kinds_by_prefix or {}

    def library_marker_kinds(self, symbol_uid: str) -> set[str]:
        return set(self._marker_kinds)

    def is_event_signal(self, symbol_uid: str) -> bool:
        # signal_register is derived from EVENT pub/sub topology now; a stub that
        # carries the signal_register hint stands in for that graph proof.
        return "signal_register" in self._marker_kinds

    def caller_package_dispersion(self, symbol_uid: str) -> float:
        return self._dispersion

    def is_cfg_driver(self, symbol_uid: str) -> bool:
        return self._driver

    def outgoing_kind_edges(self, symbol_uid, kinds) -> int:  # type: ignore[no-untyped-def]
        return self._kind_edges

    def outgoing_handles_count(self, symbol_uid: str) -> int:
        return 0

    def outgoing_injects_count(self, symbol_uid: str) -> int:
        return 0

    def peer_container_kinds_for(self, qualified_name_prefix: str) -> set[str]:
        return set(self._peer_kinds_by_prefix.get(qualified_name_prefix, set()))


# ---------------------------------------------------------------------------
# registry_class — cross-symbol structural floor under marker-only kinds
# ---------------------------------------------------------------------------


def test_registry_class_fires_when_peer_method_carries_metadata_carrier():
    profile = _profile(
        [_fact("struct", "class_def", uid="u:App", qn="sansio.app.App", kind="class")],
        uid="u:App",
        qn="sansio.app.App",
        kind="class",
    )
    probe = _StubProbe(
        peer_kinds_by_prefix={"sansio.app.App.": {"metadata_carrier"}},
    )

    matches = ContainerKindClassifier(probe).classify(profile)

    kinds = {m.kind for m in matches}
    assert "registry_class" in kinds
    rc = next(m for m in matches if m.kind == "registry_class")
    assert rc.payload["registry_method_kinds"] == ["metadata_carrier"]
    assert any("peer_method_kinds:" in p for p in rc.evidence_probes)


def test_registry_class_fires_for_middleware_chain_peer_too():
    profile = _profile(
        [_fact("struct", "class_def", uid="u:H", qn="pkg.Hub", kind="class")],
        uid="u:H", qn="pkg.Hub", kind="class",
    )
    probe = _StubProbe(
        peer_kinds_by_prefix={"pkg.Hub.": {"middleware_chain", "config_carrier"}},
    )

    matches = ContainerKindClassifier(probe).classify(profile)

    assert "registry_class" in {m.kind for m in matches}


def test_registry_class_does_not_fire_without_registry_peers():
    profile = _profile(
        [_fact("struct", "class_def", uid="u:M", qn="pkg.Plain", kind="class")],
        uid="u:M", qn="pkg.Plain", kind="class",
    )
    # Peer methods exist but carry only data_model / config_carrier kinds.
    probe = _StubProbe(
        peer_kinds_by_prefix={"pkg.Plain.": {"data_model", "config_carrier"}},
    )

    matches = ContainerKindClassifier(probe).classify(profile)

    assert "registry_class" not in {m.kind for m in matches}


def test_registry_class_does_not_fire_on_function_symbol_with_peers():
    profile = _profile(
        [_fact("struct", "function_def", uid="u:f", qn="pkg.f", kind="function")],
        uid="u:f", qn="pkg.f", kind="function",
    )
    probe = _StubProbe(
        peer_kinds_by_prefix={"pkg.f.": {"metadata_carrier"}},
    )

    matches = ContainerKindClassifier(probe).classify(profile)

    assert "registry_class" not in {m.kind for m in matches}


# ---------------------------------------------------------------------------
# Consumer-derived Variable channel: ``app = Something(); @app.route(...)``
# ---------------------------------------------------------------------------


def test_registry_class_fires_on_variable_with_registered_callable():
    """Module-level Variable with HANDLES out (consumer decorator pattern)
    structurally classifies as registry — no catalogue / external marker
    needed. The catalogue still names the *subtype* on top, but the
    generic registry classification is guaranteed.
    """
    profile = _profile(
        [
            _fact(
                "dfg",
                "registered_callable",
                uid="u:app",
                qn="myapp.app",
                kind="variable",
                payload={"count": 3},
            ),
        ],
        uid="u:app", qn="myapp.app", kind="variable",
    )

    matches = ContainerKindClassifier(NullGraphProbe()).classify(profile)

    rc = next((m for m in matches if m.kind == "registry_class"), None)
    assert rc is not None
    assert rc.payload["registered_callable_count"] == 3
    assert any("consumer_derived" in p for p in rc.evidence_probes)


def test_registry_class_does_not_fire_on_variable_without_registered_callable():
    """A Variable that holds a value but has no decorator-registered handlers
    is NOT a registry — the structural proof is the decorator binding.
    """
    profile = _profile(
        [_fact("struct", "module_scope", uid="u:v", qn="myapp.cache", kind="variable")],
        uid="u:v", qn="myapp.cache", kind="variable",
    )

    matches = ContainerKindClassifier(NullGraphProbe()).classify(profile)

    assert "registry_class" not in {m.kind for m in matches}


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
# keyed_register_callable
# ---------------------------------------------------------------------------


def _keyed_register_facts() -> list[AxisFact]:
    """Fingerprint of a registration method that writes a callable
    into a keyed container — Celery's ``TaskRegistry.register``,
    FastAPI's ``add_api_route``, Flask's ``add_url_rule``."""
    return [
        _fact("dfg", "subscript_write"),
        _fact("dfg", "keyed_write"),
        _fact("dfg", "container_write_value"),
        _fact("dfg", "callable_value"),
    ]


def test_keyed_register_callable_fires_on_full_fingerprint():
    profile = _profile(_keyed_register_facts())
    classifier = ContainerKindClassifier(NullGraphProbe())
    assert "keyed_register_callable" in {
        m.kind for m in classifier.classify(profile)
    }


def test_keyed_register_callable_rejects_when_iterating():
    """Middleware chains both iterate AND write — that is a chain, not
    a registration, so the kind must back off when both shapes overlap.
    """
    profile = _profile(
        _keyed_register_facts() + [_fact("dfg", "iteration_source")]
    )
    classifier = ContainerKindClassifier(NullGraphProbe())
    assert "keyed_register_callable" not in {
        m.kind for m in classifier.classify(profile)
    }


def test_keyed_register_callable_requires_callable_value():
    """A pure ``self[key] = some_data`` (no callable) is not a
    register-callable pattern — it is plain dict storage."""
    facts = [
        f for f in _keyed_register_facts() if f.bit != "callable_value"
    ]
    profile = _profile(facts)
    classifier = ContainerKindClassifier(NullGraphProbe())
    assert "keyed_register_callable" not in {
        m.kind for m in classifier.classify(profile)
    }


def test_keyed_register_callable_requires_subscript_write():
    facts = [
        f for f in _keyed_register_facts() if f.bit != "subscript_write"
    ]
    profile = _profile(facts)
    classifier = ContainerKindClassifier(NullGraphProbe())
    assert "keyed_register_callable" not in {
        m.kind for m in classifier.classify(profile)
    }


def test_keyed_register_callable_rejects_when_returning_value():
    """A method that mutates a container *and* returns a value is a
    dispatcher / inspector (e.g. ``APIRoute.matches`` which writes
    ``child_scope["route"] = self`` then returns ``(match, child_scope)``,
    or ``FastAPI.__call__`` which sets ``scope["root_path"]`` then
    awaits ``super().__call__``). A real registration is a pure
    side-effect setter — fires the storage bits and exits without
    propagating a value out.
    """
    profile = _profile(
        _keyed_register_facts() + [_fact("dfg", "return_output")]
    )
    classifier = ContainerKindClassifier(NullGraphProbe())
    assert "keyed_register_callable" not in {
        m.kind for m in classifier.classify(profile)
    }


# ---------------------------------------------------------------------------
# keyed_dispatch_callable
# ---------------------------------------------------------------------------


def _keyed_dispatch_facts() -> list[AxisFact]:
    """The minimum axis fingerprint of a registry-keyed dispatcher
    (Flask ``dispatch_request``-style)."""
    return [
        _fact("dfg", "subscript_read"),
        _fact("dfg", "keyed_read"),
        _fact("dfg", "container_read_key"),
        _fact("dfg", "callable_value"),
        _fact("cfg", "value_call"),
    ]


def test_keyed_dispatch_callable_fires_on_full_fingerprint():
    profile = _profile(_keyed_dispatch_facts())
    classifier = ContainerKindClassifier(NullGraphProbe())
    kinds = {m.kind for m in classifier.classify(profile)}
    assert "keyed_dispatch_callable" in kinds


def test_keyed_dispatch_callable_rejects_when_iterating():
    """Discriminator vs middleware_chain: keyed dispatch picks ONE
    callable by key; middleware iterates the container. If
    ``iteration_source`` is present in the same callable body, the
    classifier must NOT call this a keyed dispatcher.
    """
    profile = _profile(
        _keyed_dispatch_facts() + [_fact("dfg", "iteration_source")]
    )
    classifier = ContainerKindClassifier(NullGraphProbe())
    kinds = {m.kind for m in classifier.classify(profile)}
    assert "keyed_dispatch_callable" not in kinds


def test_keyed_dispatch_callable_requires_subscript_read():
    facts = [f for f in _keyed_dispatch_facts() if f.bit != "subscript_read"]
    profile = _profile(facts)
    classifier = ContainerKindClassifier(NullGraphProbe())
    assert "keyed_dispatch_callable" not in {
        m.kind for m in classifier.classify(profile)
    }


def test_keyed_dispatch_callable_requires_callable_value():
    """Without ``callable_value`` the subscript-read is just a data
    lookup, not a dispatch. The classifier must distinguish."""
    facts = [f for f in _keyed_dispatch_facts() if f.bit != "callable_value"]
    profile = _profile(facts)
    classifier = ContainerKindClassifier(NullGraphProbe())
    assert "keyed_dispatch_callable" not in {
        m.kind for m in classifier.classify(profile)
    }


def test_keyed_dispatch_callable_requires_value_call():
    facts = [f for f in _keyed_dispatch_facts() if f.bit != "value_call"]
    profile = _profile(facts)
    classifier = ContainerKindClassifier(NullGraphProbe())
    assert "keyed_dispatch_callable" not in {
        m.kind for m in classifier.classify(profile)
    }


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
