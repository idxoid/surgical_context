# Documentation Style Guide

**Goal:** Write docs that developers can understand in 2 minutes. Prioritize clarity over completeness.

---

## 1. Audience & Tone

**Who reads these docs?**
- Engineers implementing features (need code patterns, interfaces, algorithms)
- Reviewers auditing changes (need to verify correctness)
- Future maintainers (need to understand design decisions)
- Users of the API (need examples and error cases)

**Tone:**
- Direct and precise — say what you mean, not what sounds good
- Active voice — "the indexer extracts symbols" not "symbols are extracted"
- Assume the reader is smart but not familiar with this specific project
- Avoid marketing language; include trade-offs and limitations

---

## 2. Document Types & Structure

### **Type A: Spec** (`spec_*.md`)

Describes the current implementation of a system component. Audience: developers building on it.

**Structure:**
```
# Component Name — Spec

## Overview
One paragraph: what it does, where the code is, how to call it.

## Design
Why does it work this way? What are the trade-offs?
(Optional if design is obvious from code)

## API / Interface
Functions/classes with signatures and docstrings.
Include parameter types and return values.

## Examples
Copy-paste-able code that works. Assume minimal context.

## Limitations (current)
What doesn't work. Bullet list.

## Planned Extensions
What's on the roadmap. Bullet list.
```

**Example opening:**
```
# Anchor Linker — Spec

`sidecar/indexer/anchor.py` — links documentation chunks to code symbols 
via DocAnchor nodes in Neo4j. Enables hybrid retrieval: semantic search 
finds a doc chunk → graph traversal finds the code it describes.

Entry point: `link_docs_to_symbols(neo4j, lance)`
```

---

### **Type B: Architecture** (`architectura.md`)

The big picture: how components fit together, data flow, major design decisions.

**Structure:**
```
# Architecture

## Section: High-level component diagram or narrative
  ### 1.1 Subcomponent and its role
  ### 1.2 Subcomponent and its role

## Data / Entity Reference
  Tables showing what fields live where (Neo4j vs LanceDB vs disk)

## Workflows
  Step-by-step flows for common operations (indexing, querying)

## Constraints & Trade-offs
  Why this design? What are the alternatives we rejected?
```

**Example section:**
```
### 3.2 Indexer Pipeline

Four phases run in sequence per file:

1. **Extract symbols** — tree-sitter AST walk → SymbolMetadata
2. **Link calls** — type-classified call edges (CALLS_DIRECT, CALLS_DYNAMIC, CALLS_INFERRED)
3. **Embed bodies** — symbol code → vector via all-MiniLM-L6-v2
4. **AFFECTS rebuild** — reverse BFS (depth ≤ 4) → materialized AFFECTS edges
```

---

### **Type C: Concept** (`concept.md`, `idea_*.md`)

Explains a high-level idea without referencing specific code.

**Structure:**
```
# Concept: X

## The Problem
What real-world need does this solve?

## The Idea
How does it work at a conceptual level?
(No code references)

## Why It Matters
Trade-offs: what does it gain? What does it cost?

## Related Concepts
Links to other ideas or specs that build on this.
```

**Example:**
```
# Concept: Dirty State Tracking

## The Problem
Users edit code in VS Code while the Neo4j graph is stale. 
The sidecar needs to answer questions about unpersisted edits.

## The Idea
`InMemoryOverlay` caches {file_path → raw_content} in memory. 
Re-parses the overlay on every symbol extract, never touching disk. 
When the user saves, the overlay is cleared and re-indexing begins.

## Why It Matters
**Gain:** instant responsiveness to edits; no blocking I/O.
**Cost:** memory overhead for large edits; overlay lost on crash.
```

---

## 3. Code Examples

**Good:**
```python
# ✓ Specific, copy-paste-able, shows input + output
calls = extractor.extract_calls("sidecar/indexer/code.py")
print(calls[0])
# Output: {"caller_uid": "abc123", "callee_name": "run_indexing", "rel_type": "CALLS_DIRECT"}
```

**Bad:**
```python
# ✗ Vague, pseudo-code, no context
calls = extract_calls(file)
# Returns a list of calls
```

**Guidelines:**
- Use real file paths, not placeholders
- Show the actual output format
- If importing, include the import statement
- Keep examples under 15 lines — if longer, extract into a code block with a filename reference
- Label examples: `Python`, `Cypher (Neo4j)`, `Bash`, etc.

---

## 4. Tables: Data & Relationships

**For Neo4j nodes/edges:**

```markdown
### Nodes

| Label | Properties | Description |
|---|---|---|
| File | `path, hash, last_indexed, doc_type` | Source file or doc |
| Symbol | `uid, name, kind, range, hash` | Function/class/variable |
| DocAnchor | `chunk_id` | Doc chunk reference (content lives in LanceDB) |

### Edges

| Type | Direction | Properties | Description |
|---|---|---|---|
| CONTAINS | File → Symbol | none | Symbol belongs to file |
| CALLS_DIRECT | Symbol → Symbol | none | Static call (confidence: 1.0) |
| CALLS_DYNAMIC | Symbol → Symbol | none | Dispatch call (confidence: 0.7) |
| FROM | DocAnchor → File | `type: "doc" \| "code" \| "spec" \| ...` | Doc origin or reference |
| COVERS | DocAnchor → Symbol | `anchor_type`, `confidence`, `primary_bias`, `resolver` | Doc-to-symbol link quality |
```

**For field comparisons:**

```markdown
| Field | When to use | Cost |
|---|---|---|
| `uid` (SHA256) | Stable symbol identity across re-indexing | 64 bytes per symbol |
| `content_hash` | Detect if symbol code changed without reading file | 64 bytes per symbol |
| `token_estimate` | Rank symbols by context window fit | Estimation error ±20% |
```

