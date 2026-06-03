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

    def test_extract_calls_falls_back_for_pure_exported_const_initializer(self, adapter):
        source = """
import { buildCreateSlice } from './builder'

export const createSlice = /* @__PURE__ */ buildCreateSlice()
"""
        with patch.object(adapter, "_enclosing_symbol_owner", return_value=None):
            calls = adapter.extract_calls_from_source(source, "createSlice.ts")

        call = next(call for call in calls if call.get("callee_name") == "buildCreateSlice")
        assert call["caller_uid"] == adapter._uid("createSlice.ts", "createSlice")
        assert call["rel_type"] == "CALLS_IMPORTED"
        assert call["tier"] == "imported"
        assert call["resolver"] == "ts-export-initializer-fallback-v1"
        assert call["callee_qualified_name"].endswith("builder.buildCreateSlice")

    def test_extract_calls_falls_back_inside_exported_function_body(self, adapter):
        source = """
import { createReducer } from './createReducer'

export function buildCreateSlice() {
  return function createSlice() {
    return createReducer()
  }
}
"""
        with patch.object(adapter, "_enclosing_symbol_owner", return_value=None):
            calls = adapter.extract_calls_from_source(source, "createSlice.ts")

        build_uid = next(
            symbol.uid for symbol in adapter.extract_symbols(source, "createSlice.ts")
            if symbol.name == "buildCreateSlice"
        )
        call = next(
            call for call in calls
            if call.get("caller_uid") == build_uid and call.get("callee_name") == "createReducer"
        )
        assert call["rel_type"] == "CALLS_IMPORTED"
        assert call["resolver"] == "ts-symbol-body-fallback-v1"
        assert call["callee_qualified_name"].endswith("createReducer.createReducer")

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

    def test_extract_calls_links_object_api_member_to_imported_surface(self, adapter):
        source = """
import { SidecarClient } from './sidecarClient';

export class SurgicalContextViewProvider {
  runAsk() {
    return SidecarClient.askStream('sym', 'question', {});
  }
}
"""
        calls = adapter.extract_calls_from_source(
            source, "extension/src/providers/SurgicalContextViewProvider.ts"
        )
        call = next(call for call in calls if call.get("callee_name") == "askStream")

        assert call["rel_type"] == "CALLS_IMPORTED"
        assert call["callee_qualified_name"] == "sidecarClient.SidecarClient"

    def test_extract_symbols_includes_exported_interface_via_fallback(self, adapter):
        source = """
export interface Ref<T = unknown> {
  value: T
}
"""
        symbols = adapter.extract_symbols(source, "ref.ts")
        names = {symbol.name for symbol in symbols}
        assert "Ref" in names

    def test_extract_type_references_from_function_signature(self, adapter):
        source = """
export interface ConfigureStoreOptions<S = unknown> {
  reducer: Reducer<S>
}

export type EnhancedStore<S = unknown> = Store<S>

export function configureStore<S>(
  options: ConfigureStoreOptions<S>,
): EnhancedStore<S> {
  return createStore(options.reducer)
}
"""
        symbols = adapter.extract_symbols(source, "configureStore.ts")
        configure_store = next(symbol for symbol in symbols if symbol.name == "configureStore")

        refs = adapter.extract_type_references(source, "configureStore.ts")
        signature_refs = [
            ref for ref in refs if ref["referrer_uid"] == configure_store.uid
        ]

        assert {ref["type_name"] for ref in signature_refs} >= {
            "ConfigureStoreOptions",
            "EnhancedStore",
        }
        assert ("ConfigureStoreOptions", "param") in {
            (ref["type_name"], ref["kind"]) for ref in signature_refs
        }
        assert ("EnhancedStore", "return") in {
            (ref["type_name"], ref["kind"]) for ref in signature_refs
        }
        assert "S" not in {ref["type_name"] for ref in signature_refs}

    def test_language_name(self, adapter):
        assert adapter.language_name == "typescript"

    def test_file_extensions(self, adapter):
        assert adapter.file_extensions == {".ts", ".tsx"}

    def test_extract_decorators_class_method_and_args(self, adapter):
        """Class-, method-, and arg-form decorators all produce DECORATED_BY rows
        with the same dict shape Python's adapter uses, so the existing linker
        handles both languages from one feed."""
        source = """import { Module, Controller, Get, Post, Injectable } from '@nestjs/common';
import { CatsService } from './cats.service';

@Injectable()
export class CatsService {}

@Controller('cats')
export class CatsController {
  @Get()
  findAll(): string { return 'all'; }

  @Post()
  create(): string { return 'created'; }
}

@Module({
  controllers: [CatsController],
  providers: [CatsService],
})
export class CatsModule {}
"""
        decorators = adapter.extract_decorators(source, "src/cats.ts")
        pairs = {(d["decorator_name"], d["decorated_name"]) for d in decorators}
        assert pairs == {
            ("Injectable", "CatsService"),
            ("Controller", "CatsController"),
            ("Get", "findAll"),
            ("Post", "create"),
            ("Module", "CatsModule"),
        }
        # All five carry the imported source as the qualified prefix.
        for d in decorators:
            assert d["decorator_qualified_name"].endswith(d["decorator_name"])
            assert "@nestjs" in d["decorator_qualified_name"]

    def test_extract_decorators_dotted_and_bare(self, adapter):
        """``@foo.bar`` resolves to dotted; ``@simple`` to the bare module name."""
        source = """import * as ns from './ns';
@ns.deco
class A {}

@simple
class B {}
"""
        decorators = adapter.extract_decorators(source, "src/x.ts")
        names = {(d["decorator_name"], d["decorated_name"]) for d in decorators}
        assert ("ns.deco", "A") in names
        assert ("simple", "B") in names

    def test_extract_decorators_skips_undecorated_declarations(self, adapter):
        source = "export class PlainOldClass {}\nfunction fn() {}\n"
        assert adapter.extract_decorators(source, "src/p.ts") == []

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
