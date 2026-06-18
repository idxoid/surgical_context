import pytest

from context_engine.axis.container_kind import ContainerKindClassifier, ContainerKindMatch
from context_engine.axis.contract_compiler import (
    AxisContractCompiler,
    AxisContractMatch,
    container_kind_matches_from_json,
)
from context_engine.axis.schema import AxisExtraction, AxisFact, AxisProfile


def _fact(
    axis: str,
    bit: str,
    *,
    uid="u:x",
    qn="pkg.x",
    kind="function",
    line=1,
    payload=None,
) -> AxisFact:
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


def _profile(facts: list[AxisFact], *, uid="u:x", qn="pkg.x", kind="function") -> AxisProfile:
    ext = AxisExtraction(file_path="<synthetic>", facts=facts)
    return ext.profiles.get(uid) or AxisProfile(
        symbol_uid=uid,
        qualified_name=qn,
        symbol_kind=kind,
    )


def _contracts(profile: AxisProfile) -> list[AxisContractMatch]:
    kind_matches = ContainerKindClassifier().classify(profile)
    return AxisContractCompiler().compile(profile, kind_matches)


def test_metadata_carrier_compiles_to_key_roundtrip_contract():
    profile = _profile(
        [
            _fact("dfg", "keyed_write", payload={"key": "k"}),
            _fact("dfg", "keyed_read", payload={"key": "k"}),
            _fact("struct", "literal_key"),
        ]
    )

    contracts = _contracts(profile)
    match = next(c for c in contracts if c.contract == "metadata_key_roundtrip")
    request = match.to_query_request(limit=7)

    assert match.container_kind == "metadata_carrier"
    assert request.traversal_mode == "deferred_binding_flow"
    assert request.container_kinds == ("metadata_carrier",)
    assert request.limit == 7
    assert {(req.axis, req.bit) for req in request.required_bits} == {
        ("dfg", "keyed_write"),
        ("dfg", "keyed_read"),
        ("struct", "literal_key"),
    }


def test_callable_chain_compiles_to_container_dispatch_contract():
    profile = _profile(
        [
            _fact("dfg", "callable_value"),
            _fact("dfg", "container_write_value", payload={"container": "self.chain"}),
            _fact("dfg", "iteration_source", payload={"iterable": "self.chain"}),
            _fact("cfg", "value_call"),
        ]
    )

    contracts = _contracts(profile)

    assert [c.contract for c in contracts] == ["callable_container_dispatch"]
    assert contracts[0].container_kind == "middleware_chain"
    assert contracts[0].payload["container"] == "self.chain"


def test_callable_chain_without_shared_container_stays_unproven():
    profile = _profile(
        [
            _fact("dfg", "callable_value"),
            _fact("dfg", "container_write_value", payload={"container": "self.writes"}),
            _fact("dfg", "iteration_source", payload={"iterable": "self.reads"}),
            _fact("cfg", "value_call"),
        ]
    )

    contracts = _contracts(profile)
    diagnostics = AxisContractCompiler().diagnose(
        profile,
        ContainerKindClassifier().classify(profile),
    )

    assert contracts == []
    assert [item.contract for item in diagnostics] == ["callable_container_dispatch"]
    assert diagnostics[0].missing == (
        "payload_identity:container_write_value.container==iteration_source.iterable",
    )


def test_marker_only_signal_kind_does_not_fake_dispatch_contract():
    profile = _profile([_fact("struct", "function_def")])
    marker = ContainerKindMatch(
        kind="signal_register",
        symbol_uid=profile.symbol_uid,
        qualified_name=profile.qualified_name,
        evidence_bits=(
            ("dfg", "container_write_value"),
            ("dfg", "iteration_source"),
            ("cfg", "value_call"),
        ),
        evidence_probes=("library_marker:signal_register",),
        payload={"via": "library_marker"},
    )

    contracts = AxisContractCompiler().compile(profile, [marker])

    assert contracts == []


def test_di_container_compiles_to_provider_default_binding_contract():
    profile = _profile(
        [
            _fact("struct", "function_def"),
            _fact("struct", "parameter_default", payload={"default_kind": "Call"}),
            _fact("dfg", "parameter_default_value"),
            _fact("dfg", "callable_value"),
        ]
    )

    contracts = _contracts(profile)

    assert [c.contract for c in contracts] == ["provider_default_binding"]
    assert contracts[0].to_query_request().container_kinds == ("di_container",)


