from unittest.mock import patch

import pytest

from context_engine.axis.schema import AxisExtraction
from context_engine.parser.adapters.typescript_adapter import TypeScriptAdapter


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
            "context_engine.parser.adapters.treesitter_base.TreeSitterAdapter.extract_symbols",
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
            "context_engine.parser.adapters.treesitter_base.TreeSitterAdapter.extract_symbols",
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

        assert call["rel_type"] == "CALLS_SCOPED"
        assert call["tier"] == "scoped"
        assert call["confidence"] == pytest.approx(0.9)
        assert call["resolver"] == "ts-scope-v1"

    def test_extract_calls_classifies_this_member_as_scoped(self, adapter):
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

        assert call["rel_type"] == "CALLS_SCOPED"
        assert call["tier"] == "scoped"
        assert call["confidence"] == pytest.approx(0.9)
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
            symbol.uid
            for symbol in adapter.extract_symbols(source, "createSlice.ts")
            if symbol.name == "buildCreateSlice"
        )
        call = next(
            call
            for call in calls
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
import { SidecarClient } from './context_engineClient';

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
        assert call["callee_qualified_name"] == "context_engineClient.SidecarClient.askStream"

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
        signature_refs = [ref for ref in refs if ref["referrer_uid"] == configure_store.uid]

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

    def test_extract_decorator_compositions_collects_arg_refs(self, adapter):
        """`@Module({ imports: [...], providers: [...], controllers: [...] })`
        yields one COMPOSES edge per AST-visible identifier inside an array,
        carrying the decorator name and the key for diagnostics. Spread
        elements are skipped (the expansion is not statically visible)."""
        source = """import { Module } from '@nestjs/common';
import { DatabaseModule } from './db';
import { CatsController } from './cats.controller';
import { CatsService, AuxService } from './cats.service';

@Module({
  imports: [DatabaseModule],
  controllers: [CatsController],
  providers: [CatsService, AuxService, ...spreadProviders],
  exports: [CatsService],
})
export class AppModule {}
"""
        rows = adapter.extract_decorator_compositions(source, "src/app.module.ts")
        assert {(r["decorator_key"], r["referenced_name"]) for r in rows} == {
            ("imports", "DatabaseModule"),
            ("controllers", "CatsController"),
            ("providers", "CatsService"),
            ("providers", "AuxService"),
            ("exports", "CatsService"),
        }
        # The spread element is skipped — its expansion is not statically visible.
        assert all(r["referenced_name"] != "spreadProviders" for r in rows)
        # Every row carries the decorator name and the decorated class name.
        assert {r["decorator_name"] for r in rows} == {"Module"}
        assert {r["decorated_name"] for r in rows} == {"AppModule"}

    def test_returns_function_expression_marker(self, adapter):
        """Higher-order factory pattern (function whose body returns an arrow /
        function expression) is tagged on the SymbolMetadata; plain functions
        and call-initialised variables stay unmarked. The marker is a pure AST
        fact (return arrow_function / return function_expression), not a name
        or type heuristic."""
        source = """export function Controller(opts): ClassDecorator {
  const x = 1;
  return (target) => { Reflect.defineMetadata('x', true, target); };
}

export const RequestMapping = (meta) => {
  return (target, key, desc) => desc;
};

export function plain(): string {
  const inner = () => 'x';
  return 'hello';
}

export const Post = makeDecorator(RequestMethod.POST);

function nested() {
  function inner() { return () => 1; }
  return 'x';
}
"""
        symbols = {s.name: s for s in adapter.extract_symbols(source, "src/x.ts")}
        # Top-level higher-order factories: function decl + arrow var.
        assert symbols["Controller"].returns_function_expression is True
        assert symbols["RequestMapping"].returns_function_expression is True
        # Plain string returner with a local arrow that isn't returned: no marker.
        assert symbols["plain"].returns_function_expression is False
        # Call-initialised variable: needs cross-function dataflow to tell, skipped.
        assert symbols["Post"].returns_function_expression is False
        # Outer ``nested`` returns a string. The nested ``inner`` *does* return
        # an arrow expression — and is tagged. Stops the walk at the right boundary.
        assert symbols["nested"].returns_function_expression is False
        assert symbols["inner"].returns_function_expression is True

    def test_extract_decorator_compositions_ignores_method_decorators(self, adapter):
        """Method/property decorators name request-cycle metadata, not
        composition — they must not produce COMPOSES edges."""
        source = """import { Controller, Get, Post } from '@nestjs/common';

@Controller('cats')
export class CatsController {
  @Get()
  findAll(): string { return 'all'; }
}
"""
        assert adapter.extract_decorator_compositions(source, "src/cats.ts") == []

    def test_exported_object_api_indexes_surface_and_members(self, adapter):
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
        symbols = adapter.extract_symbols(source, "extension/src/context_engineClient.ts")
        assert {symbol.name for symbol in symbols} == {"SidecarClient", "ask", "health"}
        client = next(symbol for symbol in symbols if symbol.name == "SidecarClient")
        assert client.kind == "object_api"
        assert client.signature_status == "object_api_export"
        ask = next(symbol for symbol in symbols if symbol.name == "ask")
        health = next(symbol for symbol in symbols if symbol.name == "health")
        assert ask.qualified_name.endswith(".SidecarClient.ask")
        assert health.qualified_name.endswith(".SidecarClient.health")

    def test_extract_injections_from_constructor_decorator(self, adapter):
        source = """
import { Inject } from '@nestjs/common';
import { UserService } from './user.service';

export class CatsController {
  constructor(@Inject(UserService) private readonly users: UserService) {}
}
"""
        rows = adapter.extract_injections(source, "src/cats.controller.ts")
        pairs = {(r["owner_name"], r["provider_name"]) for r in rows}
        assert ("CatsController", "UserService") in pairs

    def test_extract_injections_skips_type_only_constructor_params(self, adapter):
        source = """
import { UserService } from './user.service';

export class CatsController {
  constructor(private readonly users: UserService) {}
}
"""
        assert adapter.extract_injections(source, "src/cats.controller.ts") == []

    def test_extract_instantiations_local_and_imported(self, adapter):
        source = """
import { NestFactory } from '@nestjs/core';

class AppModule {}

export async function bootstrap() {
  const app = new AppModule();
  return new NestFactory();
}
"""
        rows = adapter.extract_instantiations(source, "src/main.ts")
        by_type = {r["type_name"]: r for r in rows}
        assert "AppModule" in by_type
        assert by_type["AppModule"]["is_external"] is False
        assert "NestFactory" in by_type
        assert by_type["NestFactory"]["is_external"] is True

    def test_extract_attr_accesses_this_reads_and_writes(self, adapter):
        source = """
export class ConfigService {
  load() {
    const x = this.options;
    this.cache = x;
  }
}
"""
        rows = adapter.extract_attr_accesses(source, "src/config.service.ts")
        kinds = {(r["attr_name"], r["kind"]) for r in rows}
        assert ("options", "read") in kinds
        assert ("cache", "write") in kinds

    def test_extract_property_api_edges(self, adapter):
        source = """
export const router = {};
router.get = function getHandler() {};
"""
        edges = adapter.extract_property_api_edges(source, "src/router.ts")
        assert len(edges) == 1
        assert edges[0].edge_type == "HAS_API"

    def test_extract_reexports_from_index_barrel(self, adapter):
        source = """
export { UserService } from './user.service';
export { AuthModule as Auth } from './auth.module';
"""
        rows = adapter.extract_reexports(source, "src/index.ts")
        by_name = {r["export_name"]: r["export_qualified_name"] for r in rows}
        assert "UserService" in by_name
        assert by_name["UserService"].endswith("user.service.UserService")
        assert by_name["Auth"].endswith("auth.module.AuthModule")

    def test_extract_symbol_aliases_from_renamed_export(self, adapter):
        source = "export { UserService as Users } from './user.service';"
        rows = adapter.extract_symbol_aliases(source, "src/public.ts")
        assert len(rows) == 1
        assert rows[0]["source_name"] == "Users"
        assert rows[0]["target_name"] == "UserService"

    def test_extract_calls_scoped_for_unique_local_and_this_method(self, adapter):
        source = """
function helper() { return 1; }
export class Svc {
  ping() {}

  run() {
    helper();
    this.ping();
  }
}
"""
        calls = adapter.extract_calls_from_source(source, "src/svc.ts")
        rels = {c["callee_name"]: c["rel_type"] for c in calls if c.get("callee_name")}
        assert rels.get("helper") == "CALLS_SCOPED"
        assert rels.get("ping") == "CALLS_SCOPED"

    def test_extract_calls_guess_unresolved_identifier(self, adapter):
        source = """
export function track() {
  fetchData();
}
"""
        calls = adapter.extract_calls_from_source(source, "src/track.ts")
        call = next(c for c in calls if c.get("callee_name") == "fetchData")
        assert call["rel_type"] == "CALLS_GUESS"
        assert call["tier"] == "guess"
        assert call["resolver"] == "ts-ambiguity-gate-v1"
        assert "callee_uid" not in call

    def test_extract_calls_skips_standard_global_identifier(self, adapter):
        source = """
export function log() {
  console.log('x');
  fetch('/api');
}
"""
        calls = adapter.extract_calls_from_source(source, "src/log.ts")
        assert not any(c.get("callee_name") == "console" for c in calls)
        assert not any(c.get("callee_name") == "fetch" for c in calls)

    def test_extract_calls_guess_any_receiver_member(self, adapter):
        source = """
export function render(ctx: any) {
  ctx.render();
}
"""
        calls = adapter.extract_calls_from_source(source, "src/view.ts")
        call = next(c for c in calls if c.get("callee_name") == "render")
        assert call["rel_type"] == "CALLS_GUESS"
        assert call["tier"] == "guess"

    def test_extract_calls_guess_ambient_global_object_member(self, adapter):
        source = """
export function track() {
  dataDogTracker.log('evt');
}
"""
        calls = adapter.extract_calls_from_source(source, "src/metrics.ts")
        call = next(c for c in calls if c.get("callee_name") == "log")
        assert call["rel_type"] == "CALLS_GUESS"

    def test_extract_calls_direct_for_lexical_callback(self, adapter):
        source = """
export function run() {
  const handler = () => {};
  handler();
}
"""
        calls = adapter.extract_calls_from_source(source, "src/run.ts")
        call = next(c for c in calls if c.get("callee_name") == "handler")
        assert call["rel_type"] == "CALLS_DIRECT"
        assert call["resolver"] == "ts-scope-graph-v1"

    def test_extract_calls_scoped_via_destructure_factory(self, adapter):
        source = """
function useTasks() {
  return { runTask() {} };
}

export function setup() {
  const { runTask } = useTasks();
  runTask();
}
"""
        calls = adapter.extract_calls_from_source(source, "src/tasks.ts")
        call = next(c for c in calls if c.get("callee_name") == "runTask")
        assert call["rel_type"] == "CALLS_SCOPED"
        assert call["callee_uid"] == adapter._uid("src/tasks.ts", "useTasks")
        assert call["resolver"] == "ts-scope-graph-v1"

    def test_extract_calls_dynamic_via_closure_param(self, adapter):
        source = """
interface Client {
  dispatch(body: unknown): void;
}

export function controller(shadowClient: Client) {
  return function handleRequest() {
    return () => {
      shadowClient.dispatch({});
    };
  };
}
"""
        calls = adapter.extract_calls_from_source(source, "src/ctrl.ts")
        dispatch = next(c for c in calls if c.get("callee_name") == "dispatch")
        assert dispatch["rel_type"] == "CALLS_DYNAMIC"
        assert dispatch["resolver"] == "ts-scope-graph-v1"

    def test_extract_instantiations_typed_local_new(self, adapter):
        source = """
class AppModule {}

export function build() {
  const routeClass: AppModule = AppModule;
  return new routeClass();
}
"""
        rows = adapter.extract_instantiations(source, "src/main.ts")
        assert any(r["type_name"] == "AppModule" for r in rows)

    def test_extract_proxy_bindings_native_proxy(self, adapter):
        source = """
import { Request } from './request';

const scopedRequest = new Proxy({} as Request, {
  get(_t, prop) { return Reflect.get(_t, prop); },
});
"""
        rows = adapter.extract_proxy_bindings(source, "src/context.ts")
        assert len(rows) == 1
        row = rows[0]
        assert row["proxy_name"] == "scopedRequest"
        assert row["target_source"] == "native_proxy"
        assert row["target_type"].endswith("Request")

    def test_extract_proxy_bindings_skips_non_proxy_new(self, adapter):
        source = "const app = new AppModule();"
        assert adapter.extract_proxy_bindings(source, "src/main.ts") == []

    def test_mark_property_accessor_symbols(self, adapter):
        source = """
class Box {
  get value() { return this._v; }
  set value(v: number) { this._v = v; }
  run() {}
}
"""
        symbols = adapter.extract_symbols(source, "src/box.ts")
        getters = [s for s in symbols if s.is_getter]
        setters = [s for s in symbols if s.is_setter]
        assert len(getters) == 1 and getters[0].name == "value"
        assert len(setters) == 1 and setters[0].name == "value"
        assert not any(s.is_getter or s.is_setter for s in symbols if s.name == "run")

    def test_extract_hooks_middleware_use(self, adapter):
        source = """
function logger(req: any, res: any, next: any) {}

export function configure(app: any) {
  app.use(logger);
}
"""
        rows = adapter.extract_hooks(source, "src/app.ts")
        assert any(r["hook_name"] == "logger" and r["via"] == "use" for r in rows)

    def test_extract_hooks_interceptors_use(self, adapter):
        source = """
function attachToken(config: any) {}

export function setup(client: any) {
  client.interceptors.request.use(attachToken);
}
"""
        rows = adapter.extract_hooks(source, "src/client.ts")
        assert any(r["hook_name"] == "attachToken" and r["via"] == "interceptors" for r in rows)

    def test_extract_hooks_lifecycle_method(self, adapter):
        source = """
export class AppService {
  onModuleInit() {}
}
"""
        rows = adapter.extract_hooks(source, "src/app.service.ts")
        assert any(r["hook_name"] == "onModuleInit" and r["target_kind"] == "method" for r in rows)

    def test_extract_hooks_subscribe(self, adapter):
        source = """
function onEvent(value: unknown) {}

export function listen(events: any) {
  events.subscribe(onEvent);
}
"""
        rows = adapter.extract_hooks(source, "src/events.ts")
        assert any(r["hook_name"] == "onEvent" and r["via"] == "subscribe" for r in rows)

    def test_extract_hooks_event_emitter_on(self, adapter):
        source = """
function onUser(user: unknown) {}

export function wire(bus: any) {
  bus.on('user', onUser);
}
"""
        rows = adapter.extract_hooks(source, "src/bus.ts")
        assert any(
            r["hook_name"] == "user"
            and r["kind"] == "config"
            and r["target_kind"] == "method"
            and r["via"] == "on"
            for r in rows
        )
        assert any(
            r["hook_name"] == "onUser" and r["target_kind"] == "handler" and r["via"] == "on"
            for r in rows
        )

    def test_extract_hooks_event_emitter_emit(self, adapter):
        source = """
export function notify(bus: any, payload: unknown) {
  bus.emit('user', payload);
}
"""
        rows = adapter.extract_hooks(source, "src/bus.ts")
        assert any(
            r["hook_name"] == "user"
            and r["kind"] == "exec"
            and r["target_kind"] == "method"
            and r["via"] == "emit"
            for r in rows
        )

    def test_extract_hooks_non_identifier_topic_wrapper_only(self, adapter):
        source = """
export function notify(bus: any) {
  bus.emit('user:login');
}
"""
        rows = adapter.extract_hooks(source, "src/bus.ts")
        assert any(
            r["hook_name"] == ""
            and r["kind"] == "exec"
            and r["via"] == "emit"
            and r.get("target_kind") == "method"
            for r in rows
        )
        assert not any(r["hook_name"] == "user:login" for r in rows)

    def test_extract_hooks_add_event_listener(self, adapter):
        source = """
function handleClick() {}

export function bind(el: HTMLElement) {
  el.addEventListener('click', handleClick);
}
"""
        rows = adapter.extract_hooks(source, "src/dom.ts")
        assert any(r["hook_name"] == "click" and r["via"] == "addEventListener" for r in rows)
        assert any(r["hook_name"] == "handleClick" and r["via"] == "addEventListener" for r in rows)

    def test_extract_hooks_subject_next(self, adapter):
        source = """
export function push(subject: any, value: number) {
  subject.next(value);
}
"""
        rows = adapter.extract_hooks(source, "src/rx.ts")
        assert any(
            r["hook_name"] == "subject"
            and r["kind"] == "exec"
            and r["target_kind"] == "object"
            and r["via"] == "next"
            for r in rows
        )

    def test_extract_inheritance_event_emitter(self, adapter):
        source = """
import { EventEmitter } from 'events';

export class AppBus extends EventEmitter {}
"""
        edges = adapter.extract_inheritance(source, "src/bus.ts")
        assert any(e.superclass_name == "EventEmitter" for e in edges)

    def test_mark_react_hook_symbols(self, adapter):
        source = """
export function useCounter() {
  return 0;
}

function userService() {}
"""
        symbols = adapter.extract_symbols(source, "src/hooks.ts")
        by_name = {s.name: s for s in symbols}
        assert by_name["useCounter"].is_react_hook is True
        assert by_name["userService"].is_react_hook is False

    def test_import_bindings_resolve_through_barrel_reexport(self, adapter, tmp_path):
        from context_engine.parser.uid import project_root_scope

        src = tmp_path / "src"
        src.mkdir()
        (src / "user.service.ts").write_text("export function UserService() {}\n")
        (src / "index.ts").write_text("export { UserService } from './user.service';\n")
        consumer = """
import { UserService } from './index';
export function run() { UserService(); }
"""
        (src / "consumer.ts").write_text(consumer)

        with project_root_scope(str(tmp_path)):
            bindings, _ = adapter._extract_import_bindings(consumer, "src/consumer.ts")
            assert bindings["UserService"].endswith("user.service.UserService")

            calls = adapter.extract_calls_from_source(consumer, "src/consumer.ts")
            call = next(c for c in calls if c.get("callee_name") == "UserService")
            assert call["rel_type"] == "CALLS_IMPORTED"
            assert call["callee_qualified_name"].endswith("user.service.UserService")

    def test_extract_reexports_includes_star_export_surface(self, adapter, tmp_path):
        from context_engine.parser.uid import project_root_scope

        src = tmp_path / "src"
        src.mkdir()
        (src / "foo.ts").write_text("export function helper() {}\n")
        barrel = "export * from './foo';\n"

        with project_root_scope(str(tmp_path)):
            rows = adapter.extract_reexports(barrel, "src/index.ts")
            by_name = {r["export_name"]: r["export_qualified_name"] for r in rows}
            assert "helper" in by_name
            assert by_name["helper"].endswith("foo.helper")

    def test_extract_metadata_bridges_define_and_read(self, adapter):
        producer = """
import { CATCH_WATERMARK } from '../constants';
export function Catch(...exceptions) {
  return (target) => {
    Reflect.defineMetadata(CATCH_WATERMARK, true, target);
  };
}
"""
        consumer = """
import { CATCH_WATERMARK } from '../constants';
export class Scanner {
  reflect(metatype) {
    return Reflect.getMetadata(CATCH_WATERMARK, metatype);
  }
}
"""
        pr = adapter.extract_metadata_bridges(producer, "src/catch.decorator.ts")
        cr = adapter.extract_metadata_bridges(consumer, "src/scanner.ts")
        define = next(r for r in pr if r["role"] == "define")
        read = next(r for r in cr if r["role"] == "read")
        assert define["via"] == "Reflect.defineMetadata"
        assert read["via"] == "Reflect.getMetadata"
        # Same key constant ⇒ identical bridge identity across files.
        assert define["key_qn"] == read["key_qn"]
        assert define["key_qn"].endswith("constants.CATCH_WATERMARK")

    def test_extract_metadata_bridges_setmetadata_producer(self, adapter):
        source = """
import { ROLES_KEY } from './constants';
export const Roles = (...roles) => SetMetadata(ROLES_KEY, roles);
"""
        rows = adapter.extract_metadata_bridges(source, "src/roles.decorator.ts")
        row = next(r for r in rows if r["via"] == "SetMetadata")
        assert row["role"] == "define"
        assert row["key_name"] == "ROLES_KEY"

    def test_extract_metadata_bridges_extend_array_and_create_context(self, adapter):
        producer = """
import { GUARDS_METADATA } from '../../constants';
import { extendArrayMetadata } from '../../utils/extend-metadata.util';
export function UseGuards(...guards) {
  return (target, key, descriptor) => {
    extendArrayMetadata(GUARDS_METADATA, guards, descriptor.value);
  };
}
"""
        consumer = """
import { GUARDS_METADATA } from '@nestjs/common/constants';
export class GuardsContextCreator {
  create(instance, callback, module, contextId) {
    return this.createContext(instance, callback, GUARDS_METADATA, contextId);
  }
}
"""
        pr = adapter.extract_metadata_bridges(producer, "packages/common/decorators/use-guards.ts")
        cr = adapter.extract_metadata_bridges(consumer, "packages/core/guards/creator.ts")
        define = next(r for r in pr if r["role"] == "define")
        read = next(r for r in cr if r["role"] == "read")

        assert define["via"] == "extendArrayMetadata"
        assert read["via"] == "createContext"
        assert define["key_name"] == "GUARDS_METADATA"
        assert read["key_name"] == "GUARDS_METADATA"
        assert define["key_qn"].endswith("constants.GUARDS_METADATA")
        assert read["key_qn"].endswith("constants.GUARDS_METADATA")

    def test_extract_metadata_bridges_reflector_gates_non_constant_key(self, adapter):
        source = """
import { ROLES_KEY } from './constants';
export class RolesGuard {
  constructor(reflector) { this.reflector = reflector; }
  canActivate(ctx) {
    const a = this.reflector.getAllAndOverride(ROLES_KEY, [ctx.getHandler()]);
    const b = someMap.get('plain-string');
    const c = cache.get(localVar);
    return a;
  }
}
"""
        rows = adapter.extract_metadata_bridges(source, "src/roles.guard.ts")
        reads = [r for r in rows if r["role"] == "read"]
        # The imported-constant Reflector read is kept; Map.get with a string /
        # local-variable key is rejected (would otherwise swamp the bridge).
        assert any(r["key_name"] == "ROLES_KEY" for r in reads)
        assert not any(r["key_name"] in {"plain-string", "localVar"} for r in reads)

    def test_monorepo_package_alias_resolves_cross_package_import(self, adapter, tmp_path):
        from context_engine.parser.uid import project_root_scope

        common = tmp_path / "packages" / "common"
        common.mkdir(parents=True)
        (common / "package.json").write_text('{"name": "@scope/common"}\n')
        (common / "constants.ts").write_text("export const KEY = '__k__';\n")
        consumer = "import { KEY } from '@scope/common/constants';\nKEY;\n"

        with project_root_scope(str(tmp_path)):
            bindings, _ = adapter._extract_import_bindings(consumer, "packages/core/scanner.ts")
            # ``@scope/common/constants`` must resolve to the in-repo module, not
            # the dotted-external ``@scope.common.constants``.
            assert bindings["KEY"].endswith("packages.common.constants.KEY")

    def test_behavioral_shape_flags_mapping_and_constructed(self, adapter):
        source = """
export class Compiler {
  compile(metadata: any) {
    const result = {};
    for (const key of metadata.keys) {
      result[key] = metadata[key];
    }
    return result;
  }
  build() {
    return new Container();
  }
  names(): string[] {
    return [1, 2, 3];
  }
  noop(x: number) {
    return x + 1;
  }
}
"""
        symbols = adapter.extract_symbols(source, "src/compiler.ts")
        by_name = {s.name: s for s in symbols}
        assert by_name["compile"].returns_mapping is True
        assert by_name["compile"].assembles_mapping_in_loop is True
        assert by_name["build"].returns_constructed_type is True
        assert by_name["names"].returns_sequence is True
        # A scalar-returning method carries no shape flag.
        n = by_name["noop"]
        assert not (n.returns_mapping or n.returns_sequence or n.returns_constructed_type)

    def test_abstract_class_extracted_as_symbol(self, adapter):
        # ``abstract class`` parses as ``abstract_class_declaration`` — a distinct
        # node type. It must still yield a class Symbol (+ qualified methods),
        # else inheritance to abstract bases (NestJS/Angular) cannot resolve.
        source = """
export abstract class ContextCreator {
  public abstract create(): void;
  public createContext(x: number): number {
    return x + 1;
  }
}
"""
        symbols = adapter.extract_symbols(source, "src/context-creator.ts")
        classes = [s for s in symbols if s.kind == "class"]
        assert [c.name for c in classes] == ["ContextCreator"]
        method = next(s for s in symbols if s.name == "createContext")
        assert method.qualified_name.endswith("ContextCreator.createContext")

    def test_abstract_class_inheritance_edge(self, adapter):
        source = """
export abstract class Base {}
export abstract class Derived extends Base {}
export class Concrete extends Derived {}
"""
        edges = adapter.extract_inheritance(source, "src/m.ts")
        pairs = {(e.subclass_uid, e.superclass_name) for e in edges}
        names = {sn for _, sn in pairs}
        # Both the abstract subclass (Derived) and the concrete one resolve their
        # bases — the regex previously skipped ``abstract class X extends Y``.
        assert "Base" in names
        assert "Derived" in names

    def test_behavioral_shape_new_map_is_mapping_not_constructed(self, adapter):
        source = """
export function index() {
  return new Map();
}
"""
        symbols = adapter.extract_symbols(source, "src/index.ts")
        sym = next(s for s in symbols if s.name == "index")
        assert sym.returns_mapping is True
        assert sym.returns_constructed_type is False

    def test_extract_axis_facts_adds_typescript_ast_bits(self, adapter):
        source = """
@Controller("users")
export class AppController extends BaseController {
  @Get(":id")
  find(@Param("id") id: string): Foo {
    this.cache = new Map();
    return this.service.fetch(id);
  }
}
"""
        facts = adapter.extract_axis_facts(source, "src/app.controller.ts")
        profiles = AxisExtraction("src/app.controller.ts", facts).profiles_by_qualified_name

        class_profile = profiles["src.app.controller.AppController"]
        assert {"decorator_application"} <= class_profile.cfg_bits
        assert {"decorator_attachment", "decorator_shape", "inheritance"} <= (
            class_profile.struct_bits
        )

        method_profile = profiles["src.app.controller.AppController.find"]
        assert {
            "call_site",
            "constructor_call",
            "decorator_application",
            "method_dispatch",
            "return_exit",
        } <= method_profile.cfg_bits
        assert {
            "attr_write",
            "call_argument",
            "constructor_value",
            "parameter_input",
            "return_output",
        } <= method_profile.dfg_bits
        assert {"annotation", "decorator_attachment", "parameter_decl"} <= (
            method_profile.struct_bits
        )
