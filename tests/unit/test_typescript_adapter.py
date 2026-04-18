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

    def test_language_name(self, adapter):
        assert adapter.language_name == "typescript"

    def test_file_extensions(self, adapter):
        assert adapter.file_extensions == {".ts", ".tsx"}
