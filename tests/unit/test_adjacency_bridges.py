from context_engine.axis.adjacency_bridges import deserialize_external_maps, serialize_external_maps


def test_external_maps_roundtrip():
    sym_to_ext = {
        "u:a": {"CALLS_EXTERNAL": {"ext:typing"}},
        "u:b": {"CALLS_EXTERNAL": {"ext:typing", "ext:os"}},
    }
    ext_to_sym = {
        "ext:typing": {"CALLS_EXTERNAL": {"u:a", "u:b"}},
        "ext:os": {"CALLS_EXTERNAL": {"u:b"}},
    }
    sym_json, ext_json = serialize_external_maps(sym_to_ext, ext_to_sym)
    got_sym, got_ext = deserialize_external_maps(sym_json, ext_json)
    assert got_sym["u:a"]["CALLS_EXTERNAL"] == {"ext:typing"}
    assert got_ext["ext:typing"]["CALLS_EXTERNAL"] == {"u:a", "u:b"}
