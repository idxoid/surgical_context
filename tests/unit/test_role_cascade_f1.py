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
