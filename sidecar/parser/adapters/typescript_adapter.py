"""TypeScript language adapter using tree-sitter."""

from sidecar.parser.adapters.treesitter_base import TreeSitterAdapter
from sidecar.parser.protocol import ImportEdge, InheritanceEdge
from sidecar.parser.uid import compute_uid, module_name_from_path


class TypeScriptAdapter(TreeSitterAdapter):
    """TypeScript parser adapter."""

    @property
    def language_name(self) -> str:
        return "typescript"

    @property
    def file_extensions(self) -> set[str]:
        return {".ts", ".tsx"}

    @property
    def ts_language_name(self) -> str:
        return "typescript"

    @property
    def symbol_query(self) -> str:
        return """
            (function_declaration name: (identifier) @func.name) @func.def
            (method_definition name: (property_identifier) @func.name) @func.def
            (class_declaration name: (type_identifier) @class.name) @class.def
            (program (lexical_declaration (variable_declarator name: (identifier) @var.name) @var.def))
        """

    @property
    def call_query(self) -> str:
        return """
            (call_expression function: (identifier) @call.name)
            (call_expression function: (member_expression property: (property_identifier) @call.name))
        """

    @property
    def parent_types(self) -> set[str]:
        return {"function_declaration", "method_definition", "class_declaration"}

    @property
    def import_query(self) -> str:
        return """
            (import_statement source: (string) @import.source) @import.stmt
            (import_specifier (identifier) @import.name) @import.spec
        """

    @property
    def inheritance_query(self) -> str:
        return ""

    def extract_imports(self, source_code: str, file_path: str) -> list[ImportEdge]:
        """Extract import statements from TypeScript source."""
        tree = self.parser.parse(bytes(source_code, "utf8"))
        query = self.language.query(self.import_query)
        captures = query.captures(tree.root_node)

        imports = []
        for node, tag in captures:
            if tag == "import.source":
                source = node.text.decode("utf-8").strip("\"'")
                import_type = "relative" if source.startswith(".") else "from_package"
                imports.append(ImportEdge(file_path, source, import_type))

        return imports

    def extract_inheritance(self, source_code: str, file_path: str) -> list[InheritanceEdge]:
        """Extract class inheritance and interface implementation from TypeScript source."""
        import re

        edges = []
        lines = source_code.split("\n")
        for line in lines:
            line = line.strip()
            if line.startswith("class "):
                extends_match = re.search(r"extends\s+(\w+)", line)
                implements_match = re.search(r"implements\s+([^{]+)", line)

                class_match = re.match(r"class\s+(\w+)", line)
                if class_match:
                    class_name = class_match.group(1)

                    if extends_match:
                        extends = extends_match.group(1)
                        subclass_uid = self._uid(file_path, class_name)
                        edges.append(InheritanceEdge(subclass_uid, extends, False))

                    if implements_match:
                        implements = implements_match.group(1)
                        for impl in implements.split(","):
                            impl = impl.strip()
                            if impl:
                                subclass_uid = self._uid(file_path, class_name)
                                edges.append(InheritanceEdge(subclass_uid, impl, True))

        return edges

    def _uid(self, file_path: str, name: str) -> str:
        qualified_name = f"{module_name_from_path(file_path)}.{name}"
        return compute_uid(qualified_name, f"{name}()->_", self.language_name)


def make_adapter() -> TypeScriptAdapter:
    """Factory function for adapter discovery."""
    return TypeScriptAdapter()
