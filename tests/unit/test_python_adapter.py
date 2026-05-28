import pytest

from sidecar.parser.adapters.python_adapter import PythonAdapter


class TestPythonAdapter:
    @pytest.fixture
    def adapter(self):
        return PythonAdapter()

    def test_extract_function(self, adapter):
        source = "def foo(): pass"
        symbols = adapter.extract_symbols(source, "test.py")
        assert len(symbols) == 1
        assert symbols[0].name == "foo"
        assert symbols[0].kind == "function"

    def test_extract_class(self, adapter):
        source = "class Bar: pass"
        symbols = adapter.extract_symbols(source, "test.py")
        assert len(symbols) == 1
        assert symbols[0].name == "Bar"
        assert symbols[0].kind == "class"

    def test_extract_multiple_symbols(self, adapter):
        source = """
def func1(): pass

class MyClass:
    pass

def func2(): pass
"""
        symbols = adapter.extract_symbols(source, "test.py")
        assert len(symbols) == 3
        names = {s.name for s in symbols}
        assert names == {"func1", "MyClass", "func2"}

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
        assert call["confidence"] == 0.8
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

    def test_proxy_binding_skips_unannotated_proxy(self, adapter):
        # `current_app = Proxy(get_current_app)` (Celery shape, no annotation): the
        # forwarded type needs the callable's return type (a separate hop), so emit
        # no binding rather than guess.
        source = """
current_app = Proxy(get_current_app)
"""
        bindings = adapter.extract_proxy_bindings(source, "pkg/_state.py")
        assert not any(b["proxy_name"] == "current_app" for b in bindings)

    def test_language_name(self, adapter):
        assert adapter.language_name == "python"

    def test_file_extensions(self, adapter):
        assert adapter.file_extensions == {".py", ".pyi"}