def test_proxy_object_compiles_to_topology_backed_indirection_contract():
    profile = _profile([_fact("struct", "module_scope")], uid="u:p", qn="pkg.proxy")
    marker = ContainerKindMatch(
        kind="proxy_object",
        symbol_uid="u:p",
        qualified_name="pkg.proxy",
        evidence_bits=(),
        evidence_probes=("graph_context:proxy_topology",),
        payload={"via": "proxy_topology"},
    )

    contracts = AxisContractCompiler().compile(profile, [marker])

    assert [c.contract for c in contracts] == ["proxy_indirection"]
    assert contracts[0].required_bits == ()
    assert contracts[0].to_query_request().container_kinds == ("proxy_object",)


def test_static_data_and_config_contracts_do_not_have_traversal_mode():
    profile = _profile(
        [
            _fact("struct", "class_def", kind="class"),
            _fact(
                "struct",
                "class_attribute",
                kind="class",
                payload={"annotation": "str", "value": '"x"'},
            ),
            _fact(
                "struct",
                "class_attribute",
                kind="class",
                payload={"annotation": "int", "value": "1"},
            ),
            _fact("struct", "annotation", kind="class"),
        ],
        kind="class",
    )

    contracts = _contracts(profile)
    by_name = {contract.contract: contract for contract in contracts}

    assert {"data_shape_declaration", "configuration_carrier"} <= set(by_name)
    assert by_name["data_shape_declaration"].traversal_mode is None
    with pytest.raises(ValueError, match="no traversal mode"):
        by_name["configuration_carrier"].to_query_request()


def test_route_register_marker_alone_does_not_prove_route_register_binding():
    profile = _profile(
        [_fact("struct", "module_scope")], uid="u:app", qn="myapp.app", kind="variable"
    )
    marker = ContainerKindMatch(
        kind="web_route_register",
        symbol_uid="u:app",
        qualified_name="myapp.app",
        evidence_bits=(),
        evidence_probes=("library_marker:web_route_register",),
        payload={},
    )
    # Catalogue match without a single registered handler — the kind is
    # present but the contract must stay unproven (marker-only does not
    # prove use).
    contracts = AxisContractCompiler().compile(profile, [marker])
    assert contracts == []

    diagnostics = AxisContractCompiler().diagnose(profile, [marker])
    assert any(d.contract == "route_register_binding" for d in diagnostics)


def test_route_register_binding_proves_when_handler_registered_via_handles_edge():
    profile = _profile(
        [
            _fact("struct", "module_scope", uid="u:app", qn="myapp.app", kind="variable"),
            _fact(
                "dfg",
                "registered_callable",
                uid="u:app",
                qn="myapp.app",
                kind="variable",
                payload={"count": 3},
            ),
        ],
        uid="u:app",
        qn="myapp.app",
        kind="variable",
    )
    marker = ContainerKindMatch(
        kind="web_route_register",
        symbol_uid="u:app",
        qualified_name="myapp.app",
        evidence_bits=(),
        evidence_probes=("library_marker:web_route_register",),
        payload={},
    )

    contracts = AxisContractCompiler().compile(profile, [marker])

    assert [c.contract for c in contracts] == ["route_register_binding"]
    request = contracts[0].to_query_request()
    assert request.container_kinds == ("web_route_register",)
    assert request.traversal_mode == "deferred_binding_flow"


def test_task_register_binding_uses_same_use_proof():
    profile = _profile(
        [
            _fact("dfg", "registered_callable", uid="u:app", qn="myapp.celery", kind="variable"),
        ],
        uid="u:app",
        qn="myapp.celery",
        kind="variable",
    )
    marker = ContainerKindMatch(
        kind="task_register",
        symbol_uid="u:app",
        qualified_name="myapp.celery",
        evidence_bits=(),
        evidence_probes=("library_marker:task_register",),
        payload={},
    )

    contracts = AxisContractCompiler().compile(profile, [marker])

    assert [c.contract for c in contracts] == ["task_register_binding"]


def test_registry_binding_inferred_fires_on_consumer_derived_variable():
    """A Variable Symbol with the consumer-derived ``registry_class`` kind
    AND ``dfg.registered_callable`` proves the generic
    ``registry_binding_inferred`` contract — no catalogue entry required.
    Allows L3 retrieval over libraries the catalogue does not enumerate.
    """
    profile = _profile(
        [
            _fact(
                "dfg",
                "registered_callable",
                uid="u:app",
                qn="myapp.app",
                kind="variable",
                payload={"count": 4},
            ),
        ],
        uid="u:app",
        qn="myapp.app",
        kind="variable",
    )
    marker = ContainerKindMatch(
        kind="registry_class",
        symbol_uid="u:app",
        qualified_name="myapp.app",
        evidence_bits=(("dfg", "registered_callable"),),
        evidence_probes=("consumer_derived:registered_handler_count=4",),
        payload={"registered_callable_count": 4},
    )

    contracts = AxisContractCompiler().compile(profile, [marker])

    names = {c.contract for c in contracts}
    assert "registry_binding_inferred" in names


