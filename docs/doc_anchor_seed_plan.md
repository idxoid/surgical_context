# Doc-anchor seed — implementation plan

Status: design approved (idxoid 2026-06-18). Roll out on **nestjs first**, then per-repo.

## Why

Weak-seed retrieval: the vector seed embeds only **body** + **signature** of a symbol.
For doc-rich/code-tiny symbols the NL-matching text lives in the doc-comment, which is
either dropped (TS/JSDoc lives *above* the AST node) or **diluted** by the body (Python
docstring is inside the node but a large body buries it). So gold sits deep in vector rank
and the per_role_limit=7 seed never reaches it.

Validated in-memory (no reindex), prototype numbers:
- **nestjs** `Module` JSDoc embeds to **rank 0/4645** (body rank 342). 3-stage seed
  (doc-anchor D ∪ vector V → 1-hop) moves both zeros off zero at **per_role_limit=7**
  (no noise bump): q03 0→0.33, new_q02 0→0.50 (walk-edges) / 1.00 (all-edges).
- **fastapi** (Python, docstring already in body): docstring-anchor lifts seed on **3/6**
  weak-seed questions; budget-controlled proof q04 D-only(7)=0.67 > V-only(7)=0.33.
  Union is complementary (D and V catch different gold).

Rejected alternatives:
- **per_role_limit bump** (7→35): closes nestjs zeros but **regresses dense ORM**
  (sqlalchemy_new_q03 1.0→0.5, bundle-stage, budget-immune) — net-negative on Python.
- **per-facet top-K union** (body∪sig): falsified — no gain at matched budget, min wins
  at larger budget; gold ranks are all ≥2-20× beyond seed budget in every facet.
- **doc-FACET-via-min** on the symbol: suffers min-crowding (min ranks a gold worse than
  its best single facet because min lifts every competitor). Separate-anchor-union is immune.

## Reuse: existing DocAnchor infra (dormant)

`context_engine/indexer/anchor.py` already has: `DocAnchor(chunk_id)` node,
`COVERS→Symbol` (owner) + `FROM→File` edges, doc-chunk Lance table `docs_axis_python_v1`
(`id, workspace_id, file_path, chunk, pending, vector, embedding_metadata`),
role_clustering `doc_anchor_count` signal, `_add_covers_edges_batch`. Built for **markdown**
docs, COVERS resolved by **identifier name-match**; currently **0 anchors** (dormant), and
**axis retrieval never reads it** (doc vector-search lives only in legacy
`_vector_search_docs` main.py). We repurpose it for in-code docstrings.

## Approved decisions

1. `owner_uid` **column** on the doc-chunk row (fast Stage-1 resolution, no Neo4j COVERS
   lookup at query time). COVERS edge kept for graph/role signal.
2. Phase 4 reverse-USES_TYPE bridge = **separate pass** (NOT a general axis-edge — USES_TYPE
   in the shared profile would reintroduce the god-fan).
3. Roll out on **one repo (nestjs)** before any all-ws reindex.

## Phases (validate after each)

### Phase 0 — schema
- `context_engine/database/lancedb_client.py`: add `owner_uid` column to the doc-chunk
  table schema (alongside `id, workspace_id, file_path, chunk, pending, vector`).
- Gate: empty column, nothing breaks.

### Phase 1 — docstring/JSDoc extraction (parser)
- `context_engine/parser/adapters/python_adapter.py`: first string literal of func/class
  body → `docstring`.
- `context_engine/parser/adapters/typescript_adapter.py` + `javascript_adapter.py`: leading
  `/** */` comment node before the symbol (reuse the above-node handling used for decorators).
- `context_engine/parser/protocol.py`: add `docstring: str` to the symbol record.
- Gate: unit test on 3-4 symbols per language; index untouched.

### Phase 2 — anchor emission (indexer) — the "link file→symbol"
- `context_engine/indexer/anchor.py`: new `ingest_symbol_docstrings(...)` — per symbol with
  a docstring: doc-chunk row (`file_path=owner file, chunk=docstring, vector=embed,
  owner_uid`) + DocAnchor + **direct `COVERS→owner_uid`** (`resolver='definition'`,
  confidence=1.0), skipping name-matching. Reuse `_add_covers_edges_batch` (anchor.py:325).
- `context_engine/indexer/fast/pipeline.py` (Stage-7 docs, ~:2072): call it with the parsed
  symbols.
- Gate (nestjs reindex): `MATCH (a:DocAnchor)-[:COVERS]->(s) RETURN count` > 0; `owner_uid`
  populated; doc-chunk table non-empty.

### Phase 3 — Stage-1 doc-anchor seed (retrieval)
- `context_engine/axis/role_retrieval.py`: `find_seeds_by_doc_anchor(ws, query, embed_fn,
  limit)` — vector-search the doc-chunk table (reuse `scan_docs_workspace`,
  lancedb_client.py:527) → top-K chunks → `owner_uid` → RoleCandidate(role="doc_anchor",
  file_path=owner). Apply the same tier weighting as `find_seeds_by_vector`.
- `context_engine/axis/pipeline.py:208` (vector_seeds stage): call it,
  `raw_by_role["doc_anchor"]` = **UNION** with vector_seed (not replace — D contributes what
  V lacks, e.g. Module rank 0).
- Gate: nestjs seed_recall ↑ on q03/new_q02; Python suite no regression.

### Phase 4 — reverse-USES_TYPE × tier bridge (pool, separate pass)
- New `context_engine/axis/doc_anchor_bridge.py`: from doc-anchor seeds → **reverse-USES_TYPE
  1-hop, filter target `file_tier=core`** (never walk USES_TYPE forward = god-fan; reverse from
  a specific seeded interface is bounded). Optional IDF weight `1/log(indeg)` to damp the
  `Type`(213) extreme. NB a flat in-degree cap is WRONG — it kills the domain-interface
  bridges (CanActivate=38, NestInterceptor=57, PipeTransform=76); the noise filter is
  file-tier on the target (CanActivate reverse = 26 files = 16 sample/integration + 10 core
  incl both gold guards).
- Wire into pool expansion in `context_engine/axis/pipeline.py` next to structural_neighbours.
- Gate: new_q02 pool_recall 0.50→1.0; p95 latency unchanged (reverse hop is narrow + tier-filtered).

## Rollout
nestjs first (reindex one ws, validate Phases 2-4 against the 2 zeros), then extend to the
22 weak-seed Python questions across sqlalchemy/django/fastapi/celery, then all benchmark ws.

## Invariants
Structural only — the doc-anchor is a structural fact (the symbol *has* a docstring) and the
bridge is reverse-USES_TYPE+tier. No name-pattern/answer-key/keyword matching (per
docs/engineering_principles.md, AI_RULES.md).