---

## 5. Links & References

**Internal (within Surgical Context):**
```markdown
See [spec_doc_anchor.md](spec_doc_anchor.md) for DocAnchor semantics.
```

**Code files:**
```markdown
[sidecar/indexer/code.py:54-60](sidecar/indexer/code.py#L54-L60) — symbol extraction phase
```

**External:**
```markdown
Uses [sentence-transformers](https://www.sbert.net/) all-MiniLM-L6-v2 (384-dim embeddings).
```

---

## 6. Status Markers

Use consistent markers to indicate maturity:

```markdown
✅ Implemented        # Shipped, tested, in production
🚧 In Progress        # Active development
📋 Planned           # On the roadmap, not started
⚠️  Known Issue       # Works but with caveats
❌ Not Implemented   # Planned but not started
🔄 Refactoring       # Working code, being redesigned
```

**Example:**
```markdown
## Phase 5: Typed Semantic Edges ✅ Complete
Goal: Classify function calls by confidence.

- [x] Python call type detection
- [x] Neo4j schema migration
- [x] BFS scoring updates
- [x] AFFECTS index
```

---

## 7. Formatting Conventions

**Filenames & paths:**
```markdown
`sidecar/indexer/code.py`       # Local paths always relative to repo root
`config.json`                   # Bare filenames if in same directory
```

**Classes, functions, properties:**
```markdown
`Neo4jClient.link_calls()`      # Class.method
`run_indexing()`                # Standalone function
`File.path`                     # Property
`SIMILARITY_THRESHOLD = 1.5`   # Constant
```

**Cypher queries:**
Use triple backticks with `cypher` language hint:
````markdown
```cypher
MATCH (s:Symbol {uid: $uid})
RETURN s.name, s.kind
```
````

**Python code:**
Use triple backticks with `python` language hint:
````markdown
```python
indexer = AFFECTSIndexer(db)
indexer.rebuild_affects(["uid1", "uid2"])
```
````

---

## 8. Naming & Terminology

**Be consistent:**
- "Neo4j" (not "neo4j" or "graph database")
- "LanceDB" (not "lancedb" or "vector store")
- "Surgical Context" (not "surgical-context" or "SC")
- "DocAnchor" (not "doc anchor" or "anchor node")
- "symbol" (not "node" or "entity" — too generic)
- "call edge" or "CALLS edge" (not just "call")
- "phase" (not "stage" or "step")

**Avoid:**
- "we" — use passive or name the component ("the indexer checks..." not "we check...")
- Acronyms without definition (define once, then use)
- "etc." — use "e.g." for examples, or list all items

---

## 9. Common Doc Patterns

### **Limitation vs. Planned Extension**

```markdown
## Limitations (current)
- File hashes are full-read; future work to use incremental delta.
- AFFECTS rebuild is synchronous per file (not batched).

## Planned Extensions
- Batch AFFECTS across files for O(1) rebuild instead of O(n).
- Parallel file processing via ThreadPoolExecutor.
```

### **Why vs. Implementation**

```markdown
## Why This Design

**Problem:** Old calls were untyped; BFS couldn't distinguish direct vs. dynamic dispatch.

**Solution:** Three edge types (CALLS_DIRECT, CALLS_DYNAMIC, CALLS_INFERRED) with 
confidence priors. Enables call-graph ranking without rewriting the BFS.

**Trade-off:** Requires language-specific parser logic. Python and TypeScript adapters implemented; additional languages follow the same plugin pattern.
```

### **Algorithm in Words**

```markdown
## Algorithm

1. For each changed symbol UID:
   - Delete existing AFFECTS edges from that symbol
   - Run reverse BFS (up to depth 4) following CALLS/DEPENDS_ON/IMPLEMENTS edges
   - Collect all reachable symbols (excluding the source symbol itself)
   - Create AFFECTS edge from source → each reachable target

2. Constraints:
   - Max fanout per level: 200 (prevents explosion on heavily-used symbols)
   - Max depth: 4 (configurable; balance accuracy vs. latency)
```

---

## 10. Review Checklist

Before submitting a doc, ask:

- [ ] **Can someone unfamiliar with this project understand it in <2 minutes?**
- [ ] **Are all code examples copy-paste-able?**
- [ ] **Are filenames relative to repo root?**
- [ ] **Do links point to files that exist?**
- [ ] **Is there a "why" behind design decisions, not just "how"?**
- [ ] **Are limitations and trade-offs listed?**
- [ ] **Is terminology consistent with other docs?**
- [ ] **Is the tone direct, not marketing-y?**
- [ ] **Does it match the structure template for its type (Spec / Architecture / Concept)?**

---

## 11. When to Write a New Doc

**Write a Spec** if:
- You're implementing a new component (parser, indexer, API endpoint)
- Someone needs to understand the interface and behavior

**Write a Concept** if:
- You're introducing an idea (dirty state tracking, AFFECTS index, intent classification)
- The idea is reused across multiple components

**Update Architecture** if:
- The data flow changes
- A new phase is added to the pipeline
- A major relationship between components shifts

**Write a Planned Extension** (in existing Spec) if:
- You know what needs to be done but can't do it now
- It's blocking future work

---

## Examples in This Repo

- **Spec example:** spec_intent_classifier.md (removed) — 6 intent types with priority orderings
- **Architecture example:** [architectura.md](architectura.md) — Section 3 data pipelines + Section 4 workflows
- **Concept example:** [concept.md](concept.md) — surgical code retrieval rationale
- **Limitation + Extension:** [spec_doc_anchor.md](spec_doc_anchor.md#limitations-current) — current gaps + roadmap

---

## Questions?

If a doc is unclear, file an issue or ask in code review. **The docs are for the team**, not for awards — clarity is the goal.
