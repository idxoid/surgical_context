# Spec: Parser Module (`sidecar/parser/`)

> **Status:** Refactored to ADR-005 plugin architecture (Phase 1 polish complete). See [spec_language_adapter.md](spec_language_adapter.md) for protocol details.

## 1. Responsibility

The parser module is the ETL entry point. It converts raw source files into structured `SymbolMetadata` objects and call-graph edges that the indexer writes into Neo4j.

It has no knowledge of Neo4j, LanceDB, or the sidecar API. It takes a file path and returns data structures.

---

## 2. Architecture — Plugin-Based (ADR-005)

### Module Structure

```
sidecar/parser/
  ├── protocol.py           # LanguageAdapter ABC + SymbolMetadata
  ├── registry.py           # LanguageAdapterRegistry (singleton)
  ├── extractor.py          # SymbolExtractor (thin dispatcher)
  └── adapters/             # language plugins
      ├── treesitter_base.py
      ├── python_adapter.py
      └── typescript_adapter.py
```

### `SymbolMetadata` (Pydantic model)

| Field | Type | Description |
|---|---|---|
| `uid` | `str` | `sha256(file_path:name)` — deterministic, stable across re-indexing |
| `name` | `str` | Identifier as it appears in source |
| `kind` | `str` | `"function"` \| `"class"` \| `"variable"` |
| `start_line` | `int` | 1-based start line |
| `end_line` | `int` | 1-based end line (inclusive) |
| `content_hash` | `str` | `sha256(full node text)` — used to detect changes without reading code |
| `file_path` | `str` | Absolute path to source file |

---

## 3. API — `SymbolExtractor`

**Signature:** `SymbolExtractor(language: Optional[str] = None)`
- If `language=None` (default) → auto-detect from file extension
- If `language="python"` → force that language (used in dirty overlay)

### Public Methods

```python
def extract(self, file_path: str) -> List[SymbolMetadata]
    # Read file from disk, parse, extract symbols
```

```python
def extract_from_source(self, source_code: str, file_path: str) -> List[SymbolMetadata]
    # Parse in-memory source (used by overlay for unsaved edits)
    # Delegates to adapter.extract_symbols()
```

```python
def extract_calls(self, file_path: str) -> List[dict]
    # Read file from disk, extract call edges
    # Returns: [{"caller_uid": str, "callee_name": str, "rel_type": str}, ...]
    # where rel_type ∈ {"CALLS_DIRECT", "CALLS_DYNAMIC", "CALLS_INFERRED"}
```

```python
def extract_calls_from_source(self, source_code: str, file_path: str) -> List[dict]
    # Parse in-memory source, extract call edges
    # Returns same format as extract_calls()
    # Delegates to adapter.extract_calls_from_source()
```

---

## 4. Registry — Dynamic Adapter Loading

### `LanguageAdapterRegistry` (singleton at `sidecar.parser.registry.REGISTRY`)

Auto-discovers adapters from `sidecar/parser/adapters/*_adapter.py`:

```python
def get_adapter(language: str) -> LanguageAdapter
def detect_language(file_path: str) -> str  # auto-detect from extension
def supported_languages() -> list[str]      # e.g., ["python", "typescript"]
def supported_adapters() -> list[LanguageAdapter]
```

---

## 5. How Adapters Work

Each language adapter implements `LanguageAdapter` protocol:

```python
@property
def language_name(self) -> str
    # "python", "typescript", etc.

@property
def file_extensions(self) -> set[str]
    # {".py", ".pyi"}, {".ts", ".tsx"}, etc.

def extract_symbols(self, source_code: str, file_path: str) -> List[SymbolMetadata]
    # Parse and return symbols

def extract_calls_from_source(self, source_code: str, file_path: str) -> List[dict]
    # Parse and return call edges with type classification
    # Returns: [{"caller_uid": str, "callee_name": str, "rel_type": str}, ...]
    # rel_type: "CALLS_DIRECT" (static/overload-safe), "CALLS_DYNAMIC" (dispatch), "CALLS_INFERRED" (string-based)
    # Default (not overridden): "CALLS_DIRECT"
```

### TreeSitterAdapter Base Class

For tree-sitter-based languages (Python, TypeScript, Go, Rust, etc.), subclass `TreeSitterAdapter`:

```python
class PythonAdapter(TreeSitterAdapter):
    @property
    def ts_language_name(self) -> str:
        return "python"
    
    @property
    def symbol_query(self) -> str:
        return """
            (function_definition name: (identifier) @func.name) @func.def
            (class_definition name: (identifier) @class.name) @class.def
            (module (expression_statement (assignment left: (identifier) @var.name) @var.def))
        """
    
    @property
    def call_query(self) -> str:
        return "(call function: (identifier) @call.name) @call.occured"
    
    @property
    def parent_types(self) -> set[str]:
        return {"function_definition", "class_definition"}
```

The base class handles all tree-sitter parsing; subclass just declares the queries.

---

## 6. Adding a New Language

Create one file: `sidecar/parser/adapters/go_adapter.py`

```python
from sidecar.parser.adapters.treesitter_base import TreeSitterAdapter

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
        return """..."""  # tree-sitter query for Go
    
    @property
    def call_query(self) -> str:
        return """..."""  # tree-sitter query for Go calls
    
    @property
    def parent_types(self) -> set[str]:
        return {"function_declaration", "method_declaration"}

def make_adapter() -> GoAdapter:
    return GoAdapter()
```

On next sidecar boot, the registry auto-discovers and registers it. No core edits needed. ✓

---

## 7. Current Adapters

| Language | Status | Extensions | Base |
|---|---|---|---|
| Python | ✅ | `.py`, `.pyi` | TreeSitterAdapter |
| TypeScript | ✅ | `.ts`, `.tsx` | TreeSitterAdapter |

---

## 8. Module-level Variable Capture ✅

Module-level `UPPER_CASE` assignments are captured as `kind="variable"` symbols. This enables DocAnchor lazy resolution for config dicts, constants, and registries that are referenced in spec docs before the code exists.

Filter: only names where `name.isupper()` are indexed — avoids noise from local variables.

---

## 9. Known Limitations

- **Shallow call detection.** Only resolves `(call function: (identifier))` — misses method calls (`obj.method()`), chained calls, and calls via attribute access.
- **No import resolution.** `callee_name` is matched globally by name in Neo4j. If two files define a function with the same name, edges will be incorrectly multi-matched.
- **No nested class/function scoping.** A method inside a class and a top-level function with the same name produce the same `uid` if they share a `file_path:name` — collision risk in files with method overloading.
- **File hash is raw bytes hex.** `indexer_main.py` reads the file as bytes and calls `.hex()` — not a proper SHA256 digest. Inconsistent with `content_hash` in `SymbolMetadata`.

Phase 3.5 will address limitations via `IMPORTS` and `DEPENDS_ON` edges (imported names, type usage).

---

## 10. Related

- [spec_language_adapter.md](spec_language_adapter.md) — detailed protocol spec (ADR-005)
- [spec_indexer.md](spec_indexer.md) — how parser integrates with indexing pipeline
- [road_map.md](road_map.md) — Phase 1 completion status
