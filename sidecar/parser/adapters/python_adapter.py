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
        return "(call) @call"

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

    def extract_calls_from_source(self, source_code: str, file_path: str) -> list[dict]:
        """Extract function calls with typed rel_type: CALLS_DIRECT, CALLS_DYNAMIC, CALLS_INFERRED."""
        tree = self.parser.parse(bytes(source_code, "utf8"))
        query = self.language.query(self.call_query)
        captures = query.captures(tree.root_node)

        calls = []

        for node, tag in captures:
            if tag == "call":
                # node is the call node; function child is the callable
                func_node = node.child_by_field_name("function")
                if not func_node:
                    continue

                # Find enclosing function or class
                parent = node.parent
                while parent and parent.type not in self.parent_types:
                    parent = parent.parent
                if not parent:
                    continue

                parent_name_node = parent.child_by_field_name("name")
                if not parent_name_node:
                    continue
                caller_name = source_code[parent_name_node.start_byte : parent_name_node.end_byte]

                # Determine call type based on function node type
                if func_node.type == "identifier":
                    # Direct identifier call: func()
                    call_name = source_code[func_node.start_byte : func_node.end_byte]
                    rel_type = self._classify_direct_call(call_name)
                elif func_node.type == "attribute":
                    # Attribute call: receiver.method()
                    # attribute node has children: identifier (receiver), ".", identifier (method)
                    children = [c for c in func_node.children if c.type == "identifier"]
                    if len(children) < 2:
                        continue
                    receiver_node = children[0]
                    method_node = children[-1]

                    receiver_text = source_code[receiver_node.start_byte : receiver_node.end_byte]
                    call_name = source_code[method_node.start_byte : method_node.end_byte]

                    if receiver_text == "self":
                        # self.method() — CALLS_DYNAMIC, check overridability
                        is_overrideable = not (
                            call_name.startswith("_") and call_name.endswith("_")
                        )
                        rel_type = "CALLS_DYNAMIC" if is_overrideable else "CALLS_DIRECT"
                    else:
                        # Other receiver (instance var, function result, etc.)
                        rel_type = "CALLS_DYNAMIC"
                else:
                    # Unsupported call pattern
                    continue

                calls.append(
                    {
                        "caller_uid": self._uid(file_path, caller_name),
                        "callee_name": call_name,
                        "rel_type": rel_type,
                    }
                )

        return calls

    def _classify_direct_call(self, call_name: str) -> str:
        """Classify a direct identifier call as DIRECT or INFERRED based on known patterns."""
        inferred_patterns = {
            "getattr",
            "setattr",
            "hasattr",
            "getattr_static",
            "operator.methodcaller",
            "methodcaller",
            "exec",
            "eval",
            "compile",
            "__import__",
            "importlib.import_module",
        }

        if call_name in inferred_patterns or call_name.startswith("globals()["):
            return "CALLS_INFERRED"

        if call_name in ("__init__", "__call__", "__getattr__", "__setattr__"):
            return "CALLS_DIRECT"

        return "CALLS_DIRECT"

    def _uid(self, file_path: str, name: str) -> str:
        return sha256(f"{file_path}:{name}".encode()).hexdigest()


def make_adapter() -> PythonAdapter:
    """Factory function for adapter discovery."""
    return PythonAdapter()
