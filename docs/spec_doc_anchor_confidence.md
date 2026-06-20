# Spec — DocAnchor Confidence & Type (Phase 9)

> **Status:** Implemented in `context_engine/indexer/anchor.py` and consumed by the context ranker (axis; the legacy `UnifiedRanker` consumer was removed 2026-06-15). Existing flat `COVERS` edges remain readable through fallback defaults; newly indexed docs write `anchor_type`, `confidence`, `primary_bias`, and `resolver`.

## 1. Problem

Before Phase 9.3, `(DocAnchor)-[:COVERS]->(Symbol)` was a boolean link: either the chunk covered the symbol or it did not. Retrieval consequences:

- A spec chapter titled "**Definition** of process_payment" is weighted identically to a spec that mentions `process_payment` as **an example** of what *not* to do.
- A chunk that covers 5 symbols contributes equally to retrieval for each — even though one of them is the main topic and the others are tangential.
- A `COVERS` edge resolved by 0.51 cosine similarity is indistinguishable from one resolved by 0.95. Noise floor equals signal ceiling.

Retrieval that can't rank its own anchors is retrieval that plateaus at "pretty good."

## 2. Design

### 2.1 Anchor Type Taxonomy

Each `COVERS` edge carries an `anchor_type`:

| Type | Meaning | Default Weight |
|---|---|---|
| `definition` | Chunk defines, documents, or specifies this symbol | 1.0 |
| `warning` | Chunk describes caveats, bugs, or anti-patterns for this symbol | 0.95 |
| `deprecated` | Chunk marks the symbol as deprecated or migration-related | 0.85 |
| `reference` | Chunk mentions the symbol name without elaborating | 0.65 |
| `example` | Chunk uses the symbol as an example / in a code block | 0.45 |

Classification rules (heuristic v1):

- `deprecated`: chunk contains deprecated / migration / removed / renamed language.
- `warning`: chunk contains warning-family markers such as "warning", "caution", "danger", "security", or "important".
- `example`: chunk contains a fenced code block or lives under tutorial/example paths.
- `definition`: chunk lives under reference docs or contains API-ish headings/terms such as parameters, returns, class, function, method, signature.
- `reference`: fallback for prose mentions.

Classifier is deterministic; output recorded as an edge property. Re-classification only required when the chunk or symbol changes.

### 2.2 Confidence Score

Each `COVERS` edge carries a `confidence: float` in `[0, 1]`:

The implemented v1 score is deterministic and intentionally cheap:

- resolver base: direct identifier links start higher than pending identifier links, which start higher than semantic-only links.
- exact symbol name mention boosts confidence.
- heading mention boosts confidence.
- code-style mention such as backticks or call syntax boosts confidence.
- definition / warning / deprecated chunks get a small lift; example chunks get a small penalty.

Anchor classification and confidence are computed in the same pass to avoid a second read.

### 2.3 Multi-Symbol Weighting

When a chunk covers N symbols, the naive current behavior is to emit N identical edges. Replacement: distribute a **primary score** across the symbols, with the most-probably-focal symbol getting the bulk:

```
primary_symbol = argmax_over_symbols(
    confidence(chunk, symbol)
)

for each symbol s:
    edge_weight = confidence(chunk, s) * primary_bias(s)

where primary_bias(primary_symbol) = 1.0
      primary_bias(other)          = 0.6
```

Effect: a chunk that mostly documents `process_payment` but also references `validate_amount` now has a strong edge to the former and a weaker edge to the latter. Retrieval that pulls this chunk via `process_payment` gets the chunk at full strength; retrieval via `validate_amount` gets it with 60% weight.

### 2.4 Edge Schema

```cypher
(a:DocAnchor {chunk_id})-[r:COVERS {
    anchor_type: "definition",
    confidence: 0.82,
    primary_bias: 1.0,
    resolver: "identifier"
}]->(s:Symbol)
```

`resolver` tag lets us diff classifier versions without rebuilding the whole graph.

### 2.5 Retrieval Impact

Unified ranker (spec_unified_ranking.md (removed)) consumes these fields:

- Doc candidate graph boost is derived from `primary_bias * confidence * anchor_type_weight`, blended with the linked symbol's graph score.
- Doc bridge candidates include anchor quality in provenance (`doc-bridge:h1,strength=...,anchor_q=...`).
- Prompt contract entries expose `documentation[].anchor_type`, `documentation[].anchor_confidence`, `documentation[].primary_bias`, and a nested `documentation[].anchor` object.

## 3. API / Interface

Implemented helper surface:

- `_classify_anchor_type(chunk_text, file_path)`
- `_anchor_confidence(chunk_text, file_path, symbol_name, resolver, semantic_score=0.0)`
- `_cover_link(uid, symbol_name, chunk_text, file_path, resolver, semantic_score, link_count)`

Indexer pipeline inserts a classification step between vector lookup and edge emission:

```
chunk → top-K symbols via vector search
      → classify each (type, confidence, primary_bias)
      → emit COVERS edges with properties
```

## 4. Examples

```python
chunk = DocChunk(
    source_file="docs/spec_payments.md",
    chunk_id="spec_payments#process-payment",
    content="## `process_payment` — Definition\n\nAccepts `amount: float`...\n"
            "Internally calls `validate_amount` to enforce business rules.\n"
)

# Primary symbol — heading match, name in code fence, high similarity
c1 = _cover_link(
    process_payment.uid,
    "process_payment",
    chunk.content,
    chunk.source_file,
    resolver="identifier",
    link_count=2,
)
# {"anchor_type": "definition", "confidence": 0.9+, "primary_bias": 0.9+}

# Secondary symbol — prose mention only
c2 = _cover_link(
    validate_amount.uid,
    "validate_amount",
    chunk.content,
    chunk.source_file,
    resolver="semantic",
    semantic_score=0.42,
    link_count=2,
)
# {"anchor_type": "definition", "confidence": 0.6-ish, "primary_bias": 0.65}

# BFS retrieval for process_payment sees c1 at full weight.
# BFS retrieval for validate_amount sees c2 de-emphasized.
```

## 5. Limitations (current)

- Heuristic classifier is English-oriented — markers like "Warning:" don't fire in other languages. Mitigation: accept locale-specific keyword lists as config.
- `primary_bias` is currently a simple focal/secondary heuristic (`1.0` for single-symbol chunks, lower for multi-symbol chunks, heading mentions lifted). Softer distributions (softmax over confidences) possible; defer until harness shows it matters.
- Anchor type doesn't distinguish between "tutorial example" and "regression example" — both collapse to `example`. Finer taxonomy is Planned.

## 6. Planned Extensions

- **Reverse anchors:** code-to-doc edges (`DOCUMENTED_BY`) for "find specs that cover this symbol" queries; symmetric to current `COVERS`.
- **LLM-assisted classification:** for ambiguous chunks (e.g. "I re-implemented `process_payment`"), defer to a small LLM call. Only when heuristic confidence is weak.

## 7. Related

- [spec_doc_anchor.md](spec_doc_anchor.md) — the DocAnchor node and current `COVERS` edge this extends.
- [spec_doc_indexer.md](spec_doc_indexer.md) — chunking pipeline upstream of classification.
- spec_unified_ranking.md (removed) — consumer of `confidence`, `anchor_type`, `primary_bias`.
- [spec_prompt_contract_observability.md](spec_prompt_contract_observability.md) — surfaces `anchor_type` / `anchor_confidence` in the contract.
