"""TreeSitterAdapter — base class for tree-sitter-based language parsers."""

from abc import abstractmethod
from collections.abc import Iterator
from hashlib import sha256

from tree_sitter import Language, Parser, Query, QueryCursor

from context_engine.parser.protocol import LanguageAdapter, SymbolMetadata
from context_engine.parser.uid import (
    UNRESOLVED_SIGNATURE,
    compute_uid,
    module_name_from_path,
    normalize_signature,
    qualified_name_for,
    signature_from_node,
    signature_hash,
)


def _node_text(node) -> str:
    return (node.text or b"").decode("utf-8")


def iter_ts_query_matches(
    language: Language,
    query_source: str,
    root_node,
) -> Iterator[tuple[int, dict[str, list]]]:
    """Iterate tree-sitter query matches via Query + QueryCursor."""
    query = Query(language, query_source)
    cursor = QueryCursor(query)
    yield from cursor.matches(root_node)


def flatten_ts_query_captures(
    language: Language,
    query_source: str,
    root_node,
) -> list[tuple]:
    """Flatten tree-sitter query matches into ``(node, tag)`` tuples."""
    captures: list[tuple] = []
    for _match_id, captures_dict in iter_ts_query_matches(language, query_source, root_node):
        for tag, nodes in captures_dict.items():
            for node in nodes:
                captures.append((node, tag))
    return captures


