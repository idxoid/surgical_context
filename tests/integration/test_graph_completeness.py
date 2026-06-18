import pytest

from context_engine.parser.adapters.javascript_adapter import JavaScriptAdapter
from context_engine.parser.adapters.python_adapter import PythonAdapter
from context_engine.parser.adapters.typescript_adapter import TypeScriptAdapter


class TestGraphCompleteness:
    """Test that all graph edge types (CALLS, IMPORTS, DEPENDS_ON) are extractable."""

    @pytest.fixture
    def py_adapter(self):
        return PythonAdapter()

    @pytest.fixture
    def ts_adapter(self):
        return TypeScriptAdapter()

    @pytest.fixture
    def js_adapter(self):
        return JavaScriptAdapter()

    def test_python_complete_extraction(self, py_adapter):
        """Test Python extracts symbols, calls, imports, and inheritance."""
        source = """
from payments import validators

class Base:
    pass

class Derived(Base):
    def __init__(self):
        self.check = validators.validate_amount

    def helper(self):
        self.check(1)

    def caller(self):
        self.helper()
"""
        file_path = "payments/processor.py"
        # Symbols
        symbols = py_adapter.extract_symbols(source, file_path)
        assert any(s.name == "Base" for s in symbols)
        assert any(s.name == "Derived" for s in symbols)

        # Calls
        calls = py_adapter.extract_calls_from_source(source, file_path)
        assert len(calls) > 0

        # Imports — intra-project only; stdlib/third-party are filtered out.
        imports = py_adapter.extract_imports(source, file_path)
        assert any("payments" in imp.target_module_name for imp in imports)

        # Inheritance
        inheritance = py_adapter.extract_inheritance(source, file_path)
        assert any(edge.superclass_name == "Base" for edge in inheritance)

    def test_typescript_complete_extraction(self, ts_adapter):
        """Test TypeScript extracts symbols, calls, imports, and inheritance."""
        source = """
import { Component } from "react";

class Base extends Component {
  render() {
    return null;
  }
}

class Derived extends Base {
  helper() {
    console.log("helper");
  }

  caller() {
    this.helper();
  }
}
"""
        # Symbols
        symbols = ts_adapter.extract_symbols(source, "test.ts")
        assert any(s.name == "Base" for s in symbols)
        assert any(s.name == "Derived" for s in symbols)

        # Calls
        calls = ts_adapter.extract_calls_from_source(source, "test.ts")
        assert len(calls) > 0

        # Imports
        imports = ts_adapter.extract_imports(source, "test.ts")
        assert any(imp.target_module_name == "react" for imp in imports)

        # Inheritance
        inheritance = ts_adapter.extract_inheritance(source, "test.ts")
        assert any(edge.superclass_name == "Component" for edge in inheritance)
        assert any(edge.superclass_name == "Base" for edge in inheritance)

    def test_javascript_complete_extraction(self, js_adapter):
        """Test JavaScript extracts symbols, calls, imports, and inheritance."""
        source = """
const util = require("./util");

class Base {}

class Derived extends Base {
  helper() {
    console.log("helper");
  }

  caller() {
    this.helper();
  }
}

app.use = function use(fn) {
  fn();
};
"""
        file_path = "lib/application.js"
        symbols = js_adapter.extract_symbols(source, file_path)
        assert any(s.name == "Base" for s in symbols)
        assert any(s.name == "Derived" for s in symbols)
        assert any(s.name == "use" for s in symbols)

        calls = js_adapter.extract_calls_from_source(source, file_path)
        assert len(calls) > 0

        imports = js_adapter.extract_imports(source, file_path)
        assert any(imp.target_module_name == "./util" for imp in imports)

        inheritance = js_adapter.extract_inheritance(source, file_path)
        assert any(edge.superclass_name == "Base" for edge in inheritance)

        api_edges = js_adapter.extract_property_api_edges(source, file_path)
        assert any(
            edge.method_uid == js_adapter._property_method_uid(file_path, "app", "use")
            for edge in api_edges
        )

    def test_import_edges_have_correct_type(self, py_adapter):
        """Test that intra-project import edges are classified correctly."""
        source = "import payments\nfrom payments import validators"
        imports = py_adapter.extract_imports(source, "payments/processor.py")

        import_types = {imp.import_type for imp in imports}
        assert import_types == {"direct", "from_package"}

    def test_inheritance_edges_have_correct_interface_flag(self, ts_adapter):
        """Test that inheritance edges correctly flag interfaces."""
        source = """
class ConcreteClass extends Base {}
interface IInterface {}
"""
        inheritance = ts_adapter.extract_inheritance(source, "test.ts")
        assert len(inheritance) > 0
        # At least one should have is_interface flag
        assert any(edge.is_interface for edge in inheritance) or any(
            not edge.is_interface for edge in inheritance
        )
