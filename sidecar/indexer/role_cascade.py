"""Discriminator-first L1/L2 role assignment (Pass 1).

Pipeline: L1 macro buckets → L2 role predicates → presence gate → per-symbol
primary + supporting roles. See docs/role_clustering_architecture.md.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Callable, Protocol


class FanProfile(Protocol):
    """Structural fan profile consumed by cascade predicates."""

    uid: str
    kind: str
    call_fan_in: float
    call_fan_out: float
    type_fan_in: float
    type_fan_out: float
    type_fan_in_param: float
    type_fan_in_isinstance: float
    type_fan_in_return: float
    type_fan_out_return: float
    api_fan_in: float
    api_fan_out: float
    inject_fan_in: float
    depend_fan_in: float
    depend_fan_out: float
    handle_fan_in: float
    handle_fan_out: float
    handler_call_fan_out: float
    decorated_in: float
    decorated_out: float
    construct_fan_out: float
    cross_package_call_in: float
    cross_package_call_out: float
    depth_from_public: int
    import_in: int
    reexport_in: int
    doc_anchor_count: int
    doc_definition_weight: float
    is_proxy_binding: bool
    external_call_fan_out: float
    external_import_fan_out: float
    external_root_count: int
    external_integration_call_fan_out: float
    external_integration_import_fan_out: float
    external_integration_root_count: int
    inherits_builtin_exception: bool

    @property
    def external_call_out_ratio(self) -> float: ...

    @property
    def external_integration_out_ratio(self) -> float: ...

    @property
    def is_class(self) -> bool: ...

    @property
    def is_function(self) -> bool: ...

    @property
    def has_documentation(self) -> bool: ...

    @property
    def call_leaf(self) -> bool: ...

    @property
    def zero_in_degree(self) -> bool: ...


_EPS = 0.05
_RUNTIME_CALL_IN_MIN = 1.0
_INTEGRATION_CALL_RATIO_MIN = 0.35
_INTEGRATION_IMPORT_MIN = 2.0


def _integration_boundary_signal(row: FanProfile) -> bool:
    """True when external topology reads as a layer gateway (C2), not plumbing."""
    ext_call = row.external_integration_call_fan_out
    if ext_call <= _EPS:
        return False
    if row.type_fan_in > max(_EPS, row.call_fan_out * 2.0):
        return False
    if row.call_fan_out <= row.call_fan_in:
        return False
    return row.external_integration_out_ratio >= _INTEGRATION_CALL_RATIO_MIN


@dataclass(frozen=True)
class RolePredicate:
    role: str
    l1: str | None
    check: Callable[[FanProfile], bool]
    specificity: int


@dataclass
class SymbolRoleAssignment:
    uid: str
    l1: str
    primary: str
    supporting: tuple[str, ...] = ()
    hits: tuple[str, ...] = ()


L1_BUCKETS = (
    "noise",
    "routing_wrap",
    "control_flow",
    "state_types",
    "boundary_integration",
    "compute_leaf",
    "unclassified",
)

L2_PREDICATES: tuple[RolePredicate, ...] = (
    RolePredicate(
        "proxy_mechanism",
        "routing_wrap",
        lambda r: r.is_proxy_binding,
        90,
    ),
    RolePredicate(
        "interceptor",
        "routing_wrap",
        lambda r: r.decorated_in > _EPS
        and r.handle_fan_out <= _EPS
        and r.type_fan_in_param < max(1.0, r.call_fan_in),
        85,
    ),
    RolePredicate(
        "registration_step",
        "routing_wrap",
        lambda r: r.handle_fan_out > _EPS,
        85,
    ),
    RolePredicate(
        "executor",
        "routing_wrap",
        lambda r: r.handle_fan_in > _EPS,
        70,
    ),
    RolePredicate(
        "dependency_solver",
        "control_flow",
        lambda r: r.type_fan_in_isinstance > _EPS
        or r.inject_fan_in > _EPS
        or (
            r.type_fan_out > _EPS
            and r.cross_package_call_out >= 2.0
            and r.import_in >= 20
            and r.depth_from_public >= 2
        ),
        72,
    ),
    RolePredicate(
        "request_router",
        "control_flow",
        lambda r: r.handle_fan_out <= _EPS
        and r.handle_fan_in <= _EPS
        and r.call_fan_in >= _RUNTIME_CALL_IN_MIN
        and r.handler_call_fan_out > _EPS,
        78,
    ),
    RolePredicate(
        "api_surface",
        "control_flow",
        lambda r: r.depth_from_public <= 1
        and r.call_fan_out > r.call_fan_in
        and (
            (r.has_documentation and (r.api_fan_in > _EPS or r.doc_definition_weight > 0))
            or r.import_in >= 10
        ),
        77,
    ),
    RolePredicate(
        "registration_step",
        "control_flow",
        lambda r: r.depth_from_public <= 1
        and r.call_fan_out > r.call_fan_in
        and r.construct_fan_out > _EPS
        and r.import_in >= 10,
        74,
    ),
    RolePredicate(
        "composition_surface",
        "control_flow",
        lambda r: r.call_fan_out > r.call_fan_in
        and r.cross_package_call_out >= 1.0
        and r.import_in >= 2,
        66,
    ),
    RolePredicate(
        "orchestrator",
        "control_flow",
        lambda r: r.call_fan_out > r.call_fan_in and r.call_fan_out > _EPS,
        70,
    ),
    RolePredicate(
        "factory_surface",
        "control_flow",
        lambda r: (r.construct_fan_out > _EPS or r.type_fan_out_return > _EPS)
        and r.call_fan_out > _EPS,
        65,
    ),
    RolePredicate(
        "schema_builder",
        "control_flow",
        lambda r: r.call_fan_out > _EPS
        and (
            (r.type_fan_in > _EPS and r.type_fan_in_return <= _EPS)
            or (r.construct_fan_out > _EPS and r.depth_from_public <= 1)
            or (r.type_fan_out > _EPS and r.call_fan_out > r.call_fan_in and r.import_in >= 20)
        ),
        71,
    ),
    RolePredicate(
        "binding_surface",
        "control_flow",
        lambda r: r.call_fan_out > r.call_fan_in
        and r.type_fan_out > _EPS
        and r.cross_package_call_out >= 1.0
        and r.import_in >= 20
        and r.depth_from_public >= 2,
        73,
    ),
    RolePredicate(
        "runtime_surface",
        "control_flow",
        lambda r: r.call_fan_in > _EPS
        and r.call_fan_out > _EPS
        and r.depth_from_public <= 2
        and r.import_in >= 10,
        71,
    ),
    RolePredicate(
        "integration_surface",
        "boundary_integration",
        lambda r: r.external_integration_call_fan_out > _EPS
        or (
            r.external_integration_import_fan_out >= _INTEGRATION_IMPORT_MIN
            and r.external_integration_call_fan_out > _EPS
        ),
        80,
    ),
    RolePredicate(
        "error_surface",
        "state_types",
        # A class inheriting a builtin exception (ValueError/Exception/...) is an
        # error type. The base is a builtin (not an in-graph node), so this comes
        # from the `inherits_builtin_exception` marker set at link time — a real AST
        # fact (`class X(..., ValueError)`), not a name match.
        lambda r: r.inherits_builtin_exception,
        88,
    ),
    RolePredicate(
        "abstract_contract",
        "state_types",
        lambda r: r.is_class
        and r.depend_fan_in > max(_EPS, r.type_fan_in_param * 1.5)
        and r.call_fan_in <= _EPS,
        85,
    ),
    RolePredicate(
        "config_surface",
        "state_types",
        lambda r: r.is_class
        and not (r.reexport_in > _EPS and r.api_fan_out > _EPS and r.call_fan_in > _EPS)
        and (
            r.type_fan_in_param > max(_EPS, r.call_fan_in)
            or (
                r.type_fan_in_param <= _EPS
                and r.depend_fan_in > _EPS
                and r.type_fan_in_isinstance > _EPS
                and r.type_fan_in <= max(1.0, r.call_fan_in)
            )
        ),
        80,
    ),
    RolePredicate(
        "representation_surface",
        "state_types",
        lambda r: r.is_class
        and r.type_fan_in > max(_EPS, r.call_fan_out * 2.0),
        75,
    ),
    RolePredicate(
        "api_surface",
        "state_types",
        lambda r: (
            r.depth_from_public <= 1
            and r.has_documentation
            and (r.api_fan_in > _EPS or r.doc_definition_weight > 0)
        )
        or (
            r.is_function
            and r.depth_from_public <= 1
            and r.depend_fan_in > _EPS
            and r.call_fan_out > _EPS
        )
        or (r.is_class and r.reexport_in > _EPS and r.api_fan_out > _EPS),
        70,
    ),
    RolePredicate(
        "registration_step",
        "state_types",
        lambda r: r.is_class
        and r.reexport_in > _EPS
        and r.api_fan_out > _EPS
        and r.call_fan_in > _EPS,
        68,
    ),
    RolePredicate(
        "dependency_solver",
        "state_types",
        # Function-side (existing): a depended-on public function is a solver.
        # Class-side (added): a class that holds isinstance-dispatch inside its
        # methods aggregates `type_fan_in_isinstance` at the class level — it IS
        # the dispatcher. Lands here because rich classes (high type_fan_in) go to
        # state_types, never reaching the control_flow dependency_solver predicate.
        lambda r: (
            r.is_function and r.depend_fan_in > _EPS and r.depth_from_public <= 1
        )
        or (r.is_class and r.type_fan_in_isinstance > _EPS),
        67,
    ),
    RolePredicate(
        "runtime_surface",
        "state_types",
        lambda r: r.is_class
        and r.call_fan_in > _EPS
        and (r.type_fan_in_param > _EPS or r.reexport_in > _EPS),
        69,
    ),
    RolePredicate(
        "executor",
        "compute_leaf",
        lambda r: r.handle_fan_in > _EPS,
        85,
    ),
    RolePredicate(
        "request_router",
        "compute_leaf",
        lambda r: r.handle_fan_out <= _EPS
        and r.handle_fan_in <= _EPS
        and r.call_fan_in >= _RUNTIME_CALL_IN_MIN
        and r.handler_call_fan_out > _EPS,
        78,
    ),
    RolePredicate(
        "validator_handle",
        "compute_leaf",
        lambda r: r.type_fan_in > _EPS
        and r.call_fan_in > _EPS
        and r.handle_fan_in <= _EPS,
        80,
    ),
    RolePredicate(
        "core_runtime",
        "compute_leaf",
        lambda r: r.call_fan_in > _EPS
        and r.call_leaf
        and r.handle_fan_in <= _EPS
        and r.type_fan_in <= max(1.0, r.call_fan_in),
        75,
    ),
    RolePredicate(
        "runtime_surface",
        "compute_leaf",
        lambda r: r.call_fan_in > _EPS
        and not r.call_leaf
        and r.depth_from_public <= 2
        and r.import_in >= 8,
        72,
    ),
    RolePredicate(
        "executor",
        "compute_leaf",
        lambda r: r.call_leaf and r.call_fan_in > _EPS and r.is_function,
        60,
    ),
)

L1_FALLBACK_ROLE: dict[str, str] = {
    "routing_wrap": "runtime_surface",
    "control_flow": "orchestrator",
    "state_types": "representation_surface",
    "boundary_integration": "integration_surface",
    "compute_leaf": "core_runtime",
    "noise": "orphan",
    "unclassified": "supporting_surface",
}

MAX_SUPPORTING = 3
DEFAULT_MIN_SUPPORT = 2
RARE_ROLE_MIN_SUPPORT = 1
RARE_ROLES = frozenset(
    {
        "proxy_mechanism",
        "interceptor",
        "abstract_contract",
        "registration_step",
        "request_router",
        "dependency_solver",
        "schema_builder",
        "integration_surface",
    }
)


def assign_l1(row: FanProfile) -> str:
    # 1. noise — dead code / orphans, but never a documented or API-exposing class
    #    (a public surface has zero *internal* in-degree by design), and never a
    #    proxy_binding (its only edge is PROXY_OF, which is not counted as in-degree
    #    but is the very signal that routes it to dispatch_and_wrap below).
    surface_class = row.is_class and (row.api_fan_out > _EPS or row.has_documentation)
    if (
        row.zero_in_degree
        and row.call_fan_out <= _EPS
        and not surface_class
        and not row.is_proxy_binding
    ):
        return "noise"
    # 2. dispatch_and_wrap — any dispatch/decoration marker (HANDLES in/out, proxy,
    #    decorated). The most specific topology in the graph; a symbol wired into the
    #    dispatch system is caught here whole (proxy_mechanism, interceptor,
    #    registration_step, executor, request_router all live in this bucket — Trap-3:
    #    a single logical role must not be split across L1 buckets).
    if (
        row.is_proxy_binding
        or row.handle_fan_in > _EPS
        or row.handle_fan_out > _EPS
        or row.decorated_in > _EPS
    ):
        return "routing_wrap"
    # 3. state_and_types — BEFORE control_flow. A fundamental data type/contract is
    #    classified by what it *is* (strong type/depend fan-in relative to what it
    #    calls), not by how many utilities it calls internally. Guards the "fat model"
    #    trap (a model with call_out > call_in must not leak to control_flow→
    #    orchestrator). No is_class gate: typing topology is primary, syntax (class vs
    #    module var / TypedDict / descriptor) secondary — non-class DTOs qualify too.
    #    An exception type is a data contract too (error_surface), even when its only
    #    in-graph signal is the builtin-exception inheritance marker.
    if (
        row.inherits_builtin_exception
        or row.type_fan_in > max(_EPS, row.call_fan_out)
        or row.depend_fan_in > max(_EPS, row.call_fan_out)
    ):
        return "state_types"
    # (kept) class with any type/api evidence but weaker than its call_out still reads
    # as a type surface rather than control flow.
    if row.is_class and (
        row.type_fan_in > _EPS
        or row.depend_fan_in > _EPS
        or row.api_fan_in > _EPS
        or row.api_fan_out > _EPS
    ):
        return "state_types"
    # 4. boundary_integration — external call/import topology (C2). Uses filtered
    #    integration fan (stdlib/test/doc plumbing excluded). Must not steal type hubs.
    if _integration_boundary_signal(row):
        return "boundary_integration"
    # 5. control_flow — orchestration/factories: calls many, is not a data type.
    if row.call_fan_out > row.call_fan_in and row.call_fan_out > _EPS:
        return "control_flow"
    # 6. compute_leaf — heavily reused terminal utilities.
    if row.call_fan_in > _EPS and row.call_leaf:
        return "compute_leaf"
    if row.call_fan_in > _EPS or row.call_fan_out > _EPS:
        return "control_flow" if row.call_fan_out >= row.call_fan_in else "compute_leaf"
    return "unclassified"


def _matching_roles(row: FanProfile, l1: str) -> list[tuple[str, int]]:
    hits: list[tuple[str, int]] = []
    seen: set[str] = set()
    for pred in sorted(L2_PREDICATES, key=lambda p: p.specificity, reverse=True):
        if pred.l1 is not None and pred.l1 != l1:
            continue
        if not pred.check(row):
            continue
        if pred.role in seen:
            continue
        seen.add(pred.role)
        hits.append((pred.role, pred.specificity))
    return hits


def assign_symbol_roles(row: FanProfile) -> SymbolRoleAssignment:
    l1 = assign_l1(row)
    hits = _matching_roles(row, l1)
    if hits:
        primary = hits[0][0]
        supporting = tuple(role for role, _ in hits[1 : 1 + MAX_SUPPORTING])
    else:
        primary = L1_FALLBACK_ROLE.get(l1, "supporting_surface")
        supporting = ()
    return SymbolRoleAssignment(
        uid=row.uid,
        l1=l1,
        primary=primary,
        supporting=supporting,
        hits=tuple(role for role, _ in hits),
    )


def assign_all(rows: list[FanProfile]) -> dict[str, SymbolRoleAssignment]:
    return {row.uid: assign_symbol_roles(row) for row in rows}


def detect_present_roles(
    assignments: dict[str, SymbolRoleAssignment],
    *,
    min_support: int = DEFAULT_MIN_SUPPORT,
) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for asn in assignments.values():
        for role in (asn.primary, *asn.supporting):
            counts[role] += 1
    present: dict[str, int] = {}
    for role, count in counts.items():
        threshold = RARE_ROLE_MIN_SUPPORT if role in RARE_ROLES else min_support
        if count >= threshold:
            present[role] = count
    return dict(sorted(present.items(), key=lambda item: (-item[1], item[0])))


def role_catalog_roles() -> tuple[str, ...]:
    roles = set(L1_FALLBACK_ROLE.values())
    for pred in L2_PREDICATES:
        roles.add(pred.role)
    roles.discard("orphan")
    return tuple(sorted(roles))