class TreeSitterAdapter(LanguageAdapter):
    """Base class for language adapters using tree-sitter."""

    @property
    @abstractmethod
    def ts_language_name(self) -> str:
        """Return the tree-sitter language name (e.g., 'python', 'typescript')."""
        pass

    @property
    @abstractmethod
    def symbol_query(self) -> str:
        """Return the tree-sitter S-expression query for symbol extraction."""
        pass

    @property
    @abstractmethod
    def call_query(self) -> str:
        """Return the tree-sitter S-expression query for call-site extraction."""
        pass

    @property
    @abstractmethod
    def parent_types(self) -> set[str]:
        """Return the set of AST node types to use as enclosing callables."""
        pass

    def __init__(self):
        """Initialize parser and language objects for this language."""
        lang_name = self.ts_language_name
        if lang_name == "python":
            from tree_sitter_python import language as lang_ptr

            self.language = Language(lang_ptr())
        elif lang_name == "typescript":
            from tree_sitter_typescript import language_typescript as lang_ptr

            self.language = Language(lang_ptr())
        else:
            raise ValueError(f"Unsupported language: {lang_name}")

        self.parser = Parser(self.language)

    def _parse(self, source_code: str):
        """Parse source code into a tree-sitter tree."""
        return self.parser.parse(bytes(source_code, "utf8"))

    @staticmethod
    def _var_names_by_parent_id(captures: list[tuple]) -> dict[int, str]:
        var_names: dict[int, str] = {}
        for node, tag in captures:
            if tag in ("var.name", "attr.name") and node.parent is not None:
                var_names[node.parent.id] = _node_text(node)
        return var_names

    def _declaration_symbol_from_capture(
        self,
        node,
        tag: str,
        file_path: str,
    ) -> SymbolMetadata | None:
        name_node = node.child_by_field_name("name")
        if not name_node:
            return None
        name = _node_text(name_node)
        content = _node_text(node)
        qualified_name = qualified_name_for(node, file_path)
        raw_signature, signature_status = signature_from_node(node, self.language_name)
        signature = normalize_signature(raw_signature or "", self.language_name)
        return SymbolMetadata(
            uid=compute_uid(qualified_name, raw_signature, self.language_name),
            name=name,
            kind="function" if tag == "func.def" else "class",
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            content_hash=self._hash(content),
            file_path=file_path,
            qualified_name=qualified_name,
            signature=signature,
            signature_hash=signature_hash(signature, self.language_name),
            signature_status=signature_status,
            language=self.language_name,
        )

    def _variable_symbol_from_capture(
        self,
        node,
        tag: str,
        var_names: dict[int, str],
        file_path: str,
    ) -> SymbolMetadata | None:
        var_name = var_names.get(node.id)
        if not var_name or not self.should_include_variable_symbol(
            node,
            tag,
            var_name,
            file_path=file_path,
        ):
            return None
        content = _node_text(node)
        qualified_name = ".".join([self._module_name(file_path), var_name])
        signature = f"{var_name}()->_"
        return SymbolMetadata(
            uid=compute_uid(qualified_name, signature, self.language_name),
            name=var_name,
            kind="variable",
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            content_hash=self._hash(content),
            file_path=file_path,
            qualified_name=qualified_name,
            signature=normalize_signature(signature, self.language_name),
            signature_hash=signature_hash(signature, self.language_name),
            signature_status="resolved",
            language=self.language_name,
        )

    def _flatten_ts_query_captures(self, query: str, root_node) -> list[tuple]:
        captures: list[tuple] = []
        for _match_id, captures_dict in iter_ts_query_matches(self.language, query, root_node):
            for tag, nodes in captures_dict.items():
                for node in nodes:
                    captures.append((node, tag))
        return captures

    def _symbol_from_capture(
        self,
        node,
        tag: str,
        var_names: dict[int, str],
        file_path: str,
    ) -> SymbolMetadata | None:
        if tag in ("func.def", "class.def"):
            return self._declaration_symbol_from_capture(node, tag, file_path)
        if tag.startswith("var."):
            return self._variable_symbol_from_capture(node, tag, var_names, file_path)
        return None

    def extract_symbols(
        self, source_code: str, file_path: str, *, tree=None
    ) -> list[SymbolMetadata]:
        """Extract functions, classes, and module-level constants from source code.

        ``tree`` may be passed by callers (e.g. ``extract_all``) that already
        have a parsed tree-sitter tree, to avoid re-parsing.
        """
        if tree is None:
            tree = self._parse(source_code)

        captures = self._flatten_ts_query_captures(self.symbol_query, tree.root_node)
        var_names = self._var_names_by_parent_id(captures)
        symbols: list[SymbolMetadata] = []
        attr_nodes: list = []
        for node, tag in captures:
            if tag == "attr.def":
                attr_nodes.append(node)
                continue
            symbol = self._symbol_from_capture(node, tag, var_names, file_path)
            if symbol is not None:
                symbols.append(symbol)
        symbols.extend(
            self._attribute_symbols(attr_nodes, var_names, file_path, base_symbols=symbols)
        )
        return symbols

    def _attribute_symbols(
        self,
        attr_nodes: list,
        var_names: dict[int, str],
        file_path: str,
        *,
        base_symbols: list[SymbolMetadata],
    ) -> list[SymbolMetadata]:
        """Variable Symbols for ``@attr.def`` captures (class-body fields).

        Adapter opt-in: only queries that tag class-field declarations produce
        these (Python parity — class attributes are named API surface that
        symbol retrieval otherwise cannot see). First capture of a name wins;
        names already claimed by a def/class of the same owner are skipped.
        Gated by ``AXIS_INDEX_CLASS_ATTRS`` like the Python extraction.
        """
        if not attr_nodes:
            return []
        import os

        if os.getenv("AXIS_INDEX_CLASS_ATTRS", "1") == "0":
            return []
        existing_qualified = {s.qualified_name for s in base_symbols}
        out: list[SymbolMetadata] = []
        for node in attr_nodes:
            name = var_names.get(node.id)
            if not name:
                continue
            qualified_name = f"{qualified_name_for(node, file_path)}.{name}"
            if qualified_name in existing_qualified:
                continue
            existing_qualified.add(qualified_name)
            signature = f"{name}()->_"
            out.append(
                SymbolMetadata(
                    uid=compute_uid(qualified_name, signature, self.language_name),
                    name=name,
                    kind="variable",
                    start_line=node.start_point[0] + 1,
                    end_line=node.end_point[0] + 1,
                    content_hash="",
                    file_path=file_path,
                    qualified_name=qualified_name,
                    signature=signature,
                    signature_hash="",
                    signature_status="resolved",
                    language=self.language_name,
                )
            )
        return out

    def _synthesized_module_symbol(self, source_code: str, file_path: str) -> SymbolMetadata:
        """One module-scope Symbol per file (Python `_module_symbol` parity).

        The uid formula matches the TS axis extractor's ``_module_scope`` so
        module-execution axis facts and module-scope call edges share one
        identity with this row instead of dangling.
        """
        module_name = module_name_from_path(file_path)
        return SymbolMetadata(
            uid=self._module_symbol_uid(file_path),
            name=module_name,
            kind="module",
            start_line=1,
            end_line=source_code.count("\n") + 1,
            content_hash="",  # structural, not content-keyed
            file_path=file_path,
            qualified_name=module_name,
            signature=UNRESOLVED_SIGNATURE,
            signature_hash="",
            signature_status="resolved",
            language=self.language_name,
        )

    def _module_symbol_uid(self, file_path: str) -> str:
        return compute_uid(
            module_name_from_path(file_path), UNRESOLVED_SIGNATURE, self.language_name
        )

    @staticmethod
    def _inside_variable_declarator(node) -> bool:
        """True for calls in a declarator initializer (``const x = call(...)``).

        Those keep their dedicated owner attribution (the declared variable),
        which is more precise than the module-scope fallback.
        """
        current = node.parent
        while current is not None and current.type != "program":
            if current.type == "variable_declarator":
                return True
            current = current.parent
        return False

    def _export_from_alias_symbols(
        self,
        tree,
        file_path: str,
        *,
        base_symbols: list[SymbolMetadata],
    ) -> list[SymbolMetadata]:
        """Variable Symbols for ``export { X } from "<package>"`` re-exports.

        Python-parity of the re-export alias extraction: the workspace's
        public name for a dependency type lives at the export site, and
        without a row that name is invisible to symbol retrieval. JS/TS
        ``export … from`` is itself the explicit re-export marker; only
        non-relative sources qualify (in-project barrels already resolve to
        the real Symbol). Gated by ``AXIS_INDEX_REEXPORT_ALIASES``.
        """
        import os

        if os.getenv("AXIS_INDEX_REEXPORT_ALIASES", "1") == "0":
            return []
        module_name = module_name_from_path(file_path)
        existing_qualified = {s.qualified_name for s in base_symbols}
        out: list[SymbolMetadata] = []
        for stmt in tree.root_node.named_children:
            if stmt.type != "export_statement":
                continue
            source = stmt.child_by_field_name("source")
            if source is None:
                continue
            spec = _node_text(source).strip().strip("\"'")
            if not spec or spec.startswith(".") or spec.startswith("/"):
                continue
            for clause in stmt.named_children:
                if clause.type != "export_clause":
                    continue
                for entry in clause.named_children:
                    if entry.type != "export_specifier":
                        continue
                    name_node = entry.child_by_field_name("alias") or entry.child_by_field_name(
                        "name"
                    )
                    if name_node is None:
                        continue
                    local_name = _node_text(name_node)
                    if not local_name:
                        continue
                    qualified_name = f"{module_name}.{local_name}"
                    if qualified_name in existing_qualified:
                        continue
                    existing_qualified.add(qualified_name)
                    signature = f"{local_name}()->_"
                    out.append(
                        SymbolMetadata(
                            uid=compute_uid(qualified_name, signature, self.language_name),
                            name=local_name,
                            kind="variable",
                            start_line=stmt.start_point[0] + 1,
                            end_line=stmt.end_point[0] + 1,
                            content_hash="",
                            file_path=file_path,
                            qualified_name=qualified_name,
                            signature=signature,
                            signature_hash="",
                            signature_status="resolved",
                            language=self.language_name,
                        )
                    )
        return out

    def should_include_variable_symbol(
        self,
        node,
        tag: str,
        name: str,
        *,
        file_path: str,
    ) -> bool:
        """Decide whether a captured module-level variable should be indexed.

        The default remains conservative: only UPPER_CASE constants are treated
        as symbols. Language adapters can override this for ecosystems where
        exported lexical declarations are part of the public API surface.
        """
        _ = (node, tag, file_path)
        return name.isupper()

    def _enclosing_parent_for_call(self, node):
        parent = node.parent
        while parent and parent.type not in self.parent_types:
            parent = parent.parent
        return parent

    def _call_edge_from_name_capture(
        self,
        node,
        file_path: str,
    ) -> dict | None:
        call_name = _node_text(node)
        parent = self._enclosing_parent_for_call(node)
        if parent is None:
            return None
        parent_name_node = parent.child_by_field_name("name")
        if parent_name_node is None:
            return None
        caller_uid = self._symbol_uid_from_node(parent, file_path)
        return {
            "caller_uid": caller_uid,
            "callee_name": call_name,
            "rel_type": "CALLS_DIRECT",
            "tier": "guess",
            "confidence": 0.4,
            "resolver": f"{self.language_name}-scope-v1",
            "call_site_line": node.start_point[0] + 1,
        }

    def extract_calls_from_source(
        self, source_code: str, file_path: str, *, tree=None
    ) -> list[dict]:
        """Extract function call edges from source code with call type classification."""
        if tree is None:
            tree = self._parse(source_code)

        captures = self._flatten_ts_query_captures(self.call_query, tree.root_node)
        calls = []
        for node, tag in captures:
            if tag != "call.name":
                continue
            call = self._call_edge_from_name_capture(node, file_path)
            if call is not None:
                calls.append(call)
        return calls

    def _uid(self, file_path: str, name: str) -> str:
        """Generate deterministic UID for a symbol."""
        qualified_name = ".".join([self._module_name(file_path), name])
        signature = f"{name}()->_"
        return compute_uid(qualified_name, signature, self.language_name)

    def _hash(self, code: str) -> str:
        """Hash code content for change detection."""
        return sha256(code.encode()).hexdigest()

    def _module_name(self, file_path: str) -> str:
        from context_engine.parser.uid import module_name_from_path

        return module_name_from_path(file_path)

    def _symbol_uid_from_node(self, node, file_path: str) -> str:
        qualified_name = qualified_name_for(node, file_path)
        raw_signature, _ = signature_from_node(node, self.language_name)
        return compute_uid(qualified_name, raw_signature, self.language_name)

    def extract_all(
        self,
        source_code: str,
        file_path: str,
        *,
        include_axis_facts: bool = False,
        project_root: str | None = None,
    ):
        """Parse once, then run all extractions over the same AST.

        Tree-sitter trees are immutable and cheap to share across queries
        within a single thread. ``FastExtractor`` already gives each worker
        its own adapter instance via ``_ThreadLocalAdapters``, so the tree
        we hold here never crosses threads.
        """
        tree = self._parse(source_code)
        symbols = self.extract_symbols(source_code, file_path, tree=tree)
        calls = self.extract_calls_from_source(source_code, file_path, tree=tree)
        imports = self.extract_imports(source_code, file_path, tree=tree)
        inheritance = self.extract_inheritance(source_code, file_path, tree=tree)
        axis_facts = None
        if include_axis_facts:
            axis_facts = self.extract_axis_facts(
                source_code,
                file_path,
                tree=tree,
                symbols=symbols,
                project_root=project_root,
            )
        from context_engine.parser.derived_facts import extract_derived_file_facts

        derived = extract_derived_file_facts(self, source_code, file_path, tree=tree)
        return symbols, calls, imports, inheritance, axis_facts, derived
