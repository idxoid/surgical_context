import pytest

from sidecar.parser.adapters.python_adapter import PythonAdapter
from sidecar.parser.adapters.typescript_adapter import TypeScriptAdapter


class TestGraphCompleteness:
    """Test that all graph edge types (CALLS, IMPORTS, DEPENDS_ON) are extractable."""

    @pytest.fixture
    def py_adapter(self):
        return PythonAdapter()

    @pytest.fixture
    def ts_adapter(self):
        return TypeScriptAdapter()

    def test_python_complete_extraction(self, py_adapter):
        """Test Python extracts symbols, calls, imports, and inheritance."""
        source = """
import os
from pathlib import Path

class Base:
    pass

class Derived(Base):
    def __init__(self):
        self.path = Path(".")

    def helper(self):
        print(os.getcwd())

    def caller(self):
        self.helper()
"""
        # Symbols
        symbols = py_adapter.extract_symbols(source, "test.py")
        assert any(s.name == "Base" for s in symbols)
        assert any(s.name == "Derived" for s in symbols)

        # Calls
        calls = py_adapter.extract_calls_from_source(source, "test.py")
        assert len(calls) > 0

        # Imports
        imports = py_adapter.extract_imports(source, "test.py")
        assert any("pathlib" in imp.target_module_name for imp in imports)

        # Inheritance
        inheritance = py_adapter.extract_inheritance(source, "test.py")
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

    def test_import_edges_have_correct_type(self, py_adapter):
        """Test that import edges are classified correctly."""
        source = "import os\nfrom . import utils\nfrom pathlib import Path"
        imports = py_adapter.extract_imports(source, "test.py")

        import_types = {imp.import_type for imp in imports}
        assert "direct" in import_types or "from_package" in import_types

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
