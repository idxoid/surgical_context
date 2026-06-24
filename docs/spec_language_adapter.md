# Spec — Language Adapter Protocol (ADR-005)

> **Status:** Implemented for the registry and baseline adapter contract. A new adapter file is auto-discovered and makes its extensions collectable without a registry edit. Full parity with Python/TypeScript/JavaScript still requires language-specific axis facts, resolution behavior, enrichment bridges, and tests; several fast-pipeline phases intentionally specialize by language.

**Code:** [context_engine/parser/protocol.py](../context_engine/parser/protocol.py),
[context_engine/parser/registry.py](../context_engine/parser/registry.py),
[context_engine/parser/adapters/](../context_engine/parser/adapters/).

**See also:**
- [spec_parser.md](spec_parser.md) — `SymbolExtractor`, module layout, indexer handoff
- [spec_indexer.md](spec_indexer.md) — indexed extensions derived from the registry
- [role_predicates.md](role_predicates.md) — Pass-1 consumes extraction-time AST markers
- [spec_call_resolution_pipeline.md](spec_call_resolution_pipeline.md) — call typing after extract

## 1. Problem

Before ADR-005, language-specific parsing logic was scattered across `context_engine/parser/`:
- Tree-sitter queries baked into `languages.py` as a `LANGUAGE_CONFIGS` dict.
- `SymbolExtractor` hard-codes the lookup.
- Adding a new language (Go, Rust, Java) means editing both files.
- Risk: as languages grow, core modules become fragile and harder to test in isolation.

ADR-005 solves this via a plugin architecture: each language implements a protocol; the extractor loads adapters dynamically.

## 2. The Protocol

### 2.1 `LanguageAdapter` — Abstract Base Class

```python
from abc import ABC, abstractmethod
from typing import List, Optional
from context_engine.parser.extractor import SymbolMetadata, CallEdge

class LanguageAdapter(ABC):
    """Plugin interface for language-specific parsing."""
    
    @property
    @abstractmethod
    def language_name(self) -> str:
        """Return the canonical language name (e.g., 'python', 'typescript', 'go')."""
        pass
    
    @property
    @abstractmethod
    def file_extensions(self) -> set[str]:
        """Return file extensions this adapter handles (e.g., {'.py', '.pyi'})."""
        pass
    
    @abstractmethod
    def extract_symbols(self, source_code: str, file_path: str) -> List[SymbolMetadata]:
        """
        Parse source code and extract top-level symbols (functions, classes, module-level constants).
        
        Args:
            source_code: full file content as string
            file_path: absolute path (used for UID generation)
        
        Returns:
            List of SymbolMetadata objects with populated uid, name, kind,
            start_line, end_line, content_hash, signature fields, language, and
            optional structural AST markers such as returns_mapping /
            returns_sequence / returns_constructed_type.
        """
        pass
    
    @abstractmethod
    def extract_calls(self, source_code: str, file_path: str) -> List['CallEdge']:
        """
        Parse source code and extract direct function calls within the file.
        
        Args:
            source_code: full file content as string
            file_path: absolute path (used to look up enclosing symbols)
        
        Returns:
            List of call dicts containing caller_uid, callee_name, rel_type,
            confidence/tier/resolver metadata, and callee_uid or
            callee_qualified_name when the adapter can resolve statically.
        """
        pass
    
    def extract_imports(self, source_code: str, file_path: str) -> List['ImportEdge']:
        """
        (Optional — Phase 3.5) Parse source code and extract import/require statements.
        
        Args:
            source_code: full file content as string
            file_path: absolute path
        
        Returns:
            List of ImportEdge(source_file, target_module_name, import_type) tuples.
            import_type: 'direct' (import x) or 'relative' (from . import x) or 'from_package' (import foo.bar.baz).
        
        Base implementation returns empty list; adapters override only if language has imports.
        """
        return []
    
    def extract_inheritance(self, source_code: str, file_path: str) -> List['InheritanceEdge']:
        """
        (Optional — Phase 3.5) Parse source code and extract class inheritance / interface implementation.
        
        Args:
            source_code: full file content as string
            file_path: absolute path
        
        Returns:
            List of InheritanceEdge(subclass_uid, superclass_name, is_interface) tuples.
            superclass_name is resolved workspace-locally during indexing.
        
        Base implementation returns empty list; adapters override only if language has inheritance.
        """
        return []
```

### 2.2 Data Classes

