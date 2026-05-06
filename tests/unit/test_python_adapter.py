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

    def test_fastapi_depends_import_sets_qualified_name_and_args(self, adapter):
        source = """
from fastapi import Depends

def get_db():
    pass

def route(dep=Depends(get_db)):
    Depends(get_db)
"""
        calls = adapter.extract_calls_from_source(source, "routes.py")
        dep_calls = [c for c in calls if c.get("callee_name") == "Depends"]
        assert len(dep_calls) >= 1
        for c in dep_calls:
            assert c.get("callee_qualified_name") == "fastapi.Depends"
            assert c.get("arguments") == ["get_db"]

    def test_fastapi_module_attribute_depends(self, adapter):
        source = """
import fastapi

def get_db():
    pass

def route():
    fastapi.Depends(get_db)
"""
        calls = adapter.extract_calls_from_source(source, "routes.py")
        dep = next(c for c in calls if c.get("callee_name") == "Depends")
        assert dep.get("callee_qualified_name") == "fastapi.Depends"
        assert dep.get("arguments") == ["get_db"]

    def test_local_depends_has_no_fastapi_qualified_name(self, adapter):
        source = """
def Depends():
    pass

def foo():
    Depends()
"""
        calls = adapter.extract_calls_from_source(source, "local.py")
        dep = next(c for c in calls if c.get("callee_name") == "Depends")
        assert "callee_qualified_name" not in dep

    def test_language_name(self, adapter):
        assert adapter.language_name == "python"

    def test_file_extensions(self, adapter):
        assert adapter.file_extensions == {".py", ".pyi"}
