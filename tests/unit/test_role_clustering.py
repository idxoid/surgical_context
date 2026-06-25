"""Pass 1 discriminator cascade tests on synthetic graphs."""

import pytest

from context_engine.indexer.role_cascade import assign_symbol_roles
from context_engine.indexer.role_clustering import (
    SymbolRow,
    assemble_symbol_rows,
    assign_role_taxonomy,
    build_role_catalog,
    filter_clustering_rows,
    role_catalog_roles,
)


def _executor_row(uid: str, *, handle_fan_in: float = 0.0) -> SymbolRow:
    return SymbolRow(
        uid=uid,
        kind="function",
        fan_in=3,
        fan_out=0,
        cross_package_in=0,
        cross_package_out=0,
        depth_from_public=2,
        doc_anchor_count=0,
        call_fan_in=3.0,
        call_fan_out=0.0,
        handle_fan_in=handle_fan_in,
    )


def _orchestrator_row(uid: str) -> SymbolRow:
    return SymbolRow(
        uid=uid,
        kind="function",
        fan_in=2,
        fan_out=8,
        cross_package_in=1,
        cross_package_out=2,
        depth_from_public=1,
        doc_anchor_count=1,
        call_fan_in=2.0,
        call_fan_out=8.0,
    )


def _data_class_row(uid: str) -> SymbolRow:
    return SymbolRow(
        uid=uid,
        kind="class",
        fan_in=10,
        fan_out=0,
        cross_package_in=6,
        cross_package_out=0,
        depth_from_public=1,
        doc_anchor_count=2,
        type_fan_in=12.0,
        call_fan_in=0.0,
        call_fan_out=0.0,
    )


def test_assign_role_taxonomy_separates_orchestrator_from_leaf_runtime():
    rows = [_orchestrator_row(f"u:orch_{i}") for i in range(2)] + [
        _executor_row(f"u:exec_{i}") for i in range(4)
    ]
    summary, assignments, present = assign_role_taxonomy(rows)

    assert all(assignments[f"u:orch_{i}"].primary == "orchestrator" for i in range(2))
    assert all(assignments[f"u:exec_{i}"].primary == "core_runtime" for i in range(4))
    assert "orchestrator" in present
    assert "core_runtime" in present
    assert summary.method == "discriminator_cascade"


def test_handle_fan_in_assigns_executor_primary():
    row = _executor_row("u:handler", handle_fan_in=2.0)
    asn = assign_symbol_roles(row)
    assert asn.primary == "executor"


def test_representation_surface_for_type_heavy_class():
    row = _data_class_row("u:model")
    asn = assign_symbol_roles(row)
    assert asn.primary == "representation_surface"


def test_build_role_catalog_keeps_only_present_roles():
    rows = [_orchestrator_row(f"u:orch_{i}") for i in range(2)] + [
        _executor_row(f"u:exec_{i}", handle_fan_in=1.0) for i in range(2)
    ]
    _, _, present = assign_role_taxonomy(rows)
    catalog = build_role_catalog(present)
    payload = catalog.to_dict()
    assert payload["schema_version"] == 3
    assert "present_roles" in payload
    assert "orchestrator" in payload["present_roles"]
    assert "executor" in payload["present_roles"]


def test_assign_role_taxonomy_empty_input():
    summary, assignments, present = assign_role_taxonomy([])
    assert summary.sample_size == 0
    assert assignments == {}
    assert present == {}


def test_filter_clustering_rows_drops_true_dangling():
    connected = _executor_row("u:worker")
    dangling = SymbolRow(
        uid="u:const",
        kind="variable",
        fan_in=0,
        fan_out=0,
        cross_package_in=0,
        cross_package_out=0,
        depth_from_public=3,
        doc_anchor_count=0,
    )
    assert filter_clustering_rows([connected, dangling]) == [connected]


def test_assemble_symbol_rows_computes_fan_in_out_per_symbol():
    symbols = [
        ("u:a", "function", "/repo/api/a.py"),
        ("u:b", "function", "/repo/api/b.py"),
        ("u:c", "function", "/repo/core/c.py"),
    ]
    edges = [
        ("u:a", "u:b"),
        ("u:a", "u:c"),
        ("u:b", "u:c"),
    ]

    rows = {row.uid: row for row in assemble_symbol_rows(symbols, edges, {})}

    assert rows["u:a"].fan_in == 0
    assert rows["u:a"].fan_out == 2
    assert rows["u:b"].fan_in == 1
    assert rows["u:b"].fan_out == 1
    assert rows["u:c"].fan_in == 2
    assert rows["u:c"].fan_out == 0


def test_assemble_symbol_rows_splits_uses_type_kind():
    symbols = [
        ("u:a", "function", "/repo/api/a.py"),
        ("u:b", "class", "/repo/api/b.py"),
    ]
    edges = [
        ("u:a", "u:b", "USES_TYPE", 1.0, "param"),
        ("u:a", "u:b", "USES_TYPE", 0.5, "isinstance"),
    ]
    rows = {row.uid: row for row in assemble_symbol_rows(symbols, edges, {})}
    assert rows["u:b"].type_fan_in_param == pytest.approx(1.0)
    assert rows["u:b"].type_fan_in_isinstance == pytest.approx(0.5)
    assert rows["u:b"].type_fan_in == pytest.approx(1.5)


def test_assemble_symbol_rows_tracks_handle_fan_out_on_decorator():
    symbols = [
        ("u:deco", "function", "/repo/api/routes.py"),
        ("u:handler", "function", "/repo/api/routes.py"),
    ]
    edges = [
        ("u:deco", "u:handler", "HANDLES", 1.0, ""),
    ]
    rows = {row.uid: row for row in assemble_symbol_rows(symbols, edges, {})}
    assert rows["u:deco"].handle_fan_out == pytest.approx(1.0)
    assert rows["u:handler"].handle_fan_in == pytest.approx(1.0)


def test_assemble_symbol_rows_depth_from_public_uses_full_graph_f13():
    """Reachability through excluded callers must not collapse depth to zero."""
    symbols = [
        ("u:framework", "function", "/repo/fastapi/app.py"),
        ("u:internal", "function", "/repo/fastapi/routing.py"),
    ]
    edges = [
        ("u:test_entry", "u:framework", "CALLS_DIRECT", 1.0, ""),
        ("u:framework", "u:internal", "CALLS_DIRECT", 1.0, ""),
    ]
    rows = {row.uid: row for row in assemble_symbol_rows(symbols, edges, {})}
    assert rows["u:framework"].depth_from_public == 1
    assert rows["u:internal"].depth_from_public == 2


def test_role_catalog_roles_lists_cascade_vocabulary():
    roles = role_catalog_roles()
    assert "orchestrator" in roles
    assert "executor" in roles
    assert "orphan" not in roles
