# Intent Classifier — Spec

## Overview

**Purpose:** Detect the user's query intent, resolve it against the index-time repository capability contract, and rank retrieval tiers (code, specs, architecture, concepts, ideas) accordingly. Enables adaptive payload assembly while making unsupported or shallow reasoning explicit.

**Current status:** Implemented as a deterministic keyword classifier plus an Intent Resolution Contract in `sidecar/context/intent_classifier.py`. The classifier returns a primary desired intent plus observability metadata (`distribution`, `confidence`, `ambiguous`, `matched_keywords`). The resolver then intersects that desired intent with the `repository_profile` emitted by indexing and records an `effective_mode`, available capabilities, and risks. Full multi-label routing remains deferred: the current ranker still routes by one primary intent, while preserving the distribution and resolution metadata in the prompt contract for debugging.

---

## Intent Types & Priority Orderings

### 1. Navigation
**Question pattern:** "Where is X? What calls X? Where is this defined?"

**Priority order (highest → lowest):**
```
code → cross-refs → architecture → specs → concept → idea
```

**Rationale:** Need exact file location + immediate graph neighbors. Docs rarely help locate code.

---

### 2. Debugging
**Question pattern:** "Why does X fail? Why does this break? What's wrong here?"

**Priority order:**
```
code → cross-refs → specs → architecture → concept → idea
```

**Rationale:** Buggy code + its callers + the spec it should satisfy. Architecture/concepts less critical for fixing.

---

### 3. Refactoring
**Question pattern:** "Change X to use Y. Rename X everywhere. Move X to module Z."

**Priority order:**
```
cross-refs → code → architecture → specs → concept → idea
```

**Rationale:** Blast radius first — what uses X matters more than X itself. Need to find all dependents before refactoring.

---

### 4. Exploration
**Question pattern:** "What does this code do? How does this work? Explain this function."

**Priority order:**
```
code → concept → architecture → cross-refs → specs → idea
```

**Rationale:** Start from the code, wrap it in purpose and design context.

---

### 5. New Feature
**Question pattern:** "Add X that does Y. Build a new widget. How do I implement Z?"

**Priority order:**
```
idea → concept → architecture → specs → cross-refs → code
```

**Rationale:** No code exists yet; design context dominates. Code appears only as reference examples.

---

### 6. Design Question
**Question pattern:** "How should we approach Y? What's the best way to do Z? What pattern should we use?"

**Priority order:**
```
concept → idea → architecture → specs → code → cross-refs
```

**Rationale:** Abstract discussion — code is reference material, not the substrate. Design is not yet written.

---

### 7. Impact Analysis (Phase 4)
**Question pattern:** "If I change X, what breaks? What are the most likely affected parts? What tests depend on this?"

**Priority order:**
```
cross-refs → code → specs → architecture → concept → idea
```

**Special handling:** Test files and examples are load-bearing evidence for impact questions, but they are no longer globally unpenalized. Impact analysis uses topic-sensitive noise control: tests/examples keep full weight only when their path, name, or content overlaps the changed surface from the target/query. Unrelated benchmark or tutorial tests keep the standard noisy-candidate penalty. Impact analysis also keeps intent-specific priors: `symbol = 0.3` (downrank primary symbol), `doc = 0.5` (uprank test files and documentation).

**Rationale:** Change impact is about finding what *depends* on you, not what you depend on. Callers (cross-refs) matter most. Code under test is high-signal — if it is tied to the changed surface. Topic-sensitive noise keeps affected tests visible while preventing unrelated benchmark suites from satisfying impact roles by accident. A minimum token floor (3000) remains a grounding target, but compact contexts that fulfill all required roles may stop below floor with `context_complete_below_floor`.

**Keywords matched:** "most likely to break", "most likely to be affected", "likely to break", "what would break", "what parts", "what breaks", "are most likely"

**Ranker behavior:**
- Floor: 3000 minimum token budget
- Noise suppression: topic-related tests/examples keep `noise_factor = 1.0`; unrelated noisy candidates keep the standard penalty
- Priors: `symbol_prior = 0.3`, `doc_prior = 0.5` (emphasize dependencies + tests)
- Pass gate: **OR** semantics — either `role_recall ≥ 0.60` OR `file_recall ≥ 0.50` is sufficient (tests may not be indexed as symbols)

