"""Analytical layer for the axis extractor — axis-bit coverage and container-
kind inventory.

This module produces *reports*, not new axis bits. It walks the existing
extractor to inventory the axis bits it can emit, walks the benchmark question
packs to inventory the contracts those questions' roles would need closed, and
cross-references the two to surface gaps.

It does not author roles, propose new axis bits, or decide policy. Output is
data the human can read to decide where the next extractor / container-kind
work should land.

Terminology (see ``docs/axis_terminology.md``):

  fact      = physical AST/graph observation
  axis bit  = normalized fact on CFG/DFG/STRUCT (what L1 emits)
  contract  = provable combination of axis bits on a symbol
  role      = user/benchmark requirement, satisfied by >=1 contract
  bucket    = optimisation grouping (not used in this module)

Two layers only, per the current architectural scope:

  L1 — extractor axis bits (what the analyser can read today)
  L2 — container kinds     (what container fingerprints questions imply)

L3 (contract compiler) and L4 (role resolver) are referenced as the consumers
that *would* read these gaps, but their implementation is out of scope here.
"""

from __future__ import annotations

import ast
import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml

AxisName = Literal["cfg", "dfg", "struct"]
PROJECT_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# L1: extractor bit inventory — derived from the extractor source itself so it
# stays in lockstep with what the extractor actually emits. No hand list.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExtractorInventory:
    """Snapshot of every (axis, bit) the extractor can emit."""

    cfg: frozenset[str]
    dfg: frozenset[str]
    struct: frozenset[str]

    @property
    def all_pairs(self) -> set[tuple[AxisName, str]]:
        return (
            {("cfg", b) for b in self.cfg}
            | {("dfg", b) for b in self.dfg}
            | {("struct", b) for b in self.struct}
        )

    def has(self, axis: AxisName, bit: str) -> bool:
        return bit in {"cfg": self.cfg, "dfg": self.dfg, "struct": self.struct}[axis]

    def to_dict(self) -> dict[str, list[str]]:
        return {
            "cfg": sorted(self.cfg),
            "dfg": sorted(self.dfg),
            "struct": sorted(self.struct),
        }


def inventory_extractor_bits(
    extractor_path: Path = PROJECT_ROOT / "sidecar" / "axis" / "python_extractor.py",
) -> ExtractorInventory:
    """Walk the extractor AST to find every `self._emit(axis, bit, ...)` call.

    Conditional bits (e.g. ``"async_function_def" if async else "function_def"``)
    are unfolded so both branches enter the inventory.
    """
    with open(extractor_path) as fp:
        tree = ast.parse(fp.read())

    cfg: set[str] = set()
    dfg: set[str] = set()
    struct: set[str] = set()
    buckets: dict[str, set[str]] = {"cfg": cfg, "dfg": dfg, "struct": struct}

    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_emit"
            and len(node.args) >= 2
        ):
            continue
        axis_arg, bit_arg = node.args[0], node.args[1]
        axis = axis_arg.value if isinstance(axis_arg, ast.Constant) else None
        if axis not in buckets:
            continue
        # Bit may be a literal or a conditional expression
        for bit_value in _string_values_from(bit_arg):
            buckets[axis].add(bit_value)

    return ExtractorInventory(
        cfg=frozenset(cfg),
        dfg=frozenset(dfg),
        struct=frozenset(struct),
    )


