"""Universal role taxonomy with backward-compatible aliases.

The benchmark packs and retrieval code historically used framework-specific
role names such as ``public_entrypoint`` or ``dependency_solver``. This module
defines a smaller cross-framework role vocabulary and normalizes legacy names
into it so evaluation and ranking can share one scale.
"""

from __future__ import annotations

from collections.abc import Iterable


ROLE_ALIASES: dict[str, str] = {
    # Stable / shared roles
    "docs_or_concept": "docs_or_concept",
    "negative_lookup": "negative_lookup",
    "nearest_real_mechanism": "nearest_real_mechanism",
    "compat_bridge": "compat_bridge",
    "validator_handle": "validator_handle",
    "serializer_handle": "serializer_handle",
    "core_runtime": "core_runtime",
    "supporting_surface": "supporting_surface",

    # Public / entry / wrapper surfaces
    "public_entrypoint": "api_surface",
    "model_class": "api_surface",
    "python_wrapper": "api_surface",
    "ui_renderer": "api_surface",
    "endpoint_definition": "api_surface",
    "api_surface": "api_surface",

    # Builders / factories / registration / composition
    "registration_step": "factory_surface",
    "action_creator_factory": "factory_surface",
    "reducer_builder": "factory_surface",
    "route_registry": "factory_surface",
    "lifecycle_action_creators": "factory_surface",
    "factory_surface": "factory_surface",

    "middleware_builder": "composition_surface",
    "enhancer_builder": "composition_surface",
    "composition_result": "composition_surface",
    "composition_surface": "composition_surface",

    # Representations / structured artifacts
    "route_object": "representation_surface",
    "intermediate_model": "representation_surface",
    "schema_module": "representation_surface",
    "generated_api_surface": "representation_surface",
    "representation_surface": "representation_surface",

    # Configuration / schema / binding
    "marker_or_config": "config_surface",
    "devtools_config": "config_surface",
    "config_surface": "config_surface",

    "schema_generator": "schema_builder",
    "body_field_builder": "schema_builder",
    "schema_builder": "schema_builder",

    "body_argument_mapper": "binding_surface",
    "binding_surface": "binding_surface",

    # Runtime flow / execution
    "dependency_solver": "orchestrator",
    "action_interceptor": "orchestrator",
    "orchestrator": "orchestrator",

    "handler_or_lifecycle": "runtime_surface",
    "store_integration": "integration_surface",
    "integration_surface": "integration_surface",
    "runtime_surface": "runtime_surface",

    "runtime_executor": "executor",
    "async_executor": "executor",
    "side_effect_executor": "executor",
    "concurrency_decision": "executor",
    "executor": "executor",

    # Error / impact roles
    "response_serializer": "serializer_handle",
    "error_model": "error_surface",
    "error_handling": "error_surface",
    "error_surface": "error_surface",

    "affected_runtime": "impact_runtime",
    "affected_public_api": "impact_public_api",
    "affected_tests": "impact_test_surface",
    "impact_runtime": "impact_runtime",
    "impact_public_api": "impact_public_api",
    "impact_test_surface": "impact_test_surface",

    # Fallback / internal legacy spelling
    "related_implementation": "supporting_surface",
}


def normalize_role(role: str) -> str:
    """Map a legacy/framework-specific role name to the canonical taxonomy."""
    if not role:
        return role
    return ROLE_ALIASES.get(role, role)


def normalize_roles(roles: Iterable[str], *, dedupe: bool = True) -> list[str]:
    """Normalize an iterable of role names, optionally keeping only first hits."""
    normalized: list[str] = []
    seen: set[str] = set()
    for role in roles:
        canonical = normalize_role(role)
        if dedupe and canonical in seen:
            continue
        seen.add(canonical)
        normalized.append(canonical)
    return normalized
