"""L4 role resolver — L3 contracts + L2 container kinds → user-facing roles.

Per ``docs/python_ast_axes_and_traversal_modes.md``:

  > A role is a user-facing or benchmark-facing requirement. The thing a
  > question expects to find. A role is satisfied when ≥1 of its
  > contracts is proven.

This module is the *only* place axis-layer answers user-facing role
questions. It owns one structural table:

  ``ROLE_EVIDENCE_MAP: {role_name: RoleEvidence(contracts, kinds)}``

Each role can be satisfied by EITHER a proven L3 contract OR a recognised
L2 container kind on the symbol. Both halves matter:

  - **Contract evidence** is the strongest form — it carries proof that
    the structural pattern is actually *used* (e.g. a registry that has
    handlers bound). Consumer instances generate it.
  - **Kind evidence** is the existence-only form — the symbol IS the
    shape, but the use-proof side may be missing (e.g. a class definition
    inside a library has ``web_route_register`` kind but no
    ``route_register_binding`` contract because nothing decorates it
    inside its own workspace). Library-internal definitions need this
    channel so they aren't excluded from role-level retrieval.

Both halves are honest L2/L3 outputs; neither references symbol names
or framework labels. The split decouples "is this the shape?" (kind)
from "is it actually used?" (contract); a downstream scorer can prefer
contract-backed symbols when both are present.

Discipline (mirrors the catalogue's transition-shim rules):

  - A role's evidence is the entire set of axis-proven shapes that
    satisfy it. Adding a new contract / kind that conceptually belongs
    to a role must be reflected here, *and* its inclusion must be honest.

  - A role with zero contracts AND zero kinds in the map is a no-op.

  - No role definition references symbol names or framework names.

  - When ambiguity arises (``callable_container_dispatch`` proves both
    middleware and signal patterns), the contract list contains it for
    *any* role it can legitimately answer. Subtype distinctions
    (middleware vs signal vs metadata) come from the *kind* side.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# The mapping — small, structural, hand-curated.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RoleEvidence:
    """Two channels of evidence that satisfy one role."""

    contracts: frozenset[str]
    kinds: frozenset[str] = frozenset()

    def to_dict(self) -> dict[str, list[str]]:
        return {
            "contracts": sorted(self.contracts),
            "kinds": sorted(self.kinds),
        }


ROLE_EVIDENCE_MAP: dict[str, RoleEvidence] = {
    # ``binding_surface`` is the broad "values are bound now so runtime
    # can dispatch them later" umbrella role — every deferred-binding
    # shape qualifies, regardless of subtype.
    "binding_surface": RoleEvidence(
        contracts=frozenset({
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
        kinds=frozenset({
            "registry_class",
            "web_route_register",
            "task_register",
            "error_dispatch",
            "signal_register",
            "proxy_object",
            "di_container",
            "middleware_chain",
            "keyed_dispatch_callable",
            "keyed_register_callable",
            "metadata_carrier",
        }),
    ),
    # ``routing_surface`` is specifically web route binding —
    # registration *or* dispatch. ``keyed_register_callable`` covers
    # ``Flask.add_url_rule`` / ``APIRouter.add_api_route`` style
    # registration; ``keyed_dispatch_callable`` covers Flask's
    # ``dispatch_request``.
    "routing_surface": RoleEvidence(
        contracts=frozenset({"route_register_binding"}),
        kinds=frozenset({"web_route_register", "keyed_register_callable"}),
    ),
    # ``task_surface`` mirrors routing for task-queue registration.
    # ``keyed_register_callable`` catches Celery's
    # ``TaskRegistry.register`` (``self[task.name] = task``).
    "task_surface": RoleEvidence(
        contracts=frozenset({"task_register_binding"}),
        kinds=frozenset({"task_register", "keyed_register_callable"}),
    ),
    # ``error_surface`` covers both sides of error handling:
    # ``error_dispatch`` (code that catches and routes exceptions) and
    # ``error_model`` (the exception-type classes that carry and format
    # the error — e.g. click's ``UsageError`` / ``ClickException``
    # hierarchy). The ``error_model`` kind is propagated at index time
    # from builtin-exception inheritance (see
    # ``registry_class_inheritance.propagate_error_model_via_inheritance``).
    "error_surface": RoleEvidence(
        contracts=frozenset({"error_dispatch_binding"}),
        kinds=frozenset({"error_dispatch", "error_model"}),
    ),
    # ``proxy_mechanism`` is the lazy-resolution / context-binding role.
    "proxy_mechanism": RoleEvidence(
        contracts=frozenset({"proxy_indirection"}),
        kinds=frozenset({"proxy_object"}),
    ),
    # ``dependency_solver`` covers the provider→consumer wiring role.
    "dependency_solver": RoleEvidence(
        contracts=frozenset({
            "provider_default_binding",
            "dependency_injection_binding",
        }),
        kinds=frozenset({"di_container"}),
    ),
    # ``data_model_surface`` is the data_shape_declaration shape.
    "data_model_surface": RoleEvidence(
        contracts=frozenset({"data_shape_declaration"}),
        kinds=frozenset({"data_model"}),
    ),
    # ``configuration_surface`` is the config-carrier shape.
    "configuration_surface": RoleEvidence(
        contracts=frozenset({"configuration_carrier"}),
        kinds=frozenset({"config_carrier"}),
    ),
    # ``metadata_surface`` is the keyed write/read roundtrip.
    "metadata_surface": RoleEvidence(
        contracts=frozenset({"metadata_key_roundtrip"}),
        kinds=frozenset({"metadata_carrier"}),
    ),
    # ``dispatch_surface`` is the callable-container dispatch role:
    # middleware/signal chains where iteration invokes stored callables
    # *and* registry-keyed dispatchers that resolve one callable by key
    # and invoke it. Both shapes answer "where does the runtime pick a
    # callable out of a container and run it?" — the structurally
    # honest reading of the role.
    "dispatch_surface": RoleEvidence(
        contracts=frozenset({"callable_container_dispatch"}),
        kinds=frozenset({
            "middleware_chain",
            "signal_register",
            "keyed_dispatch_callable",
        }),
    ),
    # ``impact_analysis`` is a *question-shape* pseudo-role, not a
    # retrieval role: it has no contracts or kinds because no symbol
    # ever IS an impact answer in isolation. The shape fires when the
    # question asks "what depends on / breaks if / is affected by X",
    # and the consumer reads the empty evidence map as a signal to run
    # the blast-radius traversal on the other roles' candidates instead
    # of issuing a vector lookup. See
    # ``sidecar.axis.impact_traversal``.
    "impact_analysis": RoleEvidence(
        contracts=frozenset(),
        kinds=frozenset(),
    ),
    # ``trace_dependency`` is the second question-shape pseudo-role:
    # fires when the question asks "who calls X / what does X delegate
    # to / where does the flow go from here". Empty evidence means
    # vector retrieval is a no-op; the consumer reads the empty map as
    # a signal to run the impact / call-chain traversal on the other
    # roles' candidates.
    "trace_dependency": RoleEvidence(
        contracts=frozenset(),
        kinds=frozenset(),
    ),
    # ``structural_neighbour`` is a *file-level* pseudo-role used by
    # the AFFECTS-bridge pass (``sidecar.axis.structural_neighbours``)
    # to surface symbols that share the indexer's pre-computed impact
    # closure with the existing candidate pool but live in files no
    # other retrieval pass touched. Empty evidence = traversal-only.
    "structural_neighbour": RoleEvidence(
        contracts=frozenset(),
        kinds=frozenset(),
    ),
}


# Backward-compatible view: only the contract side. Existing callers
# that key by role and want contract names keep working.
ROLE_CONTRACT_MAP: dict[str, frozenset[str]] = {
    role: ev.contracts for role, ev in ROLE_EVIDENCE_MAP.items()
}


# ---------------------------------------------------------------------------
# Lookup API
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RoleResolution:
    """One role satisfied on a symbol, with the contracts AND kinds
    that proved it."""

    role: str
    satisfying_contracts: tuple[str, ...]
    satisfying_kinds: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "role": self.role,
            "satisfying_contracts": list(self.satisfying_contracts),
            "satisfying_kinds": list(self.satisfying_kinds),
        }


def resolve_roles(
    contract_names: Iterable[str] = (),
    *,
    container_kinds: Iterable[str] = (),
) -> set[str]:
    """Return every role satisfied by at least one of ``contract_names``
    OR one of ``container_kinds``.
    """
    proven_contracts = set(contract_names)
    proven_kinds = set(container_kinds)
    return {
        role
        for role, ev in ROLE_EVIDENCE_MAP.items()
        if (ev.contracts & proven_contracts) or (ev.kinds & proven_kinds)
    }


def resolve_roles_with_evidence(
    contract_names: Iterable[str] = (),
    *,
    container_kinds: Iterable[str] = (),
) -> list[RoleResolution]:
    """Same as :func:`resolve_roles` but each result carries the subsets
    of contracts AND kinds that actually fired for that role.
    """
    proven_contracts = set(contract_names)
    proven_kinds = set(container_kinds)
    out: list[RoleResolution] = []
    for role in sorted(ROLE_EVIDENCE_MAP):
        ev = ROLE_EVIDENCE_MAP[role]
        matched_contracts = tuple(sorted(ev.contracts & proven_contracts))
        matched_kinds = tuple(sorted(ev.kinds & proven_kinds))
        if not matched_contracts and not matched_kinds:
            continue
        out.append(
            RoleResolution(
                role=role,
                satisfying_contracts=matched_contracts,
                satisfying_kinds=matched_kinds,
            )
        )
    return out


def registered_roles() -> set[str]:
    """Every role name the resolver knows about."""
    return set(ROLE_EVIDENCE_MAP)


def registered_contracts() -> set[str]:
    """Every contract name that appears under at least one role."""
    out: set[str] = set()
    for ev in ROLE_EVIDENCE_MAP.values():
        out |= ev.contracts
    return out


def registered_kinds() -> set[str]:
    """Every container kind that appears under at least one role."""
    out: set[str] = set()
    for ev in ROLE_EVIDENCE_MAP.values():
        out |= ev.kinds
    return out


__all__ = [
    "ROLE_CONTRACT_MAP",
    "ROLE_EVIDENCE_MAP",
    "RoleEvidence",
    "RoleResolution",
    "registered_contracts",
    "registered_kinds",
    "registered_roles",
    "resolve_roles",
    "resolve_roles_with_evidence",
]
