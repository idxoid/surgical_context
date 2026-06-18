"""Kind → traversal-axis table — the static half of reactive axis selection.

The expansion passes currently let the *caller* choose which edges to
walk, keyed on intent. That makes the intent classifier a single point
of failure for axis selection: when intent misfires (django
``proxy_mechanism`` magnets a QuerySet, ``data_model`` magnets a
migration) the wrong edge set is walked and the answer is missed.

This module is **pattern 2** of the reactive-axis design (see the
project note ``reactive-axis-selection``): each L2 container kind
declares which traversal *axes* are natural to it. The kind was proved
structurally at index time, so "what axis flows out of a node of this
kind" is a structural property of the kind, not a guess about the
question.

It is deliberately just a table here. The *walk* that consumes it
(``walk_phased``: REGISTRY* then CONTROL) is the next step and lives
elsewhere — this module only answers "given a node's kind, which axes
may I leave it on".

The four axes
-------------

* ``CONTROL``     — imperative call flow (``CALLS_*``). "I run code."
* ``REGISTRY``    — IoC binding (``DECORATED_BY`` / ``HANDLES`` /
  ``INSTANTIATES``). "I bind callables to be dispatched later."
* ``STRUCTURAL``  — type/inheritance (``DEPENDS_ON`` / ``EXTENDS`` /
  ``INHERITED_API`` / ``HAS_API``). "I am defined by my hierarchy."
* ``COMPOSITION`` — attribute coupling (``READS_ATTR`` / ``WRITES_ATTR``
  / ``RESOLVES_ATTR``). "I hold collaborators in attributes" — the
  fourth axis the 3-axis model omitted, the one celery's bootstep
  worker (``self.strategies`` / ``self.pool``) lives on.
"""

from __future__ import annotations

from context_engine.axis.graph_walk import EdgeProfile


class Axis:
    CONTROL = "control"
    REGISTRY = "registry"
    STRUCTURAL = "structural"
    COMPOSITION = "composition"
    DATAFLOW = "dataflow"


ALL_AXES: frozenset[str] = frozenset(
    {Axis.CONTROL, Axis.REGISTRY, Axis.STRUCTURAL, Axis.COMPOSITION, Axis.DATAFLOW}
)


# Each axis → the relationship whitelist that realises it. The two FLOW axes
# (CONTROL=call flow via CALLS, DATAFLOW=value flow via the AFFECTS
# parameter/return impact closure) pair against the three STRUCTURE axes
# (REGISTRY/STRUCTURAL/COMPOSITION). Widening an axis here reaches every
# reactive walk at once.
AXIS_EDGES: dict[str, tuple[str, ...]] = {
    Axis.CONTROL: EdgeProfile.CALLS,
    Axis.REGISTRY: ("DECORATED_BY", "HANDLES", "INSTANTIATES"),
    Axis.STRUCTURAL: (
        "DEPENDS_ON",
        "EXTENDS_EXTERNAL",
        "INHERITED_API",
        "HAS_API",
    ),
    Axis.COMPOSITION: ("READS_ATTR", "WRITES_ATTR", "RESOLVES_ATTR"),
    Axis.DATAFLOW: ("AFFECTS",),
}


