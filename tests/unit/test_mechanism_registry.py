import json

from sidecar.context.mechanism_registry import (
    ROLE_CATALOG_MECHANISM_BACKFILL_KEY,
    ROLE_CATALOG_MECHANISM_REQUIRED_ROLES_KEY,
    determine_preloaded_mechanism,
    known_mechanisms,
    merge_preloaded_mechanisms_into_role_catalog,
    pick_mechanism_by_role_overlap,
    preloaded_mechanism_catalog_extensions,
    required_roles_for_mechanism,
    role_backfill_specs_for_mechanism,
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


def test_preloaded_dispatch_stub_returns_empty_for_framework_like_symbols():
    assert (
        determine_preloaded_mechanism(
            _target("FastAPI", "/repo/fastapi/applications.py"),
            "How does FastAPI register path operations?",
        )
        == ""
    )
    assert (
        determine_preloaded_mechanism(
            _target("BaseModel", "/repo/pydantic/main.py"),
            "How does BaseModel validation flow work?",
        )
        == ""
    )
    assert (
        determine_preloaded_mechanism(
            _target("createApi", "/repo/packages/toolkit/src/query/createApi.ts"),
            "How does RTK Query define an API slice and connect generated endpoints into the store?",
        )
        == ""
    )


def test_builtin_required_roles_and_known_mechanisms_are_empty():
    assert required_roles_for_mechanism("fastapi_openapi_generation") == []
    assert known_mechanisms() == ()


def test_known_mechanisms_includes_catalog_overlay_keys():
    catalog = {
        ROLE_CATALOG_MECHANISM_REQUIRED_ROLES_KEY: {"custom_mech": ["api_surface"]},
        ROLE_CATALOG_MECHANISM_BACKFILL_KEY: {"other_mech": {}},
    }
    assert set(known_mechanisms(role_catalog=catalog)) == {"custom_mech", "other_mech"}


def test_preloaded_registry_returns_empty_for_unknown_codebase():
    assert determine_preloaded_mechanism(_target("Router"), "How does middleware execute?") == ""
    assert required_roles_for_mechanism("unknown") == []


def test_role_catalog_overrides_required_roles():
    catalog = {
        ROLE_CATALOG_MECHANISM_REQUIRED_ROLES_KEY: {
            "fastapi_openapi_generation": ["executor", "runtime_surface"],
        }
    }
    roles = required_roles_for_mechanism(
        "fastapi_openapi_generation",
        role_catalog=catalog,
    )
    assert roles == ["executor", "runtime_surface"]
    assert (
        required_roles_for_mechanism(
            "fastapi_openapi_generation",
            role_catalog={ROLE_CATALOG_MECHANISM_REQUIRED_ROLES_KEY: {}},
        )
        == []
    )


def test_role_catalog_overrides_backfill_specs():
    catalog = {
        ROLE_CATALOG_MECHANISM_BACKFILL_KEY: {
            "fastapi_endpoint_execution": {
                "executor": [
                    {"name": "custom_runner", "path_hint": "/repo/run.py", "priority": 1.0},
                ],
            },
        }
    }
    specs = role_backfill_specs_for_mechanism(
        "fastapi_endpoint_execution",
        role_catalog=catalog,
    )
    assert specs["executor"][0]["name"] == "custom_runner"
    assert specs["executor"][0]["path_hint"] == "/repo/run.py"
    builtin = role_backfill_specs_for_mechanism(
        "fastapi_endpoint_execution",
        role_catalog={ROLE_CATALOG_MECHANISM_BACKFILL_KEY: {}},
    )
    assert builtin == {}


def test_builtin_backfill_specs_include_auto_registration_flow():
    specs = role_backfill_specs_for_mechanism("auto:registration_flow")
    assert "factory_surface" in specs
    assert any(row["name"] == "register_blueprint" for row in specs["factory_surface"])
    assert "runtime_surface" in specs
    assert any(row["name"] == "wsgi_app" for row in specs["runtime_surface"])


def test_pick_mechanism_by_role_overlap_requires_two_distinct_roles():
    assert pick_mechanism_by_role_overlap(["executor"]) == ""
    assert pick_mechanism_by_role_overlap(["executor", "executor"]) == ""


def test_pick_mechanism_by_role_overlap_matches_catalog_template():
    catalog = {
        ROLE_CATALOG_MECHANISM_REQUIRED_ROLES_KEY: {
            "fastapi_endpoint_execution": ["executor", "runtime_surface"],
        }
    }
    mech = pick_mechanism_by_role_overlap(
        {"executor", "runtime_surface", "api_surface"},
        target_role="executor",
        role_catalog=catalog,
        min_score=0.41,
    )
    assert mech == "fastapi_endpoint_execution"


def test_preloaded_mechanism_catalog_extensions_are_json_serializable():
    ext = preloaded_mechanism_catalog_extensions()
    raw = json.dumps(ext, sort_keys=True)
    loaded = json.loads(raw)
    merged = merge_preloaded_mechanisms_into_role_catalog({"schema_version": 2})
    assert merged["schema_version"] == 2
    assert (
        merged[ROLE_CATALOG_MECHANISM_REQUIRED_ROLES_KEY]
        == loaded[ROLE_CATALOG_MECHANISM_REQUIRED_ROLES_KEY]
    )