```python
from dataclasses import dataclass

@dataclass
class CallEdge:
    """A function call from one symbol to another (or to an unresolved external)."""
    caller_uid: str        # UID of the enclosing function/class
    callee_name: str       # Display name at the call site
    callee_uid: str | None # Preferred resolved target UID
    callee_qualified_name: str | None # Import-qualified fallback
    callee_line: int       # Line number of the call site (optional, for debugging)
    rel_type: str          # CALLS_SCOPED / CALLS_IMPORTED / CALLS_DYNAMIC / ...
    confidence: float
    tier: str
    resolver: str

@dataclass
class ImportEdge:
    """An import statement from one file to another or external package."""
    source_file: str       # Absolute path of the file doing the importing
    target_module_name: str # Name of the module/package imported (e.g., 'os', 'numpy', './utils')
    import_type: str       # 'direct', 'relative', or 'from_package'

@dataclass
class InheritanceEdge:
    """Class inheritance or interface implementation."""
    subclass_uid: str      # UID of the subclass/implementer
    superclass_name: str   # Unresolved name of the superclass/interface
    is_interface: bool     # True if superclass is an interface (TS/Java) vs. a class
```

`SymbolMetadata` also carries extraction-time AST markers used by Pass 1:

| Field | Meaning |
|---|---|
| `returns_function_expression` | Top-level return yields a function expression; higher-order factory signal |
| `returns_mapping` | Top-level return yields a mapping shape |
| `returns_sequence` | Top-level return yields a sequence shape |
| `returns_constructed_type` | Top-level return yields a capitalized constructed call result |

These markers are monotone booleans: multiple returns OR together. They are
shape facts, not dataflow; Pass-1 predicates in [role_predicates.md](role_predicates.md)
read them — they should not imply that the engine knows where each returned value came from.

## 3. Adapter Registry & Discovery

### 3.1 `LanguageAdapterRegistry` — Central Catalog

```python
class LanguageAdapterRegistry:
    """Singleton registry of available language adapters."""
    
    def __init__(self):
        self._adapters: dict[str, LanguageAdapter] = {}
        self._ext_to_lang: dict[str, str] = {}
    
    def register(self, adapter: LanguageAdapter) -> None:
        """Register an adapter instance."""
        lang = adapter.language_name
        if lang in self._adapters:
            raise ValueError(f"Adapter for {lang!r} already registered")
        self._adapters[lang] = adapter
        for ext in adapter.file_extensions:
            if ext in self._ext_to_lang:
                raise ValueError(f"Extension {ext!r} already mapped to {self._ext_to_lang[ext]!r}")
            self._ext_to_lang[ext] = lang
    
    def get_adapter(self, language: str) -> LanguageAdapter:
        """Fetch adapter by language name."""
        if language not in self._adapters:
            raise ValueError(f"No adapter registered for language: {language!r}")
        return self._adapters[language]
    
    def detect_language(self, file_path: str) -> str:
        """Auto-detect language from file extension."""
        ext = os.path.splitext(file_path)[1].lower()
        if ext not in self._ext_to_lang:
            raise ValueError(f"Unknown file extension: {ext!r}")
        return self._ext_to_lang[ext]
    
    def supported_languages(self) -> list[str]:
        """Return list of registered language names."""
        return sorted(self._adapters.keys())
```

### 3.2 Bootstrap — Auto-discover Adapters

On context_engine startup, auto-load all adapters from `context_engine/parser/adapters/`:

```python
def bootstrap_adapters() -> LanguageAdapterRegistry:
    """Load all adapters from the adapters/ directory."""
    registry = LanguageAdapterRegistry()
    adapters_dir = Path(__file__).parent / "adapters"
    
    for module_file in adapters_dir.glob("*_adapter.py"):
        module_name = module_file.stem  # e.g., "python_adapter"
        try:
            mod = importlib.import_module(f"context_engine.parser.adapters.{module_name}")
            # Adapters export a `make_adapter()` factory function
            adapter = mod.make_adapter()
            registry.register(adapter)
        except Exception as e:
            logger.warning(f"Failed to load adapter {module_name}: {e}")
    
    return registry

# Global singleton
ADAPTER_REGISTRY = bootstrap_adapters()
```

## 4. Refactored `SymbolExtractor`

### 4.1 New Design

```python
class SymbolExtractor:
    """Multi-language symbol and call-graph extractor."""
    
    def __init__(self, language: Optional[str] = None, registry: LanguageAdapterRegistry = ADAPTER_REGISTRY):
        self.language = language
        self.registry = registry
    
    def extract(self, file_path: str) -> List[SymbolMetadata]:
        """Extract symbols from file."""
        with open(file_path, 'r', encoding='utf-8') as f:
            source_code = f.read()
        return self.extract_from_source(source_code, file_path)
    
    def extract_from_source(self, source_code: str, file_path: str) -> List[SymbolMetadata]:
        """Extract symbols from in-memory source (supports dirty overlay)."""
        language = self.language or self.registry.detect_language(file_path)
        adapter = self.registry.get_adapter(language)
        return adapter.extract_symbols(source_code, file_path)
    
    def extract_calls(self, file_path: str) -> List[CallEdge]:
        """Extract call edges from file."""
        with open(file_path, 'r', encoding='utf-8') as f:
            source_code = f.read()
        language = self.language or self.registry.detect_language(file_path)
        adapter = self.registry.get_adapter(language)
        return adapter.extract_calls(source_code, file_path)
    
    def extract_imports(self, file_path: str) -> List[ImportEdge]:
        """Extract import edges from file (Phase 3.5+)."""
        with open(file_path, 'r', encoding='utf-8') as f:
            source_code = f.read()
        language = self.language or self.registry.detect_language(file_path)
        adapter = self.registry.get_adapter(language)
        return adapter.extract_imports(source_code, file_path)
```

