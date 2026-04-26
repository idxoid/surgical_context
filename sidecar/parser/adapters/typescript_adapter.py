"""TypeScript language adapter using tree-sitter."""

from sidecar.parser.adapters.treesitter_base import TreeSitterAdapter
from sidecar.parser.protocol import ImportEdge, InheritanceEdge
from sidecar.parser.uid import (
    compute_uid,
    module_name_from_path,
    qualified_name_for,
    signature_from_node,
)


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

    def extract_imports(
        self, source_code: str, file_path: str, *, tree=None
    ) -> list[ImportEdge]:
        """Extract import statements from TypeScript source."""
        if tree is None:
            tree = self._parse(source_code)
        query = self.language.query(self.import_query)
        captures = query.captures(tree.root_node)

        imports = []
        for node, tag in captures:
            if tag == "import.source":
                source = node.text.decode("utf-8").strip("\"'")
                import_type = "relative" if source.startswith(".") else "from_package"
                imports.append(ImportEdge(file_path, source, import_type))

        return imports

    def extract_inheritance(
        self, source_code: str, file_path: str, *, tree=None
    ) -> list[InheritanceEdge]:
        """Extract class inheritance and interface implementation from TypeScript source.

        Line-based regex; ``tree`` is accepted for ``extract_all`` parity.
        """
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

    def extract_calls_from_source(
        self, source_code: str, file_path: str, *, tree=None
    ) -> list[dict]:
        """Extract TypeScript calls with direct vs dynamic dispatch classification."""
        if tree is None:
            tree = self._parse(source_code)
        query = self.language.query("(call_expression) @call")
        captures = query.captures(tree.root_node)

        symbols = self.extract_symbols(source_code, file_path, tree=tree)
        by_name: dict[str, list] = {}
        for symbol in symbols:
            by_name.setdefault(symbol.name, []).append(symbol)

        calls = []
        for node, tag in captures:
            if tag != "call":
                continue

            func_node = node.child_by_field_name("function")
            if not func_node:
                continue

            parent = node.parent
            while parent and parent.type not in self.parent_types:
                parent = parent.parent
            if not parent:
                continue

            caller_uid = self._uid_for_node(parent, source_code, file_path)
            callee_uid = None
            call_name = ""
            rel_type = "CALLS_DIRECT"
            tier = "direct"
            confidence = 1.0

            if func_node.type == "identifier":
                call_name = source_code[func_node.start_byte : func_node.end_byte]
            elif func_node.type == "member_expression":
                named_children = [child for child in func_node.children if child.is_named]
                if len(named_children) < 2:
                    continue
                receiver_node = named_children[0]
                method_node = named_children[-1]
                receiver_text = source_code[receiver_node.start_byte : receiver_node.end_byte]
                call_name = source_code[method_node.start_byte : method_node.end_byte]
                rel_type = "CALLS_DYNAMIC"
                tier = "dynamic"
                confidence = 0.7
                if receiver_text == "this":
                    callee_uid = self._resolve_method_uid(parent, call_name, by_name)
            else:
                continue

            if callee_uid == caller_uid:
                continue

            call = {
                "caller_uid": caller_uid,
                "callee_name": call_name,
                "rel_type": rel_type,
                "tier": tier,
                "confidence": confidence,
                "resolver": "ts-scope-v1",
                "call_site_line": node.start_point[0] + 1,
            }
            if callee_uid:
                call["callee_uid"] = callee_uid
            calls.append(call)

        return calls

    def _uid(self, file_path: str, name: str) -> str:
        qualified_name = f"{module_name_from_path(file_path)}.{name}"
        return compute_uid(qualified_name, f"{name}()->_", self.language_name)

    def _uid_for_node(self, node, source_code: str, file_path: str) -> str:
        qualified_name = qualified_name_for(node, source_code, file_path)
        raw_signature, _ = signature_from_node(node, source_code, self.language_name)
        return compute_uid(qualified_name, raw_signature, self.language_name)

    def _resolve_method_uid(
        self, caller_node, method_name: str, by_name: dict[str, list]
    ) -> str | None:
        candidates = by_name.get(method_name, [])
        if not candidates:
            return None

        class_node = caller_node
        while class_node and class_node.type != "class_declaration":
            class_node = class_node.parent
        if not class_node:
            return str(candidates[0].uid) if len(candidates) == 1 else None

        class_name_node = class_node.child_by_field_name("name")
        if not class_name_node:
            return None
        class_name = class_name_node.text.decode("utf-8")
        for candidate in candidates:
            if f".{class_name}.{method_name}" in candidate.qualified_name:
                return str(candidate.uid)
        return str(candidates[0].uid) if len(candidates) == 1 else None


def make_adapter() -> TypeScriptAdapter:
    """Factory function for adapter discovery."""
    return TypeScriptAdapter()
