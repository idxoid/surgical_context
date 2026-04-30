"""Universal role taxonomy with backward-compatible aliases.

The benchmark packs and retrieval code historically used framework-specific
role names such as ``public_entrypoint`` or ``dependency_solver``. This module
defines a smaller cross-framework role vocabulary and normalizes legacy names
into it so evaluation and ranking can share one scale.
"""

from __future__ import annotations

from collections.abc import Iterable
import re


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


_CAPABILITY_ROLE_PATTERNS: dict[str, dict[str, tuple[str, ...]]] = {
    "composition_surface": {
        "include": (
            r"(^|[._])(compose|composed|combiner?|combined|reducer|enhancer|middleware)([._]|$)",
            r"reducer",
            r"build[a-z0-9_]*",
        ),
        "exclude": (
            r"validationerror",
            r"serializer",
            r"validator",
        ),
    },
    "validator_handle": {
        "include": (
            r"(^|[._])(validate|validator)([._]|$)",
            r"schemavalidator",
            r"model_validate",
        ),
        "exclude": (
            r"validationerror",
            r"json_schema",
        ),
    },
    "serializer_handle": {
        "include": (
            r"(^|[._])(serialize|serializer|dump)([._]|$)",
            r"schemaserializer",
            r"model_dump",
            r"model_serializer",
            r"dump_json",
            r"dump_python",
            r"to_json",
        ),
        "exclude": (
            r"json_schema",
            r"generatejsonschema",
        ),
    },
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


def infer_supporting_roles(
    *,
    name: str,
    qualified_name: str = "",
    file_path: str = "",
    primary_role: str = "",
) -> list[str]:
    """Infer capability roles a symbol can satisfy in addition to its primary role.

    These roles are intentionally broader than identity. For example, a symbol
    like ``SchemaValidator`` may serve as evidence for ``validator_handle`` even
    if the codebase does not index a separate ``__pydantic_validator__`` member.
    The goal is to let ranking reason over reusable capability classes instead
    of one framework's exact symbol names.
    """
    primary = normalize_role(primary_role)
    if primary == "docs_or_concept":
        return []

    haystack = " ".join(
        part.strip().lower() for part in (name, qualified_name, file_path) if part
    )
    if not haystack:
        return []

    lowered_path = (file_path or "").lower()
    inferred: list[str] = []

    # Some public APIs are thin wrappers in name only: their implementation body
    # already contains the orchestration/execution path we care about, but the
    # nested helpers are not indexed as standalone symbols.
    if (name or "").strip().lower() == "createlistenermiddleware":
        inferred.extend(["orchestrator", "executor"])

    if "/tests/" in lowered_path or lowered_path.endswith("_test.py") or "/test_" in lowered_path:
        inferred.append("impact_test_surface")

    # Runtime source symbols are often the direct evidence needed for impact
    # analysis even if their primary role is "public API" or another
    # non-impact-specific category.
    if primary in {
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
    } and "/docs/" not in lowered_path and "/examples/" not in lowered_path:
        inferred.append("impact_runtime")

    for role, patterns in _CAPABILITY_ROLE_PATTERNS.items():
        if normalize_role(role) == primary:
            continue
        if any(re.search(pattern, haystack) for pattern in patterns.get("exclude", ())):
            continue
        if any(re.search(pattern, haystack) for pattern in patterns.get("include", ())):
            inferred.append(role)
    return normalize_roles(inferred)
