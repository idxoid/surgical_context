"""config_surface marker-base branch (Param / F4 kind-split follow-up)."""

from context_engine.indexer.role_cascade import assign_symbol_roles
from context_engine.indexer.role_clustering import SymbolRow


def _class_row(**kwargs) -> SymbolRow:
    base = {
        "uid": "u:cls",
        "kind": "class",
        "fan_in": 0,
        "fan_out": 0,
        "cross_package_in": 0,
        "cross_package_out": 0,
        "depth_from_public": 1,
        "doc_anchor_count": 0,
    }
    base.update(kwargs)
    return SymbolRow(**base)


def test_config_surface_from_param_fan_in():
    row = _class_row(type_fan_in_param=10.0, type_fan_in=10.0, call_fan_in=1.0)
    asn = assign_symbol_roles(row)
    assert asn.primary == "config_surface"


def test_config_surface_marker_base_param_class():
    """Param-like: subclasses carry param USES_TYPE; base has isinstance + depend."""
    row = _class_row(
        type_fan_in_param=0.0,
        type_fan_in_isinstance=1.0,
        type_fan_in=1.0,
        depend_fan_in=3.6,
        call_fan_in=5.0,
        call_fan_out=0.0,
    )
    asn = assign_symbol_roles(row)
    assert asn.primary == "config_surface"


def test_representation_hub_not_marker_base_config():
    row = _class_row(
        type_fan_in_param=0.0,
        type_fan_in_isinstance=1.0,
        type_fan_in=50.0,
        depend_fan_in=2.0,
        call_fan_in=5.0,
        call_fan_out=0.0,
    )
    asn = assign_symbol_roles(row)
    assert asn.primary == "representation_surface"
    assert "config_surface" not in asn.hits
