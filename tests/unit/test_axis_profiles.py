"""Kind → axis table — coverage, edge mapping, and helpers."""

from __future__ import annotations

from sidecar.axis.axis_profiles import (
    ALL_AXES,
    AXIS_EDGES,
    KIND_AXES,
    Axis,
    axes_for_kinds,
    edges_for_axes,
)
from sidecar.axis.container_kind import _PREDICATES


def test_every_catalogue_kind_has_axes():
    """The import-time guard already enforces this, but assert it
    explicitly so the contract is visible: every L2 kind plus the
    propagated error_model carries an axis profile."""
    catalogue = set(_PREDICATES.keys()) | {"error_model"}
    assert catalogue <= set(KIND_AXES)


def test_no_axis_profile_for_unknown_kind():
    catalogue = set(_PREDICATES.keys()) | {"error_model"}
    assert set(KIND_AXES) <= catalogue


def test_every_axis_has_edges():
    for axis in ALL_AXES:
        assert AXIS_EDGES.get(axis), f"{axis} has no edges"


def test_axes_in_table_are_known():
    for kind, axes in KIND_AXES.items():
        assert axes <= ALL_AXES, f"{kind} has unknown axis in {axes}"


def test_axes_for_kinds_unions():
    # proxy_object = {COMPOSITION, CONTROL}; web_route_register =
    # {REGISTRY, CONTROL}. Union covers three axes.
    out = axes_for_kinds({"proxy_object", "web_route_register"})
    assert out == frozenset({Axis.COMPOSITION, Axis.CONTROL, Axis.REGISTRY})


def test_axes_for_unknown_kind_is_empty():
    """A node with no classified kind has no reactive axis — the caller
    falls back to intent-chosen edges."""
    assert axes_for_kinds({"not_a_kind"}) == frozenset()


def test_edges_for_axes_flattens_and_dedupes():
    # CONTROL + REGISTRY: CALLS_* then DECORATED_BY/HANDLES/INSTANTIATES,
    # no duplicates, control first (stable order).
    edges = edges_for_axes({Axis.REGISTRY, Axis.CONTROL})
    assert edges[0] == "CALLS"  # control block leads
    assert "DECORATED_BY" in edges
    assert len(edges) == len(set(edges))


def test_edges_for_axes_order_is_deterministic():
    a = edges_for_axes({Axis.CONTROL, Axis.STRUCTURAL})
    b = edges_for_axes({Axis.STRUCTURAL, Axis.CONTROL})
    assert a == b  # set input, stable axis order out


def test_composition_axis_carries_attr_edges():
    """The fourth axis must map to the attribute-coupling edges — the
    channel celery's bootstep worker lives on."""
    edges = edges_for_axes({Axis.COMPOSITION})
    assert "READS_ATTR" in edges
    assert "WRITES_ATTR" in edges


def test_data_model_is_structural_only():
    """A data model has no call/registry nature — pure structure. Guards
    against the kind leaking onto control-flow walks."""
    assert KIND_AXES["data_model"] == frozenset({Axis.STRUCTURAL})


def test_error_model_spans_structure_and_control():
    """error_model: defined by hierarchy (STRUCTURAL), raised/handled by
    reaching code (CONTROL)."""
    assert KIND_AXES["error_model"] == frozenset({Axis.STRUCTURAL, Axis.CONTROL})
