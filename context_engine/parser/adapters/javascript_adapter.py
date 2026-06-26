"""JavaScript language adapter using tree-sitter."""

import re
from typing import cast

from context_engine.parser.adapters.js_ts_fallback_patterns import (
    CHAINED_PROPERTY_FUNC_API_RE,
    PROPERTY_ARROW_API_RE,
    PROPERTY_FUNC_API_RE,
)
from context_engine.parser.adapters.treesitter_base import (
    TreeSitterAdapter,
    flatten_ts_query_captures,
)
from context_engine.parser.adapters.typescript_adapter import TypeScriptAdapter
from context_engine.parser.import_scan import (
    collect_js_ts_import_bindings,
    module_name_for_js_resolved_path,
    resolve_import_module_name,
)
from context_engine.parser.protocol import ClassApiEdge, ImportEdge, InheritanceEdge, SymbolMetadata
from context_engine.parser.uid import (
    compute_uid,
    module_name_from_path,
    normalize_signature,
    qualified_name_for,
    signature_from_node,
    signature_hash,
)


class JavaScriptAdapter(TreeSitterAdapter):
    """JavaScript parser adapter."""

    # HTTP endpoint extraction is shared with the TypeScript adapter below.
    # Keep its AST helpers on this adapter as well so the shared implementation
    # can preserve JavaScript-specific UID generation through ``self``.
    _CLASS_DECL_TYPES = TypeScriptAdapter._CLASS_DECL_TYPES
    _CONTROLLER_DECORATORS = TypeScriptAdapter._CONTROLLER_DECORATORS
    _DECORATABLE_NODE_TYPES = TypeScriptAdapter._DECORATABLE_NODE_TYPES

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
    _PROPERTY_FUNC_API_RE = PROPERTY_FUNC_API_RE
    _PROPERTY_ARROW_API_RE = PROPERTY_ARROW_API_RE
    _CHAINED_PROPERTY_FUNC_API_RE = CHAINED_PROPERTY_FUNC_API_RE
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

    def extract_axis_facts(
        self,
        source_code: str,
        file_path: str,
        *,
        tree=None,
        symbols: list[SymbolMetadata] | None = None,
        project_root: str | None = None,
    ):
        """Return common symbol facts plus JavaScript AST-physical axis facts."""
        from context_engine.parser.adapters.javascript_axis_extractor import (
            JavaScriptAxisExtractor,
        )

        facts = super().extract_axis_facts(
            source_code,
            file_path,
            tree=tree,
            symbols=symbols,
            project_root=project_root,
        )
        if tree is None:
            tree = self._parse(source_code)
        js_facts = JavaScriptAxisExtractor(self).extract_facts(
            source_code,
            file_path,
            tree=tree,
        )
        return [*facts, *js_facts]

    @staticmethod
    def _iter_nodes(node):
        yield from TypeScriptAdapter._iter_nodes(node)

    _node_text = staticmethod(TypeScriptAdapter._node_text)

    _member_expression_dotted = TypeScriptAdapter._member_expression_dotted
    _decorator_base_name = TypeScriptAdapter._decorator_base_name
    _member_expression_path = TypeScriptAdapter._member_expression_path
    _nth_positional_argument = TypeScriptAdapter._nth_positional_argument
    _http_call_site_uid = TypeScriptAdapter._http_call_site_uid
    _enclosing_class_uid = TypeScriptAdapter._enclosing_class_uid
    _http_path_from_decorator = TypeScriptAdapter._http_path_from_decorator
    _http_path_from_call_argument = TypeScriptAdapter._http_path_from_call_argument
    _http_handler_uid_from_call = TypeScriptAdapter._http_handler_uid_from_call
    _uid_for_symbol_name = TypeScriptAdapter._uid_for_symbol_name

    @classmethod
    def _decoratable_sibling_after(cls, parent, deco):
        return TypeScriptAdapter._decoratable_sibling_after(parent, deco)

    def _string_literal_text(self, node) -> str:
        return TypeScriptAdapter._string_literal_text(cast(TypeScriptAdapter, self), node)

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

    def _append_javascript_export_fallback_symbols(
        self,
        symbols: list[SymbolMetadata],
        existing_names: set[str],
        existing_uids: set[str],
        file_path: str,
        source_code: str,
    ) -> None:
        for match in self._EXPORTED_FUNC_FALLBACK_RE.finditer(source_code):
            self._append_module_fallback_symbol(
                symbols,
                existing_names,
                existing_uids,
                file_path,
                source_code,
                start_offset=match.start(),
                name=match.group(1) or match.group(2),
                kind="function",
            )
        for match in self._MODULE_EXPORT_FUNC_FALLBACK_RE.finditer(source_code):
            self._append_module_fallback_symbol(
                symbols,
                existing_names,
                existing_uids,
                file_path,
                source_code,
                start_offset=match.start(),
                name=match.group(1),
                kind="function",
            )
        for match in self._EXPORTED_VAR_FALLBACK_RE.finditer(source_code):
            self._append_module_fallback_symbol(
                symbols,
                existing_names,
                existing_uids,
                file_path,
                source_code,
                start_offset=match.start(),
                name=match.group(1) or match.group(2),
                kind="variable",
            )
        for name, start_offset in self._module_export_object_keys(source_code):
            self._append_module_fallback_symbol(
                symbols,
                existing_names,
                existing_uids,
                file_path,
                source_code,
                start_offset=start_offset,
                name=name,
                kind="variable",
            )

    def _append_javascript_property_fallback_symbols(
        self,
        symbols: list[SymbolMetadata],
        existing_names: set[str],
        existing_uids: set[str],
        file_path: str,
        source_code: str,
    ) -> None:
        for match in self._CHAINED_PROPERTY_FUNC_API_RE.finditer(source_code):
            self._append_property_fallback_symbol(
                symbols,
                existing_names,
                existing_uids,
                file_path,
                source_code,
                start_offset=match.start(),
                owner=match.group(1) or "",
                name=match.group(2) or "",
            )
        for match in self._PROPERTY_FUNC_API_RE.finditer(source_code):
            self._append_property_fallback_symbol(
                symbols,
                existing_names,
                existing_uids,
                file_path,
                source_code,
                start_offset=match.start(),
                owner=match.group(1) or "",
                name=match.group(3) or match.group(2) or "",
            )
        for match in self._PROPERTY_ARROW_API_RE.finditer(source_code):
            self._append_property_fallback_symbol(
                symbols,
                existing_names,
                existing_uids,
                file_path,
                source_code,
                start_offset=match.start(),
                owner=match.group(1) or "",
                name=match.group(2) or "",
            )

    def _finalize_javascript_symbols(
        self,
        symbols: list[SymbolMetadata],
        source_code: str,
        file_path: str,
        *,
        tree,
    ) -> None:
        from context_engine.parser.adapters.typescript_adapter import TypeScriptAdapter
        from context_engine.parser.docstring_extract import attach_docstrings

        ts_helpers = TypeScriptAdapter()
        ts_helpers._mark_property_accessor_symbols(symbols, tree, source_code, file_path)
        ts_helpers._mark_react_hook_symbols(symbols)
        ts_helpers._mark_behavioral_shape_symbols(symbols, tree)
        attach_docstrings(
            symbols,
            source_code,
            file_path,
            tree=tree,
            language=self.language_name,
        )

    def extract_symbols(
        self, source_code: str, file_path: str, *, tree=None
    ) -> list[SymbolMetadata]:
        """Extract JS symbols with a fallback for exported lexical APIs and module.exports."""
        symbols = super().extract_symbols(source_code, file_path, tree=tree)
        existing_names = {symbol.name for symbol in symbols}
        existing_uids = {symbol.uid for symbol in symbols}

        self._append_javascript_export_fallback_symbols(
            symbols, existing_names, existing_uids, file_path, source_code
        )
        self._append_javascript_property_fallback_symbols(
            symbols, existing_names, existing_uids, file_path, source_code
        )

        if tree is None:
            tree = self._parse(source_code)
        self._finalize_javascript_symbols(symbols, source_code, file_path, tree=tree)
        return symbols

    def extract_proxy_bindings(self, source_code: str, file_path: str, *, tree=None) -> list[dict]:
        from context_engine.parser.adapters.typescript_adapter import TypeScriptAdapter

        return TypeScriptAdapter.extract_proxy_bindings(
            cast(TypeScriptAdapter, self), source_code, file_path, tree=tree
        )

    def extract_hooks(self, source_code: str, file_path: str, *, tree=None) -> list[dict]:
        from context_engine.parser.adapters.typescript_adapter import TypeScriptAdapter

        return TypeScriptAdapter.extract_hooks(
            cast(TypeScriptAdapter, self), source_code, file_path, tree=tree
        )

    def extract_metadata_bridges(
        self, source_code: str, file_path: str, *, tree=None
    ) -> list[dict]:
        from context_engine.parser.adapters.typescript_adapter import TypeScriptAdapter

        return TypeScriptAdapter.extract_metadata_bridges(
            cast(TypeScriptAdapter, self), source_code, file_path, tree=tree
        )

    def extract_http_endpoints(self, source_code: str, file_path: str, *, tree=None) -> list[dict]:
        from context_engine.parser.adapters.typescript_adapter import TypeScriptAdapter

        return TypeScriptAdapter.extract_http_endpoints(
            cast(TypeScriptAdapter, self), source_code, file_path, tree=tree
        )

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

    def _collect_js_import_sources(
        self,
        captures: list[tuple],
        source_code: str,
    ) -> list[str]:
        sources: list[str] = []
        for node, tag in captures:
            if tag == "import.source":
                sources.append((node.text or b"").decode("utf-8").strip("\"'"))
            elif tag == "require.call":
                call_text = (node.text or b"").decode("utf-8")
                if "require(" in call_text:
                    match = re.search(r"require\(['\"]([^'\"]+)['\"]\)", call_text)
                    if match:
                        sources.append(match.group(1))
        for match in re.finditer(r"require\(\s*['\"]([^'\"]+)['\"]\s*\)", source_code):
            sources.append(match.group(1))
        return sources

    def extract_imports(self, source_code: str, file_path: str, *, tree=None) -> list[ImportEdge]:
        """Extract import and require statements from JavaScript source."""
        if tree is None:
            tree = self._parse(source_code)

        captures = flatten_ts_query_captures(self.language, self.import_query, tree.root_node)
        imports: list[ImportEdge] = []
        seen: set[tuple[str, str, str]] = set()
        for source in self._collect_js_import_sources(captures, source_code):
            import_type = "relative" if source.startswith(".") else "from_package"
            key = (file_path, source, import_type)
            if key in seen:
                continue
            seen.add(key)
            imports.append(ImportEdge(file_path, source, import_type))
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

        for pattern, owner_specs in self._property_api_edge_patterns():
            for match in pattern.finditer(source_code):
                for owner, prop, method_name in owner_specs(match):
                    add(owner, prop, method_name)
        return edges

    @classmethod
    def _property_api_edge_patterns(cls):
        return (
            (
                cls._CHAINED_PROPERTY_FUNC_API_RE,
                lambda match: (
                    (match.group(1), match.group(2), match.group(2)),
                    (match.group(3), match.group(4), match.group(5) or match.group(4)),
                ),
            ),
            (
                cls._PROPERTY_FUNC_API_RE,
                lambda match: ((match.group(1), match.group(2), match.group(3) or match.group(2)),),
            ),
            (
                cls._PROPERTY_ARROW_API_RE,
                lambda match: ((match.group(1), match.group(2), match.group(2)),),
            ),
        )

    def extract_symbol_aliases(self, source_code: str, file_path: str, *, tree=None) -> list[dict]:
        """Extract static symbol-level aliases from CommonJS export surfaces."""
        module_name = module_name_from_path(file_path)
        import_bindings, _ = self._extract_import_bindings(source_code, file_path)
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

    def _js_classify_identifier_call_node(
        self,
        func_node,
        *,
        ts,
        call_at_byte: int,
        import_bindings: dict[str, str],
        by_name: dict[str, list],
        scope_graph,
        source_code: str,
    ) -> tuple[str, str, str, float, str | None, bool, str, str]:
        call_name = source_code[func_node.start_byte : func_node.end_byte]
        rel_type, tier, confidence, _, callee_uid, skip_call, callee_qn = (
            ts._classify_identifier_call(
                call_name,
                import_bindings=import_bindings,
                by_name=by_name,
                scope_graph=scope_graph,
                at_byte=call_at_byte,
            )
        )
        resolver = "js-scope-v1" if rel_type != "CALLS_GUESS" else "js-ambiguity-gate-v1"
        return call_name, rel_type, tier, confidence, callee_uid, skip_call, callee_qn, resolver

    def _js_classify_member_call_node(
        self,
        func_node,
        *,
        parent,
        ts,
        call_at_byte: int,
        import_bindings: dict[str, str],
        by_name: dict[str, list],
        scope_graph,
        source_code: str,
    ) -> tuple[str, str, str, float, str | None, bool, str, str] | None:
        named_children = [child for child in func_node.children if child.is_named]
        if len(named_children) < 2:
            return None
        receiver_node = named_children[0]
        method_node = named_children[-1]
        receiver_text = source_code[receiver_node.start_byte : receiver_node.end_byte]
        call_name = source_code[method_node.start_byte : method_node.end_byte]
        rel_type, tier, confidence, _, callee_uid, skip_call, callee_qn = (
            ts._classify_member_call(
                receiver_text,
                call_name,
                parent=parent,
                import_bindings=import_bindings,
                by_name=by_name,
                scope_graph=scope_graph,
                at_byte=call_at_byte,
            )
        )
        resolved = self._resolve_method_uid(
            parent,
            call_name,
            by_name,
            source_code=source_code,
        )
        if receiver_text == "this" and resolved:
            callee_uid = resolved
            rel_type = "CALLS_SCOPED"
            tier = "scoped"
            confidence = 0.9
        resolver = "js-scope-v1" if rel_type != "CALLS_GUESS" else "js-ambiguity-gate-v1"
        return call_name, rel_type, tier, confidence, callee_uid, skip_call, callee_qn, resolver

    def _js_call_from_node(
        self,
        node,
        func_node,
        *,
        parent,
        caller_uid: str,
        source_code: str,
        import_bindings: dict[str, str],
        by_name: dict[str, list],
        scope_graph,
        ts,
    ) -> dict | None:
        call_at_byte = node.start_byte
        call_kind = "construct" if node.type == "new_expression" else "call"
        classified = None
        if func_node.type == "identifier":
            classified = self._js_classify_identifier_call_node(
                func_node,
                ts=ts,
                call_at_byte=call_at_byte,
                import_bindings=import_bindings,
                by_name=by_name,
                scope_graph=scope_graph,
                source_code=source_code,
            )
        elif func_node.type == "member_expression":
            classified = self._js_classify_member_call_node(
                func_node,
                parent=parent,
                ts=ts,
                call_at_byte=call_at_byte,
                import_bindings=import_bindings,
                by_name=by_name,
                scope_graph=scope_graph,
                source_code=source_code,
            )
        else:
            return None
        if classified is None:
            return None

        call_name, rel_type, tier, confidence, callee_uid, skip_call, callee_qn, resolver = (
            classified
        )
        if skip_call or callee_uid == caller_uid:
            return None

        call = {
            "caller_uid": caller_uid,
            "callee_name": call_name,
            "rel_type": rel_type,
            "tier": tier,
            "confidence": confidence,
            "resolver": resolver,
            "call_site_line": node.start_point[0] + 1,
            "call_kind": call_kind,
        }
        if callee_uid:
            call["callee_uid"] = callee_uid
        self._apply_imported_callee_qualified_name(
            call,
            func_node=func_node,
            call_name=call_name,
            import_bindings=import_bindings,
            source_code=source_code,
            callee_qn=callee_qn,
        )
        return call

    def _js_call_from_capture(
        self,
        node,
        tag: str,
        *,
        symbol_uids: set[str],
        source_code: str,
        file_path: str,
        import_bindings: dict[str, str],
        by_name: dict[str, list],
        scope_graph,
        ts,
    ) -> dict | None:
        if tag != "call":
            return None

        func_node = node.child_by_field_name("function")
        if func_node is None and node.type == "new_expression":
            func_node = node.child_by_field_name("constructor")
        if func_node is None:
            return None

        parent = self._enclosing_symbol_owner(node)
        if parent is None:
            return None

        caller_uid = self._caller_uid_for_indexed_owner(
            parent,
            symbol_uids,
            source_code,
            file_path,
        )
        if not caller_uid:
            return None

        return self._js_call_from_node(
            node,
            func_node,
            parent=parent,
            caller_uid=caller_uid,
            source_code=source_code,
            import_bindings=import_bindings,
            by_name=by_name,
            scope_graph=scope_graph,
            ts=ts,
        )

    def _javascript_call_index_context(
        self,
        source_code: str,
        file_path: str,
        *,
        tree,
    ) -> tuple[dict[str, list], set[str], dict[str, str], object, object]:
        from context_engine.parser.adapters.ts_scope_graph import TsScopeGraph
        from context_engine.parser.adapters.typescript_adapter import TypeScriptAdapter

        symbols = self.extract_symbols(source_code, file_path, tree=tree)
        by_name: dict[str, list] = {}
        for symbol in symbols:
            by_name.setdefault(symbol.name, []).append(symbol)
        symbol_uids = {str(symbol.uid) for symbol in symbols}
        import_bindings, _ = self._extract_import_bindings(source_code, file_path)
        ts = TypeScriptAdapter()
        scope_graph = TsScopeGraph.build(
            tree.root_node,
            import_bindings=import_bindings,
            node_text=TypeScriptAdapter._node_text,
            normalize_require=lambda path: self._normalize_import_source(file_path, path),
        )
        return by_name, symbol_uids, import_bindings, scope_graph, ts

    def extract_calls_from_source(
        self, source_code: str, file_path: str, *, tree=None
    ) -> list[dict]:
        """Extract JavaScript calls with ambiguity-gated resolution."""
        from context_engine.parser.adapters.typescript_adapter import TypeScriptAdapter

        if tree is None:
            tree = self._parse(source_code)

        captures = flatten_ts_query_captures(
            self.language,
            """
            (call_expression) @call
            (new_expression) @call
            """,
            tree.root_node,
        )

        by_name, symbol_uids, import_bindings, scope_graph, ts = self._javascript_call_index_context(
            source_code,
            file_path,
            tree=tree,
        )

        calls = []
        for node, tag in captures:
            call = self._js_call_from_capture(
                node,
                tag,
                symbol_uids=symbol_uids,
                source_code=source_code,
                file_path=file_path,
                import_bindings=import_bindings,
                by_name=by_name,
                scope_graph=scope_graph,
                ts=ts,
            )
            if call is not None:
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

    def _extract_import_bindings(
        self, source_code: str, file_path: str
    ) -> tuple[dict[str, str], set[str]]:
        return collect_js_ts_import_bindings(source_code, file_path, self._normalize_import_source)

    def _normalize_import_source(self, file_path: str, source: str) -> str:
        return resolve_import_module_name(
            file_path,
            source,
            module_for_resolved=module_name_for_js_resolved_path,
        )

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

    def _append_module_fallback_symbol(
        self,
        symbols: list[SymbolMetadata],
        existing_names: set[str],
        existing_uids: set[str],
        file_path: str,
        source_code: str,
        *,
        start_offset: int,
        name: str | None,
        kind: str,
    ) -> None:
        if not name or name in existing_names:
            return
        start_line, end_line, content = self._fallback_symbol_span(source_code, start_offset)
        signature = normalize_signature(f"{name}()->_", self.language_name)
        qualified_name = f"{module_name_from_path(file_path)}.{name}"
        uid = compute_uid(qualified_name, signature, self.language_name)
        symbols.append(
            SymbolMetadata(
                uid=uid,
                name=name,
                kind=kind,
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

    def _append_property_fallback_symbol(
        self,
        symbols: list[SymbolMetadata],
        existing_names: set[str],
        existing_uids: set[str],
        file_path: str,
        source_code: str,
        *,
        start_offset: int,
        owner: str,
        name: str,
    ) -> None:
        owner = owner.strip()
        name = name.strip()
        qualified_name = self._property_method_qualified_name(file_path, owner, name)
        uid = compute_uid(qualified_name, f"{name}()->_", self.language_name)
        if not name or not owner or uid in existing_uids:
            return
        start_line, end_line, content = self._fallback_symbol_span(source_code, start_offset)
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

    @staticmethod
    def _apply_imported_callee_qualified_name(
        call: dict,
        *,
        func_node,
        call_name: str,
        import_bindings: dict[str, str],
        source_code: str,
        callee_qn: str,
    ) -> None:
        if call.get("rel_type") != "CALLS_IMPORTED":
            return
        if callee_qn:
            call["callee_qualified_name"] = callee_qn
            return
        if func_node.type == "identifier":
            call["callee_qualified_name"] = import_bindings[call_name]
            return
        receiver_node = [child for child in func_node.children if child.is_named][0]
        receiver_text = source_code[receiver_node.start_byte : receiver_node.end_byte]
        base = import_bindings.get(receiver_text, "")
        if base:
            call["callee_qualified_name"] = f"{base}.{call_name}"

    def _fallback_symbol_span(
        self,
        source_code: str,
        start_offset: int,
    ) -> tuple[int, int, str]:
        """Best-effort line span for exported symbol text fallbacks."""
        return TypeScriptAdapter._fallback_symbol_span(source_code, start_offset)

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

    def _member_assignment_from_expression(
        self,
        parent,
        node,
        source_code: str,
    ) -> tuple[str, str] | None:
        named_children = [child for child in parent.children if child.is_named]
        rhs = named_children[-1] if named_children else None
        if (
            len(named_children) < 2
            or rhs is None
            or rhs.start_byte != node.start_byte
            or rhs.end_byte != node.end_byte
            or rhs.type != node.type
        ):
            return None
        lhs = named_children[0]
        member = self._member_expression_parts(lhs, source_code)
        if not member:
            return None
        owner, prop = member
        name_node = node.child_by_field_name("name")
        method_name = prop
        if name_node is not None:
            method_name = source_code[name_node.start_byte : name_node.end_byte]
        return owner, method_name

    def _property_assignment_for_value_node(self, node, source_code: str) -> tuple[str, str] | None:
        parent = node.parent
        while parent:
            if parent.type == "assignment_expression":
                assignment = self._member_assignment_from_expression(parent, node, source_code)
                if assignment:
                    return assignment
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

    @staticmethod
    def _method_uid_for_class_name(
        candidates: list,
        class_name: str,
        method_name: str,
    ) -> str | None:
        for candidate in candidates:
            if f".{class_name}.{method_name}" in candidate.qualified_name:
                return str(candidate.uid)
        return None

    @staticmethod
    def _single_method_candidate_uid(candidates: list) -> str | None:
        return str(candidates[0].uid) if len(candidates) == 1 else None

    @staticmethod
    def _enclosing_class_declaration(node):
        class_node = node
        while class_node and class_node.type != "class_declaration":
            class_node = class_node.parent
        return class_node

    def _resolve_method_uid_from_property_assignment(
        self,
        caller_node,
        method_name: str,
        candidates: list,
        source_code: str,
    ) -> str | None:
        if not source_code or caller_node.type not in {"function_expression", "arrow_function"}:
            return None
        assignment = self._property_assignment_for_value_node(caller_node, source_code)
        if not assignment:
            return None
        owner, _method = assignment
        for candidate in candidates:
            if f".{owner}.{method_name}" in candidate.qualified_name:
                return str(candidate.uid)
        return None

    def _method_uid_for_class_node(
        self,
        class_node,
        method_name: str,
        candidates: list,
    ) -> str | None:
        class_name_node = class_node.child_by_field_name("name")
        if not class_name_node:
            return None
        class_name = class_name_node.text.decode("utf-8")
        resolved = self._method_uid_for_class_name(candidates, class_name, method_name)
        if resolved:
            return resolved
        return self._single_method_candidate_uid(candidates)

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

        class_node = self._enclosing_class_declaration(caller_node)
        if not class_node:
            resolved = self._resolve_method_uid_from_property_assignment(
                caller_node,
                method_name,
                candidates,
                source_code,
            )
            if resolved:
                return resolved
            return self._single_method_candidate_uid(candidates)

        return self._method_uid_for_class_node(class_node, method_name, candidates)


def make_adapter() -> JavaScriptAdapter:
    """Factory function for adapter discovery."""
    return JavaScriptAdapter()