**Known gaps / current review findings:**
- `impact_analysis` classification is fragile because the current implementation scores matches but still selects the primary intent by fixed precedence. Impact questions containing "break" can route as `debugging` even when the impact score is higher. Primary selection should use max score, with precedence only as a tie-breaker.
- Benchmark-style impact questions should remain regression tests across the real-repo question pack. They should verify generic impact roles (`impact_runtime`, `impact_public_api`, `impact_test_surface`) instead of framework-named routing behavior.
- `/impact` and `impact_analysis` retrieval are currently separate surfaces. `/impact` reads materialized `AFFECTS` reachability, while `impact_analysis` ranker mode uses intent priors, topic-sensitive test noise, and impact roles. They should converge through the same retrieval contract.
- `AFFECTS` is reachability evidence, not causal breakage proof. It should eventually contribute candidates to ranker with provenance such as `affects`, plus depth/path/relation/confidence metadata.
- User-facing wording should stay conservative: "likely affected", "reachability-based candidates", or "blast-radius candidates"; avoid "will break" unless tests/runtime evidence prove it.

---

## Content Tiers (Definition)

| Tier | Content | Source |
|---|---|---|
| **code** | Actual code snippets (functions, classes) | GraphExpander + CodeResolver |
| **cross-refs** | Callers, callees, dependencies (graph neighbors) | GraphExpander |
| **specs** | `spec_*.md` documents | DocResolver (FROM {type: "spec"}) |
| **architecture** | `architectura.md` | DocResolver (FROM {type: "architecture"}) |
| **concept** | `concept.md` | DocResolver (FROM {type: "concept"}) |
| **idea** | `idea_*.md` documents | DocResolver (FROM {type: "idea"}) |

---

## Intent Resolution Contract

Intent has two layers:

1. **Desired intent** — what the user appears to ask for.
2. **Effective mode** — what the current repository index can responsibly support.

This prevents shallow text classification from pretending that every repo can satisfy every question type. For example, an impact query on a repo whose profile says `impact_analysis = shallow_partial` becomes `shallow_reachability_impact`, not definitive blast-radius analysis.

Serialized shape:

```json
{
  "desired_intent": "impact_analysis",
  "effective_mode": "shallow_reachability_impact",
  "degraded": true,
  "required_capabilities": [
    "impact_analysis",
    "static_call_reasoning",
    "runtime_registry_semantics"
  ],
  "available_capabilities": {
    "impact_analysis": "shallow_partial",
    "static_call_reasoning": "medium",
    "runtime_registry_semantics": "low"
  },
  "repository_readiness": "partial",
  "risks": [
    "impact may miss dynamic/framework/test-surface edges"
  ]
}
```

Current effective modes include:

| Desired intent | Effective modes |
|---|---|
| navigation | `exact_symbol_navigation`, `low_confidence_navigation` |
| debugging | `code_grounded_debugging`, `limited_debugging_context` |
| refactor | `reverse_dependency_refactor_candidates`, `limited_refactor_search` |
| exploration | `code_grounded_explanation`, `docs_grounded_explanation`, `mechanism_explanation_with_caveats` |
| new_feature | `design_context_planning` |
| design_question | `design_reasoning` |
| impact_analysis | `reachability_impact_candidates`, `shallow_reachability_impact`, `unsupported_impact_request` |
| any | `unprofiled_intent_routing` when no repository profile is available |

The resolution is emitted in the prompt contract under `intent_details.resolution` and mirrored as `metadata.effective_intent_mode`.

---

## Budget Allocation (Optional Refinement)

For more sophisticated payloads, allocate budget across tier groups rather than strict waterfall:

| Intent | Primary (%) | Secondary (%) | Tail (%) |
|---|---|---|---|
| navigation | 70 | 20 | 10 |
| debugging | 50 | 30 | 20 |
| refactor | 50 | 30 | 20 |
| exploration | 40 | 40 | 20 |
| new feature | 30 | 40 | 30 |
| design question | 35 | 35 | 30 |
| **impact analysis** | **60** | **25** | **15** |

**Primary** = top 2 tiers in priority order  
**Secondary** = middle 2 tiers  
**Tail** = bottom 2 tiers

