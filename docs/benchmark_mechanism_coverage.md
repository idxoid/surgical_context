# Benchmark Findings: Mechanism Coverage vs. Question Pass Rate

> **Status:** Historical analysis of an early benchmark debugging pass, updated to reflect the current generic retrieval architecture. Keep it for the framing, not for the exact outcomes.

## Current Snapshot

What changed since this note was written:

- the benchmark now normalizes legacy `required_roles` into a canonical cross-framework role taxonomy before computing `role_recall`
- the original framework-shaped fixes have been replaced by generic layers:
  - repository profiles emit archetype signals, not repo/framework identities
  - Python import extraction infers stdlib/installed-package imports and preserves workspace packages without a hand-maintained framework list
  - semantic hints use shared typed rules (`semantic_hints.yaml`) rather than per-framework hint files
  - trace recovery uses dependency/provider/container/resolve signals and explicit recovery provenance instead of framework-symbol pairs
- current real-repo warnings are mostly precision/file-coverage tails, not missing role coverage

So the document's main lesson still stands:

**do not tune by pass rate alone; classify failures into graph-structure gaps, doc-link gaps, and ranking noise.**

### Latest Real-Repo Run

Last local run: 2026-05-08, all repositories from
`tests/fixtures/real_repo_question_pack.yaml`, using `--no-index` against the
current local indexes.

| Repo | Questions | Precision@5 | File Recall | Role Recall | Tokens | Notes |
|---|---:|---:|---:|---:|---:|---|
| FastAPI | 8/8 | 0.11 | 0.75 | 1.00 | 24,950 | impact `fastapi_q06`: p=0.12, file=1.00, `impact_context_complete` |
| Pydantic | 8/8 | 0.05 | 0.88 | 1.00 | 26,026 | impact `pydantic_q06`: p=0.12, file=1.00, `impact_context_complete` |
| Redux Toolkit | 8/8 | 0.08 | 0.81 | 1.00 | 14,544 | impact `rtk_q05`: p=0.11, file=1.00, `context_complete_below_floor` |
| Django | 5/5 | 0.06 | 0.80 | 1.00 | 14,887 | impact `django_q05`: p=0.05, file=1.00, `pool_exhausted` |
| Flask | 5/5 | 0.08 | 1.00 | 1.00 | 12,696 | impact `flask_q05`: p=0.05, file=1.00, `expansion_no_progress` |
| Express | 4/4 | 0.29 | 1.00 | 1.00 | 3,860 | - |
| NestJS | 4/4 | 0.09 | 0.88 | 1.00 | 10,776 | - |
| SQLAlchemy | 4/4 | 0.05 | 0.88 | 1.00 | 11,316 | - |
| Vue | 4/4 | 0.04 | 1.00 | 1.00 | 19,720 | - |
| **Total** | **50/50** | - | - | - | **138,775** | all repos green with `--no-index` |

Impact-analysis detail from the same run:

| Repo | Question | Precision | File Recall | Tokens | Stop Reason | Missing Expected Symbols |
|---|---|---:|---:|---:|---|---|
| FastAPI | `fastapi_q06` / `serialize_response` | 0.12 | 1.00 | 2,250 | `impact_context_complete` | `APIRoute`, `response_model` |
| Pydantic | `pydantic_q06` / `Field` | 0.12 | 1.00 | 4,299 | `impact_context_complete` | - |
| Redux Toolkit | `rtk_q05` / `createSlice` | 0.11 | 1.00 | 2,220 | `context_complete_below_floor` | - |
| Django | `django_q05` / `Model` | 0.05 | 1.00 | 2,563 | `pool_exhausted` | `DeferredAttribute`, `Field` |
| Flask | `flask_q05` / `Flask` | 0.05 | 1.00 | 2,425 | `expansion_no_progress` | `Map`, `url_map` |

The pass rate is saturated, so the useful signal is now the second table:
impact questions still expose missing expected symbols and low precision even
when role and file gates are green. The next precision pass should therefore
target ordering and stop conditions, not broader role recovery.

### Validity Note: Role Recall Saturation

In the current real-repo benchmark snapshots, `role_recall` can become saturated:
for many positive questions the ranker reports `role_recall = 1.00` question by
question, not merely on average. That is useful, but it should not be presented
as proof without a methodology caveat.

There are two honest interpretations:

**Optimistic:** the mechanism-aware ranker can now recover every relationship
type we have formalized as `required_roles`. In this reading, the remaining
work has shifted from mechanism coverage to precision: fewer sibling-subsystem
neighbors, fewer broad docs, and fewer low-gain candidates.

**Skeptical:** some `required_roles` were refined while debugging the ranker,
with knowledge of what the current system returns. If an independent reviewer
annotated the same questions before seeing retrieval output, they might choose
stricter or different roles, and `role_recall` could drop.

The right claim for an article is therefore modest:

