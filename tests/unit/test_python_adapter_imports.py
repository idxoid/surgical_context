import pytest

from sidecar.parser.adapters.python_adapter import PythonAdapter


class TestPythonImports:
    @pytest.fixture
    def adapter(self):
        return PythonAdapter()

    def test_filters_stdlib_imports(self, adapter):
        source = "import os\nimport sys\nimport re"
        imports = adapter.extract_imports(source, "test.py")
        assert len(imports) == 0

    def test_filters_third_party_imports(self, adapter):
        source = "from pathlib import Path\nimport neo4j\nimport pandas"
        imports = adapter.extract_imports(source, "test.py")
        assert len(imports) == 0

    def test_keeps_internal_imports(self, adapter):
        source = (
            "from sidecar.database.neo4j_client import Neo4jClient\nimport sidecar.context.types"
        )
        imports = adapter.extract_imports(source, "test.py")
        assert len(imports) == 2
        names = {imp.target_module_name for imp in imports}
        assert "sidecar.database.neo4j_client" in names
        assert "sidecar.context.types" in names

    def test_keeps_repo_local_absolute_imports_even_if_package_name_is_common(self, adapter):
        source = "from pandas.io import read_csv\nimport pandas.errors"
        imports = adapter.extract_imports(source, "QA/repos/pandas/pandas/core/frame.py")

        assert len(imports) == 2
        names = {imp.target_module_name for imp in imports}
        assert "pandas.io" in names
        assert "pandas.errors" in names

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
