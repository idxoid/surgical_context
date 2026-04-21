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

    def test_language_name(self, adapter):
        assert adapter.language_name == "typescript"

    def test_file_extensions(self, adapter):
        assert adapter.file_extensions == {".ts", ".tsx"}
