from sidecar.context.role_taxonomy import infer_supporting_roles


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
