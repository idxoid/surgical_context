"""C2: boundary_integration L1 + integration_surface L2 from external features."""

from context_engine.indexer.external_boundary import is_integration_external_root
from context_engine.indexer.role_cascade import assign_symbol_roles
from context_engine.indexer.role_clustering import SymbolRow


def _row(**kwargs) -> SymbolRow:
    base = dict(
        uid="u:gw",
        kind="function",
        fan_in=0,
        fan_out=0,
        cross_package_in=0,
        cross_package_out=0,
        depth_from_public=2,
        doc_anchor_count=0,
    )
    base.update(kwargs)
    return SymbolRow(**base)


def test_integration_external_root_filters_plumbing():
    assert is_integration_external_root("starlette")
    assert not is_integration_external_root("typing")
    assert not is_integration_external_root("pytest")


def test_boundary_integration_from_external_call_ratio():
    row = _row(
        call_fan_in=1.0,
        call_fan_out=3.0,
        external_integration_call_fan_out=2.0,
        external_integration_root_count=1,
    )
    asn = assign_symbol_roles(row)
    assert asn.l1 == "boundary_integration"
    assert asn.primary == "integration_surface"


def test_plumbing_external_calls_do_not_trigger_boundary_bucket():
    row = _row(
        call_fan_in=1.0,
        call_fan_out=3.0,
        external_call_fan_out=5.0,
        external_integration_call_fan_out=0.0,
    )
    asn = assign_symbol_roles(row)
    assert asn.l1 == "control_flow"
    assert asn.primary == "orchestrator"


def test_type_hub_not_stolen_by_boundary_signal():
    row = _row(
        kind="class",
        type_fan_in=20.0,
        call_fan_out=2.0,
        call_fan_in=1.0,
        external_integration_call_fan_out=3.0,
    )
    asn = assign_symbol_roles(row)
    assert asn.l1 == "state_types"
    assert asn.primary == "representation_surface"


def test_import_boundary_requires_external_calls_not_file_imports_alone():
    row = _row(
        call_fan_in=2.0,
        call_fan_out=0.5,
        import_in=4,
        external_integration_import_fan_out=3.0,
        external_integration_call_fan_out=0.0,
    )
    asn = assign_symbol_roles(row)
    assert asn.l1 != "boundary_integration"
    assert asn.primary != "integration_surface"
