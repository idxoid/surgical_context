"""Unit tests for TS/JS lexical scope graph."""

from context_engine.parser.adapters.typescript_adapter import TypeScriptAdapter


class TestTsScopeGraph:
    def test_resolve_destructure_from_require(self):
        adapter = TypeScriptAdapter()
        source = """
const { utils } = require('./config');

function boot() {
  utils();
}
"""
        tree = adapter._parse(source)
        import_bindings, _ = adapter._extract_import_bindings(source, "src/boot.ts")
        from context_engine.parser.adapters.ts_scope_graph import TsScopeGraph

        graph = TsScopeGraph.build(
            tree.root_node,
            import_bindings=import_bindings,
            node_text=adapter._node_text,
            normalize_require=lambda path: adapter._normalize_import_source("src/boot.ts", path),
        )
        binding = graph.resolve_name("utils", source.index("utils();"))
        assert binding is not None
        assert binding.kind == "destructure"
        assert binding.init_import_qn.endswith("config")