def _string_values_from(node: ast.AST) -> list[str]:
    """Best-effort: extract every literal string this expression can resolve to."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.IfExp):
        return _string_values_from(node.body) + _string_values_from(node.orelse)
    return []


# ---------------------------------------------------------------------------
# Logical-role -> contract family mapping (conservative).
#
# This is a *reading* of the questions the benchmarks already pose. We do not
# invent new contracts; each name corresponds to a contract family described in
# docs/python_ast_axis_fact_gap_analysis.md or to a shape implied by an
# explicit expected_role.
#
# Multiple contracts per role are interpreted as OR: any one satisfies the
# logical role for a question.
# ---------------------------------------------------------------------------

ROLE_CONTRACTS: dict[str, list[str]] = {
    # registration / binding family
    "binding_surface": [
        "dependency_binding",
        "route_param_binding",
        "context_proxy_scope",
        "shape_projection",
    ],
    "registration_step": [
        "deferred_registration",
        "metadata_write_read",
    ],
    "factory_surface": [
        "deferred_registration",  # decorator_factory variant
    ],
    "composition_surface": [
        "middleware_chain_append",
        "deferred_registration",
    ],
    # dispatch / runtime family
    "request_router": [
        "registry_read_dispatch",
    ],
    "executor": [
        "registry_read_dispatch",
        "dispatch_loop",
    ],
    "runtime_executor": [
        "registry_read_dispatch",
    ],
    "runtime_surface": [
        "dispersed_runtime_position",
    ],
    "handler_or_lifecycle": [
        "runtime_adapter",
        "deferred_registration",
    ],
    "concurrency_decision": [
        "runtime_adapter",
    ],
    "interceptor": [
        "runtime_adapter",
    ],
    "core_runtime": [
        "dispersed_runtime_position",
    ],
    "dependency_solver": [
        "dependency_binding",
        "registry_read_dispatch",
    ],
    "proxy_mechanism": [
        "context_proxy_scope",
    ],
    # data / type family
    "representation_surface": [
        "data_contract_type",
    ],
    "schema_builder": [
        "shape_projection",
    ],
    "validator_handle": [
        "metadata_write_read",
    ],
    "serializer_handle": [
        "shape_projection",
    ],
    # surface family
    "api_surface": [
        "public_symbol_surface",
    ],
    "public_entrypoint": [
        "public_symbol_surface",
    ],
    "config_surface": [
        "config_effect",
    ],
    "integration_surface": [
        "external_integration_boundary",
    ],
    "error_surface": [
        "error_handling",
    ],
    # impact / abstract
    "abstract_contract": [
        "data_contract_type",
    ],
    "impact_runtime": [
        "dispersed_runtime_position",
    ],
    "impact_public_api": [
        "public_symbol_surface",
    ],
    "impact_test_surface": [
        # closed by retrieved test files; not provable via axis bits alone
    ],
    # ----------------------------------------------------------------------
    # Extended mapping — splitting broad roles into mechanism-specific
    # contracts. Per the principle stated in
    # docs/contract_compiler_topology_principle.md: a logical role is the OR
    # of contracts that prove it; framework-shaped role names map to the
    # underlying structural mechanism, not to a framework. Container kind
    # discriminates the concrete instance at the L2 layer.
    # ----------------------------------------------------------------------
    # broad orchestration — three plausible mechanisms; the actual one a
    # specific question hits is whichever contract closes against the seed.
    "orchestrator": [
        "registry_read_dispatch",
        "dispatch_loop",
        "runtime_adapter",
    ],
    # framework markers (Pydantic Field, FastAPI Depends, Click context arg
    # etc.) — these are either config carriers, metadata write/reads, or DI
    # marker bindings depending on the specific marker shape.
    "marker_or_config": [
        "config_effect",
        "metadata_write_read",
        "dependency_binding",
    ],
    # "operation_executor", "handler_executor", "operation/handler invoked
    # via registered lookup".
    "operation_executor": ["registry_read_dispatch"],
    "handler_executor": ["registry_read_dispatch", "dispatch_loop"],
    # routing — the route, the registry, the builder, the matcher.
    "route_object": ["data_contract_type", "public_symbol_surface"],
    "route_registry": ["registry_read_dispatch"],
    "route_builder": ["deferred_registration"],
    "route_matcher": ["registry_read_dispatch"],
    # schema / template / shape-emitting compilers — all are shape projections
    # at the structural level (read input shape, emit output shape).
    "schema_generator": ["shape_projection"],
    "sql_compiler": ["shape_projection"],
    "template_compiler": ["shape_projection"],
    "vnode_builder": ["shape_projection"],
    "patch_engine": ["shape_projection"],
    "mapper": ["shape_projection"],
    "response_builder": ["shape_projection"],
    "composition_result": ["shape_projection"],
    "field_generator": ["shape_projection"],
    "body_field_builder": ["shape_projection", "data_contract_type"],
    # middleware family — all proven by middleware_chain_append; the chain
    # container is the L2 discriminator.
    "middleware_registry": ["middleware_chain_append"],
    "middleware_chain": ["middleware_chain_append"],
    "middleware_pattern": ["middleware_chain_append"],
    "middleware_builder": ["middleware_chain_append", "deferred_registration"],
    "composition_pattern": ["middleware_chain_append", "deferred_registration"],
    "enhancer_builder": ["middleware_chain_append", "deferred_registration"],
    "mounting": ["middleware_chain_append", "deferred_registration"],
    # builders / factories — deferred_registration covers the
    # "declaration-time emits a callable/value into a registry" shape.
    "builder_pattern": ["shape_projection", "deferred_registration"],
    "action_creator_factory": ["deferred_registration"],
    "reducer_builder": ["deferred_registration"],
    "lifecycle_action_creators": ["deferred_registration"],
    "endpoint_definition": ["data_contract_type", "deferred_registration"],
    "decorator_processor": ["deferred_registration"],
    "component_factory": ["deferred_registration"],
    "hook_registry": ["deferred_registration", "registry_read_dispatch"],
    # runtime adapters — sync/async bridges, wrappers, lazy/deferred shapes.
    "compat_bridge": ["runtime_adapter"],
    "lazy_executor": ["runtime_adapter"],
    "lazy_loader": ["runtime_adapter"],
    "async_executor": ["runtime_adapter"],
    "action_interceptor": ["runtime_adapter"],
    "side_effect_executor": ["runtime_adapter"],
    "effect_executor": ["runtime_adapter"],
    "header_handler": ["runtime_adapter"],
    "lifecycle_handler": ["runtime_adapter", "deferred_registration"],
    "reactive_system": ["runtime_adapter"],
    # DI / dependency family — provider/argument resolution.
    "dependency_tracker": ["dependency_binding", "registry_read_dispatch"],
    "intermediate_model": ["data_contract_type"],
    "body_argument_mapper": ["route_param_binding", "dependency_binding"],
    "di_container": ["dependency_binding"],
    "instance_resolver": ["dependency_binding"],
    "provider_registry": ["registry_read_dispatch", "dependency_binding"],
    "module_registry": ["registry_read_dispatch"],
    "store_integration": ["registry_read_dispatch"],
    "metadata_registry": ["metadata_write_read", "registry_read_dispatch"],
    "fk_resolver": ["dependency_binding"],
    # context / proxy / thread-local — all are scoped resource bindings.
    "context_accessor": ["context_proxy_scope"],
    "thread_local": ["context_proxy_scope"],
    "transaction_handler": ["context_proxy_scope"],
    "reactive_proxy": ["context_proxy_scope"],
    # ORM-shaped registries / introspection / tables.
    "table_registry": ["registry_read_dispatch", "data_contract_type"],
    "identity_map": ["registry_read_dispatch"],
    "collection_manager": ["dispatch_loop"],
    "change_notifier": ["middleware_chain_append", "registry_read_dispatch"],
    "model_introspection": ["metadata_write_read"],
    # config knob
    "devtools_config": ["config_effect"],
    # surface family
    "generated_api_surface": ["public_symbol_surface"],
    # broad / weak / fallback — runtime_participant is the weakest background
    # fact (just "reachable on a runtime path").
    "supporting_surface": ["dispersed_runtime_position"],
    "state_tracker": ["dispersed_runtime_position"],
    "control_flow": ["dispersed_runtime_position"],
    "metaprogramming": ["runtime_adapter"],
    "request_processor": ["registry_read_dispatch", "runtime_adapter"],
    # ----------------------------------------------------------------------
    # Roles NOT provable from axis bits — these are doc-tier signals or
    # query-intent markers, not structural roles. Listed explicitly (empty
    # contract list) so coverage analysis stops flagging them as gaps.
    # ----------------------------------------------------------------------
    "docs_or_concept": [
        # closed by retrieved documentation, not axis facts.
    ],
    "negative_lookup": [
        # query intent: "no such symbol exists in workspace". Resolved by the
        # query layer when target_selector returns no match, not by contracts.
    ],
    "nearest_real_mechanism": [
        # query intent that pairs with negative_lookup: surface the closest
        # mechanism instead. Not a structural role.
    ],
}


# ---------------------------------------------------------------------------
# Contract family -> minimal axis-bit requirements + container-kind dependency.
#
# Bits are quoted from the matrix in
# docs/python_ast_axis_fact_gap_analysis.md. Multiple bit alternatives in a
# single slot are written as a tuple (any-of). A contract may also require a
# container of a specific kind; ``None`` means container kind is irrelevant.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContractRequirement:
    """One contract's minimal bit + container-kind requirements."""

    name: str
    cfg: tuple[tuple[str, ...], ...] = ()  # each inner tuple is an any-of slot
    dfg: tuple[tuple[str, ...], ...] = ()
    struct: tuple[tuple[str, ...], ...] = ()
    container_kind: str | None = None  # required L2 container fingerprint
    notes: str = ""

    def axis_slots(self) -> list[tuple[AxisName, tuple[str, ...]]]:
        return (
            [("cfg", slot) for slot in self.cfg]
            + [("dfg", slot) for slot in self.dfg]
            + [("struct", slot) for slot in self.struct]
        )


