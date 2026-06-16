"""JavaScript language adapter using tree-sitter."""

import re
from pathlib import Path

from tree_sitter import Query

from sidecar.parser.adapters.treesitter_base import TreeSitterAdapter
from sidecar.parser.protocol import ClassApiEdge, ImportEdge, InheritanceEdge, SymbolMetadata
from sidecar.parser.uid import (
    compute_uid,
    module_name_from_path,
    normalize_signature,
    qualified_name_for,
    signature_from_node,
    signature_hash,
)


class JavaScriptAdapter(TreeSitterAdapter):
    """JavaScript parser adapter."""

    _PUBLIC_VARIABLE_VALUE_TYPES = frozenset(
        {
            "call_expression",
            "new_expression",
            "await_expression",
            "arrow_function",
            "function_expression",
            "member_expression",
            "object",
            "array",
        }
    )

    _EXPORTED_VAR_FALLBACK_RE = re.compile(
        r"(?m)^exports\.([A-Za-z_$][\w$]*)\s*=|^export\s+(?:const|let|var)\s+([A-Za-z_$][\w$]*)\b"
    )
    _EXPORTED_FUNC_FALLBACK_RE = re.compile(
        r"(?m)^exports\.([A-Za-z_$][\w$]*)\s*=\s*(?:async\s+)?function|^export\s+(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\b"
    )
    _MODULE_EXPORT_FUNC_FALLBACK_RE = re.compile(
        r"(?m)^module\.exports\s*=\s*(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\b"
    )
    _PROPERTY_FUNC_FALLBACK_RE = re.compile(
        r"(?m)^[ \t]*[A-Za-z_$][\w$]*\.([A-Za-z_$][\w$]*)\s*=\s*(?:async\s+)?function(?:\s+([A-Za-z_$][\w$]*))?\b"
    )
    _PROPERTY_ARROW_FALLBACK_RE = re.compile(
        r"(?m)^[ \t]*[A-Za-z_$][\w$]*\.([A-Za-z_$][\w$]*)\s*=\s*(?:async\s+)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>"
    )
    _PROPERTY_FUNC_API_RE = re.compile(
        r"(?m)^[ \t]*([A-Za-z_$][\w$]*)\.([A-Za-z_$][\w$]*)\s*=\s*(?:async\s+)?function(?:\s+([A-Za-z_$][\w$]*))?\b"
    )
    _PROPERTY_ARROW_API_RE = re.compile(
        r"(?m)^[ \t]*([A-Za-z_$][\w$]*)\.([A-Za-z_$][\w$]*)\s*=\s*(?:async\s+)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>"
    )
    _CHAINED_PROPERTY_FUNC_API_RE = re.compile(
        r"(?m)^[ \t]*([A-Za-z_$][\w$]*)\.([A-Za-z_$][\w$]*)\s*=\s*([A-Za-z_$][\w$]*)\.([A-Za-z_$][\w$]*)\s*=\s*(?:async\s+)?function(?:\s+([A-Za-z_$][\w$]*))?\b"
    )
    _COMMONJS_REQUIRE_DEFAULT_RE = re.compile(
        r"(?m)^[ \t]*(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*require\(\s*['\"]([^'\"]+)['\"]\s*\)"
    )
    _COMMONJS_EXPORT_ALIAS_RE = re.compile(
        r"(?m)^[ \t]*(?:module\.)?exports\.([A-Za-z_$][\w$]*)\s*=\s*([^;\n]+)"
    )
    _IDENTIFIER_TEXT_RE = re.compile(r"^[A-Za-z_$][\w$]*$")
    _MEMBER_TEXT_RE = re.compile(r"^([A-Za-z_$][\w$]*)\.([A-Za-z_$][\w$]*)$")

    @property
    def language_name(self) -> str:
        return "javascript"

    @property
    def file_extensions(self) -> set[str]:
        return {".js", ".jsx"}

    @property
    def ts_language_name(self) -> str:
        return "typescript"

    @property
    def symbol_query(self) -> str:
        return """
            (function_declaration (identifier) @func.name) @func.def
            (class_declaration (type_identifier) @class.name) @class.def
            (program (lexical_declaration (variable_declarator name: (identifier) @var.name) @var.top_level))
            (program (variable_declaration (variable_declarator name: (identifier) @var.name) @var.top_level))
        """

    @property
    def call_query(self) -> str:
        return """
            (call_expression function: (identifier) @call.name)
            (call_expression function: (member_expression property: (property_identifier) @call.name))
        """

    @property
    def parent_types(self) -> set[str]:
        return {
            "function_declaration",
            "function_expression",
            "arrow_function",
            "method_definition",
            "class_declaration",
        }

    @property
    def import_query(self) -> str:
        return """
            (import_statement source: (string) @import.source) @import.stmt
            (export_statement source: (string) @import.source) @import.stmt
            (import_specifier (identifier) @import.name) @import.spec
            (call_expression function: (identifier) @require.call) @require.expr
        """

    def extract_symbols(
        self, source_code: str, file_path: str, *, tree=None
    ) -> list[SymbolMetadata]:
        """Extract JS symbols with a fallback for exported lexical APIs and module.exports."""
        symbols = super().extract_symbols(source_code, file_path, tree=tree)
        existing_names = {symbol.name for symbol in symbols}
        existing_uids = {symbol.uid for symbol in symbols}

        for match in self._EXPORTED_FUNC_FALLBACK_RE.finditer(source_code):
            name = match.group(1) or match.group(2)
            if not name or name in existing_names:
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
            existing_uids.add(symbols[-1].uid)

        for match in self._MODULE_EXPORT_FUNC_FALLBACK_RE.finditer(source_code):
            name = match.group(1)
            if not name or name in existing_names:
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
            existing_uids.add(symbols[-1].uid)
        for match in self._CHAINED_PROPERTY_FUNC_API_RE.finditer(source_code):
            owner = (match.group(1) or "").strip()
            name = (match.group(2) or "").strip()
            qualified_name = self._property_method_qualified_name(file_path, owner, name)
            uid = compute_uid(qualified_name, f"{name}()->_", self.language_name)
            if not name or not owner or uid in existing_uids:
                continue
            start_line, end_line, content = self._fallback_symbol_span(source_code, match.start())
            signature = normalize_signature(f"{name}()->_", self.language_name)
            symbols.append(
                SymbolMetadata(
                    uid=uid,
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
            existing_uids.add(uid)
        for match in self._PROPERTY_FUNC_API_RE.finditer(source_code):
            owner = (match.group(1) or "").strip()
            name = (match.group(3) or match.group(2) or "").strip()
            qualified_name = self._property_method_qualified_name(file_path, owner, name)
            uid = compute_uid(qualified_name, f"{name}()->_", self.language_name)
            if not name or not owner or uid in existing_uids:
                continue
            start_line, end_line, content = self._fallback_symbol_span(source_code, match.start())
            signature = normalize_signature(f"{name}()->_", self.language_name)
            symbols.append(
                SymbolMetadata(
                    uid=uid,
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
            existing_uids.add(uid)
        for match in self._PROPERTY_ARROW_API_RE.finditer(source_code):
            owner = (match.group(1) or "").strip()
            name = (match.group(2) or "").strip()
            qualified_name = self._property_method_qualified_name(file_path, owner, name)
            uid = compute_uid(qualified_name, f"{name}()->_", self.language_name)
            if not name or not owner or uid in existing_uids:
                continue
            start_line, end_line, content = self._fallback_symbol_span(source_code, match.start())
            signature = normalize_signature(f"{name}()->_", self.language_name)
            symbols.append(
                SymbolMetadata(
                    uid=uid,
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
            existing_uids.add(uid)

        for match in self._EXPORTED_VAR_FALLBACK_RE.finditer(source_code):
            name = match.group(1) or match.group(2)
            if not name or name in existing_names:
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
            existing_uids.add(symbols[-1].uid)
        for name, start_offset in self._module_export_object_keys(source_code):
            if not name or name in existing_names:
                continue
            start_line, end_line, content = self._fallback_symbol_span(source_code, start_offset)
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
            existing_uids.add(symbols[-1].uid)
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
        """Treat exported lexical declarations as public API symbols."""
        if super().should_include_variable_symbol(
            node, tag, name, source_code=source_code, file_path=file_path
        ):
            return True
        if tag == "var.exported_def":
            return True
        if tag == "var.top_level":
            value = node.child_by_field_name("value")
            if value is None:
                return False
            return self._is_public_variable_value(value)
        return False

    def _is_public_variable_value(self, node) -> bool:
        if node is None:
            return False
        if node.type in self._PUBLIC_VARIABLE_VALUE_TYPES:
            return True
        if node.type == "assignment_expression":
            named_children = [child for child in node.children if child.is_named]
            if len(named_children) < 2:
                return False
            return self._is_public_variable_value(named_children[-1])
        return False

    @property
    def inheritance_query(self) -> str:
        return ""

    def extract_imports(self, source_code: str, file_path: str, *, tree=None) -> list[ImportEdge]:
        """Extract import and require statements from JavaScript source."""
        if tree is None:
            tree = self._parse(source_code)
        query = Query(self.language, self.import_query)

        # Flatten captures from matches into (node, tag) tuples
        captures = []
        for _match_id, captures_dict in query.matches(tree.root_node):
            for tag, nodes in captures_dict.items():
                for node in nodes:
                    captures.append((node, tag))

        imports = []
        seen: set[tuple[str, str, str]] = set()

        def add_import(source: str) -> None:
            import_type = "relative" if source.startswith(".") else "from_package"
            key = (file_path, source, import_type)
            if key in seen:
                return
            seen.add(key)
            imports.append(ImportEdge(file_path, source, import_type))

        for node, tag in captures:
            if tag == "import.source":
                source = (node.text or b"").decode("utf-8").strip("\"'")
                add_import(source)
            elif tag == "require.call":
                call_text = (node.text or b"").decode("utf-8")
                if "require(" in call_text:
                    match = re.search(r"require\(['\"]([^'\"]+)['\"]\)", call_text)
                    if match:
                        add_import(match.group(1))

        for match in re.finditer(r"require\(\s*['\"]([^'\"]+)['\"]\s*\)", source_code):
            add_import(match.group(1))

        return imports

    def extract_property_api_edges(
        self, source_code: str, file_path: str, *, tree=None
    ) -> list[ClassApiEdge]:
        """Extract owner-symbol API edges from static property function assignments."""
        edges: list[ClassApiEdge] = []
        seen: set[tuple[str, str]] = set()

        def add(owner: str, prop: str, method_name: str) -> None:
            owner = owner.strip()
            method_name = (method_name or prop).strip()
            if not owner or not method_name or owner in {"exports", "module"}:
                return
            key = (owner, method_name)
            if key in seen:
                return
            seen.add(key)
            edges.append(
                ClassApiEdge(
                    class_uid=self._uid(file_path, owner),
                    method_uid=self._property_method_uid(file_path, owner, method_name),
                    edge_type="HAS_API",
                )
            )

        for match in self._CHAINED_PROPERTY_FUNC_API_RE.finditer(source_code):
            add(match.group(1), match.group(2), match.group(2))
            add(match.group(3), match.group(4), match.group(5) or match.group(4))
        for match in self._PROPERTY_FUNC_API_RE.finditer(source_code):
            add(match.group(1), match.group(2), match.group(3) or match.group(2))
        for match in self._PROPERTY_ARROW_API_RE.finditer(source_code):
            add(match.group(1), match.group(2), match.group(2))
        return edges

    def extract_symbol_aliases(self, source_code: str, file_path: str, *, tree=None) -> list[dict]:
        """Extract static symbol-level aliases from CommonJS export surfaces."""
        module_name = module_name_from_path(file_path)
        import_bindings = self._extract_import_bindings(source_code, file_path)
        aliases: list[dict] = []
        seen: set[tuple[str, str, str, str]] = set()

        def add_alias(
            source_name: str,
            target_name: str,
            target_qualified_name: str,
            *,
            kind: str,
            start_offset: int,
            match_by_name: bool,
            confidence: float,
        ) -> None:
            source_name = source_name.strip()
            target_name = target_name.strip()
            target_qualified_name = target_qualified_name.strip()
            if not source_name or not target_name:
                return
            key = (source_name, target_name, target_qualified_name, kind)
            if key in seen:
                return
            seen.add(key)
            aliases.append(
                {
                    "source_uid": self._uid(file_path, source_name),
                    "source_name": source_name,
                    "target_name": target_name,
                    "target_qualified_name": target_qualified_name,
                    "file_path": file_path,
                    "kind": kind,
                    "confidence": confidence,
                    "line": source_code.count("\n", 0, start_offset) + 1,
                    "match_by_name": match_by_name,
                }
            )

        for match in self._COMMONJS_REQUIRE_DEFAULT_RE.finditer(source_code):
            alias = match.group(1).strip()
            source = self._normalize_import_source(file_path, match.group(2).strip())
            if not alias or not source:
                continue
            add_alias(
                alias,
                alias,
                f"{source}.{alias}",
                kind="commonjs_require_default",
                start_offset=match.start(),
                match_by_name=False,
                confidence=0.8,
            )

        for match in self._COMMONJS_EXPORT_ALIAS_RE.finditer(source_code):
            export_name = match.group(1).strip()
            rhs = self._strip_commonjs_alias_rhs(match.group(2))
            target_name, target_qualified_name = self._commonjs_alias_target(
                rhs,
                module_name,
                import_bindings,
            )
            if not target_name:
                continue
            add_alias(
                export_name,
                target_name,
                target_qualified_name,
                kind="commonjs_export_alias",
                start_offset=match.start(),
                match_by_name=export_name != target_name,
                confidence=0.85,
            )

        for name, start_offset in self._module_export_object_keys(source_code):
            add_alias(
                name,
                name,
                f"{module_name}.{name}",
                kind="commonjs_export_object",
                start_offset=start_offset,
                match_by_name=False,
                confidence=0.75,
            )

        return aliases

    def extract_inheritance(
        self, source_code: str, file_path: str, *, tree=None
    ) -> list[InheritanceEdge]:
        """Extract class inheritance from JavaScript source.

        Line-based regex; ``tree`` is accepted for ``extract_all`` parity.
        """
        edges = []
        lines = source_code.split("\n")
        for line in lines:
            line = line.strip()
            if line.startswith("class "):
                extends_match = re.search(r"extends\s+(\w+)", line)
                class_match = re.match(r"class\s+(\w+)", line)
                if class_match and extends_match:
                    class_name = class_match.group(1)
                    extends = extends_match.group(1)
                    subclass_uid = self._uid(file_path, class_name)
                    edges.append(InheritanceEdge(subclass_uid, extends, False))

        return edges

    def extract_calls_from_source(
        self, source_code: str, file_path: str, *, tree=None
    ) -> list[dict]:
        """Extract JavaScript calls with direct vs dynamic dispatch classification."""
        if tree is None:
            tree = self._parse(source_code)
        query = Query(
            self.language,
            """
            (call_expression) @call
            (new_expression) @call
            """,
        )

        # Flatten captures from matches into (node, tag) tuples
        captures = []
        for _match_id, captures_dict in query.matches(tree.root_node):
            for tag, nodes in captures_dict.items():
                for node in nodes:
                    captures.append((node, tag))

        symbols = self.extract_symbols(source_code, file_path, tree=tree)
        by_name: dict[str, list] = {}
        for symbol in symbols:
            by_name.setdefault(symbol.name, []).append(symbol)
        symbol_uids = {str(symbol.uid) for symbol in symbols}

        import_bindings = self._extract_import_bindings(source_code, file_path)
        calls = []
        for node, tag in captures:
            if tag != "call":
                continue

            func_node = node.child_by_field_name("function")
            if func_node is None and node.type == "new_expression":
                func_node = node.child_by_field_name("constructor")
            if not func_node:
                continue

            parent = self._enclosing_symbol_owner(node)
            if not parent:
                continue

            caller_uid = self._caller_uid_for_indexed_owner(
                parent,
                symbol_uids,
                source_code,
                file_path,
            )
            if not caller_uid:
                continue
            callee_uid = None
            call_name = ""
            rel_type = "CALLS_DIRECT"
            tier = "direct"
            confidence = 1.0
            call_kind = "construct" if node.type == "new_expression" else "call"

            if func_node.type == "identifier":
                call_name = source_code[func_node.start_byte : func_node.end_byte]
                if call_name in import_bindings:
                    rel_type = "CALLS_IMPORTED"
                    tier = "imported"
                    confidence = 0.9
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
                    callee_uid = self._resolve_method_uid(
                        parent,
                        call_name,
                        by_name,
                        source_code=source_code,
                    )
                elif receiver_text in import_bindings:
                    rel_type = "CALLS_IMPORTED"
                    tier = "imported"
                    confidence = 0.9
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
                "resolver": "js-scope-v1",
                "call_site_line": node.start_point[0] + 1,
                "call_kind": call_kind,
            }
            if callee_uid:
                call["callee_uid"] = callee_uid
            if rel_type == "CALLS_IMPORTED":
                if func_node.type == "identifier":
                    call["callee_qualified_name"] = import_bindings[call_name]
                else:
                    receiver_node = [child for child in func_node.children if child.is_named][0]
                    receiver_text = source_code[receiver_node.start_byte : receiver_node.end_byte]
                    base = import_bindings.get(receiver_text, "")
                    if base:
                        call["callee_qualified_name"] = f"{base}.{call_name}"
            calls.append(call)

        return calls

    def _caller_uid_for_indexed_owner(
        self,
        node,
        symbol_uids: set[str],
        source_code: str,
        file_path: str,
    ) -> str | None:
        """Return the nearest enclosing owner that is materialized as a Symbol.

        JavaScript often hides meaningful work inside inline callbacks/getters
        that are not indexed as first-class symbols. Preserve the call fact by
        assigning it to the nearest indexed containing API/function symbol.
        """
        current = node
        while current is not None:
            uid = self._caller_uid_for_owner(current, source_code, file_path)
            if uid and uid in symbol_uids:
                return uid
            current = self._enclosing_symbol_owner(current)
        return None

    def _extract_import_bindings(self, source_code: str, file_path: str) -> dict[str, str]:
        bindings: dict[str, str] = {}
        for match in re.finditer(
            r"import\s+([^;]+?)\s+from\s+['\"]([^'\"]+)['\"]",
            source_code,
        ):
            spec = match.group(1).strip()
            source = self._normalize_import_source(file_path, match.group(2).strip())
            if not spec or not source:
                continue
            if spec.startswith("{") and spec.endswith("}"):
                self._parse_named_import_bindings(spec[1:-1], source, bindings)
            elif spec.startswith("* as "):
                alias = spec[len("* as ") :].strip()
                if alias:
                    bindings[alias] = source
            elif "," in spec:
                default_alias, rest = spec.split(",", 1)
                default_alias = default_alias.strip()
                if default_alias:
                    bindings[default_alias] = source
                rest = rest.strip()
                if rest.startswith("{") and rest.endswith("}"):
                    self._parse_named_import_bindings(rest[1:-1], source, bindings)
            else:
                bindings[spec] = source
        for match in re.finditer(
            r"const\s+\{\s*([^}]+)\s*\}\s*=\s*require\(\s*['\"]([^'\"]+)['\"]\s*\)",
            source_code,
        ):
            source = self._normalize_import_source(file_path, match.group(2).strip())
            self._parse_named_import_bindings(match.group(1), source, bindings)
        for match in re.finditer(
            r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*require\(\s*['\"]([^'\"]+)['\"]\s*\)",
            source_code,
        ):
            alias = match.group(1).strip()
            source = self._normalize_import_source(file_path, match.group(2).strip())
            if alias and source:
                bindings[alias] = source
        return bindings

    @staticmethod
    def _parse_named_import_bindings(spec: str, source: str, out: dict[str, str]) -> None:
        for part in spec.split(","):
            token = part.strip()
            if not token:
                continue
            if " as " in token:
                imported, alias = token.split(" as ", 1)
                imported = imported.strip()
                alias = alias.strip()
            elif ":" in token:
                imported, alias = token.split(":", 1)
                imported = imported.strip()
                alias = alias.strip()
            else:
                imported = token
                alias = token
            if alias and imported:
                out[alias] = f"{source}.{imported}"

    def _normalize_import_source(self, file_path: str, source: str) -> str:
        if not source:
            return ""
        if not source.startswith("."):
            return source.replace("/", ".")
        base = Path(file_path).parent
        resolved = (base / source).resolve()
        candidates = [
            resolved.with_suffix(".js"),
            resolved.with_suffix(".jsx"),
            resolved / "index.js",
        ]
        for candidate in candidates:
            if candidate.exists():
                return module_name_from_path(str(candidate))
        return source.lstrip("./").replace("/", ".")

    @classmethod
    def _strip_commonjs_alias_rhs(cls, rhs: str) -> str:
        rhs = rhs.strip()
        rhs = re.sub(r"//.*$", "", rhs).strip()
        if rhs.startswith("(") and rhs.endswith(")"):
            rhs = rhs[1:-1].strip()
        return rhs

    @classmethod
    def _commonjs_alias_target(
        cls,
        rhs: str,
        module_name: str,
        import_bindings: dict[str, str],
    ) -> tuple[str, str]:
        if not rhs or rhs.startswith("require("):
            return "", ""
        identifier = cls._IDENTIFIER_TEXT_RE.match(rhs)
        if identifier:
            target_name = identifier.group(0)
            imported = import_bindings.get(target_name, "")
            if imported:
                return target_name, f"{imported}.{target_name}"
            return target_name, f"{module_name}.{target_name}"

        member = cls._MEMBER_TEXT_RE.match(rhs)
        if member:
            receiver, prop = member.groups()
            imported = import_bindings.get(receiver, "")
            if imported:
                return prop, f"{imported}.{prop}"
            return prop, f"{module_name}.{prop}"

        return "", ""

    @staticmethod
    def _module_export_object_keys(source_code: str) -> list[tuple[str, int]]:
        out: list[tuple[str, int]] = []
        match = re.search(r"module\.exports\s*=\s*\{", source_code)
        if not match:
            return out
        start = match.end()
        depth = 1
        idx = start
        while idx < len(source_code) and depth > 0:
            char = source_code[idx]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
            idx += 1
        if depth != 0:
            return out
        body = source_code[start : idx - 1]
        for prop in re.finditer(r"([A-Za-z_$][\w$]*)\s*(?::\s*[A-Za-z_$][\w$]*)?\s*(?:,|$)", body):
            name = prop.group(1)
            out.append((name, start + prop.start()))
        return out

    def _fallback_symbol_span(
        self,
        source_code: str,
        start_offset: int,
    ) -> tuple[int, int, str]:
        """Best-effort line span for exported symbol text fallbacks."""
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

    def _property_method_qualified_name(self, file_path: str, owner: str, name: str) -> str:
        return f"{module_name_from_path(file_path)}.{owner}.{name}"

    def _property_method_uid(self, file_path: str, owner: str, name: str) -> str:
        qualified_name = self._property_method_qualified_name(file_path, owner, name)
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
        if node.type in {"function_expression", "arrow_function"}:
            assignment = self._property_assignment_for_value_node(node, source_code)
            if assignment:
                owner, method_name = assignment
                return self._property_method_uid(file_path, owner, method_name)
        return self._uid_for_node(node, source_code, file_path)

    def _property_assignment_for_value_node(self, node, source_code: str) -> tuple[str, str] | None:
        parent = node.parent
        while parent:
            if parent.type == "assignment_expression":
                named_children = [child for child in parent.children if child.is_named]
                rhs = named_children[-1] if named_children else None
                if (
                    len(named_children) >= 2
                    and rhs is not None
                    and rhs.start_byte == node.start_byte
                    and rhs.end_byte == node.end_byte
                    and rhs.type == node.type
                ):
                    lhs = named_children[0]
                    member = self._member_expression_parts(lhs, source_code)
                    if member:
                        owner, prop = member
                        name_node = node.child_by_field_name("name")
                        method_name = prop
                        if name_node is not None:
                            method_name = source_code[name_node.start_byte : name_node.end_byte]
                        return owner, method_name
            if parent.type in {"function_declaration", "class_declaration", "method_definition"}:
                return None
            parent = parent.parent
        return None

    @staticmethod
    def _member_expression_parts(node, source_code: str) -> tuple[str, str] | None:
        if node is None or node.type != "member_expression":
            return None
        named_children = [child for child in node.children if child.is_named]
        if len(named_children) < 2:
            return None
        receiver_node = named_children[0]
        property_node = named_children[-1]
        receiver = source_code[receiver_node.start_byte : receiver_node.end_byte]
        prop = source_code[property_node.start_byte : property_node.end_byte]
        if not receiver or not prop:
            return None
        return receiver, prop

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
        self,
        caller_node,
        method_name: str,
        by_name: dict[str, list],
        *,
        source_code: str = "",
    ) -> str | None:
        candidates = by_name.get(method_name, [])
        if not candidates:
            return None

        class_node = caller_node
        while class_node and class_node.type != "class_declaration":
            class_node = class_node.parent
        if not class_node:
            if source_code and caller_node.type in {"function_expression", "arrow_function"}:
                assignment = self._property_assignment_for_value_node(caller_node, source_code)
                if assignment:
                    owner, _method = assignment
                    for candidate in candidates:
                        if f".{owner}.{method_name}" in candidate.qualified_name:
                            return str(candidate.uid)
            return str(candidates[0].uid) if len(candidates) == 1 else None

        class_name_node = class_node.child_by_field_name("name")
        if not class_name_node:
            return None
        class_name = class_name_node.text.decode("utf-8")
        for candidate in candidates:
            if f".{class_name}.{method_name}" in candidate.qualified_name:
                return str(candidate.uid)
        return str(candidates[0].uid) if len(candidates) == 1 else None


def make_adapter() -> JavaScriptAdapter:
    """Factory function for adapter discovery."""
    return JavaScriptAdapter()
