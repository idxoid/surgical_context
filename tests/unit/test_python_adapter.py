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

    def test_language_name(self, adapter):
        assert adapter.language_name == "python"

    def test_file_extensions(self, adapter):
        assert adapter.file_extensions == {".py", ".pyi"}
