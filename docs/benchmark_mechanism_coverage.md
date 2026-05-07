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
