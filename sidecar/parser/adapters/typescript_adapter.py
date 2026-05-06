"""TypeScript language adapter using tree-sitter."""

import re

from tree_sitter import Query

from sidecar.parser.adapters.treesitter_base import TreeSitterAdapter
from sidecar.parser.protocol import ImportEdge, InheritanceEdge, SymbolMetadata
from sidecar.parser.uid import (
    compute_uid,
    module_name_from_path,
    normalize_signature,
    qualified_name_for,
    signature_from_node,
    signature_hash,
)


class TypeScriptAdapter(TreeSitterAdapter):
    """TypeScript parser adapter."""

    _EXPORTED_VAR_FALLBACK_RE = re.compile(
        r"(?m)^export\s+(?:const|let|var)\s+([A-Za-z_$][\w$]*)\b"
    )
    _EXPORTED_FUNC_FALLBACK_RE = re.compile(
        r"(?m)^export\s+(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\b"
    )

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
            (program (export_statement (lexical_declaration (variable_declarator name: (identifier) @var.name) @var.exported_def)))
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

    def extract_symbols(
        self, source_code: str, file_path: str, *, tree=None
    ) -> list[SymbolMetadata]:
        """Extract TS symbols with a fallback for exported lexical APIs.

        Tree-sitter can recover imperfectly on very type-heavy files and skip
        otherwise simple `export const foo = ...` declarations. We still want
        those public API surfaces indexed, so we add a conservative text
        fallback for top-level exported lexical declarations that were not
        surfaced by the AST query.
        """
        symbols = super().extract_symbols(source_code, file_path, tree=tree)
        existing_names = {symbol.name for symbol in symbols}

        for match in self._EXPORTED_FUNC_FALLBACK_RE.finditer(source_code):
            name = match.group(1)
            if name in existing_names:
                continue

            start_line, end_line, content = self._fallback_symbol_span(
                source_code,
                match.start(),
            )
            signature = normalize_signature(f"{name}()->_", self.language_name)
            qualified_name = f"{module_name_from_path(file_path)}.{name}"
            symbols.append(
                SymbolMetadata(
                    uid=compute_uid(qualified_name, signature, self.language_name),
                    name=name,
                    kind="function",
                    start_line=start_line,
                    end_line=end_line,
                    content_hash=self._hash(content),
                    file_path=file_path,
                    qualified_name=qualified_name,
                    signature=signature,
                    signature_hash=signature_hash(signature, self.language_name),
                    signature_status="fallback_export",
                    language=self.language_name,
                )
            )
            existing_names.add(name)

        for match in self._EXPORTED_VAR_FALLBACK_RE.finditer(source_code):
            name = match.group(1)
            if name in existing_names:
                continue

            start_line, end_line, content = self._fallback_symbol_span(
                source_code,
                match.start(),
            )
            signature = normalize_signature(f"{name}()->_", self.language_name)
            qualified_name = f"{module_name_from_path(file_path)}.{name}"
            symbols.append(
                SymbolMetadata(
                    uid=compute_uid(qualified_name, signature, self.language_name),
                    name=name,
                    kind="variable",
                    start_line=start_line,
                    end_line=end_line,
                    content_hash=self._hash(content),
                    file_path=file_path,
                    qualified_name=qualified_name,
                    signature=signature,
                    signature_hash=signature_hash(signature, self.language_name),
                    signature_status="fallback_export",
                    language=self.language_name,
                )
            )
            existing_names.add(name)
        return symbols

    def should_include_variable_symbol(
        self,
        node,
        tag: str,
        name: str,
        *,
        source_code: str,
        file_path: str,
    ) -> bool:
        """Treat exported lexical declarations as public API symbols.

        TypeScript libraries commonly publish their top-level API as
        ``export const foo = ...`` rather than ``function foo()``. Indexing
        those declarations makes retrieval work across TS codebases without
        hard-coding framework names like Redux Toolkit.
        """
        if super().should_include_variable_symbol(
            node, tag, name, source_code=source_code, file_path=file_path
        ):
            return True
        return tag == "var.exported_def"

    @property
    def inheritance_query(self) -> str:
        return ""

    def extract_imports(self, source_code: str, file_path: str, *, tree=None) -> list[ImportEdge]:
        """Extract import statements from TypeScript source."""
        if tree is None:
            tree = self._parse(source_code)
        query = Query(self.language, self.import_query)

        # Flatten captures from matches into (node, tag) tuples
        captures = []
        for match_id, captures_dict in query.matches(tree.root_node):
            for tag, nodes in captures_dict.items():
                for node in nodes:
                    captures.append((node, tag))

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
        query = Query(self.language, "(call_expression) @call")

        # Flatten captures from matches into (node, tag) tuples
        captures = []
        for match_id, captures_dict in query.matches(tree.root_node):
            for tag, nodes in captures_dict.items():
                for node in nodes:
                    captures.append((node, tag))

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

            parent = self._enclosing_symbol_owner(node)
            if not parent:
                continue

            caller_uid = self._caller_uid_for_owner(parent, source_code, file_path)
            if not caller_uid:
                continue
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

    def _fallback_symbol_span(
        self,
        source_code: str,
        start_offset: int,
    ) -> tuple[int, int, str]:
        """Best-effort line span for exported symbol text fallbacks.

        For simple `export const` wrappers we keep the single line. For exported
        functions, we try to capture the full brace-delimited body so prompt
        resolution can recover implementation context even when tree-sitter is
        in error-recovery mode.
        """
        line_start = source_code.rfind("\n", 0, start_offset) + 1
        start_line = source_code.count("\n", 0, line_start) + 1
        line_end = source_code.find("\n", start_offset)
        search_from = start_offset
        close_paren = source_code.find(")", start_offset)
        if close_paren != -1 and (line_end == -1 or close_paren <= line_end + 200):
            search_from = close_paren
        brace_start = source_code.find("{", search_from)

        if brace_start == -1 or (line_end != -1 and brace_start > line_end):
            if line_end == -1:
                line_end = len(source_code)
            content = source_code[line_start:line_end]
            return start_line, start_line, content

        depth = 0
        end_offset = len(source_code)
        for idx in range(brace_start, len(source_code)):
            char = source_code[idx]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    end_offset = idx + 1
                    break

        end_line = source_code.count("\n", 0, end_offset) + 1
        content = source_code[line_start:end_offset]
        return start_line, end_line, content

    def _uid(self, file_path: str, name: str) -> str:
        qualified_name = f"{module_name_from_path(file_path)}.{name}"
        return compute_uid(qualified_name, f"{name}()->_", self.language_name)

    def _uid_for_node(self, node, source_code: str, file_path: str) -> str:
        qualified_name = qualified_name_for(node, source_code, file_path)
        raw_signature, _ = signature_from_node(node, source_code, self.language_name)
        return compute_uid(qualified_name, raw_signature, self.language_name)

    def _enclosing_symbol_owner(self, node):
        parent = node.parent
        while parent:
            if parent.type in self.parent_types:
                return parent
            if parent.type == "variable_declarator" and self._is_top_level_variable_declarator(
                parent
            ):
                return parent
            parent = parent.parent
        return None

    def _caller_uid_for_owner(self, node, source_code: str, file_path: str) -> str | None:
        if node.type == "variable_declarator":
            name_node = node.child_by_field_name("name")
            if not name_node:
                return None
            name = source_code[name_node.start_byte : name_node.end_byte]
            return self._uid(file_path, name)
        return self._uid_for_node(node, source_code, file_path)

    @staticmethod
    def _is_top_level_variable_declarator(node) -> bool:
        parent = node.parent
        while parent:
            if parent.type == "program":
                return True
            if parent.type in {"function_declaration", "method_definition", "class_declaration"}:
                return False
            parent = parent.parent
        return False

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
