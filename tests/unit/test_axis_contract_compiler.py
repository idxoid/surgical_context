import pytest

from sidecar.axis.container_kind import ContainerKindClassifier, ContainerKindMatch
from sidecar.axis.contract_compiler import (
    AxisContractCompiler,
    AxisContractMatch,
    container_kind_matches_from_json,
)
from sidecar.axis.schema import AxisExtraction, AxisFact, AxisProfile


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

    assert contracts == []


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


def test_proxy_object_compiles_to_marker_backed_indirection_contract():
    profile = _profile([_fact("struct", "module_scope")], uid="u:p", qn="pkg.proxy")
    marker = ContainerKindMatch(
        kind="proxy_object",
        symbol_uid="u:p",
        qualified_name="pkg.proxy",
        evidence_bits=(),
        evidence_probes=("library_marker:proxy_object",),
        payload={"via": "library_marker"},
    )

    contracts = AxisContractCompiler().compile(profile, [marker])

    assert [c.contract for c in contracts] == ["proxy_indirection"]
    assert contracts[0].required_bits == ()
    assert contracts[0].to_query_request().container_kinds == ("proxy_object",)


def test_static_data_and_config_contracts_do_not_have_traversal_mode():
    profile = _profile(
        [
            _fact("struct", "class_def", kind="class"),
            _fact("struct", "class_attribute", kind="class", payload={"annotation": "str", "value": '"x"'}),
            _fact("struct", "class_attribute", kind="class", payload={"annotation": "int", "value": "1"}),
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


def test_container_kind_matches_from_json_round_trips_persisted_matches():
    raw = (
        "[{\"kind\": \"metadata_carrier\", \"symbol_uid\": \"u:x\", "
        "\"qualified_name\": \"pkg.x\", "
        "\"evidence_bits\": [[\"dfg\", \"keyed_write\"]], "
        "\"evidence_probes\": [], \"payload\": {\"shared_key_count\": 1}}]"
    )

    matches = container_kind_matches_from_json(raw)

    assert len(matches) == 1
    assert matches[0].kind == "metadata_carrier"
    assert matches[0].evidence_bits == (("dfg", "keyed_write"),)
    assert matches[0].payload == {"shared_key_count": 1}


def test_container_kind_matches_from_json_ignores_invalid_payload():
    assert container_kind_matches_from_json("{bad") == []
    assert container_kind_matches_from_json("[{}]") == []
