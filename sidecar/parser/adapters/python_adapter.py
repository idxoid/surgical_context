"""Python language adapter using tree-sitter."""

from hashlib import sha256

from sidecar.parser.adapters.treesitter_base import TreeSitterAdapter
from sidecar.parser.protocol import ImportEdge, InheritanceEdge


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

    @property
    def import_query(self) -> str:
        return """
            (import_statement name: (dotted_name) @import.name)
            (import_statement name: (identifier) @import.name)
        """

    def extract_imports(self, source_code: str, file_path: str) -> list[ImportEdge]:
        """Extract import statements from Python source."""
        imports = []
        for line in source_code.split("\n"):
            line = line.strip()
            if line.startswith("import "):
                parts = line[7:].split(",")
                for part in parts:
                    module = part.strip()
                    if module:
                        imports.append(ImportEdge(file_path, module, "direct"))
            elif line.startswith("from "):
                match = line.split(" import ")
                if len(match) == 2:
                    module = match[0][5:].strip()
                    if module == ".":
                        imports.append(ImportEdge(file_path, ".", "relative"))
                    else:
                        imports.append(ImportEdge(file_path, module, "from_package"))
        return imports

    def extract_inheritance(self, source_code: str, file_path: str) -> list[InheritanceEdge]:
        """Extract class inheritance from Python source."""
        edges = []
        lines = source_code.split("\n")
        for _i, line in enumerate(lines):
            line = line.strip()
            if line.startswith("class "):
                match = line[6:].split(":")[0].strip()
                if "(" in match:
                    class_name = match.split("(")[0].strip()
                    bases_str = match.split("(")[1].rstrip(")")
                    for base in bases_str.split(","):
                        base_name = base.strip()
                        if base_name:
                            subclass_uid = self._uid(file_path, class_name)
                            edges.append(InheritanceEdge(subclass_uid, base_name, False))
        return edges

    def _uid(self, file_path: str, name: str) -> str:
        return sha256(f"{file_path}:{name}".encode()).hexdigest()


def make_adapter() -> PythonAdapter:
    """Factory function for adapter discovery."""
    return PythonAdapter()
