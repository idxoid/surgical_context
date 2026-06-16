"""L4 role resolver — contracts → roles."""

from __future__ import annotations

import pytest

from sidecar.axis.contract_compiler import AxisContractCompiler
from sidecar.axis.role_resolver import (
    ROLE_CONTRACT_MAP,
    registered_contracts,
    registered_roles,
    resolve_roles,
    resolve_roles_with_evidence,
)


def test_every_role_contract_reference_is_a_real_contract():
    """Catches typos / drift between the role map and the contract
    compiler. Any role pointing at a contract the compiler doesn't
    register is unprovable and therefore a no-op — flag it instead of
    silently shipping."""
    valid = set(AxisContractCompiler().registered_contracts())
    missing = registered_contracts() - valid
    assert not missing, (
        f"role map references contracts that aren't registered in the "
        f"contract compiler: {sorted(missing)}. Either register them or "
        "remove them from ROLE_CONTRACT_MAP."
    )


def test_resolve_roles_is_empty_when_no_contracts_proven():
    assert resolve_roles([]) == set()
    assert resolve_roles_with_evidence([]) == []


def test_route_register_binding_satisfies_routing_and_binding_surface():
    roles = resolve_roles(["route_register_binding"])
    # Should satisfy the specific routing role AND the broad
    # binding_surface umbrella role.
    assert {"routing_surface", "binding_surface"} <= roles


def test_dependency_injection_binding_satisfies_dependency_solver():
    roles = resolve_roles(["dependency_injection_binding"])
    assert "dependency_solver" in roles
    assert "binding_surface" in roles
    assert "routing_surface" not in roles


def test_resolve_roles_with_evidence_carries_satisfying_contracts():
    resolutions = resolve_roles_with_evidence(
        ["route_register_binding", "registry_binding_inferred"],
    )
    by_role = {r.role: r for r in resolutions}
    # binding_surface satisfied by both inputs.
    assert set(by_role["binding_surface"].satisfying_contracts) == {
        "route_register_binding",
        "registry_binding_inferred",
    }
    # routing_surface satisfied only by route_register_binding.
    assert by_role["routing_surface"].satisfying_contracts == ("route_register_binding",)


@pytest.mark.parametrize(
    "contract, expected_roles",
    [
        ("proxy_indirection", {"proxy_mechanism", "binding_surface"}),
        ("data_shape_declaration", {"data_model_surface"}),
        ("configuration_carrier", {"configuration_surface"}),
        ("metadata_key_roundtrip", {"metadata_surface", "binding_surface"}),
        (
            "callable_container_dispatch",
            {"dispatch_surface", "binding_surface"},
        ),
        ("task_register_binding", {"task_surface", "binding_surface"}),
        ("error_dispatch_binding", {"error_surface", "binding_surface"}),
    ],
)
def test_single_contract_satisfies_expected_role_set(
    contract: str, expected_roles: set[str]
) -> None:
    assert resolve_roles([contract]) == expected_roles


def test_unknown_contract_satisfies_nothing():
    assert resolve_roles(["definitely_not_a_real_contract"]) == set()


def test_registered_roles_match_map_keys():
    assert registered_roles() == set(ROLE_CONTRACT_MAP)
