from sidecar.context.mechanism_registry import (
    determine_preloaded_mechanism,
    known_mechanisms,
    required_roles_for_mechanism,
)
from sidecar.context.types import SubgraphNode


def _target(name: str, file_path: str = "/repo/src/main.py") -> SubgraphNode:
    return SubgraphNode(
        uid=f"u:{name}",
        name=name,
        file_path=file_path,
        range=[1, 10],
        token_estimate=80,
        relation="target",
        direction="primary",
        depth=0,
        relevance_score=1.0,
        kind="function",
    )


def test_preloaded_registry_matches_known_framework_mechanisms():
    assert (
        determine_preloaded_mechanism(
            _target("FastAPI", "/repo/fastapi/applications.py"),
            "How does FastAPI register path operations?",
        )
        == "fastapi_route_registration"
    )
    assert (
        determine_preloaded_mechanism(
            _target("BaseModel", "/repo/pydantic/main.py"),
            "How does BaseModel validation flow work?",
        )
        == "pydantic_validation_core_bridge"
    )
    assert (
        determine_preloaded_mechanism(
            _target("createApi", "/repo/packages/toolkit/src/query/createApi.ts"),
            "How does RTK Query define an API slice and connect generated endpoints into the store?",
        )
        == "api_store_integration_pipeline"
    )


def test_preloaded_registry_keeps_query_specific_splits():
    assert (
        determine_preloaded_mechanism(
            _target("model_dump", "/repo/pydantic/main.py"),
            "Which pure Python wrappers rely on pydantic-core?",
        )
        == "pydantic_python_core_boundary"
    )
    assert (
        determine_preloaded_mechanism(
            _target("model_dump", "/repo/pydantic/main.py"),
            "How does serialization return output?",
        )
        == "pydantic_serialization_bridge"
    )
    assert (
        determine_preloaded_mechanism(
            _target("serialize_response", "/repo/fastapi/routing.py"),
            "If serialization changes, what tests break?",
        )
        == "fastapi_serialization_impact"
    )


def test_preloaded_registry_exposes_required_roles_without_fallback_roles():
    roles = required_roles_for_mechanism("fastapi_openapi_generation")

    assert roles == ["api_surface", "schema_builder", "factory_surface"]
    assert "executor" not in roles
    assert "runtime_surface" not in roles
    assert "fastapi_openapi_generation" in known_mechanisms()


def test_preloaded_registry_returns_empty_for_unknown_codebase():
    assert determine_preloaded_mechanism(_target("Router"), "How does middleware execute?") == ""
    assert required_roles_for_mechanism("unknown") == []
