"""TreeSitterAdapter — base class for tree-sitter-based language parsers."""

from abc import abstractmethod
from hashlib import sha256
from typing import Iterator

from tree_sitter import Language, Parser, Query, QueryCursor

from context_engine.parser.protocol import LanguageAdapter, SymbolMetadata
from context_engine.parser.uid import (
    compute_uid,
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

    def extract_symbols(
        self, source_code: str, file_path: str, *, tree=None
    ) -> list[SymbolMetadata]:
        """Extract functions, classes, and module-level constants from source code.

        ``tree`` may be passed by callers (e.g. ``extract_all``) that already
        have a parsed tree-sitter tree, to avoid re-parsing.
        """
        if tree is None:
            tree = self._parse(source_code)

        # Flatten captures from matches into (node, tag) tuples
        captures = []
        for _match_id, captures_dict in iter_ts_query_matches(
            self.language, self.symbol_query, tree.root_node
        ):
            for tag, nodes in captures_dict.items():
                for node in nodes:
                    captures.append((node, tag))

        # Collect var.name nodes keyed by their parent var.def node id
        var_names: dict[int, str] = {}
        for node, tag in captures:
            if tag == "var.name" and node.parent is not None:
                var_names[node.parent.id] = _node_text(node)

        symbols = []
        for node, tag in captures:
            if tag in ("func.def", "class.def"):
                name_node = node.child_by_field_name("name")
                if not name_node:
                    continue
                name = _node_text(name_node)
                content = _node_text(node)
                qualified_name = qualified_name_for(node, source_code, file_path)
                raw_signature, signature_status = signature_from_node(
                    node, source_code, self.language_name
                )
                signature = normalize_signature(raw_signature or "", self.language_name)
                symbols.append(
                    SymbolMetadata(
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
                )
            elif tag.startswith("var."):
                var_name = var_names.get(node.id)
                if not var_name or not self.should_include_variable_symbol(
                    node,
                    tag,
                    var_name,
                    source_code=source_code,
                    file_path=file_path,
                ):
                    continue
                content = _node_text(node)
                qualified_name = ".".join([self._module_name(file_path), var_name])
                signature = f"{var_name}()->_"
                symbols.append(
                    SymbolMetadata(
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
                )
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
        """Decide whether a captured module-level variable should be indexed.

        The default remains conservative: only UPPER_CASE constants are treated
        as symbols. Language adapters can override this for ecosystems where
        exported lexical declarations are part of the public API surface.
        """
        return name.isupper()

    def extract_calls_from_source(
        self, source_code: str, file_path: str, *, tree=None
    ) -> list[dict]:
        """Extract function call edges from source code with call type classification."""
        if tree is None:
            tree = self._parse(source_code)

        # Flatten captures from matches into (node, tag) tuples
        captures = []
        for _match_id, captures_dict in iter_ts_query_matches(
            self.language, self.call_query, tree.root_node
        ):
            for tag, nodes in captures_dict.items():
                for node in nodes:
                    captures.append((node, tag))

        calls = []
        for node, tag in captures:
            if tag == "call.name":
                call_name = _node_text(node)
                parent = node.parent
                while parent and parent.type not in self.parent_types:
                    parent = parent.parent
                if parent:
                    parent_name_node = parent.child_by_field_name("name")
                    if parent_name_node:
                        caller_uid = self._symbol_uid_from_node(parent, source_code, file_path)
                        calls.append(
                            {
                                "caller_uid": caller_uid,
                                "callee_name": call_name,
                                "rel_type": "CALLS_DIRECT",
                                "tier": "guess",
                                "confidence": 0.4,
                                "resolver": f"{self.language_name}-scope-v1",
                                "call_site_line": node.start_point[0] + 1,
                            }
                        )
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

    def _symbol_uid_from_node(self, node, source_code: str, file_path: str) -> str:
        qualified_name = qualified_name_for(node, source_code, file_path)
        raw_signature, _ = signature_from_node(node, source_code, self.language_name)
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
        return symbols, calls, imports, inheritance, axis_facts
