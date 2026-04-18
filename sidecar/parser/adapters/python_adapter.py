"""Python language adapter using tree-sitter."""

from sidecar.parser.adapters.treesitter_base import TreeSitterAdapter


class PythonAdapter(TreeSitterAdapter):
    """Python parser adapter."""

    @property
    def language_name(self) -> str:
        return "python"

    @property
    def file_extensions(self) -> set[str]:
        return {".py", ".pyi"}

    @property
    def ts_language_name(self) -> str:
        return "python"

    @property
    def symbol_query(self) -> str:
        return """
            (function_definition name: (identifier) @func.name) @func.def
            (class_definition name: (identifier) @class.name) @class.def
            (module (expression_statement (assignment left: (identifier) @var.name) @var.def))
        """

    @property
    def call_query(self) -> str:
        return "(call function: (identifier) @call.name) @call.occured"

    @property
    def parent_types(self) -> set[str]:
        return {"function_definition", "class_definition"}


def make_adapter() -> PythonAdapter:
    """Factory function for adapter discovery."""
    return PythonAdapter()