## 5. Example: Python Adapter

### 5.1 File: `context_engine/parser/adapters/python_adapter.py`

```python
import tree_sitter_languages
from hashlib import sha256
from context_engine.parser.protocol import LanguageAdapter, SymbolMetadata, CallEdge

class PythonAdapter(LanguageAdapter):
    """Python language adapter using tree-sitter."""
    
    SYMBOL_QUERY = """
        (function_definition name: (identifier) @func.name) @func.def
        (class_definition name: (identifier) @class.name) @class.def
        (module (expression_statement (assignment left: (identifier) @var.name) @var.def))
    """
    
    CALL_QUERY = "(call function: (identifier) @call.name) @call.occured"
    
    def __init__(self):
        self.parser = tree_sitter_languages.get_parser("python")
        self.language = tree_sitter_languages.get_language("python")
    
    @property
    def language_name(self) -> str:
        return "python"
    
    @property
    def file_extensions(self) -> set[str]:
        return {".py", ".pyi"}
    
    def extract_symbols(self, source_code: str, file_path: str) -> list[SymbolMetadata]:
        """Extract functions, classes, and module-level constants."""
        tree = self.parser.parse(bytes(source_code, "utf8"))
        query = self.language.query(self.SYMBOL_QUERY)
        captures = query.captures(tree.root_node)
        
        var_names = {}
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
                    kind="function" if tag == "func.def" else "class",
                    start_line=node.start_point[0] + 1,
                    end_line=node.end_point[0] + 1,
                    content_hash=self._hash(content),
                    file_path=file_path,
                ))
        
        return symbols
    
    def extract_calls(self, source_code: str, file_path: str) -> list[CallEdge]:
        """Extract function calls."""
        tree = self.parser.parse(bytes(source_code, "utf8"))
        query = self.language.query(self.CALL_QUERY)
        captures = query.captures(tree.root_node)
        
        # Build a map of symbol names → UIDs for this file (for matching callers)
        symbols = self.extract_symbols(source_code, file_path)
        symbol_names = {s.name: s.uid for s in symbols}
        
        edges = []
        for node, tag in captures:
            if tag == 'call.name':
                callee_name = node.text.decode('utf-8')
                # Walk up to find enclosing function/class
                parent = node.parent
                while parent and parent.type not in ('function_definition', 'class_definition'):
                    parent = parent.parent
                
                if parent and parent.type in ('function_definition', 'class_definition'):
                    name_node = parent.child_by_field_name('name')
                    caller_name = name_node.text.decode('utf-8') if name_node else None
                    if caller_name:
                        caller_uid = symbol_names.get(caller_name)
                        if caller_uid:
                            edges.append(CallEdge(caller_uid, callee_name, node.start_point[0] + 1))
        
        return edges
    
    def _uid(self, file_path: str, name: str) -> str:
        return sha256(f"{file_path}:{name}".encode()).hexdigest()
    
    def _hash(self, code: str) -> str:
        return sha256(code.encode()).hexdigest()

def make_adapter() -> LanguageAdapter:
    """Factory function for adapter discovery."""
    return PythonAdapter()
```

## 6. Adding a New Language (Walkthrough)

### 6.1 Go Example

1. **Create adapter file** `context_engine/parser/adapters/go_adapter.py`:

```python
from context_engine.parser.adapters.treesitter_base import TreeSitterAdapter


class GoAdapter(TreeSitterAdapter):
    
    @property
    def language_name(self) -> str:
        return "go"
    
    @property
    def file_extensions(self) -> set[str]:
        return {".go"}

    @property
    def ts_language_name(self) -> str:
        return "go"

    @property
    def symbol_query(self) -> str:
        return """
            (function_declaration name: (identifier) @func.name) @func.def
            (type_declaration (type_spec name: (type_identifier) @type.name) @type.def)
        """

    @property
    def call_query(self) -> str:
        return "(call_expression function: (identifier) @call.name) @call.occurrence"

    @property
    def parent_types(self) -> set[str]:
        return {"function_declaration", "method_declaration"}


def make_adapter() -> GoAdapter:
    return GoAdapter()
```

