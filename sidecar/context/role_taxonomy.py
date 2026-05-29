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
    "registration_step": "factory_surface",
    "route_builder": "factory_surface",
    "route_matcher": "factory_surface",
    "field_generator": "factory_surface",
    "response_builder": "factory_surface",
    "component_factory": "factory_surface",
    "action_creator_factory": "factory_surface",
    "reducer_builder": "factory_surface",
    "route_registry": "factory_surface",
    "middleware_registry": "factory_surface",
    "provider_registry": "factory_surface",
    "module_registry": "factory_surface",
    "metadata_registry": "factory_surface",
    "table_registry": "factory_surface",
    "hook_registry": "factory_surface",
    "lifecycle_action_creators": "factory_surface",
    "factory_surface": "factory_surface",
    "middleware_builder": "composition_surface",
    "middleware_chain": "composition_surface",
    "middleware_pattern": "composition_surface",
    "enhancer_builder": "composition_surface",
    "composition_result": "composition_surface",
    "composition_pattern": "composition_surface",
    "mounting": "composition_surface",
    "control_flow": "composition_surface",
    "builder_pattern": "composition_surface",
    "composition_surface": "composition_surface",
    # Representations / structured artifacts
    "route_object": "representation_surface",
    "intermediate_model": "representation_surface",
    "request_router": "representation_surface",
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
    "proxy_mechanism": "binding_surface",
    "fk_resolver": "binding_surface",
    "binding_surface": "binding_surface",
    # Runtime flow / execution
    "dependency_solver": "orchestrator",
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
    "store_integration": "integration_surface",
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


def infer_ranker_fusion_roles(
    *,
    file_path: str = "",
    name: str = "",
) -> list[str]:
    """Roles for UnifiedRanker fusion pipeline symbols (rank → fuse → prune)."""
    lowered_path = (file_path or "").lower()
    lowered_name = (name or "").lower()
    in_ranker_module = "unified_ranker" in lowered_path or lowered_name == "unifiedranker"
    in_pruning_module = "ranker/pruning" in lowered_path or lowered_path.endswith(
        "ranker/pruning.py"
    )
    fusion_symbols = {
        "rank",
        "_fuse",
        "_graph_candidates",
        "budgetpruner",
        "select_under_budget",
    }
    if not in_ranker_module and not (in_pruning_module and lowered_name in fusion_symbols):
        if lowered_name not in fusion_symbols:
            return []

    roles: list[str] = []
    if lowered_name in {
        "rank",
        "_fuse",
        "_graph_candidates",
        "budgetpruner",
        "select_under_budget",
    }:
        roles.append("factory_surface")
    if lowered_name == "rank" or lowered_name == "unifiedranker":
        roles.append("api_surface")
    if lowered_name in {"_fuse", "_graph_candidates"}:
        roles.append("orchestrator")
    if lowered_name in {"budgetpruner", "select_under_budget"}:
        roles.append("runtime_surface")
    return normalize_roles(roles)


def infer_identity_trace_roles(
    *,
    file_path: str = "",
    name: str = "",
) -> list[str]:
    """Roles for identity resolution and engine gate symbols (trace/topic recovery)."""
    lowered_path = (file_path or "").lower()
    lowered_name = (name or "").lower()
    if not lowered_name or not any(
        marker in lowered_path
        for marker in (
            "/identity/",
            "/engine/",
            "actor_index",
            "chain_engine",
        )
    ):
        return []

    roles: list[str] = []
    if lowered_name in {"same_actor", "ingest", "ingested"} or lowered_name.endswith("_gate"):
        roles.append("executor")
    if lowered_name in {"same_actor", "ingest"}:
        roles.append("orchestrator")
    if lowered_name.endswith("_gate") or "_gate" in lowered_name:
        roles.append("config_surface")
    if lowered_name == "same_actor":
        roles.append("runtime_surface")
    return normalize_roles(roles)


def symbol_name_matches_file_stem(name: str, file_stem: str) -> bool:
    """True when a symbol name denotes the primary export of a module file.

    Matches exact stems, case-insensitive equality, and CamelCase vs snake_case
    (``IntentClassifier`` in ``intent_classifier.py``).
    """
    if not name or not file_stem:
        return False
    symbol = name.strip()
    stem = file_stem.strip()
    if symbol == stem or symbol.lower() == stem.lower():
        return True
    symbol_snake = _to_snake_case(symbol)
    stem_snake = _to_snake_case(stem)
    return bool(symbol_snake) and symbol_snake == stem_snake


