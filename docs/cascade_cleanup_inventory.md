# Cascade cleanup — Phase 0 inventory & dependency map

Migration of the legacy ranking cascade (`sidecar/context/`) to the axis
pipeline (`sidecar/axis/`). This is the Phase 0 deliverable: the exact
A/B/C classification of every `sidecar/context` module by its
**production** (non-test, non-QA) importers, plus the `/ask` API
contract the replacement must honour.

Measured 2026-06-11 by scanning `from sidecar.context.<mod>` across
`sidecar/`, `QA/`, `tests/`.

## The border principle

`sidecar/context/` is NOT all cascade. Only **class A** is deleted;
B migrates into the indexer (it is index-time infra, and the axis
pipeline itself depends on indexing); C is shared runtime infra.

## Class A — DELETE (cascade-ranking, ~7200 LOC)

Removed only AFTER the `/ask` endpoints are switched to axis (Phase 3).
Every module below has **zero production importers** outside
`sidecar/context` except where noted (the exception is `main.py`'s
cascade wiring, which Phase 1–3 replaces).

| module | production importer | note |
|---|---|---|
| unified_ranker | (QA/tests only) | cascade entry |
| arbitrator | main.py (`ContextArbitrator`) | cascade orchestrator; `/ask`, `/search/unified` |
| ranker/candidate_pool | main.py (`VectorSearcher`) | axis has `scan_workspace_rows`; keep VectorSearcher only if `/search` still needs it |
| ranker/scoring | — | 0 importers |
| ranker/pruning | — | 0 |
| ranker/recovery* | — | the 4 naming-branch fixtures (DI/hook/publish/consume) the axis invariant bans |
| ranker/role_backfill | — | 0 |
| ranker/role_fulfilment | — | 0 |
| ranker/subgraph_assembler | — | 0 |
| ranker/budget_selector | — | 0 |
| ranker/target_selector | — | 0 |
| ranker/graph_candidate_source | — | 0 |
| ranker/vector_candidate_source | — | 0 |
| weight_tuner | (tests only) | 0 prod |
| role_taxonomy | (QA/tests only) | 0 prod |
| prompt_compiler | (tests only) | legacy prompt build; Phase 1 needs an axis equivalent |
| graph_expander | (tests only) | 0 prod |
| deduplicator | (tests only) | 0 prod |
| code_resolver | (tests only) | 0 prod — but verify axis context_builder covers code fetch |
| mechanism_registry | indexer (2) | **answer-key, already INERT** (`determine_preloaded_mechanism` always `""`); indexer import is removable |
| mechanism_packs/ | (tests only) | answer-key YAML fixtures (banned) |
| intent_classifier (legacy) | main.py (`IntentClassifier`) | axis has its own `axis/intent_classifier`; delete after switch |
| prompt_compiler (`PromptCompiler`) | (via arbitrator) | **provider-side**, builds PromptContext from ranked candidates INSIDE arbitrator; deleted with cascade — but the axis provider needs its own ContextBundle→PromptContext adapter (Phase 1), NOT a full prompt rewrite |

## Class B — MIGRATE → `sidecar/indexer/`

Index-time infrastructure, NOT ranking. The indexer (which axis itself
relies on) imports these. Move them under `sidecar/indexer/`, do not
delete.

| module | importer | destination |
|---|---|---|
| framework_hints | `indexer/fast/pipeline.py` (`FrameworkHintsIndexer`) | `sidecar/indexer/framework_hints.py` |
| ranker/signal_constants | `indexer/role_clustering.py` (`NOISE_PATH_PATTERNS`) | `sidecar/indexer/signal_constants.py` |

## Class C — KEEP / relocate (shared runtime infra)

Not cascade; the axis answer path needs them too.

| module | importer | note |
|---|---|---|
| **types.`PromptContext`** | main.py (consumer seam) | **THE provider↔consumer CONTRACT** — `_resolve_ask_context()` returns it, consumers call `to_system_prompt()`. NOT cascade. KEEP. The migration swaps the PROVIDER behind this contract, not the contract. |
| types.`to_system_prompt()` | (consumer) | consumer-side render on PromptContext; untouched |
| types.`SymbolContext` / `DocChunk` | (part of PromptContext) | the context payload types PromptContext carries; KEEP with the contract |
| overlay (`InMemoryOverlay`) | main.py | runtime uncommitted-edit overlay; relocate to `sidecar/overlay.py` or keep |
| doc_resolver (`DocResolver`) | main.py (`/search`, doc context) | doc-chunk retrieval; axis answer path needs docs |
| types.`Subgraph` | `cache/layered.py` | cache type; relocate to cache or a small shared types module |
| types.`RESOLVER_VERSION` | main.py | version stamp; relocate |