2. On next context_engine boot, `bootstrap_adapters()` auto-discovers and registers the Go adapter. Its extensions become eligible for baseline collection and the generic symbol/call/import/inheritance handoff without editing the registry or collector.

3. **Add a test** in `tests/unit/test_go_adapter.py`:

```python
def test_go_extract_symbols():
    adapter = GoAdapter()
    source = """
    func main() {
        println("hello")
    }
    """
    symbols = adapter.extract_symbols(source, "main.go")
    assert len(symbols) == 1
    assert symbols[0].name == "main"
```

4. For production parity, add `extract_axis_facts()` coverage and review the
   language-specific phases in `context_engine/indexer/fast/pipeline.py`. HTTP,
   proxy, framework, package/re-export, and other enrichment bridges do not
   become Go-aware merely because the adapter is registered.

## 7. Implementation Status

### Phase 1 ✅ Complete

Adapter protocol and registry fully implemented:
1. ✅ `context_engine/parser/protocol.py` with `LanguageAdapter` ABC and data classes.
2. ✅ `context_engine/parser/adapters/` — `python_adapter.py`, `typescript_adapter.py`, `javascript_adapter.py` (auto-discovered via `make_adapter()`).
3. ✅ `SymbolExtractor` refactored to use registry and auto-detect language.
4. ✅ Unit + integration tests for adapter loading and language detection (`tests/integration/test_adapter_registry.py`, per-adapter unit tests).

### Phase 3.5 ✅ Complete

Graph Completeness extends adapters:
1. ✅ `extract_imports()` implemented in Python (text-based) and TypeScript/JavaScript (regex/tree-sitter) adapters.
2. ✅ `extract_inheritance()` implemented in Python and TypeScript adapters for class/interface hierarchies.
3. ✅ Indexer creates `IMPORTS` (File→File) and `DEPENDS_ON` (Symbol→Symbol) edges from adapter output.
4. ✅ Axis graph walks and Pass-1 fan profiles consume those dependency edges (replacing the deleted cascade BFS).
5. ✅ Integration tests verify import/inheritance extraction (`tests/integration/test_graph_completeness.py`).

### Phase 3.6 ✅ Complete — TypeScript `object_api` surfaces

The TypeScript adapter collapses `export const Foo = { ... }` client objects into a single symbol:

- **kind:** `object_api`
- **signature_status:** `object_api_export`
- nested method symbols inside the object literal are suppressed to reduce graph noise
- HTTP calls inside the object (`post('/ask')`, `fetch('/health')`) are attributed to the enclosing `object_api` symbol

Cross-language trace (for example extension `SidecarClient` → context_engine `/ask` handler) is **not** wired today — regex `SEMANTIC_HINT` hints were removed; revisit when TS indexing emits structural route/call edges.

### Phase 5+

Add new language adapters (Go, Rust, Java, etc.) as demand grows. Each is a single file.

## 8. Testing Strategy

### 8.1 Unit Tests — Per Adapter

File: `tests/unit/test_*_adapter.py`

```python
class TestPythonAdapter:
    def test_extract_functions(self):
        adapter = PythonAdapter()
        source = "def foo(): pass"
        symbols = adapter.extract_symbols(source, "test.py")
        assert len(symbols) == 1
        assert symbols[0].name == "foo"
    
    def test_extract_classes(self):
        # ...
    
    def test_extract_calls_within_class(self):
        # ...
    
    def test_extract_calls_unresolved(self):
        # ... calling external functions
```

### 8.2 Integration Tests — Registry

File: `tests/integration/test_adapter_registry.py`

```python
def test_registry_loads_all_adapters():
    registry = bootstrap_adapters()
    assert "python" in registry.supported_languages()
    assert "typescript" in registry.supported_languages()

def test_detect_language_by_extension():
    registry = bootstrap_adapters()
    assert registry.detect_language("foo.py") == "python"
    assert registry.detect_language("bar.ts") == "typescript"
```

## 9. Non-Goals

- **Not** a full AST walker. Adapters use tree-sitter queries; they don't build full control-flow graphs.
- **Not** type-aware. Call resolution happens later in Neo4j matching by name.
- **Not** thread-safe. Adapters are instantiated once per language; no parallel parsing inside an adapter.

## 10. Related

- [spec_parser.md](spec_parser.md) — parser module layout and indexer integration
- [spec_indexer.md](spec_indexer.md) — file collection uses registry extensions
- [architectura.md](architectura.md) — `IMPORTS` / `DEPENDS_ON` in the graph schema
- [road_map.md](road_map.md) — Phase 1 polish, Phase 3.5 extension
