import pytest

from context_engine.parser.adapters.javascript_adapter import JavaScriptAdapter


class TestJavaScriptAdapter:
    @pytest.fixture
    def adapter(self):
        return JavaScriptAdapter()

    def test_extract_calls_marks_named_import_call_as_calls_imported(self, adapter):
        source = """
import { Router } from "express";

function bootstrap() {
  Router();
}
"""
        calls = adapter.extract_calls_from_source(source, "bootstrap.js")
        call = next(call for call in calls if call.get("callee_name") == "Router")

        assert call["rel_type"] == "CALLS_IMPORTED"
        assert call["tier"] == "imported"
        assert call["callee_qualified_name"] == "express.Router"

    def test_extract_calls_marks_require_destructure_call_as_calls_imported(self, adapter):
        source = """
const { createApp } = require("vue");

function bootstrap() {
  createApp();
}
"""
        calls = adapter.extract_calls_from_source(source, "bootstrap.js")
        call = next(call for call in calls if call.get("callee_name") == "createApp")

        assert call["rel_type"] == "CALLS_IMPORTED"
        assert call["tier"] == "imported"
        assert call["callee_qualified_name"] == "vue.createApp"

    def test_extract_calls_marks_imported_constructor_as_calls_imported(self, adapter):
        source = """
var Router = require('router');

app.init = function init() {
  this.router = new Router({ strict: true });
}
"""
        calls = adapter.extract_calls_from_source(source, "application.js")
        call = next(call for call in calls if call.get("callee_name") == "Router")

        assert call["caller_uid"] == adapter._property_method_uid("application.js", "app", "init")
        assert call["rel_type"] == "CALLS_IMPORTED"
        assert call["tier"] == "imported"
        assert call["callee_qualified_name"] == "router"
        assert call["call_kind"] == "construct"

    def test_extract_calls_attributes_unindexed_nested_callback_to_indexed_owner(self, adapter):
        source = """
var Router = require('router');

app.init = function init() {
  Object.defineProperty(this, 'router', {
    get: function getrouter() {
      return new Router();
    }
  });
}
"""
        calls = adapter.extract_calls_from_source(source, "application.js")
        call = next(call for call in calls if call.get("callee_name") == "Router")

        assert call["caller_uid"] == adapter._property_method_uid("application.js", "app", "init")
        assert call["rel_type"] == "CALLS_IMPORTED"
        assert call["callee_qualified_name"] == "router"
        assert call["call_kind"] == "construct"

    def test_extract_imports_includes_commonjs_require_sources(self, adapter):
        source = """
var proto = require('./application');
const { Buffer } = require("node:buffer");
const again = require('./application');
"""
        imports = adapter.extract_imports(source, "lib/express.js")
        by_target = {(edge.target_module_name, edge.import_type) for edge in imports}

        assert ("./application", "relative") in by_target
        assert ("node:buffer", "from_package") in by_target
        assert sum(1 for edge in imports if edge.target_module_name == "./application") == 1

    def test_extract_imports_includes_export_from_sources(self, adapter):
        imports = adapter.extract_imports('export { Router } from "./router";', "lib/index.js")
        by_target = {(edge.target_module_name, edge.import_type) for edge in imports}

        assert ("./router", "relative") in by_target

    def test_extract_symbols_includes_top_level_app_instance_binding(self, adapter):
        source = """
const express = require("express");
const app = express();
"""
        symbols = adapter.extract_symbols(source, "app.js")
        names = {symbol.name for symbol in symbols}
        assert "app" in names

    def test_extract_symbols_includes_module_exports_named_function(self, adapter):
        source = """
module.exports = function middleware(req, res, next) {
  next();
}
"""
        symbols = adapter.extract_symbols(source, "middleware.js")
        names = {symbol.name for symbol in symbols}
        assert "middleware" in names

    def test_extract_symbols_includes_module_exports_object_keys(self, adapter):
        source = """
const middleware = () => {};
module.exports = {
  middleware,
  handler: middleware,
};
"""
        symbols = adapter.extract_symbols(source, "middleware.js")
        names = {symbol.name for symbol in symbols}
        assert "middleware" in names
        assert "handler" in names

    def test_extract_symbols_includes_assigned_named_function_property(self, adapter):
        source = """
var app = exports = module.exports = {};
app.use = function use(fn) {
  return fn;
};
"""
        symbols = adapter.extract_symbols(source, "application.js")
        names = {symbol.name for symbol in symbols}
        assert "app" in names
        assert "use" in names

        edges = adapter.extract_property_api_edges(source, "application.js")
        assert any(
            edge.class_uid == adapter._uid("application.js", "app")
            and edge.method_uid == adapter._property_method_uid("application.js", "app", "use")
            for edge in edges
        )

    def test_extract_symbols_includes_assigned_arrow_function_property(self, adapter):
        source = """
var app = {};
app.handle = (req, res) => req && res;
"""
        symbols = adapter.extract_symbols(source, "application.js")
        names = {symbol.name for symbol in symbols}
        assert "handle" in names

    def test_extract_symbol_aliases_links_export_to_required_module_symbol(self, adapter, tmp_path):
        lib = tmp_path / "lib"
        lib.mkdir()
        (lib / "response.js").write_text("var res = {};\nmodule.exports = res;\n")
        source = """
var res = require('./response');
exports.response = res;
"""

        aliases = adapter.extract_symbol_aliases(source, str(lib / "express.js"))

        export_alias = next(alias for alias in aliases if alias["source_name"] == "response")
        assert export_alias["target_name"] == "res"
        assert export_alias["target_qualified_name"] == "response.res"
        assert export_alias["match_by_name"] is True

    def test_extract_symbol_aliases_links_default_require_exact_only(self, adapter, tmp_path):
        lib = tmp_path / "lib"
        lib.mkdir()
        (lib / "request.js").write_text("var req = {};\nmodule.exports = req;\n")
        source = "var req = require('./request');\n"

        aliases = adapter.extract_symbol_aliases(source, str(lib / "express.js"))

        require_alias = next(alias for alias in aliases if alias["source_name"] == "req")
        assert require_alias["target_name"] == "req"
        assert require_alias["target_qualified_name"] == "request.req"
        assert require_alias["match_by_name"] is False

    def test_extract_symbol_aliases_keeps_same_name_exports_exact_only(self, adapter):
        source = """
var Router = require('router');
exports.Router = Router;
"""

        aliases = adapter.extract_symbol_aliases(source, "lib/express.js")

        export_alias = next(
            alias
            for alias in aliases
            if alias["source_name"] == "Router" and alias["kind"] == "commonjs_export_alias"
        )
        assert export_alias["target_name"] == "Router"
        assert export_alias["target_qualified_name"] == "router.Router"
        assert export_alias["match_by_name"] is False

    def test_extract_property_api_edges_links_owner_to_assigned_method(self, adapter):
        source = """
var res = Object.create(proto);
res.status = function status(code) {
  return this;
};
res.send = (body) => body;
"""

        edges = adapter.extract_property_api_edges(source, "response.js")

        assert len(edges) == 2
        assert {edge.edge_type for edge in edges} == {"HAS_API"}
        assert {edge.class_uid for edge in edges} == {adapter._uid("response.js", "res")}
        assert {edge.method_uid for edge in edges} == {
            adapter._property_method_uid("response.js", "res", "status"),
            adapter._property_method_uid("response.js", "res", "send"),
        }

    def test_extract_property_api_edges_links_chained_property_aliases(self, adapter):
        source = """
var res = Object.create(proto);
res.set =
res.header = function header(field, val) {
  return this;
};
"""

        symbols = adapter.extract_symbols(source, "response.js")
        edges = adapter.extract_property_api_edges(source, "response.js")

        assert "set" in {symbol.name for symbol in symbols}
        assert {edge.method_uid for edge in edges} == {
            adapter._property_method_uid("response.js", "res", "set"),
            adapter._property_method_uid("response.js", "res", "header"),
        }

    def test_property_method_symbol_does_not_collapse_with_require_binding(self, adapter):
        source = """
var send = require('send');
var res = Object.create(proto);
res.send = function send(body) {
  return this;
};
"""

        symbols = adapter.extract_symbols(source, "response.js")
        send_symbols = [symbol for symbol in symbols if symbol.name == "send"]
        edges = adapter.extract_property_api_edges(source, "response.js")

        assert {symbol.qualified_name for symbol in send_symbols} == {
            "response.send",
            "response.res.send",
        }
        assert any(
            edge.method_uid == adapter._property_method_uid("response.js", "res", "send")
            for edge in edges
        )

    def test_calls_inside_property_method_use_owner_qualified_caller(self, adapter):
        source = """
var send = require('send');
var res = Object.create(proto);
res.send = function send(body) {
  return this;
};
res.json = function json(obj) {
  return this.send(obj);
};
"""

        calls = adapter.extract_calls_from_source(source, "response.js")

        call = next(call for call in calls if call.get("callee_name") == "send")
        assert call["caller_uid"] == adapter._property_method_uid("response.js", "res", "json")
        assert call["callee_uid"] == adapter._property_method_uid("response.js", "res", "send")

    def test_extract_calls_guess_unresolved_identifier(self, adapter):
        source = """
function bootstrap() {
  mixpanel.track('evt');
}
"""
        calls = adapter.extract_calls_from_source(source, "bootstrap.js")
        call = next(c for c in calls if c.get("callee_name") == "track")
        assert call["rel_type"] == "CALLS_GUESS"
        assert call["resolver"] == "js-ambiguity-gate-v1"