This prevents a single over-eager tier from starving others. Example: in debugging, code gets ~50% of the budget, cross-refs get ~30%, and specs/architecture/concepts/ideas share ~20%. For impact analysis, cross-refs (callers) dominate at 60%, with code and docs splitting the remainder to capture test coverage.

---

## Intent Classification

**Input:** User query (text)

**Current process:** deterministic keyword matching.

- Lowercase the query.
- Match standalone keywords with word boundaries and phrase keywords by substring.
- Score each matching intent by keyword specificity: multi-word phrases score higher than short generic words.
- Choose the primary intent by fixed precedence among intents that matched at least one keyword.
- Compute a normalized distribution across all matched intents.
- Mark `ambiguous=true` when the second-best score is close to the strongest score.
- Default to `exploration` with confidence `0.0` when no keyword matches.

**Primary intent precedence:**

```
debugging → impact_analysis → refactor → new_feature → design_question → navigation → exploration
```

This precedence is intentional but imperfect. For example, a query that includes both "why" and "where" routes as debugging; a query that includes "add" and "best way" routes as new_feature before design_question. The distribution/ambiguous metadata exists so these mixed cases are visible even before full multi-label routing.

**Deferred alternatives:**
- Lightweight LLM classification for ambiguous queries.
- Hybrid regex + LLM fallback.
- Learned classifier from feedback traces.

**Output:** Intent label + metadata (confidence, matched keywords)

Current serialized metadata:

```json
{
  "primary": "impact_analysis",
  "distribution": {
    "impact_analysis": 0.68,
    "debugging": 0.32
  },
  "confidence": 0.68,
  "ambiguous": true,
  "matched_keywords": {
    "impact_analysis": ["what breaks"],
    "debugging": ["break"]
  }
}
```

---

## Toward Intent as Retrieval Contract

Current limitation: `sidecar/context/intent_classifier.py` is still a keyword router. It finds matching words, chooses one primary intent by precedence, and stores a distribution for observability. Downstream, however, `ContextArbitrator` still routes almost the whole retrieval pipeline through `intent_signal.primary`, and `RankerScoring.intent_priors()` reduces intent to a coarse `symbol` / `doc` prior. The result is visible but shallow intent handling: useful as a first-pass hint, not yet a rich retrieval policy.

The next design step is to treat intent not as a question label, but as a **retrieval contract**: what evidence shapes must appear in the assembled prompt, and how strongly graph vs docs vs tests should be weighted.

Split the single `Intent` enum into multiple axes (orthogonal where possible):

- **`task`**: `locate`, `explain_behavior`, `trace_dependency`, `diagnose_failure`, `change_code`, `impact`
- **`evidence`**: `definition`, `call_chain`, `reverse_callers`, `runtime_registration`, `docs`, `tests`, `examples`
- **`answer_contract`**: e.g. find the location, explain the flow, produce blast-radius candidates, propose a change plan

Introduce an **`IntentPlan`** object (sketch):

```python
IntentPlan(
    task="trace_dependency",
    evidence=["call_chain", "runtime_registration", "imports"],
    graph_direction=["out", "imports", "semantic_hint"],
    role_targets=["api_surface", "runtime_surface", "representation_surface"],
    doc_bias=0.2,
    code_bias=0.8,
    test_policy="topic_only",
    confidence=0.74,
)
```

Use the existing **`distribution`**, not only the primary intent. This document already lists multi-label routing from `intent.distribution` as deferred, but the current code does not apply it to ranking. **`intent_weight`** should become the weighted sum of plausible intent policies, not the prior for a single primary enum such as `Intent.DEBUGGING`.

Add deeper retrieval modes aligned with benchmark-style questions:

- **`explain_behavior`** — prioritize mechanism roles + ordered call slices over raw neighborhood breadth.
- **`trace_dependency`** — bias toward forward/backward graph passes and import bridges; optional semantic rescue for missing edges.
- **`impact_analysis` / `impact`** — unify vocabulary so profile readiness, `/impact`, ranker floors, `AFFECTS`, tests, and public API surfaces all describe the same task.
- **`find_usage` / `locate`** — narrow symbol/doc retrieval before expanding graph radius.
- **`compare_design`** — bias toward architecture/spec docs plus representative implementations.
- **`implement_change`** — combine reference implementations, contracts, and narrow blast-radius candidates.

