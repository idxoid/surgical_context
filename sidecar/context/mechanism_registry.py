"""Preloaded mechanism profiles for known framework families.

The ranker should not learn FastAPI/Pydantic/RTK behavior from benchmark
questions. This registry keeps known mechanism profiles in one replaceable
place, while repository-derived strategy profiles remain the generic fallback
for unknown codebases.

Design artifact, not dead code: keep this module until query-time ranking
consumes the indexer-produced role catalog (see ``docs/spec_indexer.md``).
Do not delete as cleanup-only churn.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from sidecar.context.role_taxonomy import normalize_roles
from sidecar.context.types import SubgraphNode


@dataclass(frozen=True)
class MechanismContext:
    target: SubgraphNode
    name: str
    query: str
    file_path: str


@dataclass(frozen=True)
class MechanismRule:
    mechanism: str
    match: Callable[[MechanismContext], bool]


_REQUIRED_ROLES: dict[str, tuple[str, ...]] = {
    "fastapi_route_registration": (
        "api_surface",
        "factory_surface",
        "representation_surface",
        "runtime_surface",
    ),
    "fastapi_dependency_injection": (
        "api_surface",
        "config_surface",
        "representation_surface",
        "orchestrator",
        "runtime_surface",
    ),
    "fastapi_request_body_dependency_resolution": (
        "runtime_surface",
        "schema_builder",
        "orchestrator",
        "binding_surface",
    ),
    "fastapi_endpoint_execution": ("executor", "runtime_surface"),
    "fastapi_serialization_impact": (
        "impact_runtime",
        "impact_public_api",
        "impact_test_surface",
    ),
    "fastapi_openapi_generation": (
        "api_surface",
        "schema_builder",
        "factory_surface",
    ),
    "pydantic_validation_core_bridge": (
        "api_surface",
        "construction_surface",
        "runtime_surface",
        "validator_handle",
        "core_runtime",
        "orchestrator",
        "executor",
    ),
    "pydantic_python_core_boundary": (
        "api_surface",
        "validator_handle",
        "serializer_handle",
        "core_runtime",
    ),
    "pydantic_serialization_bridge": (
        "api_surface",
        "serializer_handle",
        "core_runtime",
    ),
    "pydantic_json_schema_generation": (
        "api_surface",
        "schema_builder",
        "representation_surface",
    ),
    "pydantic_v1_compat_surface": (
        "api_surface",
        "compat_bridge",
        "docs_or_concept",
    ),
    "pydantic_alias_impact": (
        "impact_runtime",
        "impact_public_api",
        "impact_test_surface",
    ),
    "pydantic_validation_error_assembly": (
        "api_surface",
        "core_runtime",
        "error_surface",
    ),
    "state_factory_pipeline": (
        "api_surface",
        "factory_surface",
        "composition_surface",
    ),
    "listener_orchestration_pipeline": (
        "api_surface",
        "orchestrator",
        "executor",
    ),
    "runtime_configuration_pipeline": (
        "api_surface",
        "composition_surface",
        "config_surface",
    ),
    "async_lifecycle_pipeline": (
        "api_surface",
        "factory_surface",
        "executor",
        "error_surface",
    ),
    "api_store_integration_pipeline": (
        "api_surface",
        "representation_surface",
        "integration_surface",
    ),
    "workspace_structure": (
        "api_surface",
        "core_runtime",
        "docs_or_concept",
        "supporting_surface",
    ),
}


_ROLE_BACKFILL_SPECS: dict[str, dict[str, list[dict[str, str | float]]]] = {
    "fastapi_route_registration": {
        "factory_surface": [
            {"name": "add_api_route", "path_hint": "/fastapi/applications.py", "priority": 1.0},
            {"name": "api_route", "path_hint": "/fastapi/applications.py", "priority": 0.9},
            {"name": "add_api_route", "path_hint": "/fastapi/routing.py", "priority": 0.8},
        ],
        "representation_surface": [
            {"name": "APIRoute", "path_hint": "/fastapi/routing.py", "priority": 1.0},
        ],
        "runtime_surface": [
            {"name": "get_request_handler", "path_hint": "/fastapi/routing.py", "priority": 1.0},
        ],
    },
    "fastapi_dependency_injection": {
        "config_surface": [
            {"name": "Depends", "path_hint": "/fastapi/params.py", "priority": 1.0},
            {"name": "Security", "path_hint": "/fastapi/params.py", "priority": 0.7},
        ],
        "representation_surface": [
            {"name": "Dependant", "path_hint": "/fastapi/dependencies/models.py", "priority": 1.0},
            {"name": "get_dependant", "path_hint": "/fastapi/dependencies/utils.py", "priority": 0.95},
            {"name": "get_flat_dependant", "path_hint": "/fastapi/dependencies/utils.py", "priority": 0.8},
        ],
        "orchestrator": [
            {"name": "solve_dependencies", "path_hint": "/fastapi/dependencies/utils.py", "priority": 1.0},
        ],
        "runtime_surface": [
            {"name": "get_request_handler", "path_hint": "/fastapi/routing.py", "priority": 1.0},
        ],
    },
    "fastapi_request_body_dependency_resolution": {
        "schema_builder": [
            {"name": "get_body_field", "path_hint": "/fastapi/dependencies/utils.py", "priority": 1.0},
        ],
        "orchestrator": [
            {"name": "solve_dependencies", "path_hint": "/fastapi/dependencies/utils.py", "priority": 0.95},
        ],
        "runtime_surface": [
            {"name": "get_request_handler", "path_hint": "/fastapi/routing.py", "priority": 1.0},
        ],
        "binding_surface": [
            {"name": "request_body_to_args", "path_hint": "/fastapi/dependencies/utils.py", "priority": 1.0},
        ],
    },
    "fastapi_endpoint_execution": {
        "executor": [
            {"name": "run_endpoint_function", "path_hint": "/fastapi/routing.py", "priority": 1.0},
        ],
        "runtime_surface": [
            {"name": "get_request_handler", "path_hint": "/fastapi/routing.py", "priority": 0.95},
        ],
    },
    "fastapi_serialization_impact": {
        "impact_runtime": [
            {"name": "serialize_response", "path_hint": "/fastapi/routing.py", "priority": 1.0},
            {"name": "get_request_handler", "path_hint": "/fastapi/routing.py", "priority": 0.85},
        ],
        "impact_public_api": [
            {"name": "APIRoute", "path_hint": "/fastapi/routing.py", "priority": 1.0},
            {"name": "FastAPI", "path_hint": "/fastapi/applications.py", "priority": 0.8},
        ],
        "impact_test_surface": [
            {"name": "test_valid_exclude_unset", "path_hint": "/tests/test_serialize_response_model.py", "priority": 1.0},
            {"name": "test_no_response_model_object", "path_hint": "/tests/test_serialize_response_dataclass.py", "priority": 0.9},
            {"name": "test_response_validation_error_includes_endpoint_context", "path_hint": "/tests/test_validation_error_context.py", "priority": 0.85},
        ],
    },
    "fastapi_openapi_generation": {
        "api_surface": [
            {"name": "openapi", "path_hint": "/fastapi/applications.py", "priority": 1.0},
        ],
        "schema_builder": [
            {"name": "get_openapi", "path_hint": "/fastapi/openapi/utils.py", "priority": 1.0},
            {"name": "get_openapi_path", "path_hint": "/fastapi/openapi/utils.py", "priority": 0.85},
            {"name": "get_fields_from_routes", "path_hint": "/fastapi/openapi/utils.py", "priority": 0.8},
        ],
        "factory_surface": [
            {"name": "get_openapi_operation_metadata", "path_hint": "/fastapi/openapi/utils.py", "priority": 0.9},
        ],
    },
    "pydantic_validation_core_bridge": {
        "construction_surface": [
            {"name": "__init__", "path_hint": "/pydantic/main.py", "priority": 1.0},
        ],
        "runtime_surface": [
            {"name": "model_validate", "path_hint": "/pydantic/main.py", "priority": 1.0},
        ],
        "orchestrator": [
            {"name": "complete_model_class", "path_hint": "/pydantic/_internal/_model_construction.py", "priority": 1.0},
        ],
        "validator_handle": [
            {"name": "__pydantic_validator__", "path_hint": "/pydantic/main.py", "priority": 1.0},
        ],
        "core_runtime": [
            {"name": "SchemaValidator", "path_hint": "/pydantic-core/python/pydantic_core/_pydantic_core.pyi", "priority": 0.95},
        ],
        "executor": [
            {"name": "validate_python", "path_hint": "/pydantic-core/python/pydantic_core/_pydantic_core.pyi", "priority": 1.0},
            {"name": "validate_json", "path_hint": "/pydantic-core/python/pydantic_core/_pydantic_core.pyi", "priority": 0.95},
            {"name": "validate_strings", "path_hint": "/pydantic-core/python/pydantic_core/_pydantic_core.pyi", "priority": 0.95},
        ],
    },
    "pydantic_python_core_boundary": {
        "validator_handle": [
            {"name": "__pydantic_validator__", "path_hint": "/pydantic/main.py", "priority": 1.0},
        ],
        "serializer_handle": [
            {"name": "SchemaSerializer", "path_hint": "/pydantic-core/python/pydantic_core/_pydantic_core.pyi", "priority": 1.0},
        ],
        "core_runtime": [
            {"name": "SchemaValidator", "path_hint": "/pydantic-core/python/pydantic_core/_pydantic_core.pyi", "priority": 0.95},
        ],
    },
    "pydantic_serialization_bridge": {
        "serializer_handle": [
            {"name": "__pydantic_serializer__", "path_hint": "/pydantic/main.py", "priority": 1.0},
        ],
        "core_runtime": [
            {"name": "SchemaSerializer", "path_hint": "/pydantic-core/python/pydantic_core/_pydantic_core.pyi", "priority": 0.95},
        ],
    },
    "pydantic_json_schema_generation": {
        "schema_builder": [
            {"name": "GenerateJsonSchema", "path_hint": "/pydantic/json_schema.py", "priority": 1.0},
        ],
        "representation_surface": [
            {"name": "json_schema", "path_hint": "/pydantic/json_schema.py", "priority": 0.95},
        ],
    },
    "pydantic_v1_compat_surface": {
        "compat_bridge": [
            {"name": "v1", "path_hint": "/pydantic/__init__.py", "priority": 1.0},
        ],
        "api_surface": [
            {"name": "BaseModel", "path_hint": "/pydantic/v1/main.py", "priority": 0.9},
        ],
    },
    "pydantic_validation_error_assembly": {
        "api_surface": [
            {"name": "model_validate", "path_hint": "/pydantic/main.py", "priority": 1.0},
        ],
        "core_runtime": [
            {"name": "SchemaValidator", "path_hint": "/pydantic-core/python/pydantic_core/_pydantic_core.pyi", "priority": 0.95},
        ],
        "error_surface": [
            {"name": "ValidationError", "path_hint": "/pydantic-core/python/pydantic_core/_pydantic_core.pyi", "priority": 1.0},
        ],
    },
    "state_factory_pipeline": {
        "factory_surface": [
            {"name": "createAction", "path_hint": "/packages/toolkit/src/createAction.ts", "priority": 1.0},
            {"name": "createReducer", "path_hint": "/packages/toolkit/src/createReducer.ts", "priority": 0.95},
            {"name": "buildCreateSlice", "path_hint": "/packages/toolkit/src/createSlice.ts", "priority": 0.8},
        ],
        "composition_surface": [
            {"name": "createReducer", "path_hint": "/packages/toolkit/src/createReducer.ts", "priority": 0.9},
        ],
    },
    "runtime_configuration_pipeline": {
        "composition_surface": [
            {"name": "getDefaultMiddleware", "path_hint": "/packages/toolkit/src/getDefaultMiddleware.ts", "priority": 1.0},
            {"name": "getDefaultEnhancers", "path_hint": "/packages/toolkit/src/getDefaultEnhancers.ts", "priority": 0.95},
        ],
        "config_surface": [
            {"name": "composeWithDevTools", "path_hint": "/packages/toolkit/src/devtoolsExtension.ts", "priority": 0.95},
            {"name": "devToolsEnhancer", "path_hint": "/packages/toolkit/src/devtoolsExtension.ts", "priority": 0.9},
        ],
    },
    "async_lifecycle_pipeline": {
        "factory_surface": [
            {"name": "createAction", "path_hint": "/packages/toolkit/src/createAction.ts", "priority": 0.9},
        ],
        "executor": [
            {"name": "createAsyncThunk", "path_hint": "/packages/toolkit/src/createAsyncThunk.ts", "priority": 1.0},
        ],
    },
    "api_store_integration_pipeline": {
        "representation_surface": [
            {"name": "coreModule", "path_hint": "/packages/toolkit/src/query/core/module.ts", "priority": 1.0},
            {"name": "injectEndpoint", "path_hint": "/packages/toolkit/src/query/core/module.ts", "priority": 0.9},
        ],
        "integration_surface": [
            {"name": "buildCreateApi", "path_hint": "/packages/toolkit/src/query/createApi.ts", "priority": 1.0},
            {"name": "setupListeners", "path_hint": "/packages/toolkit/src/query/core/setupListeners.ts", "priority": 0.75},
        ],
    },
    "listener_orchestration_pipeline": {
        "orchestrator": [
            {"name": "addListener", "path_hint": "/packages/toolkit/src/listenerMiddleware/index.ts", "priority": 1.0},
            {"name": "createListenerEntry", "path_hint": "/packages/toolkit/src/listenerMiddleware/index.ts", "priority": 0.9},
        ],
        "executor": [
            {"name": "runTask", "path_hint": "/packages/toolkit/src/listenerMiddleware/task.ts", "priority": 1.0},
            {"name": "notifyListener", "path_hint": "/packages/toolkit/src/listenerMiddleware/index.ts", "priority": 0.85},
        ],
    },
}


def required_roles_for_mechanism(mechanism: str) -> list[str]:
    """Return registry roles for a known mechanism, or an empty list."""
    return normalize_roles(_REQUIRED_ROLES.get(mechanism, ()))


def determine_preloaded_mechanism(target: SubgraphNode, query: str = "") -> str:
    """Return the best preloaded mechanism for a target, if one matches."""
    context = MechanismContext(
        target=target,
        name=(target.name or "").lower(),
        query=(query or "").lower(),
        file_path=(target.file_path or "").lower(),
    )
    for rule in _RULES:
        if rule.match(context):
            return rule.mechanism
    return ""


def role_backfill_specs_for_mechanism(mechanism: str) -> dict[str, list[dict[str, str | float]]]:
    """Return registry role backfill specs for a known mechanism, or an empty dict."""
    return _ROLE_BACKFILL_SPECS.get(mechanism, {})


def known_mechanisms() -> tuple[str, ...]:
    return tuple(_REQUIRED_ROLES)


def _name_in(*names: str) -> Callable[[MechanismContext], bool]:
    return lambda c: c.name in names


def _query_has(c: MechanismContext, *phrases: str) -> bool:
    return any(phrase in c.query for phrase in phrases)


def _query_has_all(c: MechanismContext, *phrases: str) -> bool:
    return all(phrase in c.query for phrase in phrases)


def _pydantic_core_boundary_query(c: MechanismContext) -> bool:
    return _query_has(
        c,
        "pure python",
        "wrapper",
        "wrappers",
        "pydantic-core",
        "rely on pydantic-core",
    )


def _fastapi_serialization_impact(c: MechanismContext) -> bool:
    return c.name == "serialize_response" and _query_has(
        c,
        "affect",
        "break",
        "change",
        "impact",
        "test",
    )


def _workspace_structure(c: MechanismContext) -> bool:
    return "monorepo" in c.query or (
        "core runtime" in c.query and ("docs" in c.query or "examples" in c.query)
    )


def _rtk_runtime_configuration(c: MechanismContext) -> bool:
    return c.name == "configurestore" or (
        (
            "/packages/toolkit/" in c.file_path
            or "redux" in c.file_path
            or "configurestore" in c.query
        )
        and _query_has(c, "middleware", "enhancers", "devtools")
    )


_RULES: tuple[MechanismRule, ...] = (
    MechanismRule(
        "fastapi_route_registration",
        _name_in("fastapi", "apirouter", "add_api_route", "api_route"),
    ),
    MechanismRule(
        "fastapi_dependency_injection",
        _name_in("depends", "get_dependant", "dependant"),
    ),
    MechanismRule(
        "fastapi_request_body_dependency_resolution",
        _name_in("request_body_to_args", "get_body_field"),
    ),
    MechanismRule("fastapi_serialization_impact", _fastapi_serialization_impact),
    MechanismRule(
        "fastapi_endpoint_execution",
        _name_in("run_endpoint_function", "serialize_response", "solve_dependencies"),
    ),
    MechanismRule(
        "fastapi_openapi_generation",
        _name_in("get_openapi", "openapi", "get_openapi_path", "get_fields_from_routes"),
    ),
    MechanismRule(
        "pydantic_python_core_boundary",
        lambda c: c.name == "basemodel" and _pydantic_core_boundary_query(c),
    ),
    MechanismRule("pydantic_validation_core_bridge", _name_in("basemodel")),
    MechanismRule(
        "pydantic_python_core_boundary",
        lambda c: c.name in ("model_validate", "__pydantic_validator__", "schemavalidator")
        and _pydantic_core_boundary_query(c),
    ),
    MechanismRule(
        "pydantic_validation_core_bridge",
        _name_in("model_validate", "__pydantic_validator__", "schemavalidator"),
    ),
    MechanismRule(
        "pydantic_python_core_boundary",
        lambda c: c.name in ("model_dump", "__pydantic_serializer__", "schemaserializer")
        and _pydantic_core_boundary_query(c),
    ),
    MechanismRule(
        "pydantic_serialization_bridge",
        _name_in("model_dump", "__pydantic_serializer__", "schemaserializer"),
    ),
    MechanismRule(
        "pydantic_json_schema_generation",
        _name_in("model_json_schema", "generatejsonschema", "json_schema"),
    ),
    MechanismRule("pydantic_validation_error_assembly", _name_in("validationerror")),
    MechanismRule("pydantic_alias_impact", _name_in("field", "aliaschoices", "aliaspath")),
    MechanismRule(
        "pydantic_v1_compat_surface",
        lambda c: c.name == "v1" or "/pydantic/v1/" in c.file_path,
    ),
    MechanismRule("workspace_structure", _workspace_structure),
    MechanismRule(
        "state_factory_pipeline",
        lambda c: c.name == "createslice" or _query_has_all(c, "action creator", "reducer"),
    ),
    MechanismRule(
        "listener_orchestration_pipeline",
        lambda c: c.name == "createlistenermiddleware"
        or (_query_has_all(c, "listener middleware") and _query_has(c, "intercept", "side effect")),
    ),
    MechanismRule("runtime_configuration_pipeline", _rtk_runtime_configuration),
    MechanismRule(
        "async_lifecycle_pipeline",
        lambda c: c.name == "createasyncthunk"
        or _query_has(c, "pending", "fulfilled", "rejected", "async thunk"),
    ),
    MechanismRule(
        "api_store_integration_pipeline",
        lambda c: c.name == "createapi"
        or ("api slice" in c.query and ("endpoint" in c.query or "store" in c.query)),
    ),
)
