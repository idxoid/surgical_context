"""Axis query plans.

This is the first compiler-facing layer over the physical axis index. It
does not infer roles or intents; callers provide axis bits/container kinds,
and the plan renders deterministic storage filters plus graph traversal mode.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from sidecar.axis.schema import AxisName

TraversalMode = Literal["immediate_control_flow", "deferred_binding_flow"]
GraphDirection = Literal["out", "in", "both"]

_AXIS_COLUMNS: dict[AxisName, str] = {
    "cfg": "cfg_bits",
    "dfg": "dfg_bits",
    "struct": "struct_bits",
}

_CONTROL_EDGE_TYPES = (
    "CALLS",
    "CALLS_DIRECT",
    "CALLS_SCOPED",
    "CALLS_IMPORTED",
    "CALLS_DYNAMIC",
    "CALLS_INFERRED",
    "CALLS_GUESS",
)

_STRUCTURAL_BINDING_EDGE_TYPES = (
    "DECORATED_BY",
    "USES_TYPE",
    "INJECTS",
    "HANDLES",
    "REFERENCES",
    "HAS_API",
    "INHERITED_API",
)


def _quote_lance_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


@dataclass(frozen=True, order=True)
class AxisRequirement:
    """One required physical axis bit."""

    axis: AxisName
    bit: str

    def __post_init__(self) -> None:
        if self.axis not in _AXIS_COLUMNS:
            raise ValueError(f"Unknown axis: {self.axis}")
        if not self.bit.strip():
            raise ValueError("Axis requirement bit cannot be empty")


@dataclass(frozen=True)
class GraphExpansionStep:
    """One deterministic graph expansion stage after LanceDB seed selection."""

    name: str
    edge_types: tuple[str, ...]
    direction: GraphDirection
    max_depth: int = 1

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("Graph expansion step name cannot be empty")
        if not self.edge_types:
            raise ValueError("Graph expansion step must name at least one edge type")
        if self.max_depth < 1:
            raise ValueError("Graph expansion step max_depth must be >= 1")

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "edge_types": list(self.edge_types),
            "direction": self.direction,
            "max_depth": self.max_depth,
        }


@dataclass(frozen=True)
class AxisQueryRequest:
    """Data-only query request.

    ``required_bits`` and ``container_kinds`` are supplied by an upstream
    intent/contract layer. This module only compiles them.
    """

    traversal_mode: TraversalMode
    required_bits: tuple[AxisRequirement, ...] = ()
    optional_bits: tuple[AxisRequirement, ...] = ()
    container_kinds: tuple[str, ...] = ()
    target_node_kinds: tuple[str, ...] = ("Symbol",)
    limit: int = 30

    def __post_init__(self) -> None:
        if self.traversal_mode not in {"immediate_control_flow", "deferred_binding_flow"}:
            raise ValueError(f"Unknown traversal mode: {self.traversal_mode}")
        if self.limit < 1:
            raise ValueError("Axis query limit must be >= 1")


@dataclass(frozen=True)
class AxisQueryPlan:
    """Compiled query shape consumed by storage adapters."""

    traversal_mode: TraversalMode
    required_bits: tuple[AxisRequirement, ...]
    optional_bits: tuple[AxisRequirement, ...]
    container_kinds: tuple[str, ...]
    target_node_kinds: tuple[str, ...]
    expansion_steps: tuple[GraphExpansionStep, ...]
    stop_conditions: tuple[str, ...]
    limit: int
    workspace_id: str
    lance_predicate: str = field(repr=False)

    def to_dict(self) -> dict[str, object]:
        return {
            "traversal_mode": self.traversal_mode,
            "required_bits": [{"axis": req.axis, "bit": req.bit} for req in self.required_bits],
            "optional_bits": [{"axis": req.axis, "bit": req.bit} for req in self.optional_bits],
            "container_kinds": list(self.container_kinds),
            "target_node_kinds": list(self.target_node_kinds),
            "expansion_steps": [step.to_dict() for step in self.expansion_steps],
            "stop_conditions": list(self.stop_conditions),
            "limit": self.limit,
            "workspace_id": self.workspace_id,
            "lance_predicate": self.lance_predicate,
        }


def _axis_bit_predicate(requirement: AxisRequirement) -> str:
    column = _AXIS_COLUMNS[requirement.axis]
    return f"array_has({column}, {_quote_lance_string(requirement.bit)})"


def _container_kind_predicate(kind: str) -> str:
    """Match against the dedicated ``container_kinds`` list column.

    Earlier revisions used a substring ``LIKE`` over the serialized
    ``axis_container_kinds_json`` blob; that match was fragile (a payload
    value containing the literal ``"kind": "X"`` could collide) and could
    not be index-backed by Lance. The dedicated column is a list of strings
    populated alongside the JSON blob; ``array_has`` is exact and structural.
    """

    if not kind.strip():
        raise ValueError("Container kind cannot be empty")
    return f"array_has(container_kinds, {_quote_lance_string(kind)})"


def render_axis_bits_predicate(
    *,
    required_bits: tuple[AxisRequirement, ...] = (),
    container_kinds: tuple[str, ...] = (),
) -> str:
    """Lance prefilter for axis bits/kinds only (workspace-scoped tables)."""
    clauses: list[str] = []
    clauses.extend(_axis_bit_predicate(req) for req in sorted(set(required_bits)))
    clauses.extend(_container_kind_predicate(kind) for kind in sorted(set(container_kinds)))
    return " AND ".join(clauses) if clauses else "true"


def render_lance_predicate(
    workspace_id: str,
    *,
    required_bits: tuple[AxisRequirement, ...] = (),
    container_kinds: tuple[str, ...] = (),
) -> str:
    """Render a deterministic LanceDB prefilter for axis symbol rows."""

    clauses = [f"workspace_id = {_quote_lance_string(workspace_id)}"]
    clauses.extend(_axis_bit_predicate(req) for req in sorted(set(required_bits)))
    clauses.extend(_container_kind_predicate(kind) for kind in sorted(set(container_kinds)))
    return " AND ".join(clauses)


def _expansion_steps_for_mode(mode: TraversalMode) -> tuple[GraphExpansionStep, ...]:
    if mode == "immediate_control_flow":
        return (
            GraphExpansionStep(
                name="control_call_expansion",
                edge_types=_CONTROL_EDGE_TYPES,
                direction="out",
                max_depth=2,
            ),
        )
    if mode == "deferred_binding_flow":
        return (
            GraphExpansionStep(
                name="binding_structure_expansion",
                edge_types=_STRUCTURAL_BINDING_EDGE_TYPES,
                direction="both",
                max_depth=1,
            ),
            GraphExpansionStep(
                name="deferred_runtime_dispatch",
                edge_types=_CONTROL_EDGE_TYPES,
                direction="both",
                max_depth=2,
            ),
        )
    raise ValueError(f"Unknown traversal mode: {mode}")


def _stop_conditions_for_mode(mode: TraversalMode) -> tuple[str, ...]:
    if mode == "immediate_control_flow":
        return ("token_budget", "call_depth_exhausted")
    if mode == "deferred_binding_flow":
        return ("registry_or_metadata_read_reached", "dispatch_target_reached", "token_budget")
    raise ValueError(f"Unknown traversal mode: {mode}")


def compile_axis_query(request: AxisQueryRequest, *, workspace_id: str) -> AxisQueryPlan:
    """Compile a request into storage-facing filters and traversal steps."""

    return AxisQueryPlan(
        traversal_mode=request.traversal_mode,
        required_bits=tuple(sorted(set(request.required_bits))),
        optional_bits=tuple(sorted(set(request.optional_bits))),
        container_kinds=tuple(sorted(set(request.container_kinds))),
        target_node_kinds=tuple(sorted(set(request.target_node_kinds))),
        expansion_steps=_expansion_steps_for_mode(request.traversal_mode),
        stop_conditions=_stop_conditions_for_mode(request.traversal_mode),
        limit=request.limit,
        workspace_id=workspace_id,
        lance_predicate=render_lance_predicate(
            workspace_id,
            required_bits=request.required_bits,
            container_kinds=request.container_kinds,
        ),
    )


__all__ = [
    "AxisQueryPlan",
    "AxisQueryRequest",
    "AxisRequirement",
    "GraphExpansionStep",
    "compile_axis_query",
    "render_axis_bits_predicate",
    "render_lance_predicate",
]
