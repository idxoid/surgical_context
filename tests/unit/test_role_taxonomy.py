from sidecar.context.role_taxonomy import infer_supporting_roles


def test_fastapi_path_does_not_claim_representation_from_ast_substring():
    roles = infer_supporting_roles(
        file_path="/repo/fastapi/fastapi/param_functions.py",
        primary_role="api_surface",
        name="Depends",
        kind="function",
    )

    assert "representation_surface" not in roles


def test_models_path_claims_representation_surface():
    roles = infer_supporting_roles(
        file_path="/repo/fastapi/fastapi/dependencies/models.py",
        primary_role="core_runtime",
        name="Dependant",
        kind="class",
    )

    assert "representation_surface" in roles
