from sidecar.context.role_taxonomy import infer_supporting_roles, normalize_role


def test_f5_module_composition_maps_to_composition_surface():
    # `module_composition` / `gateway` aliases were removed after the pack was
    # rewritten to use precise cascade roles directly; unknown aliases now
    # canonicalize-as-self. The surviving aliases still map correctly.
    assert normalize_role("store_integration") == "composition_surface"
    assert normalize_role("integration_surface") == "integration_surface"


def test_f6_registry_aliases_disambiguate_by_sense():
    # `handler_registry` / `state_registry` aliases were removed after the pack
    # was rewritten; surviving `*_registry` aliases still disambiguate by sense.
    assert normalize_role("route_registry") == "registration_step"
    assert normalize_role("provider_registry") == "orchestrator"
    assert normalize_role("module_registry") == "composition_surface"
    assert normalize_role("metadata_registry") == "runtime_surface"


def test_no_name_pattern_role_inference():
    """Role inference is structural only — a symbol name never drives a role.

    ``Depends`` in param_functions.py used to be force-mapped to config/representation
    by name tables; with name-pattern inference removed it claims neither.
    """
    roles = infer_supporting_roles(
        file_path="/repo/fastapi/fastapi/param_functions.py",
        primary_role="api_surface",
        name="Depends",
        kind="function",
    )

    assert "representation_surface" not in roles
    assert "config_surface" not in roles


def test_test_path_claims_impact_test_surface():
    """Structural path fact (location), not a name pattern."""
    roles = infer_supporting_roles(
        file_path="/repo/app/tests/test_thing.py",
        primary_role="runtime_surface",
        name="anything",
        kind="function",
    )

    assert "impact_test_surface" in roles
