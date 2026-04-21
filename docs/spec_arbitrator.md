# Context Arbitrator — Spec

## Overview

`sidecar/context/arbitrator.py` assembles the JSON Prompt Contract for a target symbol. It is an orchestrator only: it does graph expansion, code/doc resolution, deduplication, and prompt compilation, but it does not call the LLM.

---

## Class: ContextArbitrator

```python
ContextArbitrator(neo4j_client, overlay: InMemoryOverlay | None = None, vector_db=None)
```

### get_context_for_symbol(symbol_name, question="", token_budget=4000) -> PromptContext | str

Returns a `PromptContext`, or an error string like `Error: Symbol '...' not found in graph.` Callers translate that string into endpoint-level errors.

**Pipeline:**

1. **Intent classification** — `IntentClassifier.classify_intent(question)` selects the retrieval priority profile.
2. **Graph expansion** — `GraphExpander.expand(symbol_name, token_budget)` performs priority-queue BFS over typed symbol edges and returns a `Subgraph` with budget metadata.
3. **Deduplication** — `ContextDeduplicator.deduplicate(subgraph)` removes redundant graph nodes before code is read.
4. **Code resolution** — `CodeResolver(overlay).resolve(file_path, start, end)` reads dirty overlay content first, then falls back to disk.
5. **Doc resolution** — if `vector_db` is present, `DocResolver(vector_db).search(f"{symbol_name} {question}", limit=3)` returns LanceDB doc chunks before compilation.
6. **Prompt compilation** — `PromptCompiler.compile_with_intent(...)` builds `PromptContext` with intent-aware doc tier selection.

---

## Data Objects

### PromptContext

```python
@dataclass
class PromptContext:
    primary_source: SymbolContext
    graph_context: list[SymbolContext]
    documentation: list[DocChunk]
    budget: dict
    mode: str
    intent: str
    tier_tokens: dict

    def to_system_prompt(self) -> str
    def to_dict(self) -> dict
    def token_count(self) -> int
```

`to_dict()` is the API-facing JSON Prompt Contract. It includes `mode`, `intent`, `metadata.query_intent`, `metadata.tiers_used`, `metadata.tier_tokens`, selected code/doc chunks, and graph budget fields.

### SymbolContext

```python
@dataclass
class SymbolContext:
    symbol: str
    file_path: str
    relation: str
    direction: str
    depth: int
    relevance_score: float
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

---

## Overlay Priority Rule

| State | Source |
|---|---|
| File is dirty | `InMemoryOverlay` |
| File is clean | Local filesystem |

The graph supplies symbol file paths and line ranges. Only the source of text changes when a dirty overlay exists.

---

## Current Limitations

- The initial symbol lookup still accepts a display `Symbol.name`; workspace-scoped lookup prevents cross-branch leakage, but exact UID target selection is still a product/API improvement.
- Workspace identity is applied to graph queries, but not yet surfaced in the JSON Prompt Contract.
- Doc chunks do not yet expose retrieval scores, provenance details, or pruning reasons in `to_dict()`.
- Backpressure and batching for mass file-change events live outside the arbitrator and remain roadmap work.

---

## Planned Extensions

- Stable symbol identity and scoped target lookup.
- Workspace/branch-aware graph expansion.
- Prompt-contract observability: scores, provenance, pruning reasons, resolver version, model route, and trace ID.
- Unified ranking across graph nodes and semantic doc chunks.