# A contract is "structural negative space" when its proof requires showing
# that strong contracts and anchor-graph connectivity are ABSENT, not just
# that some background bit is present. These are L2/L3 composite contracts —
# the L1 bit content is permissive, the discriminator lives in the
# graph-context predicates (low outgoing fan to kind-classified containers,
# dispersion of callers across packages, no CFG driver position).
#
# Listed here so the coverage report distinguishes them from genuinely weak
# fallback contracts. A negative-space contract is honest evidence of
# "structurally inconspicuous runtime node" — not a fallback.
NEGATIVE_SPACE_CONTRACTS: frozenset[str] = frozenset({"dispersed_runtime_position"})

# Contracts whose required bits are themselves weakly-discriminating (just
# "reachable on a runtime path"). Questions whose only contract is one of these
# are technically covered but the analytical headline is informational — L3
# will need to grade contract strength when resolving roles.
WEAK_CONTRACTS: frozenset[str] = frozenset()


CONTRACTS: dict[str, ContractRequirement] = {
    "deferred_registration": ContractRequirement(
        name="deferred_registration",
        cfg=(("decorator_application",), ("value_call",)),
        dfg=(
            ("callable_value",),
            ("container_write_value", "keyed_write"),
            ("container_read_key", "iteration_source"),
        ),
        struct=(("decorator_shape",),),
        container_kind="registry_kind",
        notes="Declaration/import-time write of a callable into a registry, "
        "later read and invoked. Container kind discriminates web/task/signal/etc.",
    ),
    "dependency_binding": ContractRequirement(
        name="dependency_binding",
        cfg=(("call_site",),),
        dfg=(
            ("parameter_default_value",),
            ("callable_value",),
            ("call_argument",),
        ),
        struct=(("parameter_decl",), ("parameter_default",)),
        container_kind="di_container",
        notes="Marker/provider value as parameter default → solved to argument "
        "at call site. Discriminated by the container holding the providers.",
    ),
    "route_param_binding": ContractRequirement(
        name="route_param_binding",
        cfg=(("call_site",),),
        dfg=(("parameter_input",), ("call_argument",)),
        struct=(("parameter_decl",), ("literal_key",)),
        container_kind="web_route_register",
        notes="Route pattern (literal_key) captures bind to handler parameters.",
    ),
    "context_proxy_scope": ContractRequirement(
        name="context_proxy_scope",
        cfg=(("context_enter_exit",),),
        dfg=(("context_resource",), ("attr_read",)),
        struct=(),
        container_kind="proxy_object",
        notes="Scoped resource set/read inside context manager.",
    ),
    "shape_projection": ContractRequirement(
        name="shape_projection",
        cfg=(),
        dfg=(
            ("attr_read", "subscript_read"),
            ("return_shape_kind", "constructed_output", "collection_assembly"),
        ),
        struct=(),
        container_kind=None,
        notes="Source field reads land in constructed/mapping return shape.",
    ),
    "metadata_write_read": ContractRequirement(
        name="metadata_write_read",
        cfg=(),
        dfg=(("keyed_write",), ("keyed_read",)),
        struct=(("decorator_shape", "literal_key"),),
        container_kind="metadata_carrier",
        notes="Same literal key identity written and read on a metadata container.",
    ),
    "middleware_chain_append": ContractRequirement(
        name="middleware_chain_append",
        cfg=(("call_site",),),
        dfg=(("callable_value",), ("container_write_value",)),
        struct=(),
        container_kind="middleware_chain",
        notes="Function appends/wraps a callable into an ordered chain.",
    ),
    "registry_read_dispatch": ContractRequirement(
        name="registry_read_dispatch",
        cfg=(("loop_driver", "branch_selector"), ("value_call",)),
        dfg=(("container_read_key", "iteration_source"),),
        struct=(),
        container_kind="registry_kind",
        notes="Loop or selector reads a registered value and invokes it.",
    ),
    "dispatch_loop": ContractRequirement(
        name="dispatch_loop",
        cfg=(("loop_driver",), ("value_call",)),
        dfg=(("iteration_source",), ("callable_value",)),
        struct=(),
        container_kind="registry_kind",
        notes="Iteration over registered handlers, each invoked in turn.",
    ),
    "runtime_adapter": ContractRequirement(
        name="runtime_adapter",
        cfg=(("async_suspend_resume", "call_site"),),
        dfg=(("callable_value", "call_argument"),),
        struct=(),
        container_kind=None,
        notes="Wrapper continuation call; sync/async bridge or middleware wrap.",
    ),
    "runtime_participant": ContractRequirement(
        name="runtime_participant",
        cfg=(("call_site",),),
        dfg=(),
        struct=(),
        container_kind=None,
        notes="Reachable on a runtime path; weak background fact, not a discriminator. "
        "Retained for contracts that only need 'is callable / reachable' as a base.",
    ),
    "dispersed_runtime_position": ContractRequirement(
        name="dispersed_runtime_position",
        # Structural negative space: a positive contract whose proof requires
        # three concurrent conditions, two of them about ABSENCE. The axis-bit
        # base is permissive (any reachable callable), the discriminator lives
        # in graph-context checks that the L2/L3 stack will perform.
        cfg=(("call_site", "callable_body"),),
        dfg=(),
        struct=(),
        container_kind=None,
        notes=(
            "Symbol is reachable on a runtime path AND meets three "
            "structural-negative criteria:\n"
            "  1. Low edge density: outgoing edges to kind-classified "
            "containers (registry / entrypoint / data_model) are below "
            "threshold.\n"
            "  2. High dispersion: caller fan is spread across packages, "
            "no concentrated anchor module.\n"
            "  3. Cyclic neutrality: symbol is not a CFG driver — not the "
            "top of a dispatch loop or a registered handler invocation.\n"
            "Proof requires L2 container kind output and graph topology, "
            "so this contract lives at L3 even though its bit content is "
            "permissive. It is NOT a weak fallback — it is positive "
            "evidence of 'structurally inconspicuous runtime node'."
        ),
    ),
    "data_contract_type": ContractRequirement(
        name="data_contract_type",
        cfg=(),
        dfg=(),
        struct=(
            ("class_def",),
            ("annotation", "class_attribute", "instance_attribute_hint"),
        ),
        container_kind="data_model",
        notes="Declared class with typed members; data carrier shape.",
    ),
    "public_symbol_surface": ContractRequirement(
        name="public_symbol_surface",
        cfg=(),
        dfg=(),
        struct=(("function_def", "async_function_def", "class_def"),),
        container_kind=None,
        notes="A documented or re-exported declaration. Re-export proof lives in "
        "the graph layer (RE_EXPORTS edge), not in axis bits.",
    ),
    "config_effect": ContractRequirement(
        name="config_effect",
        cfg=(("branch_condition",),),
        dfg=(
            ("attr_read", "keyed_read"),
            ("branch_influence",),
        ),
        struct=(),
        container_kind="config_carrier",
        notes="Read of a config value drives a branch / call selection.",
    ),
    "error_handling": ContractRequirement(
        name="error_handling",
        cfg=(("exception_raise_value", "exception_handler_type"),),
        dfg=(),
        struct=(),
        container_kind="error_dispatch",
        notes="Raise value and / or except type. Dispatch container optional when only thrown.",
    ),
    "external_integration_boundary": ContractRequirement(
        name="external_integration_boundary",
        cfg=(("call_site",),),
        dfg=(),
        struct=(("import_dependency",),),
        container_kind=None,
        notes="Call against an import-dependent external symbol. The "
        "non-plumbing filter is a property of the graph layer, not a bit.",
    ),
}


