"""Discriminator-first L1/L2 role assignment prototype.

Implements the pipeline described in docs/role_clustering_architecture.md:
L1 macro buckets (rule gates) → L2 role predicates (catalog discriminators) →
presence gate → per-symbol primary + supporting roles.

Designed for QA/prototype_multidim_fan_clustering.py; no production imports.
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
_SETUP_DEPTH_MAX = 2
_RUNTIME_CALL_IN_MIN = 1.0


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
    "compute_leaf",
    "unclassified",
)

# L2 predicates ordered by specificity (highest first) within each L1.
# See docs/role_catalog.md distinctiveness summary + §9–§10.
L2_PREDICATES: tuple[RolePredicate, ...] = (
    # --- routing_wrap ---
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
        "request_router",
        "routing_wrap",
        lambda r: r.handle_fan_out > _EPS
        and r.depth_from_public <= 4
        and r.call_fan_in >= _RUNTIME_CALL_IN_MIN,
        80,
    ),
    RolePredicate(
        "registration_step",
        "routing_wrap",
        lambda r: r.handle_fan_out > _EPS
        and r.depth_from_public <= _SETUP_DEPTH_MAX
        and r.call_fan_in < _RUNTIME_CALL_IN_MIN,
        75,
    ),
    RolePredicate(
        "executor",
        "routing_wrap",
        lambda r: r.handle_fan_in > _EPS,
        70,
    ),
    # --- control_flow ---
    RolePredicate(
        "dependency_solver",
        "control_flow",
        lambda r: r.type_fan_in_isinstance > _EPS or r.inject_fan_in > _EPS,
        80,
    ),
    RolePredicate(
        "composition_surface",
        "control_flow",
        lambda r: r.call_fan_out > r.call_fan_in
        and r.cross_package_call_out >= 1.0
        and r.import_in >= 2,
        75,
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
        # Constructs objects: either an explicit construction (INSTANTIATES fan-out,
        # the precise signal) or the return-typed-object heuristic. Both gated on
        # call_fan_out so a pure data class is not a factory.
        lambda r: (r.construct_fan_out > _EPS or r.type_fan_out_return > _EPS)
        and r.call_fan_out > _EPS,
        65,
    ),
    RolePredicate(
        "schema_builder",
        "control_flow",
        lambda r: r.call_fan_out > _EPS
        and r.type_fan_in > _EPS
        and r.type_fan_in_return <= _EPS,
        60,
    ),
    # --- state_types ---
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
        lambda r: r.type_fan_in_param > max(_EPS, r.call_fan_in),
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
        # A documented class that exposes more operations (HAS_API fan-out) than it
        # is consumed as a type is the public operational surface (FastAPI,
        # APIRouter); depth/api_fan_in are unreliable here because its callers are
        # user-side and excluded (F12/F13). The `> type_fan_in` guard keeps classes
        # consumed as types (models, config) in representation/config_surface.
        # A symbol surfaced on the package public API (RE_EXPORTS in-degree) that
        # also exposes substantial behavior (HAS_API fan-out) is the operational
        # api_surface (FastAPI, APIRouter). reexport_in is the orthogonal "I am the
        # public surface" axis — independent of type_fan_in (under which FastAPI is
        # also consumed as a type) and of depth/api_fan_in (unreliable under F13).
        # Re-exported markers/data classes with little behavior (Body, Settings)
        # keep their config/representation primary.
        or (r.is_class and r.reexport_in > _EPS and r.api_fan_out > _EPS),
        70,
    ),
    # --- compute_leaf ---
    RolePredicate(
        "executor",
        "compute_leaf",
        lambda r: r.handle_fan_in > _EPS,
        85,
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
    }
)


def assign_l1(row: FanProfile) -> str:
    # A documented class that exposes an API surface (HAS_API fan-out) is a public
    # entry, not dead code, even at zero internal in-degree: its callers live in
    # user code that Pass-1 excludes (F12/F13). Exempt it from the noise sink.
    surface_class = row.is_class and (row.api_fan_out > _EPS or row.has_documentation)
    if row.zero_in_degree and row.call_fan_out <= _EPS and not surface_class:
        return "noise"
    if row.is_proxy_binding:
        return "routing_wrap"
    if (
        row.handle_fan_in > _EPS
        or row.handle_fan_out > _EPS
        or row.decorated_in > _EPS
    ):
        return "routing_wrap"
    if row.call_fan_out > row.call_fan_in and row.call_fan_out > _EPS:
        return "control_flow"
    if row.is_class and (
        row.type_fan_in > _EPS
        or row.depend_fan_in > _EPS
        or row.api_fan_in > _EPS
        or row.api_fan_out > _EPS
    ):
        return "state_types"
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
    """All L2 roles the cascade can emit (for phantom-role comparison)."""
    roles = set(L1_FALLBACK_ROLE.values())
    for pred in L2_PREDICATES:
        roles.add(pred.role)
    roles.discard("orphan")
    return tuple(sorted(roles))
