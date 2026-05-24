from unittest.mock import patch

import pytest

from sidecar.parser.adapters.typescript_adapter import TypeScriptAdapter


class TestTypeScriptAdapter:
    @pytest.fixture
    def adapter(self):
        return TypeScriptAdapter()

    def test_extract_function(self, adapter):
        source = "function foo() {}"
        symbols = adapter.extract_symbols(source, "test.ts")
        assert len(symbols) == 1
        assert symbols[0].name == "foo"
        assert symbols[0].kind == "function"

    def test_extract_class(self, adapter):
        source = "class Bar {}"
        symbols = adapter.extract_symbols(source, "test.ts")
        assert len(symbols) == 1
        assert symbols[0].name == "Bar"
        assert symbols[0].kind == "class"

    def test_extract_multiple_symbols(self, adapter):
        source = """
function func1() {}
class MyClass {}
function func2() {}
"""
        symbols = adapter.extract_symbols(source, "test.ts")
        assert len(symbols) == 3
        names = {s.name for s in symbols}
        assert names == {"func1", "MyClass", "func2"}

    def test_extracts_exported_lower_camel_const_as_public_api_symbol(self, adapter):
        source = "export const createSlice = buildCreateSlice()"
        symbols = adapter.extract_symbols(source, "createSlice.ts")

        assert len(symbols) == 1
        assert symbols[0].name == "createSlice"
        assert symbols[0].kind == "variable"

    def test_does_not_extract_non_exported_lower_camel_const(self, adapter):
        source = "const localHelper = buildCreateSlice()"
        symbols = adapter.extract_symbols(source, "helpers.ts")

        assert symbols == []

    def test_extracts_exported_const_via_text_fallback_when_ast_misses(self, adapter):
        source = "export const createSlice = buildCreateSlice()"

        with patch(
            "sidecar.parser.adapters.treesitter_base.TreeSitterAdapter.extract_symbols",
            return_value=[],
        ):
            symbols = adapter.extract_symbols(source, "createSlice.ts")

        assert len(symbols) == 1
        assert symbols[0].name == "createSlice"
        assert symbols[0].kind == "variable"
        assert symbols[0].signature_status == "fallback_export"

    def test_extracts_exported_function_via_text_fallback_when_ast_misses(self, adapter):
        source = """
export function buildCreateSlice() {
  return function createSlice() {
    return createReducer()
  }
}
"""

        with patch(
            "sidecar.parser.adapters.treesitter_base.TreeSitterAdapter.extract_symbols",
            return_value=[],
        ):
            symbols = adapter.extract_symbols(source, "createSlice.ts")

        build = next(symbol for symbol in symbols if symbol.name == "buildCreateSlice")
        assert build.kind == "function"
        assert build.start_line == 2
        assert build.end_line == 6
        assert build.signature_status == "fallback_export"

    def test_extract_calls(self, adapter):
        source = """
function foo() {
    bar();
}

function bar() {}
"""
        calls = adapter.extract_calls_from_source(source, "test.ts")
        assert len(calls) > 0
        assert any(call.get("callee_name") == "bar" for call in calls)

    def test_extract_calls_classifies_top_level_call_as_direct(self, adapter):
        source = """
function foo() {
    bar();
}

function bar() {}
"""
        calls = adapter.extract_calls_from_source(source, "test.ts")
        call = next(call for call in calls if call.get("callee_name") == "bar")

        assert call["rel_type"] == "CALLS_DIRECT"
        assert call["tier"] == "direct"
        assert call["confidence"] == 1.0
        assert call["resolver"] == "ts-scope-v1"

    def test_extract_calls_classifies_this_member_as_dynamic(self, adapter):
        source = """
class Worker {
  helper() {}

  run() {
    this.helper();
  }
}
"""
        calls = adapter.extract_calls_from_source(source, "worker.ts")
        call = next(call for call in calls if call.get("callee_name") == "helper")

        assert call["rel_type"] == "CALLS_DYNAMIC"
        assert call["tier"] == "dynamic"
        assert call["confidence"] == 0.7
        assert call["resolver"] == "ts-scope-v1"
        assert call.get("callee_uid")

    def test_extract_calls_classifies_object_member_as_dynamic_without_uid(self, adapter):
        source = """
class Worker {
  run(service: Runner) {
    service.execute();
  }
}
"""
        calls = adapter.extract_calls_from_source(source, "worker.ts")
        call = next(call for call in calls if call.get("callee_name") == "execute")

        assert call["rel_type"] == "CALLS_DYNAMIC"
        assert call["tier"] == "dynamic"
        assert "callee_uid" not in call

    def test_typed_function_call_owner_uid_matches_extracted_symbol_uid(self, adapter):
        source = """
export function Module(metadata: ModuleMetadata): ClassDecorator {
  validateModuleKeys(Object.keys(metadata));
}
"""
        symbols = adapter.extract_symbols(source, "module.decorator.ts")
        calls = adapter.extract_calls_from_source(source, "module.decorator.ts")

        module_symbol = next(symbol for symbol in symbols if symbol.name == "Module")
        assert calls
        assert {call["caller_uid"] for call in calls} == {module_symbol.uid}

    def test_extract_calls_from_exported_const_wrapper(self, adapter):
        source = """
export const createApi = buildCreateApi(coreModule())
function buildCreateApi() {}
function coreModule() {}
"""
        calls = adapter.extract_calls_from_source(source, "api.ts")

        assert any(call.get("callee_name") == "buildCreateApi" for call in calls)
        assert any(call.get("callee_name") == "coreModule" for call in calls)
        create_api_uid = adapter._uid("api.ts", "createApi")
        assert all(call["caller_uid"] == create_api_uid for call in calls)

    def test_extract_calls_marks_named_import_call_as_calls_imported(self, adapter):
        source = """
import { createRouter } from "vue-router";

function bootstrap() {
  createRouter();
}
"""
        calls = adapter.extract_calls_from_source(source, "bootstrap.ts")
        call = next(call for call in calls if call.get("callee_name") == "createRouter")

        assert call["rel_type"] == "CALLS_IMPORTED"
        assert call["tier"] == "imported"
        assert call["callee_qualified_name"] == "vue-router.createRouter"

    def test_extract_calls_marks_namespace_member_call_as_calls_imported(self, adapter):
        source = """
import * as core from "@nestjs/core";

function bootstrap() {
  core.NestFactory();
}
"""
        calls = adapter.extract_calls_from_source(source, "main.ts")
        call = next(call for call in calls if call.get("callee_name") == "NestFactory")

        assert call["rel_type"] == "CALLS_IMPORTED"
        assert call["tier"] == "imported"
        assert call["callee_qualified_name"] == "@nestjs.core.NestFactory"

    def test_extract_symbols_includes_exported_interface_via_fallback(self, adapter):
        source = """
export interface Ref<T = unknown> {
  value: T
}
"""
        symbols = adapter.extract_symbols(source, "ref.ts")
        names = {symbol.name for symbol in symbols}
        assert "Ref" in names

    def test_language_name(self, adapter):
        assert adapter.language_name == "typescript"

    def test_file_extensions(self, adapter):
        assert adapter.file_extensions == {".ts", ".tsx"}

    def test_exported_object_api_indexes_single_surface(self, adapter):
        source = """
export const SidecarClient = {
  ask() {
    return post('/ask', {});
  },
  health() {
    return fetch(`${getBaseUrl()}/health`);
  },
};
"""
        symbols = adapter.extract_symbols(source, "extension/src/sidecarClient.ts")
        assert {symbol.name for symbol in symbols} == {"SidecarClient"}
        sidecar = symbols[0]
        assert sidecar.kind == "object_api"
        assert sidecar.signature_status == "object_api_export"