# ---------------------------------------------------------------------------
# L2: container-kind catalogue (analytical sketch).
#
# Each container kind is described by the FINGERPRINT a future L2 classifier
# would have to detect. The fingerprint is written in terms of axis bits and
# graph topology hints. We are NOT classifying here; we are listing what L2
# would need to express. A kind that recurs across multiple frameworks is
# healthy; one that ties to a single library is a candidate for library marker
# instead of new kind.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContainerKindSpec:
    name: str
    description: str
    distinguishing_bits: tuple[str, ...]
    topology_hints: tuple[str, ...]
    expected_frameworks: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "description": self.description,
            "distinguishing_bits": list(self.distinguishing_bits),
            "topology_hints": list(self.topology_hints),
            "expected_frameworks": list(self.expected_frameworks),
        }


CONTAINER_KINDS: dict[str, ContainerKindSpec] = {
    "web_route_register": ContainerKindSpec(
        name="web_route_register",
        description="Container that maps URL patterns to handler callables.",
        distinguishing_bits=(
            "struct.decorator_shape",
            "dfg.keyed_write",
            "struct.literal_key",
        ),
        topology_hints=(
            "outgoing HAS_API fan to many handler-callables",
            "USES_TYPE / external import root in {starlette.routing, werkzeug.routing, fastapi.routing}",
            "keyed_write keys include HTTP method literals or URL patterns",
        ),
        expected_frameworks=(
            "Flask",
            "FastAPI",
            "Django (URLconf)",
            "Starlette",
            "Express",
            "NestJS",
        ),
    ),
    "task_register": ContainerKindSpec(
        name="task_register",
        description="Container that registers callables as deferred / queued tasks.",
        distinguishing_bits=(
            "struct.decorator_shape",
            "dfg.callable_value",
            "dfg.keyed_write",
        ),
        topology_hints=(
            "import_dependency to messaging packages (kombu/amqp/billiard/redis transport)",
            "INSTANTIATES of queue-like objects",
            "decorator_shape carries task-options payload (literal keys: name, queue, retries)",
        ),
        expected_frameworks=("Celery", "RQ", "Dramatiq", "Huey"),
    ),
    "signal_register": ContainerKindSpec(
        name="signal_register",
        description="Bidirectional callable storage: receivers attached and later iterated.",
        distinguishing_bits=(
            "dfg.callable_value",
            "dfg.container_write_value",
            "dfg.iteration_source",
        ),
        topology_hints=(
            "class with `connect`/`disconnect`/`send` shape (axis fingerprint, not name)",
            "no web/task/model fingerprint present",
            "callable storage iterated under a fan-out call site",
        ),
        expected_frameworks=("Django signals", "blinker", "Vue ref", "RTK listener middleware"),
    ),
    "data_model": ContainerKindSpec(
        name="data_model",
        description="Class whose body declares field-typed descriptors / annotations.",
        distinguishing_bits=(
            "struct.class_def",
            "struct.class_attribute",
            "struct.annotation",
            "dfg.constructed_output",
        ),
        topology_hints=(
            "multiple class_attribute + annotation pairs in class body",
            "methods returning constructed value of the same class",
            "validators / serializers referencing the same class",
        ),
        expected_frameworks=(
            "Pydantic Model",
            "Django Model",
            "SQLAlchemy declarative_base",
            "msgspec",
            "attrs",
            "dataclass",
        ),
    ),
    "di_container": ContainerKindSpec(
        name="di_container",
        description="Object resolving provider references to argument slots.",
        distinguishing_bits=(
            "dfg.parameter_default_value",
            "dfg.callable_value",
            "dfg.call_argument",
        ),
        topology_hints=(
            "parameter default holds marker carrying a callable provider",
            "call site resolves provider into the argument slot",
            "no route/task fingerprint",
        ),
        expected_frameworks=(
            "FastAPI Depends",
            "Click context",
            "NestJS provider",
            "pytest fixture",
        ),
    ),
    "middleware_chain": ContainerKindSpec(
        name="middleware_chain",
        description="Ordered list of callables appended at registration and invoked in sequence.",
        distinguishing_bits=(
            "dfg.callable_value",
            "dfg.container_write_value",
            "dfg.iteration_source",
        ),
        topology_hints=(
            "single container appended into via a builder method",
            "iteration_source over the container drives sequential invocation",
            "each callable receives the next callable as argument (wrapper continuation)",
        ),
        expected_frameworks=(
            "ASGI/WSGI middleware stack",
            "Express app.use",
            "Django MIDDLEWARE",
            "NestJS interceptors",
        ),
    ),
    "config_carrier": ContainerKindSpec(
        name="config_carrier",
        description="Object carrying typed configuration values consumed by other code.",
        distinguishing_bits=(
            "struct.class_def",
            "struct.annotation",
            "dfg.attr_read",
            "dfg.keyed_read",
        ),
        topology_hints=(
            "class body holds annotated literal defaults",
            "field reads observed in branch conditions elsewhere",
        ),
        expected_frameworks=("Pydantic Settings", "Django settings", "Celery conf", "ConfigDict"),
    ),
    "error_dispatch": ContainerKindSpec(
        name="error_dispatch",
        description="Container mapping exception types to handler callables.",
        distinguishing_bits=(
            "cfg.exception_handler_type",
            "dfg.callable_value",
            "dfg.keyed_write",
        ),
        topology_hints=(
            "literal key in container is an exception class",
            "value read at runtime and invoked from raise path",
        ),
        expected_frameworks=(
            "FastAPI exception_handlers",
            "Flask errorhandler",
            "Django middleware",
        ),
    ),
    "proxy_object": ContainerKindSpec(
        name="proxy_object",
        description="Object whose attribute reads/writes resolve to a scoped target.",
        distinguishing_bits=(
            "dfg.context_resource",
            "dfg.attr_read",
        ),
        topology_hints=(
            "lazy-proxy pattern (LocalProxy, ContextVar wrapper)",
            "graph layer: is_proxy_binding marker already exists",
        ),
        expected_frameworks=("Flask current_app/request", "FastAPI Depends-scoped"),
    ),
    "metadata_carrier": ContainerKindSpec(
        name="metadata_carrier",
        description="Object holding declarative metadata read at runtime.",
        distinguishing_bits=(
            "dfg.keyed_write",
            "dfg.keyed_read",
            "struct.literal_key",
        ),
        topology_hints=(
            "write and read share literal key identity",
            "key set is small, fixed at declaration time",
        ),
        expected_frameworks=(
            "Pydantic field info",
            "SQLAlchemy mapper info",
            "NestJS metadata reflection",
            "dataclass __init_subclass__",
        ),
    ),
    # registry_kind is an *abstract* parameter for contracts that take any
    # registry-shaped container. The concrete kind is one of the above (web /
    # task / signal / middleware / error_dispatch / metadata_carrier).
    "registry_kind": ContainerKindSpec(
        name="registry_kind",
        description="Abstract parameter — any concrete registry-shaped container kind.",
        distinguishing_bits=(),
        topology_hints=(
            "any of web_route_register / task_register / signal_register / middleware_chain / "
            "error_dispatch / metadata_carrier",
        ),
        expected_frameworks=(),
    ),
}


