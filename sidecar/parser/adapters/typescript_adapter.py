"""TypeScript language adapter using tree-sitter."""

from sidecar.parser.adapters.treesitter_base import TreeSitterAdapter


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
        return "(call_expression function: (identifier) @call.name)"

    @property
    def parent_types(self) -> set[str]:
        return {"function_declaration", "method_definition", "class_declaration"}


def make_adapter() -> TypeScriptAdapter:
    """Factory function for adapter discovery."""
    return TypeScriptAdapter()
