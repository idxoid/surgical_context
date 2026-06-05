"""F1: registration_step vs request_router structural separation."""

from sidecar.indexer.role_cascade import assign_symbol_roles
from sidecar.indexer.role_clustering import SymbolRow, assemble_symbol_rows


def _row(**kwargs) -> SymbolRow:
    base = dict(
        uid="u:x",
        kind="function",
        fan_in=0,
        fan_out=0,
        cross_package_in=0,
        cross_package_out=0,
        depth_from_public=1,
        doc_anchor_count=0,
    )
    base.update(kwargs)
    return SymbolRow(**base)


def test_registration_step_is_handle_fan_out_only():
    decorator = _row(uid="u:deco", handle_fan_out=2.0, call_fan_in=5.0)
    asn = assign_symbol_roles(decorator)
    assert asn.l1 == "routing_wrap"
    assert asn.primary == "registration_step"


def test_request_router_requires_handler_call_fan_out_not_handle_fan_out():
    router = _row(
        uid="u:router",
        call_fan_in=3.0,
        call_fan_out=2.0,
        handler_call_fan_out=1.0,
    )
    asn = assign_symbol_roles(router)
    assert asn.l1 == "compute_leaf"
    assert asn.primary == "request_router"


def test_handle_fan_out_does_not_map_to_request_router():
    decorator = _row(uid="u:deco", handle_fan_out=1.0, call_fan_in=5.0)
    asn = assign_symbol_roles(decorator)
    assert "request_router" not in asn.hits
    assert asn.primary == "registration_step"


def test_assemble_symbol_rows_counts_handler_call_fan_out():
    symbols = [
        ("u:deco", "function", "/repo/api/routes.py"),
        ("u:handler", "function", "/repo/api/routes.py"),
        ("u:router", "function", "/repo/api/routes.py"),
    ]
    edges = [
        ("u:deco", "u:handler", "HANDLES", 1.0, ""),
        ("u:router", "u:handler", "CALLS_DIRECT", 1.0, ""),
    ]
    rows = {row.uid: row for row in assemble_symbol_rows(symbols, edges, {})}
    assert rows["u:router"].handler_call_fan_out == 1.0
    assert rows["u:deco"].handler_call_fan_out == 0.0


def test_api_surface_cross_bucket_for_documented_control_flow():
    row = _row(
        uid="u:get_openapi",
        call_fan_out=5.6,
        call_fan_in=1.7,
        depth_from_public=1,
        doc_anchor_count=0,
        doc_definition_weight=0.0,
        import_in=20,
    )
    asn = assign_symbol_roles(row)
    assert asn.l1 == "control_flow"
    assert asn.primary == "api_surface"


def test_orchestrator_when_control_flow_not_documented_api_surface():
    row = _row(uid="u:orchestrator", call_fan_out=4.0, call_fan_in=1.0)
    asn = assign_symbol_roles(row)
    assert asn.l1 == "control_flow"
    assert asn.primary == "orchestrator"


def test_get_openapi_like_flow_collects_schema_and_registration_supporting_roles():
    row = _row(
        uid="u:get_openapi_like",
        call_fan_out=5.6,
        call_fan_in=1.7,
        depth_from_public=1,
        doc_anchor_count=0,
        doc_definition_weight=0.0,
        construct_fan_out=1.0,
        import_in=20,
    )
    asn = assign_symbol_roles(row)
    assert asn.l1 == "control_flow"
    assert asn.primary == "api_surface"
    assert "schema_builder" in asn.hits
    assert "registration_step" in asn.hits
    assert "schema_builder" in asn.supporting


def test_runtime_surface_state_types_for_runtime_class():
    row = _row(
        uid="u:runtime_class",
        kind="class",
        call_fan_in=0.4,
        type_fan_in=41.2,
        type_fan_in_param=40.0,
        reexport_in=1,
    )
    asn = assign_symbol_roles(row)
    assert asn.l1 == "state_types"
    assert "runtime_surface" in asn.hits


def test_commonjs_api_owner_is_composition_surface_not_orphan():
    row = _row(
        uid="u:app_owner",
        kind="variable",
        api_fan_out=16.0,
        depth_from_public=1,
    )
    asn = assign_symbol_roles(row)
    assert asn.l1 == "state_types"
    assert asn.primary == "composition_surface"
    assert "api_surface" in asn.hits


def test_registration_step_state_types_for_framework_entry_class():
    row = _row(
        uid="u:framework_entry",
        kind="class",
        call_fan_in=0.4,
        type_fan_in=41.2,
        type_fan_in_param=40.0,
        api_fan_out=1.0,
        reexport_in=1,
    )
    asn = assign_symbol_roles(row)
    assert asn.l1 == "state_types"
    assert "registration_step" in asn.hits
    assert asn.primary != "config_surface"


def test_runtime_surface_compute_leaf_for_non_leaf_runtime_executor():
    row = _row(
        uid="u:runtime_leaflike",
        call_fan_in=0.9,
        call_fan_out=0.7,
        depth_from_public=1,
        import_in=13,
    )
    asn = assign_symbol_roles(row)
    assert asn.l1 == "compute_leaf"
    assert "runtime_surface" in asn.hits


def test_runtime_surface_control_flow_for_public_runtime_orchestrator():
    row = _row(
        uid="u:runtime_flow",
        call_fan_in=0.9,
        call_fan_out=5.7,
        depth_from_public=2,
        import_in=101,
    )
    asn = assign_symbol_roles(row)
    assert asn.l1 == "control_flow"
    assert "runtime_surface" in asn.hits


def test_dependency_solver_state_types_for_depends_like_function():
    row = _row(
        uid="u:depends_like",
        depend_fan_in=0.9,
        call_fan_out=0.7,
        depth_from_public=0,
    )
    asn = assign_symbol_roles(row)
    assert asn.l1 == "state_types"
    assert "dependency_solver" in asn.hits
    assert "api_surface" in asn.hits


def test_binding_and_schema_surface_for_type_heavy_public_control_flow():
    row = _row(
        uid="u:request_body_like",
        call_fan_in=0.9,
        call_fan_out=5.7,
        type_fan_out=2.0,
        cross_package_out=3,
        import_in=101,
        depth_from_public=2,
    )
    asn = assign_symbol_roles(row)
    assert asn.l1 == "control_flow"
    assert "binding_surface" in asn.hits
    assert "schema_builder" in asn.hits
    assert "dependency_solver" in asn.hits
