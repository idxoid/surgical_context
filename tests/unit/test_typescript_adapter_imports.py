import pytest

from sidecar.parser.adapters.typescript_adapter import TypeScriptAdapter
from sidecar.parser.uid import project_root_scope


class TestTypeScriptImports:
    @pytest.fixture
    def adapter(self):
        return TypeScriptAdapter()

    def test_extract_named_imports(self, adapter):
        source = 'import { useState } from "react"'
        imports = adapter.extract_imports(source, "test.ts")
        assert any(imp.target_module_name == "react" for imp in imports)

    def test_extract_default_import(self, adapter):
        source = 'import express from "express"'
        imports = adapter.extract_imports(source, "test.ts")
        assert any(imp.target_module_name == "express" for imp in imports)

    def test_extract_relative_import(self, adapter):
        source = 'import { util } from "./utils"'
        imports = adapter.extract_imports(source, "test.ts")
        assert any(imp.import_type == "relative" for imp in imports)

    def test_extract_export_from_as_import_edge(self, adapter):
        source = 'export { createRenderer } from "./renderer"'
        imports = adapter.extract_imports(source, "test.ts")
        assert any(imp.target_module_name == "./renderer" for imp in imports)

    def test_import_binding_resolves_dotted_relative_basename(self, adapter, tmp_path):
        source_file = tmp_path / "decorators" / "module.decorator.ts"
        target_file = tmp_path / "utils" / "validate-module-keys.util.ts"
        source_file.parent.mkdir(parents=True)
        target_file.parent.mkdir(parents=True)
        source_file.write_text(
            """
import { validateModuleKeys } from '../utils/validate-module-keys.util';

export function Module(metadata: ModuleMetadata): ClassDecorator {
  validateModuleKeys(Object.keys(metadata));
}
""",
            encoding="utf-8",
        )
        target_file.write_text(
            "export function validateModuleKeys(keys: string[]) {}\n",
            encoding="utf-8",
        )

        with project_root_scope(str(tmp_path)):
            calls = adapter.extract_calls_from_source(
                source_file.read_text(encoding="utf-8"),
                str(source_file),
            )
        imported = next(call for call in calls if call["callee_name"] == "validateModuleKeys")

        assert imported["rel_type"] == "CALLS_IMPORTED"
        assert imported["callee_qualified_name"].endswith(
            "utils.validate-module-keys.util.validateModuleKeys"
        )

    def test_extract_class_inheritance(self, adapter):
        source = "class Child extends Parent {\n}"
        inheritance = adapter.extract_inheritance(source, "test.ts")
        assert len(inheritance) == 1
        assert inheritance[0].superclass_name == "Parent"

    def test_extract_interface_implementation(self, adapter):
        source = "class Child implements IParent {\n}"
        inheritance = adapter.extract_inheritance(source, "test.ts")
        assert len(inheritance) >= 1
        assert any(edge.superclass_name == "IParent" for edge in inheritance)

    def test_no_imports_returns_empty(self, adapter):
        source = "const x = 1"
        imports = adapter.extract_imports(source, "test.ts")
        assert len(imports) == 0

    def test_no_inheritance_returns_empty(self, adapter):
        source = "class Simple {}"
        inheritance = adapter.extract_inheritance(source, "test.ts")
        assert len(inheritance) == 0
