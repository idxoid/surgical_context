"""LanguageAdapter protocol — ADR-005 plugin architecture."""

from abc import ABC, abstractmethod
from dataclasses import dataclass

from pydantic import BaseModel


class SymbolMetadata(BaseModel):
    """Extracted code symbol (function, class, or module-level constant)."""

    uid: str
    name: str
    kind: str  # "function" | "class" | "variable"
    start_line: int
    end_line: int
    content_hash: str
    file_path: str
    token_estimate: int = 0  # tiktoken estimate; computed at index time
    qualified_name: str = ""
    signature: str = "<unresolved>"
    signature_hash: str = ""
    signature_status: str = "unresolved"
    language: str = ""


@dataclass
class ImportEdge:
    """An import statement from one file to another or external package."""

    source_file: str
    target_module_name: str
    import_type: str  # "direct" | "relative" | "from_package"


@dataclass
class InheritanceEdge:
    """Class inheritance or interface implementation."""

    subclass_uid: str
    superclass_name: str
    is_interface: bool


class LanguageAdapter(ABC):
    """Plugin interface for language-specific parsing."""

    @property
    @abstractmethod
    def language_name(self) -> str:
        """Return the canonical language name (e.g., 'python', 'typescript')."""
        pass

    @property
    @abstractmethod
    def file_extensions(self) -> set[str]:
        """Return file extensions this adapter handles (e.g., {'.py', '.pyi'})."""
        pass

    @abstractmethod
    def extract_symbols(self, source_code: str, file_path: str) -> list[SymbolMetadata]:
        """
        Parse source code and extract top-level symbols (functions, classes, constants).

        Args:
            source_code: full file content as string
            file_path: absolute path (used for UID generation)

        Returns:
            List of SymbolMetadata objects with uid, name, kind, start_line, end_line, content_hash.
        """
        pass

    @abstractmethod
    def extract_calls_from_source(self, source_code: str, file_path: str) -> list[dict]:
        """
        Parse source code and extract direct function calls within the file.

        Args:
            source_code: full file content as string
            file_path: absolute path (used to look up enclosing symbols)

        Returns:
            List of dicts with keys:
            - caller_uid: str
            - callee_name: str (unresolved — matched by name in Neo4j during indexing)
            - rel_type: str ("CALLS_DIRECT" | "CALLS_DYNAMIC" | "CALLS_INFERRED")
        """
        pass

    def extract_imports(
        self, source_code: str, file_path: str, *, tree=None
    ) -> list[ImportEdge]:
        """
        Parse source code and extract import/require statements.

        Optional — adapters override only if language has imports.
        ``tree`` is an optional pre-parsed tree-sitter tree the caller may
        pass to avoid re-parsing; adapters not based on tree-sitter ignore it.

        Returns:
            List of ImportEdge(source_file, target_module_name, import_type) tuples.
            target_module_name is unresolved — matched during indexing.
        """
        return []

    def extract_inheritance(
        self, source_code: str, file_path: str, *, tree=None
    ) -> list[InheritanceEdge]:
        """
        Parse source code and extract class inheritance / interface implementation.

        Optional — adapters override only if language has inheritance.
        ``tree`` is an optional pre-parsed tree-sitter tree the caller may
        pass to avoid re-parsing; adapters not based on tree-sitter ignore it.

        Returns:
            List of InheritanceEdge(subclass_uid, superclass_name, is_interface) tuples.
            superclass_name is unresolved — matched by name during indexing.
        """
        return []

    def extract_all(
        self, source_code: str, file_path: str
    ) -> tuple[
        list[SymbolMetadata],
        list[dict],
        list[ImportEdge],
        list[InheritanceEdge],
    ]:
        """One-shot extraction of every per-file artifact.

        Default implementation calls the four legacy methods, which means
        tree-sitter parses the source four times. ``TreeSitterAdapter``
        overrides this to parse once and reuse the AST. Adapters that
        don't extend ``TreeSitterAdapter`` get the default fallback for
        free.
        """
        symbols = self.extract_symbols(source_code, file_path)
        calls = self.extract_calls_from_source(source_code, file_path)
        imports = self.extract_imports(source_code, file_path)
        inheritance = self.extract_inheritance(source_code, file_path)
        return symbols, calls, imports, inheritance
