# Spec — DocAnchor Confidence & Type (Phase 9)

> **Status:** Proposed. Extends [spec_doc_anchor.md](spec_doc_anchor.md) with confidence scores, anchor type classification, and multi-symbol weighting. Current anchors are flat — every `COVERS` edge is treated as equally authoritative.

## 1. Problem

`(DocAnchor)-[:COVERS]->(Symbol)` is a boolean link: either the chunk covers the symbol or it doesn't. Retrieval consequences:

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
| `example` | Chunk uses the symbol as an example / in a code block | 0.7 |
| `reference` | Chunk mentions the symbol name without elaborating | 0.4 |
| `warning` | Chunk describes caveats, bugs, or anti-patterns for this symbol | 0.9 |
| `deprecated` | Chunk marks the symbol as deprecated | 0.6 |

Classification rules (heuristic v1):

- `definition`: chunk title matches symbol name OR chunk is the first section where the symbol appears in a spec-tagged doc.
- `example`: symbol appears inside a fenced code block AND is not in a heading.
- `reference`: symbol appears in prose only, not in headings or code blocks.
- `warning`: chunk contains a warning-family marker (`⚠️`, "Warning:", "Deprecated:", "Known issue").
- `deprecated`: chunk contains "deprecated" as a heading or leading keyword.

Classifier is deterministic; output recorded as an edge property. Re-classification only required when the chunk or symbol changes.

### 2.2 Confidence Score

Each `COVERS` edge carries a `confidence: float` in `[0, 1]`:

```
confidence = 0.4 * similarity_score_normalized
           + 0.3 * name_mention_score
           + 0.2 * heading_match_score
           + 0.1 * code_block_match_score
```

Components:

- `similarity_score_normalized`: cosine similarity between chunk embedding and symbol body embedding, mapped from `[SIMILARITY_THRESHOLD, 2.0]` → `[0, 1]`.
- `name_mention_score`: frequency of the symbol's name in the chunk, log-scaled and capped. `0.0` if never mentioned; `1.0` if mentioned ≥ 5 times.
- `heading_match_score`: `1.0` if the symbol name appears in any chunk heading, else `0.0`.
- `code_block_match_score`: `1.0` if the symbol name appears inside a fenced code block, else `0.0`.

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
    similarity: 1.73,
    resolver: "anchor-v2"
}]->(s:Symbol)
```

`resolver` tag lets us diff classifier versions without rebuilding the whole graph.

### 2.5 Retrieval Impact

Unified ranker ([spec_unified_ranking.md](spec_unified_ranking.md)) consumes these fields:

- Doc candidate `graph_score` = `primary_bias * confidence * anchor_type_weight`.
- Overlap bonus only fires when `anchor_type in {definition, warning}` — example/reference overlaps are noisier and shouldn't get the double-signal boost.

## 3. API / Interface

```python
# sidecar/indexer/anchor.py — extended

@dataclass
class AnchorClassification:
    anchor_type: str         # "definition" | "example" | "reference" | "warning" | "deprecated"
    confidence: float        # 0.0 – 1.0
    primary_bias: float      # 1.0 for primary symbol, 0.6 for others

class AnchorClassifier:
    def classify(
        self,
        chunk: DocChunk,
        symbol: Symbol,
        similarity: float,
    ) -> AnchorClassification: ...

    def classify_batch(
        self,
        chunk: DocChunk,
        candidate_symbols: list[Symbol],
    ) -> list[AnchorClassification]:
        """Runs single-symbol classify and applies primary_bias distribution."""
```

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

classifier = AnchorClassifier()

# Primary symbol — heading match, name in code fence, high similarity
c1 = classifier.classify(chunk, symbol=process_payment, similarity=1.84)
# AnchorClassification(anchor_type="definition", confidence=0.94, primary_bias=1.0)

# Secondary symbol — prose mention only
c2 = classifier.classify(chunk, symbol=validate_amount, similarity=1.52)
# AnchorClassification(anchor_type="reference", confidence=0.42, primary_bias=0.6)

# BFS retrieval for process_payment sees c1 at full weight.
# BFS retrieval for validate_amount sees c2 de-emphasized.
```

## 5. Limitations (current)

- Heuristic classifier is English-oriented — markers like "Warning:" don't fire in other languages. Mitigation: accept locale-specific keyword lists as config.
- `primary_bias` is a hard 1.0 / 0.6 split. Softer distributions (softmax over confidences) possible; defer until harness shows it matters.
- Anchor type doesn't distinguish between "tutorial example" and "regression example" — both collapse to `example`. Finer taxonomy is Planned.

## 6. Planned Extensions

- **Temporal decay:** deprecated anchors get confidence decay over time — older `deprecated` is stronger signal than newer.
- **Reverse anchors:** code-to-doc edges (`DOCUMENTED_BY`) for "find specs that cover this symbol" queries; symmetric to current `COVERS`.
- **LLM-assisted classification:** for ambiguous chunks (e.g. "I re-implemented `process_payment`"), defer to a small LLM call. Only when heuristic confidence < 0.3.

## 7. Related

- [spec_doc_anchor.md](spec_doc_anchor.md) — the DocAnchor node and current `COVERS` edge this extends.
- [spec_doc_indexer.md](spec_doc_indexer.md) — chunking pipeline upstream of classification.
- [spec_unified_ranking.md](spec_unified_ranking.md) — consumer of `confidence`, `anchor_type`, `primary_bias`.
- [spec_prompt_contract_observability.md](spec_prompt_contract_observability.md) — surfaces `anchor_type` / `anchor_confidence` in the contract.