# ---------------------------------------------------------------------------
# Question-pack analysis
# ---------------------------------------------------------------------------


def load_pack(path: Path) -> list[dict]:
    """Load a single YAML pack and return its questions."""
    with open(path) as fp:
        data = yaml.safe_load(fp) or {}
    return list(data.get("questions") or [])


def load_packs(paths: list[Path]) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for p in paths:
        for q in load_pack(p):
            qid = q.get("id")
            if qid in seen:
                continue
            seen.add(qid)
            out.append({**q, "_pack": p.name})
    return out


@dataclass
class QuestionGap:
    """Per-question coverage diagnostic.

    Missing bits / missing container kinds are computed against the union of
    contracts that could satisfy each expected role. A bit is "missing" only if
    every alternative slot lacks it AND no other contract for the same role
    covers it.
    """

    question_id: str
    repo: str
    seed: str | None
    intent: str | None
    expected_roles: list[str]
    role_to_contracts: dict[str, list[str]]
    required_bits_present: list[tuple[AxisName, str]]
    required_bits_missing: list[tuple[AxisName, str]]
    container_kinds_required: list[str]
    container_kinds_unmodeled: list[str]
    impossible_roles: list[str]  # roles with no contract mapping
    weak_only_roles: list[str] = field(default_factory=list)
    negative_space_only_roles: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "question_id": self.question_id,
            "repo": self.repo,
            "seed": self.seed,
            "intent": self.intent,
            "expected_roles": list(self.expected_roles),
            "role_to_contracts": {k: list(v) for k, v in self.role_to_contracts.items()},
            "required_bits_present": [[a, b] for a, b in self.required_bits_present],
            "required_bits_missing": [[a, b] for a, b in self.required_bits_missing],
            "container_kinds_required": list(self.container_kinds_required),
            "container_kinds_unmodeled": list(self.container_kinds_unmodeled),
            "impossible_roles": list(self.impossible_roles),
            "weak_only_roles": list(self.weak_only_roles),
            "negative_space_only_roles": list(self.negative_space_only_roles),
            "notes": list(self.notes),
        }


