"""Fast extractor — read file once, extract everything.

Wraps the existing LanguageAdapter protocol. Baseline pipeline opens each
file four times (symbols, calls, imports, inheritance). This module:

1. Reads the file from disk exactly once per index pass.
2. Reuses the same source_code string for all four extractions.
3. Uses thread-local adapter instances so tree-sitter Parsers (which are
   not thread-safe) don't race under the worker pool.
4. Calls ``adapter.extract_all(source, path)`` so the tree-sitter parse
   happens exactly once per file. Adapters that don't override
   ``extract_all`` still work — the protocol provides a default that
   falls back to the four legacy methods.
"""

import hashlib
import threading

from sidecar.parser.protocol import (
    ImportEdge,
    InheritanceEdge,
    SymbolMetadata,
)
from sidecar.parser.registry import REGISTRY
from sidecar.parser.uid import project_root_scope


class _ThreadLocalAdapters:
    """Per-thread adapter registry.

    Tree-sitter Parser objects are not thread-safe; sharing one across
    worker threads produces undefined output. Each worker thread lazily
    materializes its own adapter via the adapter's factory.
    """

    def __init__(self):
        self._local = threading.local()

    def get(self, language: str):
        cache = getattr(self._local, "adapters", None)
        if cache is None:
            cache = {}
            self._local.adapters = cache
        if language not in cache:
            cache[language] = _build_adapter(language)
        return cache[language]


def _build_adapter(language: str):
    """Instantiate a fresh adapter for the given language.

    We import the same adapter modules that the global REGISTRY uses, but
    call their ``make_adapter()`` factories directly so each worker thread
    gets its own tree-sitter Parser.
    """
    # Lazy import so cold startup cost hits only when fast indexing runs.
    import importlib

    module_name = f"sidecar.parser.adapters.{language}_adapter"
    mod = importlib.import_module(module_name)
    return mod.make_adapter()


class ExtractedFile:
    """Everything the pipeline needs from a single source file."""

    __slots__ = (
        "path",
        "source",
        "file_hash",
        "symbols",
        "calls",
        "imports",
        "inheritance",
    )

    def __init__(
        self,
        path: str,
        source: str,
        file_hash: str,
        symbols: list[SymbolMetadata],
        calls: list[dict],
        imports: list[ImportEdge],
        inheritance: list[InheritanceEdge],
    ):
        self.path = path
        self.source = source
        self.file_hash = file_hash
        self.symbols = symbols
        self.calls = calls
        self.imports = imports
        self.inheritance = inheritance


class FastExtractor:
    """Stateless-looking facade backed by thread-local adapters."""

    def __init__(self, project_root: str | None = None):
        self._adapters = _ThreadLocalAdapters()
        self.project_root = project_root

    def _resolve_language(self, file_path: str) -> str:
        # Language detection is a pure dict lookup, safe to hit the
        # shared REGISTRY from any thread.
        return REGISTRY.detect_language(file_path)

    def extract_all(self, file_path: str) -> ExtractedFile | None:
        """Read one file, run all four extractions, return bundle.

        Returns None if the file cannot be read or its language is
        unsupported. We do not raise — the pipeline wants to keep going
        if one file is malformed.
        """
        try:
            with open(file_path, "rb") as f:
                raw = f.read()
        except OSError:
            return None

        file_hash = hashlib.sha256(raw).hexdigest()

        try:
            source = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None

        try:
            language = self._resolve_language(file_path)
        except ValueError:
            return None

        adapter = self._adapters.get(language)

        # One-shot extraction via the adapter's extract_all method. For
        # tree-sitter adapters this means a single parse; for other
        # adapters it falls back to the four legacy methods (no regression).
        with project_root_scope(self.project_root):
            symbols, calls, imports, inheritance = adapter.extract_all(source, file_path)
        for sym in symbols:
            # Matches baseline's coarse 8-tokens-per-line estimate.
            line_count = sym.end_line - sym.start_line + 1
            sym.token_estimate = max(1, line_count * 8)

        return ExtractedFile(
            path=file_path,
            source=source,
            file_hash=file_hash,
            symbols=symbols,
            calls=calls,
            imports=imports,
            inheritance=inheritance,
        )


def hash_file(file_path: str) -> str:
    """Streaming sha256 of a file. Matches baseline's content-hash semantics.

    Baseline reads the file entirely into memory. Streaming keeps memory
    bounded for large generated files (bundles, minified JS).
    """
    h = hashlib.sha256()
    try:
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()