`trace_dependency` already exists implicitly as ranker mechanism/recovery logic, but it is not a first-class intent. That makes the system less perceptive: a dependency-marker question and a UI-component behavior question can both look like `exploration`, while the first needs runtime/provider tracing and the second may only need ordinary behavior explanation.

Intent planning should use more than text. The planner should consider:

- `query`
- selected `target`
- `repository_profile`
- `role_catalog`
- inferred `mechanism`
- available graph capabilities, including `AFFECTS` / impact readiness

Do not solve this by adding more keywords. A larger keyword table will become brittle quickly. Keywords should remain a cheap first pass, but the output should become a retrieval plan rather than only an enum.

First implementation step: add an `IntentPlanner` layer that accepts the current `IntentSignal`, target, repository profile, and mechanism, then returns an `IntentPlan`. Migrate the ranker gradually from `Intent` to `IntentPlan`, starting with `intent_weight`, budget floors, graph direction preferences, role requirements, and impact/AFFECTS candidate injection.

This section is **design-only** until `IntentPlan` (or equivalent) is threaded through `ContextArbitrator` → ranker weights / recovery hooks without breaking the existing prompt contract.

---

## Fallback to Standard Mode

If the top N tiers in the priority order produce zero matches:
- Try the next tier
- Continue until something is found
- If all tiers are empty → "standard mode" (no surgical context, bare LLM call)

---

## Examples

### Example 1: Debug a bug

**User query:** "Why does symbol extraction fail on relative imports?"

**Detected intent:** debugging

**Priority order:** code → cross-refs → specs → architecture → concept → idea

**Payload assembly:**
1. GraphExpander finds `PythonAdapter.extract_imports()` (code) ✓
2. Cross-refs include callers like `SymbolExtractor.extract()` ✓
3. Specs mention `spec_python_adapter.md` if exists
4. Architecture explains module design
5. Concepts / ideas unlikely to appear in budget

**Result:** LLM gets code + callers + spec. Can reason about the bug.

---

### Example 2: Add new feature

**User query:** "How do I add support for TypeScript class properties?"

**Detected intent:** new_feature

**Priority order:** idea → concept → architecture → specs → cross-refs → code

**Payload assembly:**
1. No code exists for TypeScript properties yet → code tier is empty
2. Concepts explain what "symbol extraction" means (architecture concept)
3. Architecture explains the adapter pattern
4. Specs describe the SymbolMetadata contract
5. Cross-refs to existing Python adapter as an example
6. Code appears only as reference

**Result:** LLM gets design context + patterns. Ready to propose implementation.

---

### Example 3: Refactor callgraph

**User query:** "Rename CALLS to CALLS_DIRECT everywhere in Neo4j."

**Detected intent:** refactor

**Priority order:** cross-refs → code → architecture → specs → concept → idea

**Payload assembly:**
1. Cross-refs computed first → all uses of CALLS in the code
2. Code shows each occurrence
3. Architecture explains relationship types
4. Specs describe Neo4j schema

**Result:** LLM sees the blast radius first, then the code to change.

---

## Status

**Phase:** Phase 6.1 implemented; Phase 9.2 multi-label routing deferred.  
**Reason:** The current keyword classifier is good enough for local v0.1 routing and observability, but mixed queries still collapse to one primary strategy. Retrieval precision is now a higher priority than replacing the classifier.

**Implemented:**
- Seven intent labels: navigation, debugging, refactor, exploration, new_feature, design_question, impact_analysis.
- Keyword-based primary classification.
- Intent distribution, confidence, ambiguous flag, and matched keyword metadata.
- Intent Resolution Contract against the index-time `repository_profile`.
- Prompt contract serialization of `effective_mode`, available capabilities, and risks.
- Prompt contract serialization under `intent_details`.
- Impact-analysis special handling in the ranker: higher floor, topic-sensitive test/example noise, and OR pass gate.

**Still deferred:**
- Multi-label budget routing from `intent.distribution`.
- `IntentPlanner` / `IntentPlan` retrieval contracts.
- First-class deep retrieval modes such as `trace_dependency`, `impact`, `find_usage`, `compare_design`, and `implement_change`.
- Shared impact retrieval contract between `/impact`, `AFFECTS`, and `impact_analysis` ranker mode.
- LLM fallback for ambiguous intent.
- Feedback-trained classifier.
- User-facing UI affordance when intent is ambiguous.
