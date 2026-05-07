# Benchmark Findings: Mechanism Coverage vs. Question Pass Rate

> **Status:** Historical analysis of an early FastAPI benchmark debugging pass. Keep it for the framing, not for the exact outcomes.

## Current Snapshot

What changed since this note was written:

- the benchmark now normalizes legacy `required_roles` into a canonical cross-framework role taxonomy before computing `role_recall`
- FastAPI `core12` is no longer the main blocker; the local benchmark path there is strong enough to use for tuning
- Pydantic is now the more interesting gap, and the remaining misses are narrower: validator/serializer handle recovery rather than generic “ranker can’t understand the framework”

So the document's main lesson still stands:

**do not tune by pass rate alone; classify failures into graph-structure gaps, doc-link gaps, and ranking noise.**

### Validity Note: Role Recall Saturation

In the current real-repo benchmark snapshots, `role_recall` has become saturated:
for the positive questions we track across FastAPI, Pydantic, and Redux Toolkit,
the ranker now reports `role_recall = 1.00` question by question, not merely on
average. That is useful, but it should not be presented as proof without a
methodology caveat.

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

**A. Public API → Internal Method Chain**
- FastAPI → add_api_route → APIRoute.__init__
- Mechanism: fastapi_route_registration
- Recovery: Direct CALLS edges, same-file context
- Status: ✅ Works (Q1 passes)

**B. Runtime Execution Path**
- get_request_handler → run_endpoint_function → is_coroutine_callable
- Mechanism: fastapi_endpoint_execution
- Recovery: CALLS_DIRECT, local control flow
- Status: ✅ Works (Q3 passes)

**C. Config / Marker → Consumer (Framework Lifecycle)**
- Depends → solve_dependencies
- Mechanism: fastapi_dependency_injection
- Recovery: Doc-bridge (co-mention in same chunk)
- Status: ❌ Fails — Depends and solve_dependencies are never co-mentioned
- **This is not a ranker tuning problem. This is a documentation structure gap.**

**D. Request-side Flow**
- request_body_to_args → solve_dependencies → get_body_field
- Mechanism: fastapi_request_body_dependency_resolution
- Recovery: CALLS, same-file, parameter-specific context
- Status: ⚠️ Partial (finds some symbols, misses file coverage)

## Key Insight: Not Every Gap is Tunable

### Q2 (Depends) Cannot Be Fixed by Tuning

Investigated why `Depends → solve_dependencies` connection fails:
- Query Neo4j for symbols co-mentioned with Depends in DocAnchors
- Found: 14 co-mentions total
- `solve_dependencies`: 0 co-mentions

**Conclusion:** The static call graph + doc-bridge approach cannot create this link because:
1. Depends is in params.py, solve_dependencies is in dependencies/utils.py
2. They are called in different phases of framework execution
3. No documentation discusses them as a pair

**Possible fixes (not tuning):**
- Improve doc structure (mention them together)
- Add semantic edges in the graph (Depends → RESOLVES_VIA → solve_dependencies)
- Use code comment extraction to infer intent
- Train a better embedding model

### Q4 (request_body_to_args) is Sparse

request_body_to_args has few neighbors in the graph. Even with:
- Doubled pool sizes (100 → 200, 50 → 100)
- Loosened gating (score threshold 0.25 → 0.15)
- Reduced floor floor (0.05 → 0.02)

It still only finds 3 candidates. This isn't tuning-fixable; it's architectural:
- If the code doesn't call solve_dependencies, no CALLS edge exists
- If docs don't mention request_body_to_args + solve_dependencies together, no bridge
- Possible fix: add explicit semantic edges for framework lifecycle patterns

## Correct Benchmark Structure

```yaml
- id: fastapi_q02
  mechanism: fastapi_dependency_injection
  required_roles: [public_entrypoint, marker_or_config, dependency_solver, handler_or_lifecycle]
  
  # Not: "does ranker pass this question"
  # But: "can ranker recover marker→consumer pattern in FastAPI"
  # Answer: No, because doc structure doesn't co-mention them
  
  coverage: doc_bridge_semantic_gap
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

3. **Add framework hints to Neo4j:**
   - SEMANTIC_HINT edges for well-known patterns (Depends → solve_dependencies)
   - Mark framework lifecycle phases
   - Document "co-concepts" (things discussed together in framework logic)

4. **Use expected_symbols as validation, not tuning target:**
   - If symbol isn't in code (e.g., "registration_step"), question is mislabeled
   - Correct the question, don't tune ranker to hallucinate symbols
