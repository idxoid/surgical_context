import pytest

from context_engine.parser.adapters.python_adapter import PythonAdapter


class TestPythonAdapter:
    @pytest.fixture
    def adapter(self):
        return PythonAdapter()

    def test_extract_function(self, adapter):
        # ``extract_symbols`` now also synthesizes one module Symbol per file
        # so module-scope facts have a coherent caller to attach to.
        source = "def foo(): pass"
        symbols = adapter.extract_symbols(source, "test.py")
        non_module = [s for s in symbols if s.kind != "module"]
        assert len(non_module) == 1
        assert non_module[0].name == "foo"
        assert non_module[0].kind == "function"
        # Exactly one module Symbol per file.
        modules = [s for s in symbols if s.kind == "module"]
        assert len(modules) == 1
        assert modules[0].qualified_name == "test"

    def test_extract_class(self, adapter):
        source = "class Bar: pass"
        symbols = adapter.extract_symbols(source, "test.py")
        non_module = [s for s in symbols if s.kind != "module"]
        assert len(non_module) == 1
        assert non_module[0].name == "Bar"
        assert non_module[0].kind == "class"

    def test_extract_multiple_symbols(self, adapter):
        source = """
def func1(): pass

class MyClass:
    pass

def func2(): pass
"""
        symbols = adapter.extract_symbols(source, "test.py")
        non_module_names = {s.name for s in symbols if s.kind != "module"}
        assert non_module_names == {"func1", "MyClass", "func2"}

    def test_class_attribute_symbols(self, adapter):
        source = """
class Config:
    plain = 1
    typed: int = 2
    bare: str
    plain = 3

    def method(self): pass

    class Nested:
        inner = 4

def factory():
    class Local:
        hidden = 5
"""
        symbols = adapter.extract_symbols(source, "test.py")
        attrs = {s.qualified_name: s for s in symbols if s.kind == "variable"}
        assert set(attrs) == {
            "test.Config.plain",
            "test.Config.typed",
            "test.Config.bare",
            "test.Config.Nested.inner",
        }
        plain = attrs["test.Config.plain"]
        assert plain.name == "plain"
        # First assignment wins on re-assignment of the same name.
        assert plain.start_line == 3

    def test_class_attribute_skips_names_claimed_by_defs(self, adapter):
        source = """
class Config:
    handler = None

    def handler(self): pass
"""
        symbols = adapter.extract_symbols(source, "test.py")
        claimed = [s for s in symbols if s.qualified_name == "test.Config.handler"]
        assert len(claimed) == 1
        assert claimed[0].kind == "function"

    def test_class_attribute_symbols_env_disabled(self, adapter, monkeypatch):
        monkeypatch.setenv("AXIS_INDEX_CLASS_ATTRS", "0")
        source = """
class Config:
    plain = 1
"""
        symbols = adapter.extract_symbols(source, "test.py")
        assert not [s for s in symbols if s.kind == "variable"]

    def test_reexport_alias_symbols(self, adapter):
        source = """
__all__ = ("Listed", "local_def")

from extlib.errors import Listed, Unlisted
from extlib.types import Explicit as Explicit
from extlib.misc import renamed as other_name
from extlib.plain import Ordinary

def local_def(): pass
"""
        symbols = adapter.extract_symbols(source, "pkg/mod.py")
        aliases = {s.name: s for s in symbols if s.kind == "variable"}
        # __all__ membership and the ``X as X`` idiom qualify; a plain import
        # and a renaming alias in a regular module do not.
        assert set(aliases) == {"Listed", "Explicit"}
        assert aliases["Listed"].qualified_name == "pkg.mod.Listed"
        assert aliases["Listed"].start_line == 4

    def test_reexport_alias_in_init_and_multiline(self, adapter):
        source = """
from extlib.websockets import (
    WebSocket,
    WebSocketDisconnect,
)
from .sibling import Internal
"""
        symbols = adapter.extract_symbols(source, "pkg/__init__.py")
        aliases = {s.name for s in symbols if s.kind == "variable"}
        # Package __init__ is public surface: external from-imports qualify
        # even bare; the in-project sibling import stays alias-free.
        assert aliases == {"WebSocket", "WebSocketDisconnect"}

    def test_reexport_alias_env_disabled(self, adapter, monkeypatch):
        monkeypatch.setenv("AXIS_INDEX_REEXPORT_ALIASES", "0")
        source = "from extlib.types import Explicit as Explicit\n"
        symbols = adapter.extract_symbols(source, "pkg/mod.py")
        assert not [s for s in symbols if s.kind == "variable"]

    def test_extract_calls(self, adapter):
        source = """
def foo():
    bar()

def bar():
    pass
"""
        calls = adapter.extract_calls_from_source(source, "test.py")
        assert len(calls) > 0
        assert any(call.get("callee_name") == "bar" for call in calls)

    def test_module_scope_call_attaches_to_module_symbol(self, adapter):
        source = """
def getattr_migration(module):
    return module

__getattr__ = getattr_migration(__name__)
"""
        calls = adapter.extract_calls_from_source(source, "shim.py")
        module_uid = adapter._module_symbol_identity("shim.py")[2]
        hits = [c for c in calls if c.get("callee_name") == "getattr_migration"]
        assert hits, calls
        assert hits[0]["caller_uid"] == module_uid

    def test_module_scope_call_env_disabled(self, adapter, monkeypatch):
        monkeypatch.setenv("AXIS_MODULE_SCOPE_CALLS", "0")
        source = "def f():\n    pass\n\nx = f()\n"
        calls = adapter.extract_calls_from_source(source, "shim.py")
        assert not [c for c in calls if c.get("callee_name") == "f"]

    def test_external_from_import_sets_qualified_name_and_args(self, adapter):
        source = """
from pathlib import Path

def get_root():
    pass

def route(root):
    Path(root)
"""
        calls = adapter.extract_calls_from_source(source, "routes.py")
        path_call = next(c for c in calls if c.get("callee_name") == "Path")
        assert path_call.get("callee_qualified_name") == "pathlib.Path"
        assert path_call.get("arguments") == ["root"]

    def test_package_init_relative_import_alias_keeps_current_package(self, adapter):
        source = """
from ...orm.decl_api import declarative_base as _declarative_base

def declarative_base(*arg, **kw):
    return _declarative_base(*arg, **kw)
"""
        calls = adapter.extract_calls_from_source(
            source,
            "QA/repos/sqlalchemy/lib/sqlalchemy/ext/declarative/__init__.py",
        )

        call = next(c for c in calls if c.get("callee_name") == "_declarative_base")
        assert call.get("rel_type") == "CALLS_IMPORTED"
        assert (
            call.get("callee_qualified_name")
            == "QA.repos.sqlalchemy.lib.sqlalchemy.orm.decl_api.declarative_base"
        )

    def test_external_module_attribute_sets_qualified_name_and_args(self, adapter):
        source = """
import pathlib

def get_root():
    pass

def route(root):
    pathlib.Path(root)
"""
        calls = adapter.extract_calls_from_source(source, "routes.py")
        path_call = next(c for c in calls if c.get("callee_name") == "Path")
        assert path_call.get("callee_qualified_name") == "pathlib.Path"
        assert path_call.get("arguments") == ["root"]

    def test_local_symbol_has_no_import_qualified_name(self, adapter):
        source = """
def Path():
    pass

def foo():
    Path()
"""
        calls = adapter.extract_calls_from_source(source, "local.py")
        path_call = next(c for c in calls if c.get("callee_name") == "Path")
        assert "callee_qualified_name" not in path_call

    def test_typed_tier_resolves_string_cls_collaborator_via_local_alias(self, adapter):
        # `<base>_cls = 'mod:Class'` convention + `local = self.attr` alias (Celery shape).
        source = """
class Celery:
    amqp_cls = 'pkg.app.amqp:AMQP'

    def send_task(self):
        amqp = self.amqp
        amqp.create_task_message()
"""
        calls = adapter.extract_calls_from_source(source, "pkg/app/base.py")
        call = next(c for c in calls if c["callee_name"] == "create_task_message")
        assert call["tier"] == "typed"
        assert call["confidence"] == pytest.approx(0.8)
        assert call["callee_qualified_name"] == "pkg.app.amqp.AMQP.create_task_message"

    def test_typed_tier_resolves_init_instantiation_direct_attribute(self, adapter):
        # `self.x = Class()` in __init__ + direct `self.x.method()`.
        source = """
from pkg.svc import Service

class Worker:
    def __init__(self):
        self.svc = Service()

    def run(self):
        self.svc.handle()
"""
        calls = adapter.extract_calls_from_source(source, "pkg/worker.py")
        call = next(c for c in calls if c["callee_name"] == "handle")
        assert call["tier"] == "typed"
        assert call["callee_qualified_name"] == "pkg.svc.Service.handle"

    def test_untyped_attribute_chain_emits_no_phantom_edge(self, adapter):
        # Unknown collaborator type -> no fabricated edge (preserves precision).
        source = """
class Thing:
    def run(self):
        self.unknown.do_work()
"""
        calls = adapter.extract_calls_from_source(source, "pkg/thing.py")
        assert not any(c["callee_name"] == "do_work" for c in calls)

    def test_return_type_self_method_ctor(self, adapter):
        # `s = self.factory()` where factory `return SomeClass(...)` → s : SomeClass.
        source = """
from pkg.svc import Service

class Worker:
    def make_svc(self):
        return Service()

    def run(self):
        s = self.make_svc()
        s.handle()
"""
        calls = adapter.extract_calls_from_source(source, "pkg/worker.py")
        call = next(c for c in calls if c["callee_name"] == "handle")
        assert call["tier"] == "typed"
        assert call["callee_qualified_name"] == "pkg.svc.Service.handle"

    def test_return_type_module_func_annotation(self, adapter):
        # `s = get_svc()` where `def get_svc() -> Service` → s : Service, even though
        # the body returns a non-inferable expression.
        source = """
from pkg.svc import Service

def get_svc() -> Service:
    return _global_singleton

def run():
    s = get_svc()
    s.handle()
"""
        calls = adapter.extract_calls_from_source(source, "pkg/m.py")
        call = next(c for c in calls if c["callee_name"] == "handle")
        assert call["tier"] == "typed"
        assert call["callee_qualified_name"] == "pkg.svc.Service.handle"

    def test_return_type_global_return_yields_no_edge(self, adapter):
        # `x = get_app()` where the func returns a bare global (Celery current_app
        # shape) → type not statically present, so no fabricated edge.
        source = """
def get_app():
    return _tls.current_app or default_app

def run():
    x = get_app()
    x.send_task()
"""
        calls = adapter.extract_calls_from_source(source, "pkg/state.py")
        send = next((c for c in calls if c["callee_name"] == "send_task"), None)
        assert send is None or "callee_qualified_name" not in send

    def test_attr_access_excludes_call_callees(self, adapter):
        # ``obj.method(...)`` is a call, not a data-shape attribute read — the
        # call resolver owns it. The attribute-access pass must NOT emit a
        # parallel READS_ATTR for the callee position (which, on an ambiguous
        # method name, mis-binds to the wrong same-named symbol). The receiver
        # of an outer access (``self.config`` in ``self.config.get()``) and a
        # plain value read (``self.config.value``) still emit.
        source = """
class Task:
    def apply_async(self, args=None, **options):
        app = self._get_app()
        return app.send_task(self.name, args, **options)

    def reader(self):
        x = self.config.get('k')
        return self.config.value
"""
        acc = adapter.extract_attr_accesses(source, "pkg/task.py")
        triples = {(a["accessor_name"], a["attr_name"], a["kind"]) for a in acc}
        # callees excluded
        assert ("apply_async", "send_task", "read") not in triples
        assert ("apply_async", "_get_app", "read") not in triples
        assert ("reader", "get", "read") not in triples
        # genuine reads kept
        assert ("reader", "config", "read") in triples
        assert ("reader", "value", "read") in triples
        assert ("apply_async", "name", "read") in triples

    def test_self_method_proxy_call_relink_candidate(self, adapter):
        # ``app = self._get_app(); app.send_task(...)`` where ``_get_app``
        # returns an imported global (a lazy proxy at graph time): emit a
        # points-to relink candidate carrying the returned global's qn. The
        # proxy → class hop is resolved later at graph time.
        source = """
from celery import current_app

class Task:
    _app = None

    @classmethod
    def _get_app(cls):
        if cls._app is None:
            cls._app = current_app
        return cls._app

    def apply_async(self, args=None, **options):
        app = self._get_app()
        if app.conf.task_always_eager:
            return self.apply(args)
        return app.send_task(self.name, args, **options)
"""
        cands = adapter.extract_self_method_proxy_calls(source, "celery/app/task.py")
        send = next((c for c in cands if c["callee_name"] == "send_task"), None)
        assert send is not None
        assert send["returns_global_qn"] == "celery.current_app"

    def test_self_method_proxy_call_skips_reassigned_local(self, adapter):
        # Precision: if the proxy-returning local is reassigned, its type is no
        # longer known — drop the candidate rather than guess.
        source = """
from celery import current_app

class T:
    @classmethod
    def _ga(cls):
        return current_app

    def m(self):
        a = self._ga()
        a = something_else()
        return a.send_task()
"""
        cands = adapter.extract_self_method_proxy_calls(source, "p/t.py")
        assert not any(c["callee_name"] == "send_task" for c in cands)

    def test_self_method_proxy_call_skips_non_proxy_return_method(self, adapter):
        # The source method must return an imported global; a plain helper
        # (no return-of-global) produces no candidate.
        source = """
class T:
    def helper(self):
        return self._make()

    def m(self):
        x = self.helper()
        return x.thing()
"""
        cands = adapter.extract_self_method_proxy_calls(source, "p/t.py")
        assert not any(c["callee_name"] == "thing" for c in cands)

    def test_proxy_binding_extracted_for_annotated_lazy_proxy(self, adapter):
        # `name: ProxyType = SomeProxy(...)` (Flask current_app shape): emit a proxy
        # binding to ProxyType. Cross-file call forwarding happens at index time
        # (ProxyBinding node + PROXY_OF edge), not here in per-file extraction.
        source = """
from werkzeug.local import LocalProxy

current_app: FlaskProxy = LocalProxy(_cv_app, "app")
"""
        bindings = adapter.extract_proxy_bindings(source, "pkg/globals.py")
        b = next(b for b in bindings if b["proxy_name"] == "current_app")
        assert b["proxy_qualified_name"] == "pkg.globals.current_app"
        assert b["target_type"] == "pkg.globals.FlaskProxy"

    def test_proxy_binding_extracts_context_attr_binding_for_local_proxy(self, adapter):
        source = """
from contextvars import ContextVar
from werkzeug.local import LocalProxy

from .ctx import AppContext

_cv_app: ContextVar[AppContext] = ContextVar("flask.app_ctx")
request: RequestProxy = LocalProxy(  # type: ignore[assignment]
    _cv_app, "request", unbound_message="missing"
)
"""
        bindings = adapter.extract_proxy_bindings(source, "pkg/globals.py")
        b = next(b for b in bindings if b["proxy_name"] == "request")

        assert b["target_type"] == "pkg.globals.RequestProxy"
        assert b["context_var"] == "_cv_app"
        assert b["context_type"] == "pkg.ctx.AppContext"
        assert b["context_attr"] == "request"
        assert b["binding_source"] == "context_attr"

    def test_proxy_binding_skips_unannotated_proxy(self, adapter):
        # `current_app = Proxy(get_current_app)` (Celery shape, no annotation): the
        # forwarded type needs the callable's return type (a separate hop), so emit
        # no binding rather than guess.
        source = """
current_app = Proxy(get_current_app)
"""
        bindings = adapter.extract_proxy_bindings(source, "pkg/_state.py")
        assert not any(b["proxy_name"] == "current_app" for b in bindings)

    def test_extract_decorators_forms(self, adapter):
        # @name, @obj.attr(...), @call() all yield a DECORATED_BY fact with the
        # decorator's callable identifier as the base name.
        source = """
@my_decorator
def handler(x):
    return x

@app.route("/p")
def view():
    pass

@register()
def job():
    pass
"""
        decos = adapter.extract_decorators(source, "pkg/m.py")
        by_decorated = {d["decorated_name"]: d["decorator_name"] for d in decos}
        assert by_decorated["handler"] == "my_decorator"
        assert by_decorated["view"] == "route"
        assert by_decorated["job"] == "register"

    def test_extract_decorators_records_dotted_callable_owner(self, adapter):
        source = """
from pkg import registry

@registry.Owner.strategy_for(kind="x")
class Handler:
    pass
"""
        decos = adapter.extract_decorators(source, "app/handlers.py")
        deco = decos[0]

        assert deco["decorator_name"] == "strategy_for"
        assert deco["decorator_callable_name"] == "registry.Owner.strategy_for"
        assert deco["decorator_qualified_name"] == "pkg.registry.Owner.strategy_for"
        assert deco["decorator_owner_name"] == "registry.Owner"
        assert deco["decorator_owner_qualified_name"] == "pkg.registry.Owner"

    def test_extract_decorators_skips_builtins(self, adapter):
        # property/staticmethod/functools.wraps are machinery, never DECORATED_BY targets.
        source = """
import functools

class C:
    @property
    def p(self):
        return 1

    @staticmethod
    def s():
        pass

    @functools.wraps(fn)
    def w(self):
        pass

    @app.task
    def tsk(self):
        pass
"""
        decos = adapter.extract_decorators(source, "pkg/m.py")
        names = {d["decorated_name"]: d["decorator_name"] for d in decos}
        assert names == {"tsk": "task"}  # only the real framework decorator survives

    def test_language_name(self, adapter):
        assert adapter.language_name == "python"

    def test_file_extensions(self, adapter):
        assert adapter.file_extensions == {".py", ".pyi"}

    def test_extract_injections_links_provider(self, adapter):
        source = """
from fastapi import Depends

def get_query():
    pass

async def read_items(q: str = Depends(get_query)):
    pass
"""
        rows = adapter.extract_injections(source, "app/routes.py")
        pairs = {(r["owner_name"], r["provider_name"]) for r in rows}
        # Structural: the wrapped provider symbol, not the marker name.
        assert ("read_items", "get_query") in pairs


