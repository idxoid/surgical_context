# Context retrieval — layered truth model

## Status

Draft — design contract. Implementation today is split across the indexer, `framework_hints`, LanceDB, Pass 1 role catalog, and `UnifiedRanker`; this document names the **intended boundaries** so framework-specific or ranker-only fixes do not stand in for missing graph truth.

## Recent implementation notes (2026-05-05)

- **Layer 2 gate tightened:** `call_argument_link` rules can now require a resolved callee namespace prefix (e.g. `fastapi.*.Depends`) before emitting `SEMANTIC_HINT`; this avoids local-name collisions from unrelated `Depends(...)`.
- **Parser support generalized:** `sidecar/parser/qualified_import_roots.yaml` now controls per-language external import roots that still produce alias→qualified bindings (used by Python today; intended for C/C++/C# adapters too).
- **Layer 4 trace fallback strengthened:** trace mode now seats recovery anchors from imported modules, runtime-name seeds, and sibling-directory expansion when import topology is sparse, with explicit provenance (e.g. `recovery:import-module-trace`).
- **Benchmark UX:** workspace-mode negative lookups (expected absent symbols) are printed as correct rejection instead of raw "not found" error text.

## Execution status (implemented vs pending)

### ✅ Implemented in current branch

- Layer-2 qualified-callee safety gate for DI-like hint rules.
- Per-language qualified import roots config (`sidecar/parser/qualified_import_roots.yaml`) with Python wired in.
- Layer-4 trace recovery hardening for sparse import topology:
  - import/module recovery rows
  - runtime symbol seed rows
  - sibling-directory symbol expansion
- Prompt-contract/benchmark wording for expected absent workspace symbols.

### 🚧 Still pending

- Move bundled framework YAML hints to shared typed rule instance storage (workspace-extensible source of truth).
- Strengthen Layer-1 import completeness directly in indexer (reduce ranker-side recovery dependence).
- Add explicit pool-vs-pruned telemetry per missed expected file in benchmark output.

## Problem (systemic)

Some questions require evidence in **other modules** than the primary symbol (e.g. a thin API marker vs. a runtime resolver in a sibling package). If the **durable graph** does not record that relationship, the ranker can only **heal** the gap with heuristics. Heuristics are necessary as a safety net, but they must not be the only definition of “what is connected” — otherwise every stack becomes a new patch in one Python file.

## Layer model

```mermaid
flowchart TB
  subgraph L1["Layer 1 — Durable graph truth"]
    A[AST / calls / imports / inheritance]
    B[File–File IMPORTS]
    C[DocAnchor COVERS / FROM]
    D[Workspace JSON: role_catalog, repository_profile]
  end
  subgraph L2["Layer 2 — Declarative hint overlay"]
    E[Rule types / subtypes: call_argument_link, ...]
    F[SEMANTIC_HINT and similar typed edges]
  end
  subgraph L3["Layer 3 — Soft signals"]
    G[LanceDB vectors over symbols/docs]
  end
  subgraph L4["Layer 4 — Retrieval policy"]
    H[UnifiedRanker: sort, budget, gain, trace modes]
  end
  subgraph L5["Layer 5 — Evaluation"]
    I[qa_benchmark: expected_files, roles, gates]
  end
  L1 --> L2
  L1 --> L3
  L1 --> L4
  L2 --> L4
  L3 --> L4
  L4 --> L5
```

### Layer 1 — Durable graph truth (authoritative for “can we prove it?”)

**Owns:** Neo4j topology the indexer commits to: `File`, `Symbol`, typed call edges, `IMPORTS` between files, `AFFECTS` where materialized, `DocAnchor` links, workspace-persisted JSON that is part of the index contract (`role_catalog_json`, `repository_profile_json`).

**Rule:** If the product claims “X is part of the dependency story for Y”, that claim should **eventually** be representable as Layer 1 data (edge or file-level import), or be explicitly tagged as lower confidence (see Layer 2).

**Not sufficient alone:** static graphs often miss framework-specific wiring; that is why Layer 2 exists.

### Layer 2 — Declarative hint overlay (authoritative for “we know the pattern”)

**Owns:** Small, **typed** rules (e.g. `call_argument_link` with metadata `kind: dependency_injection`) that create **specialized edges** (e.g. `SEMANTIC_HINT`) the extractor cannot infer. Today some rules live in bundled YAML under `sidecar/context/`; the **target end state** is rules keyed by **shared rule types / subtypes**, not per-framework filenames (see `framework_hints.py` module note).

**Current safety contract:** Rules may require extractor-provided qualification metadata (example: `require_callee_qualified_prefix`) so a hint depends on both **pattern match** and **namespace evidence**.

**Rule:** Hints are **first-class** graph facts for retrieval, not ad hoc ranker constants. They should be versionable and workspace-extensible.

### Layer 3 — Soft signals (non-proof, high recall)

**Owns:** Vector similarity (LanceDB), coarse doc co-occurrence, anything that can suggest but not **prove** structure.

**Rule:** Must not be the only path to **mandatory** evidence for a trace-style question; may rank and break ties.

### Layer 4 — Retrieval policy (orchestration, not new facts)

**Owns:** `UnifiedRanker` (and friends): pool fusion, role-aware sort tiers, token budget, marginal gain, trace/ DI heuristics (e.g. resolving package imports on disk when graph import edges are missing).

**Current fallback contract:** when Layer 1 import edges are incomplete for trace-style questions, policy may add **bounded** recovery candidates from module imports, runtime symbol seeds, and same-directory neighbors; all such additions must keep explicit recovery provenance.

**Rule:** Policy may **prefer** or **include** nodes that are already in the pool; it may apply **clarified fallbacks** when Layer 1 is incomplete. It should not silently redefine “truth” without marking provenance (e.g. `recovery:import-module-trace`).

### Layer 5 — Evaluation

**Owns:** `qa_benchmark` and packs: `expected_files`, `expected_symbols`, role gates.

**Rule:** Failing a gate is a **product signal**: distinguish “graph never contained the file” (indexer / checkout) from “graph had it but policy dropped it” (Layer 4) using structured pruned reasons and future telemetry.

## Contracts (summary)

| Question | Layer that must answer it |
|----------|---------------------------|
| Is there a path in the graph from A to B? | 1 (possibly extended by 2) |
| Is this symbol in the same import cone as the target? | 1 (`IMPORTS`) or 4 (filesystem resolution fallback) |
| Should we stop selecting because gain is low? | 4 only |
| Did we satisfy the benchmark file hints? | 5 — but **root cause** is diagnosed in 1–4 |

## Migration direction (no single ticket)

1. **Strengthen Layer 1** for patterns that repeat (file-level import graph completeness; optional package-root resolution in indexer, not only ranker).
2. **Move framework literals** from scattered YAML to **typed hint instances** (Layer 2) with stable IDs and metadata.
3. **Keep Layer 4** as a small set of **named policies** (trace breadth, doc deferral) configurable per intent, not an unbounded list of special cases.
4. **Add observability**: for each pruned or skipped high-value file, record whether the node was **absent from the pool** vs. **pruned in selection**.

## Related docs

- [spec_storage.md](spec_storage.md) — Neo4j / LanceDB split
- [spec_unified_ranking.md](spec_unified_ranking.md) — ranker behavior
- [spec_indexer.md](spec_indexer.md) — indexing passes
