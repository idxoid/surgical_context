# Spec: Parser Module (`context_engine/parser/`)

> **Status:** Refactored to ADR-005 plugin architecture (Phase 1 polish complete). See [spec_language_adapter.md](spec_language_adapter.md) for protocol details.

## 1. Responsibility

The parser module is the ETL entry point. It converts raw source files into structured `SymbolMetadata` objects and call-graph edges that the indexer writes into Neo4j.

It has no knowledge of Neo4j, LanceDB, or the sidecar API. It takes a file path and returns data structures.

---

## 2. Architecture — Plugin-Based (ADR-005)

### Module Structure

```
context_engine/parser/
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
| `uid` | `str` | Stable UID v2: `sha256(language:qualified_name|normalized_signature)[:16]` |
| `name` | `str` | Identifier as it appears in source |
| `kind` | `str` | `"function"` \| `"class"` \| `"variable"` |
| `start_line` | `int` | 1-based start line |
| `end_line` | `int` | 1-based end line (inclusive) |
| `content_hash` | `str` | `sha256(full node text)` — used to detect changes without reading code |
| `file_path` | `str` | Absolute path to source file |
| `qualified_name` | `str` | Module/scope-qualified symbol path |
| `signature` | `str` | Normalized signature with parameter names/defaults stripped |
| `signature_hash` | `str` | Compact hash of the normalized signature |
| `signature_status` | `str` | `"resolved"` or `"unresolved"` |
| `language` | `str` | Adapter language name |
| `returns_function_expression` | `bool` | Function body has a top-level return of a function expression; used for higher-order factory roles |
| `returns_mapping` | `bool` | Function body has a top-level return of a mapping shape (`{...}`, `dict(...)`, dict comprehension) |
| `returns_sequence` | `bool` | Function body has a top-level return of a sequence shape (`[...]`, `list(...)`, tuple, set/comprehension) |
| `returns_constructed_type` | `bool` | Function body has a top-level return of a constructed type (`SomeType(...)`) |

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
    # Returns call records with resolver metadata:
    # caller_uid, callee_name, rel_type, confidence, tier, resolver,
    # and optionally callee_uid or callee_qualified_name.
```

```python
def extract_calls_from_source(self, source_code: str, file_path: str) -> List[dict]
    # Parse in-memory source, extract call edges
    # Returns same format as extract_calls()
    # Delegates to adapter.extract_calls_from_source()
```

---

## 4. Registry — Dynamic Adapter Loading

### `LanguageAdapterRegistry` (singleton at `context_engine.parser.registry.REGISTRY`)

Auto-discovers adapters from `context_engine/parser/adapters/*_adapter.py`:

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

Adapters may also expose language-specific extraction hooks consumed by the indexer:
imports, inheritance, decorators, type references, re-exports, instantiations, DI
bindings, proxy bindings, and return-shape markers. The Python adapter currently
populates the return-shape booleans during `extract_symbols()` from top-level
`return` statements only; it deliberately ignores nested functions/classes so an
inner helper does not paint the outer function as returning a mapping/sequence.

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

Create one file: `context_engine/parser/adapters/go_adapter.py`

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

- **Python call resolution is staged, not complete.** Direct/scoped/imported/self,
  typed collaborator, proxy, re-export, instantiation, and injection facts exist,
  but dynamic dispatch through dict lookups, unannotated runtime proxies, and
  cross-function value flow remain out of scope.
- **Return-shape markers are AST shape only.** `returns_mapping`,
  `returns_sequence`, and `returns_constructed_type` say what a function returns,
  not where the returned values came from. They are a foundation for binding/data
  roles, not full dataflow.
- **TypeScript and JavaScript have partial parity.** TS/JS adapters expose many
  structural edges, but Python has the deepest typed collaborator and return-shape
  path today.
- **Single-file hot path is intentionally smaller.** Full repository passes run
  role taxonomy and global enrichment phases; single-file indexing does not rebuild
  every project-level derived feature.

Future phases should add bounded field reads/writes, callable-as-value edges, and
data-shape propagation before promoting the remaining binding/dataflow roles.

---

## 10. Related

- [spec_language_adapter.md](spec_language_adapter.md) — detailed protocol spec (ADR-005)
- [spec_indexer.md](spec_indexer.md) — how parser integrates with indexing pipeline
- [road_map.md](road_map.md) — Phase 1 completion status
