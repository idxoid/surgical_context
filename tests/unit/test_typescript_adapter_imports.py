import pytest

from sidecar.parser.adapters.typescript_adapter import TypeScriptAdapter


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
