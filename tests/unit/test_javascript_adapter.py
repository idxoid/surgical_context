import pytest

from sidecar.parser.adapters.javascript_adapter import JavaScriptAdapter


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
        assert "use" in names

    def test_extract_symbols_includes_assigned_arrow_function_property(self, adapter):
        source = """
var app = {};
app.handle = (req, res) => req && res;
"""
        symbols = adapter.extract_symbols(source, "application.js")
        names = {symbol.name for symbol in symbols}
        assert "handle" in names
