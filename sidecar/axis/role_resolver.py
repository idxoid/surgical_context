"""L4 role resolver — contracts → user-facing roles.

Per ``docs/python_ast_axes_and_traversal_modes.md``:

  > A role is a user-facing or benchmark-facing requirement. The thing a
  > question expects to find. A role is satisfied when ≥1 of its
  > contracts is proven.

This module is the *only* place axis-layer answers user-facing role
questions. It owns one structural table:

  ``ROLE_CONTRACT_MAP: {role_name: frozenset of contract names}``

The table is a small, hand-curated OR-of-contracts mapping. Each role
names what the user / ranker / benchmark asks for; each contract listed
under a role is a *structural proof shape* that, if proven on a symbol,
satisfies that role on the same symbol. Roles are **not** axis bits and
**not** container kinds — they are the final consumer-facing layer.

Discipline (mirrors the catalogue's transition-shim rules):

  - A role's contract list is the entire set of axis-proven shapes that
    satisfy it. Adding a new contract that conceptually belongs to a
    role must be reflected here, *and* its inclusion must be honest —
    the contract has to genuinely answer the role's question.

  - A role with zero contracts in the map is a no-op (returns empty
    set). Don't add empty roles to the map.

  - No role definition references container kinds, symbol names, or
    framework names. Roles are answered through contracts; contracts
    have all the structural context.

  - When ambiguity arises (``callable_container_dispatch`` proves both
    middleware and signal patterns), the contract list contains it for
    *any* role it can legitimately answer. Distinguishing middleware
    vs signal at the role layer requires inspecting the underlying
    container kind, which is :func:`resolve_roles_with_evidence`'s job
    rather than the bare :func:`resolve_roles`'s.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# The mapping — small, structural, hand-curated.
# ---------------------------------------------------------------------------


ROLE_CONTRACT_MAP: dict[str, frozenset[str]] = {
    # ``binding_surface`` is the broad "values are bound now so runtime
    # can dispatch them later" role — every deferred-binding shape
    # qualifies, regardless of subtype.
    "binding_surface": frozenset({
        "route_register_binding",
        "task_register_binding",
        "error_dispatch_binding",
        "registry_binding_inferred",
        "callable_container_dispatch",
        "metadata_key_roundtrip",
        "provider_default_binding",
        "dependency_injection_binding",
        "proxy_indirection",
    }),
    # ``routing_surface`` is specifically web route binding — the
    # ``route_register_binding`` contract is the only L3 proof for it.
    "routing_surface": frozenset({"route_register_binding"}),
    # ``task_surface`` mirrors routing for task-queue registration.
    "task_surface": frozenset({"task_register_binding"}),
    # ``error_surface`` covers the exception-dispatch case at L3.
    "error_surface": frozenset({"error_dispatch_binding"}),
    # ``proxy_mechanism`` is the lazy-resolution / context-binding role
    # the legacy cascade carried; proven structurally by proxy_indirection.
    "proxy_mechanism": frozenset({"proxy_indirection"}),
    # ``dependency_solver`` covers the provider→consumer wiring role.
    # Both contracts qualify; ``dependency_injection_binding`` brings
    # the cross-symbol proof on top of the shape-only one.
    "dependency_solver": frozenset({
        "provider_default_binding",
        "dependency_injection_binding",
    }),
    # ``data_model_surface`` mirrors the L3 data_shape_declaration —
    # purely structural class-with-typed-attrs surfaces.
    "data_model_surface": frozenset({"data_shape_declaration"}),
    # ``configuration_surface`` is the config-carrier shape.
    "configuration_surface": frozenset({"configuration_carrier"}),
    # ``metadata_surface`` is the keyed write/read roundtrip — the
    # ``metadata_carrier`` shape qualifies as a metadata surface for
    # role-level questions about declarative metadata.
    "metadata_surface": frozenset({"metadata_key_roundtrip"}),
    # ``dispatch_surface`` is the callable-container dispatch role
    # (middleware/signal/registry chains where iteration invokes
    # stored callables).
    "dispatch_surface": frozenset({"callable_container_dispatch"}),
}


# ---------------------------------------------------------------------------
# Lookup API
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RoleResolution:
    """One role satisfied on a symbol, with the contracts that proved it."""

    role: str
    satisfying_contracts: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "role": self.role,
            "satisfying_contracts": list(self.satisfying_contracts),
        }


def resolve_roles(contract_names: Iterable[str]) -> set[str]:
    """Return every role at least one of ``contract_names`` satisfies."""
    proven = set(contract_names)
    return {
        role
        for role, contracts in ROLE_CONTRACT_MAP.items()
        if contracts & proven
    }


def resolve_roles_with_evidence(
    contract_names: Iterable[str],
) -> list[RoleResolution]:
    """Same as :func:`resolve_roles` but each result carries the subset
    of contracts that actually fired for that role.
    """
    proven = set(contract_names)
    out: list[RoleResolution] = []
    for role in sorted(ROLE_CONTRACT_MAP):
        matched = tuple(sorted(ROLE_CONTRACT_MAP[role] & proven))
        if not matched:
            continue
        out.append(RoleResolution(role=role, satisfying_contracts=matched))
    return out


def registered_roles() -> set[str]:
    """Every role name the resolver knows about."""
    return set(ROLE_CONTRACT_MAP)


def registered_contracts() -> set[str]:
    """Every contract name that appears under at least one role."""
    out: set[str] = set()
    for contracts in ROLE_CONTRACT_MAP.values():
        out |= contracts
    return out


__all__ = [
    "ROLE_CONTRACT_MAP",
    "RoleResolution",
    "registered_contracts",
    "registered_roles",
    "resolve_roles",
    "resolve_roles_with_evidence",
]
