# Context Arbitrator — Spec

## Overview

`sidecar/arbitrator.py` — assembles the LLM prompt for a given symbol name. Handles dirty-state (unsaved file) via In-Memory Overlay. Does not call the LLM directly.

---

## Class: ContextArbitrator

```python
ContextArbitrator(neo4j_client: Neo4jClient, overlay: InMemoryOverlay | None = None)
```

### get_context_for_symbol(symbol_name: str) → PromptContext | str

Returns a `PromptContext` dataclass, or `"Error: Symbol '...' not found in graph."` string if the symbol does not exist. Callers check `isinstance(result, str)` to detect errors.

**Steps:**

1. **Graph query** — Cypher `MATCH (s:Symbol {name: $name}) OPTIONAL MATCH (s)-[:CALLS]->(dep:Symbol)` — fetches target node and all direct `[:CALLS]` dependencies in one round-trip.

2. **Code assembly** — for target + each dependency, calls `_build_symbol_context(node, relation)`.

3. **Returns** `PromptContext(primary_source=SymbolContext, graph_context=[SymbolContext, ...])`. The `documentation` list is empty — callers populate it from LanceDB after this call.

### PromptContext

```python
@dataclass
class PromptContext:
    primary_source: SymbolContext
    graph_context: list[SymbolContext]
    documentation: list[DocChunk]

    def to_system_prompt(self) -> str   # flat text for LLM system message
    def to_dict(self) -> dict           # JSON Prompt Contract shape
```

### SymbolContext

```python
@dataclass
class SymbolContext:
    symbol: str
    file_path: str
    relation: str      # "target" | "CALLS"
    is_dirty: bool
    code: str
```

### DocChunk

```python
@dataclass
class DocChunk:
    source_file: str
    chunk_id: str
    content: str
```

### _build_symbol_context(symbol_node, relation) → SymbolContext

Resolves file path via Cypher `MATCH (f:File)-[:CONTAINS]->(s:Symbol {uid}) RETURN f.path`.  
Reads `symbol_node['range']` = `[start_line, end_line]`.

**Overlay check:** if `overlay` is set and `overlay.has(file_path)` is true, reads from `InMemoryOverlay.read_lines()` instead of disk. Sets `is_dirty=True` accordingly.

---

## Overlay Priority Rule

| State | Source |
|---|---|
| File is dirty (in overlay) | `InMemoryOverlay` |
| File is clean | Local FS via `open()` |

The graph always provides the line coordinates (`range`). Only the code text source switches.

---

## Limitations (current)

- BFS depth is fixed at 1 — only direct `[:CALLS]` edges are followed.
- Symbol lookup is by `name` — if two symbols share a name across different files, both are returned and both code blocks are included.
- No deduplication of dependencies.

---

## Planned Extensions

- Configurable BFS depth parameter
- Deduplicate by `uid`
- Include `[:COVERS]` DocAnchor chunks in assembled context (currently appended separately in `/ask`)
- ~~Return structured JSON prompt object~~ ✅ Done — `PromptContext.to_dict()` implements the contract