# Kind → axes natural to a node of that kind. The reasoning per kind is
# "from a node proven to be this kind, which channels carry the answer
# the question is reaching for". Multi-axis kinds split the walk (the
# hub case). Every kind in the L2 catalogue plus the indexer-propagated
# ``error_model`` must appear — the module-load check below enforces it.
KIND_AXES: dict[str, frozenset[str]] = {
    # A registry both *holds* registered entries (REGISTRY) and is
    # *defined by* its inheritance chain (registry-ness propagates down
    # subclasses) — STRUCTURAL.
    "registry_class": frozenset({Axis.REGISTRY, Axis.STRUCTURAL}),
    # A web router registers handlers (REGISTRY) and dispatches into
    # them at request time (CONTROL).
    "web_route_register": frozenset({Axis.REGISTRY, Axis.CONTROL}),
    # A task registry binds task callables; the answer is the registered
    # task, reached along the binding.
    "task_register": frozenset({Axis.REGISTRY}),
    # Registers a callable into a keyed container (TaskRegistry.register,
    # add_url_rule) — pure binding.
    "keyed_register_callable": frozenset({Axis.REGISTRY}),
    # Reads one callable out of a keyed container and invokes it
    # (dispatch_request): pulls from the registry (REGISTRY) then runs
    # it (CONTROL).
    "keyed_dispatch_callable": frozenset({Axis.REGISTRY, Axis.CONTROL}),
    # A middleware chain stores callables and iterates+invokes them —
    # the answer is downstream of the call.
    "middleware_chain": frozenset({Axis.CONTROL}),
    # Signals attach receivers (REGISTRY) and fan out to invoke them
    # (CONTROL).
    "signal_register": frozenset({Axis.REGISTRY, Axis.CONTROL}),
    # Error dispatch catches and routes exceptions — the routing is
    # call flow.
    "error_dispatch": frozenset({Axis.CONTROL}),
    # An exception type is defined by its hierarchy (STRUCTURAL — up to
    # the builtin base) and is raised/handled by code that reaches it
    # (CONTROL, reverse).
    "error_model": frozenset({Axis.STRUCTURAL, Axis.CONTROL}),
    # A lazy proxy resolves to its target through attribute access
    # (COMPOSITION) and is consumed by the code that dereferences it
    # (CONTROL).
    "proxy_object": frozenset({Axis.COMPOSITION, Axis.CONTROL}),
    # Dependency injection binds providers to consumers (REGISTRY) and
    # the injected value is then called (CONTROL).
    "di_container": frozenset({Axis.REGISTRY, Axis.CONTROL}),
    # A data model is defined by its fields and inheritance — pure
    # structure.
    "data_model": frozenset({Axis.STRUCTURAL}),
    # A config carrier is a settings class read through attribute access
    # (COMPOSITION) and specialised by inheritance (STRUCTURAL).
    "config_carrier": frozenset({Axis.STRUCTURAL, Axis.COMPOSITION}),
    # Keyed metadata is written/read by name through attributes
    # (COMPOSITION) and behaves like a small registry (REGISTRY).
    "metadata_carrier": frozenset({Axis.COMPOSITION, Axis.REGISTRY}),
}


def axes_for_kinds(kinds: frozenset[str] | set[str] | tuple[str, ...]) -> frozenset[str]:
    """Union of axes natural to any of ``kinds``. Unknown kinds
    contribute nothing — a node with no classified kind has no reactive
    axis and falls back to the caller's intent-chosen edges."""
    out: set[str] = set()
    for k in kinds:
        out |= KIND_AXES.get(k, frozenset())
    return frozenset(out)


def edges_for_axes(axes: frozenset[str] | set[str] | tuple[str, ...]) -> tuple[str, ...]:
    """Flatten a set of axes into the deduplicated relationship list to
    hand a graph walk. Order is stable for deterministic Cypher."""
    seen: list[str] = []
    for axis in (
        Axis.CONTROL,
        Axis.REGISTRY,
        Axis.STRUCTURAL,
        Axis.COMPOSITION,
        Axis.DATAFLOW,
    ):
        if axis in axes:
            for rel in AXIS_EDGES[axis]:
                if rel not in seen:
                    seen.append(rel)
    return tuple(seen)


# Module-load consistency: every catalogue kind (+ propagated
# error_model) must have an axis profile, and every axis must have a
# non-empty edge set. A kind added without an axis is a silent reactive
# blind spot — fail loudly at import instead.
def _check_consistency() -> None:  # pragma: no cover - import-time guard
    from context_engine.axis.container_kind import _PREDICATES

    catalogue = set(_PREDICATES.keys()) | {"error_model"}
    missing = catalogue - set(KIND_AXES)
    if missing:
        raise RuntimeError(f"kinds without an axis profile: {sorted(missing)}")
    for axis in ALL_AXES:
        if not AXIS_EDGES.get(axis):
            raise RuntimeError(f"axis without edges: {axis}")
    unknown = set(KIND_AXES) - catalogue
    if unknown:
        raise RuntimeError(f"axis profile for unknown kind(s): {sorted(unknown)}")


_check_consistency()


__all__ = [
    "AXIS_EDGES",
    "ALL_AXES",
    "Axis",
    "KIND_AXES",
    "axes_for_kinds",
    "edges_for_axes",
]