def analyze_question(
    q: dict,
    inventory: ExtractorInventory,
) -> QuestionGap:
    expected = list(q.get("required_roles") or q.get("expected_roles") or [])
    role_to_contracts: dict[str, list[str]] = {}
    impossible: list[str] = []
    union_required_bits: set[tuple[AxisName, str]] = set()
    required_kinds: set[str] = set()

    for role in expected:
        contracts = ROLE_CONTRACTS.get(role)
        if contracts is None:
            impossible.append(role)
            continue
        role_to_contracts[role] = list(contracts)
        # For coverage purposes we take the union of the FIRST alternative in each
        # slot of every contract. This is intentionally pessimistic — it counts a
        # bit as required if any contract for any expected role lists it.
        for contract_name in contracts:
            spec = CONTRACTS.get(contract_name)
            if spec is None:
                continue
            if spec.container_kind:
                required_kinds.add(spec.container_kind)
            for axis, slot in spec.axis_slots():
                # For coverage we require ANY of the alternatives — if even one
                # alternative is in the inventory we count that slot satisfied.
                # We still record the canonical first option as "required" for
                # the present/missing view.
                if not slot:
                    continue
                if any(inventory.has(axis, b) for b in slot):
                    union_required_bits.add((axis, slot[0]))
                else:
                    union_required_bits.add((axis, slot[0]))

    present: list[tuple[AxisName, str]] = []
    missing: list[tuple[AxisName, str]] = []
    for axis, bit in sorted(union_required_bits):
        # We treat "any-of" success as present (so this slot's canonical name is
        # present even if the actual emitted bit is an alternative).
        if inventory.has(axis, bit):
            present.append((axis, bit))
            continue
        # Try alternatives by looking back at every contract slot that quoted
        # this bit as its head.
        satisfied = False
        for contract_list in role_to_contracts.values():
            for cname in contract_list:
                spec = CONTRACTS.get(cname)
                if not spec:
                    continue
                for ax, slot in spec.axis_slots():
                    if ax == axis and slot and slot[0] == bit:
                        if any(inventory.has(ax, alt) for alt in slot):
                            satisfied = True
                            break
                if satisfied:
                    break
            if satisfied:
                break
        if satisfied:
            present.append((axis, bit))
        else:
            missing.append((axis, bit))

    unmodeled_kinds = sorted(
        k for k in required_kinds if k not in CONTAINER_KINDS and k != "registry_kind"
    )

    notes: list[str] = []
    if impossible:
        notes.append("roles without contract mapping yet: " + ", ".join(sorted(set(impossible))))
    if not expected:
        notes.append("no expected_roles in question — coverage cannot be measured")

    # Identify roles whose contracts are all weakly-discriminating. These
    # questions count as covered in the headline number but L3 will only
    # have weak signals to work with.
    weak_only = sorted(
        role
        for role, contracts in role_to_contracts.items()
        if contracts and all(c in WEAK_CONTRACTS for c in contracts)
    )
    # Identify roles whose contracts are all negative-space (structurally
    # inconspicuous runtime positions). Covered honestly but the proof
    # depends on L2 container kind classification + graph topology.
    negative_space_only = sorted(
        role
        for role, contracts in role_to_contracts.items()
        if contracts and all(c in NEGATIVE_SPACE_CONTRACTS for c in contracts)
    )

    return QuestionGap(
        question_id=str(q.get("id")),
        repo=str(q.get("repo") or ""),
        seed=q.get("symbol"),
        intent=q.get("intent"),
        expected_roles=expected,
        role_to_contracts=role_to_contracts,
        required_bits_present=present,
        required_bits_missing=missing,
        container_kinds_required=sorted(required_kinds),
        container_kinds_unmodeled=unmodeled_kinds,
        impossible_roles=sorted(set(impossible)),
        weak_only_roles=weak_only,
        negative_space_only_roles=negative_space_only,
        notes=notes,
    )


