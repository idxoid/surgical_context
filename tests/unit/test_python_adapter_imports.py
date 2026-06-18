import pytest

from context_engine.parser.adapters.python_adapter import PythonAdapter
from context_engine.parser.uid import project_root_scope


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
            "from context_engine.database.neo4j_client import Neo4jClient\nimport context_engine.context_types"
        )
        imports = adapter.extract_imports(source, "test.py")
        assert len(imports) == 2
        names = {imp.target_module_name for imp in imports}
        assert "context_engine.database.neo4j_client" in names
        assert "context_engine.context_types" in names

    def test_keeps_repo_local_absolute_imports_even_if_package_name_is_common(self, adapter):
        source = "from pandas.io import read_csv\nimport pandas.errors"
        imports = adapter.extract_imports(source, "QA/repos/pandas/pandas/core/frame.py")

        assert len(imports) == 2
        names = {imp.target_module_name for imp in imports}
        assert "pandas.io" in names
        assert "pandas.errors" in names

    def test_keeps_workspace_package_that_is_also_installed_dependency(self, adapter, tmp_path):
        package_dir = tmp_path / "src" / "fastapi"
        package_dir.mkdir(parents=True)
        (package_dir / "__init__.py").write_text("", encoding="utf-8")
        file_path = str(package_dir / "params.py")
        source = "from fastapi.dependencies.utils import solve_dependencies"

        with project_root_scope(str(tmp_path)):
            imports = adapter.extract_imports(source, file_path)

        assert [imp.target_module_name for imp in imports] == ["fastapi.dependencies.utils"]

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

    def test_extract_multiline_generic_inheritance_heads(self, adapter):
        source = """
class Child(
    Parent[T],
    pkg.Mixin,
):
    pass
"""
        inheritance = adapter.extract_inheritance(source, "test.py")
        assert [edge.superclass_name for edge in inheritance] == ["Parent", "Mixin"]

    def test_no_imports_returns_empty(self, adapter):
        source = "x = 1\ny = 2"
        imports = adapter.extract_imports(source, "test.py")
        assert len(imports) == 0

    def test_no_inheritance_returns_empty(self, adapter):
        source = "class Simple:\n    pass"
        inheritance = adapter.extract_inheritance(source, "test.py")
        assert len(inheritance) == 0