class TestInstantiations:
    @pytest.fixture
    def adapter(self):
        return PythonAdapter()

    def _targets(self, adapter, source, path):
        return {d["type_name"] for d in adapter.extract_instantiations(source, path)}

    def test_literal_construction(self, adapter):
        source = """
class Foo:
    pass

def g():
    return Foo(1)
"""
        assert self._targets(adapter, source, "pkg/e.py") == {"Foo"}

    def test_typed_param_direct_call(self, adapter):
        # `v: type[X]` called directly constructs X (instantiate-v1).
        source = """
from pkg.routing import APIRoute

def build(cls: type[APIRoute]):
    return cls(1)
"""
        assert self._targets(adapter, source, "pkg/a.py") == {"APIRoute"}

    def test_p5_disjunction_of_typed_param_and_self_attr(self, adapter):
        # P5: `route_class = route_class_override or self.route_class; route_class(...)`
        # resolves via the type[APIRoute] param operand; self.route_class is unresolved.
        source = """
from pkg.routing import APIRoute

class APIRouter:
    def add_api_route(self, path, route_class_override: type[APIRoute] | None = None):
        route_class = route_class_override or self.route_class
        route = route_class(path)
        return route
"""
        assert self._targets(adapter, source, "pkg/routing.py") == {"APIRoute"}

    def test_p5_ternary_unions_both_class_branches(self, adapter):
        source = """
from pkg.b import Other

class Local:
    pass

def build(flag):
    cls = Local if flag else Other
    return cls(1)
"""
        assert self._targets(adapter, source, "pkg/a.py") == {"Local", "Other"}

    def test_p5_copy_propagation_chain(self, adapter):
        source = """
class Widget:
    pass

def build():
    a = Widget
    b = a
    return b()
"""
        assert self._targets(adapter, source, "pkg/w.py") == {"Widget"}

    def test_p5_call_result_is_not_a_class(self, adapter):
        # Precision: a call result is an instance, never a class object.
        source = """
def make():
    x = factory()
    y = x()
    return y
"""
        assert self._targets(adapter, source, "pkg/c.py") == set()

    def test_p5_unresolved_self_attr_emits_nothing(self, adapter):
        # Precision: no instance-attribute typing -> no fabricated construction.
        source = """
def f(self):
    cls = self.something
    return cls()
"""
        assert self._targets(adapter, source, "pkg/d.py") == set()

    def test_type_token_is_not_emitted_as_a_class(self, adapter):
        # The `type` token inside `type[X]` must not leak as a candidate class.
        source = """
from pkg.routing import APIRoute

def build(cls: type[APIRoute]):
    return cls(1)
"""
        names = {d["type_name"] for d in adapter.extract_instantiations(source, "pkg/a.py")}
        assert "type" not in names