def test_registry_binding_inferred_does_not_fire_without_registered_callable():
    """``registry_class`` alone is not enough — the structural USE proof
    (decorator-bound HANDLES out edges → ``dfg.registered_callable``) is
    required for the inferred contract to prove.
    """
    profile = _profile(
        [_fact("struct", "class_def", uid="u:c", qn="pkg.C", kind="class")],
        uid="u:c",
        qn="pkg.C",
        kind="class",
    )
    marker = ContainerKindMatch(
        kind="registry_class",
        symbol_uid="u:c",
        qualified_name="pkg.C",
        evidence_bits=(("struct", "class_def"),),
        evidence_probes=("peer_method_kinds:metadata_carrier",),
        payload={"registry_method_kinds": ["metadata_carrier"]},
    )

    contracts = AxisContractCompiler().compile(profile, [marker])

    assert "registry_binding_inferred" not in {c.contract for c in contracts}


def test_registry_binding_inferred_coexists_with_catalogue_subtype_contract():
    """When the catalogue knows the subtype (e.g. web_route_register),
    both contracts must fire: the subtype-specific one and the generic
    inferred one. They are not mutually exclusive.
    """
    profile = _profile(
        [
            _fact("dfg", "registered_callable", uid="u:app", qn="myapp.app", kind="variable"),
        ],
        uid="u:app",
        qn="myapp.app",
        kind="variable",
    )
    matches = [
        ContainerKindMatch(
            kind="web_route_register",
            symbol_uid="u:app",
            qualified_name="myapp.app",
            evidence_bits=(),
            evidence_probes=("library_marker:web_route_register",),
            payload={},
        ),
        ContainerKindMatch(
            kind="registry_class",
            symbol_uid="u:app",
            qualified_name="myapp.app",
            evidence_bits=(("dfg", "registered_callable"),),
            evidence_probes=("consumer_derived:registered_handler_count=2",),
            payload={"registered_callable_count": 2},
        ),
    ]

    contracts = AxisContractCompiler().compile(profile, matches)
    names = {c.contract for c in contracts}

    assert {"route_register_binding", "registry_binding_inferred"} <= names


def test_dependency_injection_binding_requires_cross_symbol_injected_dependency_fact():
    """The shape-only ``provider_default_binding`` still fires on a
    consumer with the right axis bits, but ``dependency_injection_binding``
    additionally requires the cross-symbol DFG proof emitted by the
    pipeline when ``outgoing_injects_count > 0``. Without the wiring
    proof, only the shape contract fires; with it, both coexist.
    """
    base_facts = [
        _fact("struct", "function_def"),
        _fact("struct", "parameter_default", payload={"default_kind": "Call"}),
        _fact("dfg", "parameter_default_value"),
        _fact("dfg", "callable_value"),
    ]
    shape_only = _profile(base_facts)
    contracts_no_inject = {c.contract for c in _contracts(shape_only)}
    assert "provider_default_binding" in contracts_no_inject
    assert "dependency_injection_binding" not in contracts_no_inject

    wired = _profile(base_facts + [_fact("dfg", "injected_dependency", payload={"count": 2})])
    contracts_wired = {c.contract for c in _contracts(wired)}
    assert "provider_default_binding" in contracts_wired
    assert "dependency_injection_binding" in contracts_wired


def test_container_kind_matches_from_json_round_trips_persisted_matches():
    raw = (
        '[{"kind": "metadata_carrier", "symbol_uid": "u:x", '
        '"qualified_name": "pkg.x", '
        '"evidence_bits": [["dfg", "keyed_write"]], '
        '"evidence_probes": [], "payload": {"shared_key_count": 1}}]'
    )

    matches = container_kind_matches_from_json(raw)

    assert len(matches) == 1
    assert matches[0].kind == "metadata_carrier"
    assert matches[0].evidence_bits == (("dfg", "keyed_write"),)
    assert matches[0].payload == {"shared_key_count": 1}


def test_container_kind_matches_from_json_ignores_invalid_payload():
    assert container_kind_matches_from_json("{bad") == []
    assert container_kind_matches_from_json("[{}]") == []