def coverage_report(
    questions: list[dict],
    inventory: ExtractorInventory,
) -> dict[str, object]:
    gaps = [analyze_question(q, inventory) for q in questions]

    # Aggregate metrics
    role_freq: Counter[str] = Counter()
    bit_demand: Counter[tuple[AxisName, str]] = Counter()
    bit_supplied: Counter[tuple[AxisName, str]] = Counter()
    missing_bit_demand: Counter[tuple[AxisName, str]] = Counter()
    container_demand: Counter[str] = Counter()
    container_unmodeled: Counter[str] = Counter()
    impossible_roles: Counter[str] = Counter()

    for g in gaps:
        for role in g.expected_roles:
            role_freq[role] += 1
        for r in g.impossible_roles:
            impossible_roles[r] += 1
        for k in g.container_kinds_required:
            container_demand[k] += 1
        for k in g.container_kinds_unmodeled:
            container_unmodeled[k] += 1
        for ax, b in g.required_bits_present:
            bit_demand[(ax, b)] += 1
            bit_supplied[(ax, b)] += 1
        for ax, b in g.required_bits_missing:
            bit_demand[(ax, b)] += 1
            missing_bit_demand[(ax, b)] += 1

    return {
        "summary": {
            "total_questions": len(gaps),
            "questions_with_all_required_bit_types_modeled": sum(
                1 for g in gaps if not g.required_bits_missing
            ),
            "questions_with_unmodeled_required_bit_types": sum(
                1 for g in gaps if g.required_bits_missing
            ),
            "questions_with_impossible_roles": sum(1 for g in gaps if g.impossible_roles),
            "questions_with_unmodeled_kinds": sum(1 for g in gaps if g.container_kinds_unmodeled),
            "questions_with_only_weak_role_contracts": sum(1 for g in gaps if g.weak_only_roles),
            "questions_with_only_negative_space_role_contracts": sum(
                1 for g in gaps if g.negative_space_only_roles
            ),
        },
        "role_demand": role_freq.most_common(),
        "impossible_roles": impossible_roles.most_common(),
        "bit_demand": {
            "all": {f"{a}.{b}": c for (a, b), c in bit_demand.most_common()},
            "missing": {f"{a}.{b}": c for (a, b), c in missing_bit_demand.most_common()},
            "supplied": {f"{a}.{b}": c for (a, b), c in bit_supplied.most_common()},
        },
        "container_kind_demand": container_demand.most_common(),
        "container_kinds_unmodeled": container_unmodeled.most_common(),
        "extractor_inventory": inventory.to_dict(),
        "per_question": [g.to_dict() for g in gaps],
    }


# ---------------------------------------------------------------------------
# Markdown report writer
# ---------------------------------------------------------------------------


