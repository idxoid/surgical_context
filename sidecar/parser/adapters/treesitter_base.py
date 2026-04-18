"""TreeSitterAdapter — base class for tree-sitter-based language parsers."""

from abc import abstractmethod
from hashlib import sha256

import tree_sitter_languages

from sidecar.parser.protocol import LanguageAdapter, SymbolMetadata


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
        self.parser = tree_sitter_languages.get_parser(self.ts_language_name)
        self.language = tree_sitter_languages.get_language(self.ts_language_name)

    def extract_symbols(self, source_code: str, file_path: str) -> list[SymbolMetadata]:
        """Extract functions, classes, and module-level constants from source code."""
        tree = self.parser.parse(bytes(source_code, "utf8"))
        query = self.language.query(self.symbol_query)
        captures = query.captures(tree.root_node)

        # Collect var.name nodes keyed by their parent var.def node id
        var_names: dict[int, str] = {}
        for node, tag in captures:
            if tag == 'var.name':
                var_names[node.parent.id] = node.text.decode('utf-8')

        symbols = []
        for node, tag in captures:
            if tag in ('func.def', 'class.def'):
                name_node = node.child_by_field_name('name')
                if not name_node:
                    continue
                name = name_node.text.decode('utf-8')
                content = node.text.decode('utf-8')
                symbols.append(SymbolMetadata(
                    uid=self._uid(file_path, name),
                    name=name,
                    kind="function" if tag == 'func.def' else "class",
                    start_line=node.start_point[0] + 1,
                    end_line=node.end_point[0] + 1,
                    content_hash=self._hash(content),
                    file_path=file_path
                ))
            elif tag == 'var.def':
                name = var_names.get(node.id)
                if not name or not name.isupper():
                    continue
                content = node.text.decode('utf-8')
                symbols.append(SymbolMetadata(
                    uid=self._uid(file_path, name),
                    name=name,
                    kind="variable",
                    start_line=node.start_point[0] + 1,
                    end_line=node.end_point[0] + 1,
                    content_hash=self._hash(content),
                    file_path=file_path
                ))
        return symbols

    def extract_calls_from_source(self, source_code: str, file_path: str) -> list[dict]:
        """Extract function call edges from source code."""
        tree = self.parser.parse(bytes(source_code, "utf8"))
        query = self.language.query(self.call_query)
        captures = query.captures(tree.root_node)

        calls = []
        for node, tag in captures:
            if tag == 'call.name':
                call_name = source_code[node.start_byte:node.end_byte]
                parent = node.parent
                while parent and parent.type not in self.parent_types:
                    parent = parent.parent
                if parent:
                    parent_name_node = parent.child_by_field_name('name')
                    if parent_name_node:
                        caller_name = source_code[parent_name_node.start_byte:parent_name_node.end_byte]
                        calls.append({
                            "caller_uid": self._uid(file_path, caller_name),
                            "callee_name": call_name
                        })
        return calls

    def _uid(self, file_path: str, name: str) -> str:
        """Generate deterministic UID for a symbol."""
        return sha256(f"{file_path}:{name}".encode()).hexdigest()

    def _hash(self, code: str) -> str:
        """Hash code content for change detection."""
        return sha256(code.encode()).hexdigest()
