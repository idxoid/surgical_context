"""Universal role taxonomy with backward-compatible aliases.

The benchmark packs and retrieval code historically used framework-specific
role names such as ``public_entrypoint`` or ``dependency_solver``. This module
defines a smaller cross-framework role vocabulary and normalizes legacy names
into it so evaluation and ranking can share one scale.
"""

from __future__ import annotations

import re
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
    # registration_step is a distinct cascade discriminator (decorator handle_fan_out);
    # keep it canonical so the engine's output matches and the map stays idempotent (F14).
    "registration_step": "registration_step",
    "route_builder": "factory_surface",
    "route_matcher": "factory_surface",
    "field_generator": "factory_surface",
    "response_builder": "factory_surface",
    "component_factory": "factory_surface",
    "action_creator_factory": "factory_surface",
    "reducer_builder": "factory_surface",
    # F6: disambiguate overloaded ``*_registry`` legacy names by structural sense.
    "handler_registry": "registration_step",
    "route_registry": "registration_step",
    "middleware_registry": "registration_step",
    "hook_registry": "registration_step",
    "provider_registry": "orchestrator",
    "module_registry": "composition_surface",
    "state_registry": "runtime_surface",
    "metadata_registry": "runtime_surface",
    "table_registry": "runtime_surface",
    "lifecycle_action_creators": "factory_surface",
    "factory_surface": "factory_surface",
    "middleware_builder": "composition_surface",
    "middleware_chain": "composition_surface",
    "middleware_pattern": "composition_surface",
    "enhancer_builder": "composition_surface",
    "composition_result": "composition_surface",
    "composition_pattern": "composition_surface",
    "module_composition": "composition_surface",
    "mounting": "composition_surface",
    "control_flow": "composition_surface",
    "builder_pattern": "composition_surface",
    "composition_surface": "composition_surface",
    # Representations / structured artifacts
    "route_object": "representation_surface",
    "intermediate_model": "representation_surface",
    # request_router is a distinct cascade discriminator (dynamic dispatch); canonical (F14).
    "request_router": "request_router",
    "reactive_system": "representation_surface",
    "reactive_proxy": "representation_surface",
    "vnode_builder": "representation_surface",
    "mapper": "representation_surface",
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
    "model_introspection": "binding_surface",
    "context_accessor": "binding_surface",
    # proxy_mechanism is a distinct cascade discriminator (PROXY_OF); canonical (F14).
    "proxy_mechanism": "proxy_mechanism",
    "fk_resolver": "binding_surface",
    "binding_surface": "binding_surface",
    # Runtime flow / execution
    # dependency_solver is a distinct cascade discriminator (isinstance/inject dispatch);
    # canonical so it is not falsely satisfied by any orchestrator (F14).
    "dependency_solver": "dependency_solver",
    "di_container": "orchestrator",
    "instance_resolver": "orchestrator",
    "decorator_processor": "orchestrator",
    "template_compiler": "orchestrator",
    "sql_compiler": "orchestrator",
    "dependency_tracker": "orchestrator",
    "request_processor": "orchestrator",
    "state_tracker": "orchestrator",
    "action_interceptor": "orchestrator",
    "orchestrator": "orchestrator",
    "handler_or_lifecycle": "runtime_surface",
    "request_lifecycle": "runtime_surface",
    "lifecycle_handler": "runtime_surface",
    "view_dispatcher": "runtime_surface",
    "handler_executor": "runtime_surface",
    "thread_local": "runtime_surface",
    "lazy_loader": "runtime_surface",
    "identity_map": "runtime_surface",
    "collection_manager": "runtime_surface",
    "change_notifier": "runtime_surface",
    "patch_engine": "runtime_surface",
    "header_handler": "runtime_surface",
    "store_integration": "composition_surface",
    "gateway": "integration_surface",
    "integration_surface": "integration_surface",
    "runtime_surface": "runtime_surface",
    "runtime_executor": "executor",
    "async_executor": "executor",
    "operation_executor": "executor",
    "lazy_executor": "executor",
    "effect_executor": "executor",
    "transaction_handler": "executor",
    "side_effect_executor": "executor",
    "concurrency_decision": "executor",
    "executor": "executor",
    # Error / impact roles
    "response_serializer": "serializer_handle",
    "error_model": "error_surface",
    "error_handling": "error_surface",
    "error_surface": "error_surface",
    "serializer": "serializer_handle",
    "validator": "validator_handle",
    "validator_bridge": "validator_handle",
    "transformer": "validator_handle",
    "metaprogramming": "core_runtime",
    "affected_runtime": "impact_runtime",
    "affected_public_api": "impact_public_api",
    "affected_tests": "impact_test_surface",
    "impact_runtime": "impact_runtime",
    "impact_public_api": "impact_public_api",
    "impact_test_surface": "impact_test_surface",
    # Fallback / internal legacy spelling
    "related_implementation": "supporting_surface",
}


