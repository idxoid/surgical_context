import pytest

from sidecar.parser.adapters.python_adapter import PythonAdapter


class TestPythonImports:
    @pytest.fixture
    def adapter(self):
        return PythonAdapter()

    def test_extract_direct_imports(self, adapter):
        source = "import os\nimport sys"
        imports = adapter.extract_imports(source, "test.py")
        assert len(imports) >= 2
        names = {imp.target_module_name for imp in imports}
        assert "os" in names or "sys" in names

    def test_extract_from_imports(self, adapter):
        source = "from pathlib import Path"
        imports = adapter.extract_imports(source, "test.py")
        assert len(imports) > 0
        assert any(imp.target_module_name == "pathlib" for imp in imports)

    def test_extract_relative_imports(self, adapter):
        source = "from . import utils"
        imports = adapter.extract_imports(source, "test.py")
        assert any(imp.import_type == "relative" for imp in imports)

    def test_extract_inheritance_single(self, adapter):
        source = "class Child(Parent):\n    pass"
        inheritance = adapter.extract_inheritance(source, "test.py")
        assert len(inheritance) == 1
        assert inheritance[0].superclass_name == "Parent"
        assert inheritance[0].is_interface is False

    def test_extract_multiple_inheritance(self, adapter):
        source = "class Child(Parent1, Parent2):\n    pass"
        inheritance = adapter.extract_inheritance(source, "test.py")
        assert len(inheritance) >= 1

    def test_no_imports_returns_empty(self, adapter):
        source = "x = 1\ny = 2"
        imports = adapter.extract_imports(source, "test.py")
        assert len(imports) == 0

    def test_no_inheritance_returns_empty(self, adapter):
        source = "class Simple:\n    pass"
        inheritance = adapter.extract_inheritance(source, "test.py")
        assert len(inheritance) == 0