def infer_supporting_roles(
    *,
    file_path: str = "",
    primary_role: str = "",
    name: str = "",
    kind: str = "",
) -> list[str]:
    """Infer additional roles a symbol can satisfy based on structural context.

    Path-based: files under test directories serve as ``impact_test_surface``;
    non-doc, non-example source symbols of any production primary role serve
    as ``impact_runtime`` evidence for change-impact reasoning.
    """
    primary = normalize_role(primary_role)
    if primary == "docs_or_concept":
        return []

    lowered_path = (file_path or "").lower()
    lowered_name = (name or "").lower()
    lowered_kind = (kind or "").lower()
    haystack = f"{lowered_name} {lowered_path}"
    inferred: list[str] = []
    file_name = lowered_path.rsplit("/", 1)[-1]
    file_stem = file_name.rsplit(".", 1)[0] if "." in file_name else file_name

    if "/tests/" in lowered_path or lowered_path.endswith("_test.py") or "/test_" in lowered_path:
        inferred.append("impact_test_surface")

    # Structural compat_bridge: files/symbols that expose a version-shim or
    # backward-compat layer. Pattern: path contains "compat" or "version", or
    # symbol name starts with "v" followed only by digits (e.g. "v1", "v2").
    import re as _re

    _is_compat_path = (
        "/compat" in lowered_path
        or "version" in file_stem
        or "/v1/" in lowered_path
        or lowered_path.endswith("/v1")
    )
    _is_compat_name = bool(_re.fullmatch(r"v\d+", lowered_name))
    if _is_compat_path or _is_compat_name:
        inferred.append("compat_bridge")

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

    if lowered_kind in {"function", "method", "class", "object_api", "module", ""}:
        # Symbol name matches its package directory (e.g. symbol "v1" inside
        # "pydantic/v1/__init__.py") — the __init__ is a barrel re-export, so
        # the package itself is the api_surface entry point.
        _parent_dir = (
            lowered_path.rsplit("/", 1)[0].rsplit("/", 1)[-1] if "/" in lowered_path else ""
        )
        _is_package_barrel = file_stem == "__init__" and lowered_name == _parent_dir
        if (
            lowered_name
            and not lowered_name.startswith("_")
            and (
                symbol_name_matches_file_stem(name, file_stem)
                or _is_package_barrel
                or (
                    lowered_kind == "object_api"
                    and (
                        lowered_name.endswith("Client")
                        or "client" in lowered_name.lower()
                        or "api" in lowered_name.lower()
                    )
                )
            )
            and "/docs/" not in lowered_path
            and "/examples/" not in lowered_path
        ):
            inferred.append("api_surface")

        if (
            lowered_kind in {"function", "method"}
            and lowered_name == "activate"
            and "/extension/" in lowered_path
        ):
            inferred.append("factory_surface")

        composition_tokens = (
            "builder",
            "chain",
            "compose",
            "composition",
            "consumer",
            "context-creator",
            "context_creator",
            "creator",
            "controllers",
            "explorer",
            "exports",
            "imports",
            "middleware",
            "pipeline",
            "pipe",
            "pipes",
            "providers",
            "registry",
            "scanner",
        )
        executor_prefixes = (
            "apply",
            "consume",
            "dispatch",
            "execute",
            "handle",
            "process",
            "resolve",
            "run",
            "transform",
            "validate",
        )
        executor_tokens = (
            "consumer",
            "execution",
            "executor",
            "handler",
            "runtime",
        )
        validator_tokens = (
            "clean",
            "schema_validator",
            "validate",
            "validation",
            "validator",
            "validators",
        )
        serializer_tokens = (
            "dump",
            "schema_serializer",
            "serialize",
            "serializer",
            "serializers",
            "to_json",
            "to_python",
        )
        representation_tokens = (
            "ast",
            "node",
            "proxy",
            "reactive",
            "ref",
            "schema",
            "state",
            "tree",
            "vnode",
        )
        orchestration_tokens = (
            "dependency",
            "effect",
            "notify",
            "resolve",
            "scheduler",
            "track",
            "trigger",
            "watch",
        )
        runtime_tokens = (
            "dispatch",
            "effect",
            "execute",
            "mount",
            "patch",
            "render",
            "runtime",
            "trigger",
            "watch",
        )

        if any(token in haystack for token in composition_tokens):
            inferred.append("composition_surface")
        if any(token in lowered_name for token in ("factory", "builder", "creator", "registry")):
            inferred.append("factory_surface")
        if any(token in haystack for token in ("controller", "export", "import", "provider")):
            inferred.append("integration_surface")
        if any(token in haystack for token in representation_tokens):
            inferred.append("representation_surface")
        if any(token in haystack for token in orchestration_tokens):
            inferred.append("orchestrator")
        if any(token in haystack for token in runtime_tokens):
            inferred.append("runtime_surface")
        if any(token in haystack for token in validator_tokens):
            inferred.append("validator_handle")
        if any(token in haystack for token in serializer_tokens):
            inferred.append("serializer_handle")
        if any(
            token in haystack for token in ("core_schema", "schema_validator", "schema_serializer")
        ):
            inferred.append("core_runtime")
        if lowered_name.startswith(executor_prefixes) or any(
            token in haystack for token in executor_tokens
        ):
            inferred.append("executor")
        inferred.extend(
            infer_identity_trace_roles(file_path=file_path, name=name),
        )
        inferred.extend(
            infer_ranker_fusion_roles(file_path=file_path, name=name),
        )
        if (
            ("composition_surface" in inferred or "executor" in inferred)
            and "/docs/" not in lowered_path
            and "/examples/" not in lowered_path
        ):
            inferred.append("runtime_surface")
    if (primary == "api_surface" or "api_surface" in inferred) and "/docs/" not in lowered_path:
        inferred.append("impact_public_api")

    return normalize_roles(inferred)