# Eval-harness concepts, not structural roles the engine can derive (F14 class 3).
# negative_lookup / nearest_real_mechanism mark negative questions; docs_or_concept
# marks a documentation answer. Excluded from role-recall scoring so they don't count
# as misses the engine could never fulfill.
NON_STRUCTURAL_ROLES: frozenset[str] = frozenset(
    {"docs_or_concept", "negative_lookup", "nearest_real_mechanism"}
)

# Real roles with no structural discriminator today: the topology cannot separate
# them from a neighbour role (F21). serializer_handle vs validator_handle vs
# core_runtime are byte-identical in the graph (hot leaf, high call_fan_in, zero
# type fan) — telling "serializes to json" from "validates into a type" needs the
# data shape a method returns/accepts (dataflow), not a call/type edge. Excluded
# from role-recall scoring until a return-shape/dataflow pass exists; NOT faked from
# the method name (P3). Distinct from NON_STRUCTURAL_ROLES (eval concepts): these are
# genuine roles, just structurally unreachable.
STRUCTURALLY_UNREACHABLE_ROLES: frozenset[str] = frozenset(
    {"serializer_handle", "validator_handle"}
)

# Roles excluded from role-recall scoring (the engine cannot produce them today).
UNSCORED_ROLES: frozenset[str] = NON_STRUCTURAL_ROLES | STRUCTURALLY_UNREACHABLE_ROLES


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


def _to_snake_case(identifier: str) -> str:
    """Convert CamelCase / PascalCase identifiers to lower_snake_case."""
    if not identifier:
        return ""
    normalized = identifier.replace("-", "_")
    # HTTPBase -> HTTP_Base, IntentClassifier -> Intent_Classifier
    step1 = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", normalized)
    step2 = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", step1)
    return step2.replace("__", "_").strip("_").lower()


def infer_supporting_roles(
    *,
    file_path: str = "",
    primary_role: str = "",
    name: str = "",
    kind: str = "",
) -> list[str]:
    """Infer additional roles from STRUCTURAL context only (location + primary role).

    No symbol-name or keyword matching — name-pattern role inference was removed.
    Path-based: files under test directories serve as ``impact_test_surface``;
    non-doc, non-example source symbols of any production primary role serve as
    ``impact_runtime`` evidence; an ``api_surface`` primary (from Pass-1 topology)
    serves as ``impact_public_api``. ``name``/``kind`` are accepted for call-site
    compatibility but no longer drive role inference.
    """
    primary = normalize_role(primary_role)
    if primary == "docs_or_concept":
        return []

    lowered_path = (file_path or "").lower()
    inferred: list[str] = []

    if "/tests/" in lowered_path or lowered_path.endswith("_test.py") or "/test_" in lowered_path:
        inferred.append("impact_test_surface")

    if (
        primary
        in {
            "api_surface",
            "factory_surface",
            "composition_surface",
            "representation_surface",
            "config_surface",
            "schema_builder",
            "binding_surface",
            "orchestrator",
            "runtime_surface",
            "integration_surface",
            "executor",
            "validator_handle",
            "serializer_handle",
            "core_runtime",
            "error_surface",
            "compat_bridge",
            "supporting_surface",
        }
        and "/docs/" not in lowered_path
        and "/examples/" not in lowered_path
    ):
        inferred.append("impact_runtime")

    if primary == "api_surface" and "/docs/" not in lowered_path:
        inferred.append("impact_public_api")

    return normalize_roles(inferred)