def render_markdown(report: dict, output: Path) -> None:
    s = report["summary"]
    lines: list[str] = []
    lines.append("# Axis fact coverage analysis\n")
    lines.append(
        "Generated by `QA.axis_analysis`. This is L1+L2 inventory only: "
        "what bits the extractor can emit today, what contracts the benchmark "
        "questions would need closed, what container kinds those contracts "
        "imply. Not a policy decision — input for one.\n\n"
    )
    lines.append("## Summary\n\n")
    lines.append(f"- total questions: **{s['total_questions']}**\n")
    lines.append(
        f"- with all required bit types modeled: **{s['questions_with_all_required_bit_types_modeled']}**\n"
    )
    lines.append(
        f"- with at least one unmodeled required bit type: **{s['questions_with_unmodeled_required_bit_types']}**\n"
    )
    lines.append(
        "- note: this is inventory coverage only, not proof that a specific benchmark symbol actually has those bits.\n"
    )
    lines.append(f"- with unmodeled container kinds: **{s['questions_with_unmodeled_kinds']}**\n")
    lines.append(
        f"- with at least one role that has no contract mapping yet: **{s['questions_with_impossible_roles']}**\n"
    )
    lines.append(
        f"- with at least one role mapped only to weakly-discriminating contracts: **{s.get('questions_with_only_weak_role_contracts', 0)}**\n"
    )
    lines.append(
        f"- with at least one role mapped only to negative-space contracts: **{s.get('questions_with_only_negative_space_role_contracts', 0)}**\n\n"
    )
    if WEAK_CONTRACTS:
        lines.append(
            f"`WEAK_CONTRACTS = {{ {', '.join(sorted(WEAK_CONTRACTS))} }}` — required bits are themselves background facts (e.g. just `cfg.call_site`). A role mapped only to weak contracts technically counts as covered but cannot drive discrimination at the role resolver. L3 has to grade contract strength.\n\n"
        )
    lines.append(
        f"`NEGATIVE_SPACE_CONTRACTS = {{ {', '.join(sorted(NEGATIVE_SPACE_CONTRACTS))} }}` — positive contracts whose proof is built on absence: structurally inconspicuous runtime nodes (low edge density to kind-classified containers; dispersion of callers across packages; not a CFG driver). The L1 axis-bit content is permissive; the discriminator lives in L2/L3 graph-context predicates. NOT a fallback — honest evidence of background runtime position.\n\n"
    )

    lines.append("## Top role demand\n\n")
    lines.append("| role | questions |\n|---|---|\n")
    for role, count in report["role_demand"][:30]:
        lines.append(f"| `{role}` | {count} |\n")
    lines.append("\n")

    if report["impossible_roles"]:
        lines.append("## Roles without contract mapping (analyst gap)\n\n")
        lines.append(
            "These roles appear in question packs but are not yet wired to any contract family. Either the role is too generic (should be split into mechanism-specific contracts) or the mapping in `QA.axis_analysis.ROLE_CONTRACTS` is incomplete.\n\n"
        )
        lines.append("| role | questions |\n|---|---|\n")
        for role, count in report["impossible_roles"]:
            lines.append(f"| `{role}` | {count} |\n")
        lines.append("\n")

    lines.append("## Bit demand vs supply\n\n")
    lines.append(
        "Each row: a bit referenced by a contract that satisfies some expected role. ``demand`` is questions that would need it; ``supplied`` is whether the extractor currently emits it.\n\n"
    )
    lines.append("| axis.bit | demand | supplied by extractor |\n|---|---|---|\n")
    for key, demand in sorted(report["bit_demand"]["all"].items(), key=lambda x: -x[1]):
        supplied = "✅" if key not in report["bit_demand"]["missing"] else "❌"
        lines.append(f"| `{key}` | {demand} | {supplied} |\n")
    lines.append("\n")

    if report["bit_demand"]["missing"]:
        lines.append("### Bits in demand but NOT emitted (P0 extractor gap)\n\n")
        lines.append("| axis.bit | demand |\n|---|---|\n")
        for key, demand in sorted(report["bit_demand"]["missing"].items(), key=lambda x: -x[1]):
            lines.append(f"| `{key}` | {demand} |\n")
        lines.append("\n")

    lines.append("## Container kinds in demand\n\n")
    lines.append(
        "Container kind is the L2 dependency of each contract. ``demand`` is questions whose chosen contract names this kind. ``modeled`` is whether `QA.axis_analysis.CONTAINER_KINDS` carries a fingerprint sketch for it.\n\n"
    )
    lines.append("| container_kind | demand | modeled |\n|---|---|---|\n")
    for kind, demand in report["container_kind_demand"]:
        modeled = "✅" if kind in CONTAINER_KINDS else "❌"
        lines.append(f"| `{kind}` | {demand} | {modeled} |\n")
    lines.append("\n")

    if report["container_kinds_unmodeled"]:
        lines.append("### Container kinds with no fingerprint sketch yet\n\n")
        lines.append("| container_kind | demand |\n|---|---|\n")
        for kind, demand in report["container_kinds_unmodeled"]:
            lines.append(f"| `{kind}` | {demand} |\n")
        lines.append("\n")

    lines.append("## Extractor inventory snapshot\n\n")
    inv = report["extractor_inventory"]
    for axis in ("cfg", "dfg", "struct"):
        lines.append(f"### {axis.upper()} ({len(inv[axis])} bits)\n\n")
        lines.append(", ".join(f"`{b}`" for b in inv[axis]))
        lines.append("\n\n")

    output.write_text("".join(lines))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _default_pack_paths() -> list[Path]:
    base = PROJECT_ROOT / "tests" / "fixtures"
    return [
        base / "questions_python.yaml",
        base / "click_questions.yaml",
        base / "celery_questions.yaml",
        base / "publick_repo_question_pack_2.yaml",
    ]


def run(
    *,
    pack_paths: list[Path] | None = None,
    out_dir: Path = Path("/tmp/axis_analysis"),
) -> dict[str, object]:
    pack_paths = pack_paths or _default_pack_paths()
    out_dir.mkdir(parents=True, exist_ok=True)
    inventory = inventory_extractor_bits()
    questions = load_packs(pack_paths)
    report = coverage_report(questions, inventory)
    (out_dir / "axis_coverage.json").write_text(json.dumps(report, indent=2, sort_keys=False))
    render_markdown(report, out_dir / "axis_coverage.md")
    return report


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="/tmp/axis_analysis", type=Path)
    parser.add_argument("--pack", action="append", type=Path)
    args = parser.parse_args()
    rep = run(pack_paths=args.pack, out_dir=args.out)
    s = rep["summary"]
    print(
        f"total={s['total_questions']} "
        f"bit_types_modeled={s['questions_with_all_required_bit_types_modeled']} "
        f"with_unmodeled_bit_types={s['questions_with_unmodeled_required_bit_types']} "
        f"unmodeled_kinds={s['questions_with_unmodeled_kinds']}"
    )
    print(f"Reports: {args.out}/axis_coverage.json  {args.out}/axis_coverage.md")