> On the current curated benchmark, the system covers all pre-existing
> mechanism roles it is evaluated against. This is strong evidence that the
> role taxonomy is operationally useful, but it is not yet independent proof
> that the taxonomy is complete or unbiased.

The next benchmark hardening step is an independent role-label pass: freeze the
question text, expected files/symbols, mechanism, and `required_roles` before
running or tuning the ranker. Until then, `precision_at_5`, `file_recall`,
`ready_context`, and `pruned[]` should be discussed alongside `role_recall`.

## Problem Statement

Initially treated benchmark as "question score optimization" — adjusting ranker parameters to pass more questions. This led to:
- Tuning is_useful gating to favor specific candidate types
- Increasing pool sizes blindly
- Adding 2-hop doc-bridges for edge cases

This approach is wrong because:
1. It doesn't distinguish between "system limitation" and "tuning opportunity"
2. It creates false causation (bigger pools ≠ better ranking)
3. It obscures architectural gaps (e.g., marker classes → consumers)

## Better Framing: Mechanism Coverage

Instead of "pass rate", measure: **What kinds of code relationships can the ranker recover?**

### Mechanism Classes

**A. Public API → Internal Builder Chain**
- Shape: public entrypoint → builder/factory function → representation/runtime object
- Recovery: direct CALLS edges, same-file context, factory/representation/runtime role signals
- Current status: works when the graph exposes the chain; precision depends on token budget and broad-doc noise.

**B. Runtime Execution Path**
- Shape: request/operation handler → executor → runtime helper
- Recovery: CALLS_DIRECT / CALLS_SCOPED, local control flow, runtime-surface role recovery
- Current status: works well when call edges are present.

**C. Config / Marker → Runtime Consumer**
- Shape: thin marker/config API → dependency/provider/container resolver → runtime model/helper
- Recovery: typed `call_argument_link` hints when argument links exist; otherwise bounded trace recovery from imports, dependency/provider/runtime-name seeds, sibling directories, and generic config/orchestrator role signals
- Current status: no longer depends on co-mentioned docs or hardcoded symbol pairs. Remaining failures should be diagnosed as graph import gaps, recovery ranking, or expected-file precision, not as missing framework defaults.

**D. Request-side Binding Flow**
- Shape: request/body/parameter mapper → schema/model builder → dependency/runtime consumer
- Recovery: CALLS where available, import-module recovery, binding/schema/runtime role signals
- Current status: generally role-complete; file-recall tails remain useful ranking telemetry.

## Key Insight: Not Every Gap is Tunable

### Marker→Consumer Gaps Are Not Fixed by Score Tuning

The early investigation showed that static call graph + doc co-mention alone
cannot reliably connect thin marker/config APIs to runtime consumers when they
live in different modules and lifecycle phases.

**Current resolution:** the system no longer relies on doc co-mentions or a
hardcoded marker→consumer pair. It combines:

- generic import call-site qualification
- workspace-preserving Python import extraction
- typed semantic hint rules for dependency-like call-argument links
- bounded trace recovery from imported modules, runtime-name seeds, and sibling directories
- generic dependency-flow role recovery for config and orchestration surfaces

The remaining tuning question is therefore not "which framework name should we
special-case?" but "which layer failed to expose or select the expected file?"

### Sparse Binding Flows

Some binding/mapping functions have few direct graph neighbors. Bigger pools and
looser score floors can hide the symptom but do not create missing topology.
Current recovery handles these cases through import-module anchors and
schema/binding/orchestrator role signals; remaining misses should be inspected
through `ready_context.contract.pruned[]` and file-level telemetry.

## Correct Benchmark Structure

```yaml
- id: repo_q02
  mechanism: dependency_resolution_trace
  required_roles: [api_surface, config_surface, representation_surface, orchestrator, runtime_surface]

  # Not: "does ranker pass this exact repository question"
  # But: "can retrieval recover marker/config → runtime consumer evidence
  # using graph truth, typed hints, and marked recovery fallbacks?"

  coverage: sparse_import_trace
```

## Recommendations

1. **Stop tuning for pass rate.** Instead, measure mechanism coverage:
   - Public API → chain (CALLS-based): works
   - Runtime control flow (local): works
   - Marker → consumer (doc-bridge): broken for non-colocated symbols
   - Framework lifecycle (semantic edges): missing

2. **Classify failures by root cause:**
   - Architectural (graph structure) vs. tuning-fixable (ranking)
   - Current: 2/4 architectural, 1/4 sparse, 1/4 ranking-fixable

3. **Represent repeated semantic patterns as typed graph facts:**
   - `SEMANTIC_HINT` edges from shared rule types such as `call_argument_link`
   - optional workspace-extensible rule instances for project-specific mechanisms
   - explicit provenance for ranker-side recovery when the graph is still incomplete

4. **Use expected_symbols as validation, not tuning target:**
   - If symbol isn't in code (e.g., "registration_step"), question is mislabeled
   - Correct the question, don't tune ranker to hallucinate symbols