## The provider↔consumer boundary (corrected understanding)

The migration is NOT "add an LLM to axis". The context PROVIDER is
isolated from the consumers by a fixed contract: **`PromptContext`**.

- **Provider** builds a `PromptContext`. `_resolve_ask_context()`
  already has FOUR providers behind this one contract — arbitrator
  (cascade), file, workspace, direct. They are polymorphic at the seam.
- **Consumers** (`ask`, and the impact/explain modes) call
  `_resolve_ask_context()`, get a `PromptContext`, then do their own
  thing — `ctx.to_system_prompt()` → `ai_engine` → answer. This is
  ISOLATED from how the PromptContext was built.

So the cascade→axis swap is: add a FIFTH provider — an axis one — that
emits `PromptContext` from the axis pipeline's `ContextBundle`. The
consumer side (to_system_prompt, ai_engine, answer, AskResponse) is
untouched. `prompt_compiler.PromptCompiler` is provider-side (lives
inside arbitrator) and dies with the cascade; the axis provider needs
its own `ContextBundle → PromptContext` adapter — an adapter, not a
prompt/LLM rewrite.

`/ask/axis` returning context-only `AskAxisResponse` was a SEPARATE
A/B-evidence endpoint, not the migration target. The migration target
is the `_resolve_ask_context` seam.

## `/ask` API contract (preserved automatically by keeping PromptContext)

`AskResponse` (what the VSCode extension consumes) is produced by the
CONSUMER from a `PromptContext` — so keeping the contract keeps the
response shape for free:

```
symbol, answer, context, user, cloud, workspace_id, trace_id,
feedback_token, model_route, metrics, index_manifest_id/_schema_version
```

## Endpoints bound to cascade (to switch in Phase 3)

- `/ask` (main.py:1566) — `_context_arbitrator` → ContextArbitrator
- `/search/unified` (main.py:1484) — `_context_arbitrator`
- `/ask/stream` — verify binding

## Phase order (gates)

0. **Inventory** (this doc) — done.
1. **Axis provider behind the PromptContext contract** — add a fifth
   provider at the `_resolve_ask_context` seam: axis pipeline →
   `ContextBundle → PromptContext` adapter. NOT an LLM/prompt rewrite —
   the consumer (`to_system_prompt` → `ai_engine` → AskResponse) is
   untouched. GATE: axis provider emits a valid PromptContext the
   existing consumer renders.
2. **Parallel + A/B** — `_resolve_ask_context` chooses axis-provider vs
   arbitrator-provider behind a flag; A/B on real questions (recall:
   axis 0.972 vs legacy; + answer-quality judge). GATE: axis ≥ legacy.
3. **Cutover** — `_resolve_ask_context` defaults to the axis provider;
   `/search/unified` similarly; arbitrator behind flag for rollback.
4. **Indexer decouple** — migrate class B; drop mechanism_registry/packs
   (inert answer-key); relocate Subgraph. GATE: indexer imports no
   `sidecar.context`.
5. **Delete class A** (~7200 LOC) + legacy tests (~20) + QA legacy
   benchmark (`qa_benchmark.py`, `context_frontier.py`). GATE: suite
   green, no dangling imports.
6. **Cleanup** — docs/spec_indexer mechanism refs, uncommitted
   `archetype_resolver.py` (legacy PoC), final pass.

## Risks

- **Indexer coupling** (class B) — index-time, not ranking; migrate, do
  not delete, or indexing breaks (and axis stands on indexing).
- **`/ask` LLM gap** — Phase 1 is mandatory-first.
- **VSCode extension contract** — AskResponse shape above; axis must
  match or the extension is updated in lockstep.
- **prompt_compiler** — class A but Phase 1 needs an axis replacement
  before it can be deleted; sequence accordingly.
- **Strict order** — cannot delete (5) before cutover (3); cannot cutover
  before the A/B gate (2).
