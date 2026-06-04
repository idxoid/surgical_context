# Intent Classifier — Spec

## Overview

**Purpose:** Detect the user's query intent, resolve it against the index-time repository capability contract, and rank retrieval tiers (code, specs, architecture, concepts, ideas) accordingly. Enables adaptive payload assembly while making unsupported or shallow reasoning explicit.

**Current status:** Implemented as a deterministic keyword classifier plus an Intent Resolution Contract in `sidecar/context/intent_classifier.py`. The classifier returns a primary desired intent plus observability metadata (`distribution`, `confidence`, `ambiguous`, `matched_keywords`). The resolver then intersects that desired intent with the `repository_profile` emitted by indexing and records an `effective_mode`, available capabilities, and risks. Initial multi-label routing is implemented through `IntentPolicy`: the primary intent remains the anchor, while strong or ambiguous secondary intents can blend tier order, symbol/doc priors, floor budget, doc-first behavior, and supplemental roles. Hard per-tier token buckets and a richer `IntentPlan` remain deferred.

A second layer — **question-shape modulation** — extracts orthogonal lexical features from the raw query text (entity count, flow vs. state verbs, scope qualifier, direction hint, wh-word, change / failure markers) and `modulate_shape(intent, q)` returns a per-question `TraversalShape` derived from the base. So EXPLORATION dynamically becomes an "explain" or a "trace" shape depending on what the question literally asks for, without splitting the intent vocabulary. The modulator's outputs are data-only today; the consumers (graph expander, unified ranker) still read the static `INTENT_TRAVERSAL` lookup. Wiring the modulated shape through `IntentSignal` is the next hop.

---

## Intent Dictionary (Source of Truth)

`sidecar/context/intent_classifier.py` defines four parallel dictionaries keyed by `Intent`. Together they describe everything a downstream consumer needs to know per intent, and the legacy structures the codebase reads (priority orderings, doc-first set, supplemental roles, chain-pursuit set) are now derived views over them.

### `INTENT_TRAVERSAL: dict[Intent, TraversalShape]`

Per-intent traversal parameters: how far / wide / which way the engine walks from the target symbol. `TraversalShape` is a frozen dataclass with six fields:

| field | type | meaning |
|---|---|---|
| `direction` | `tuple[str, ...]` | which side of the target to walk: `("forward",)` (callees), `("backward",)` (callers), `("forward", "backward")` (both), or `("self",)` (definition only) |
| `max_depth` | `int` | how many BFS hops are softened. 1–2 is shallow; 3–5 is medium; 6+ is "transitive — chase as far as the budget allows" |
| `chase_chains` | `bool` | follow registration / marker chains beyond `max_depth` (lets the expander reach a registered handler that sits deeper than the configured hop count) |
| `breadth` | `str` | rendered output character — `focused` / `medium` / `wide` |
| `doc_first` | `bool` | render docs ahead of code in the prompt context (the user reads patterns before writing code, etc.) |
| `tier_priority` | `tuple[str, ...]` | tier fill order, highest → lowest (drives `IntentConfig.PRIORITY` and the policy's `tier_scores`) |

`doc_first` is intentionally separate from `breadth` — REFACTORING is wide on code touchpoints but code-first in rendering; NEW_FEATURE is medium on candidates but doc-first because reading existing patterns precedes writing the new one.

### `INTENT_ROLE_PROFILE: dict[Intent, tuple[str, ...]]`

Roles that *may* participate in answering this intent. Not every role exists in every repository — the engine treats the profile as a baseline preference rather than a hard requirement. Today the profile is fed into `IntentClassifier._SECONDARY_INTENT_ROLES`, which the arbitrator still adds to `required_roles`. The soft-vs-hard split is a future hop.

Example (`EXPLORATION`, eight roles): `core_runtime`, `api_surface`, `docs_or_concept`, `composition_surface`, `runtime_surface`, `abstract_contract`, `orchestrator`, `registration_step`.

### `INTENT_EDGE_PRIORITY: dict[Intent, tuple[str, ...]]`

Edge types in priority order. The same edge can carry different evidence weight for different intents — `DEBUGGING` wants `HANDLES` (handler → error path) ahead of `HAS_API` (which carries surface, not flow); `NAVIGATION` wants `HAS_API` early (the surface IS the answer); `IMPACT_ANALYSIS` leads with `AFFECTS` and `DEPENDS_ON`. **Carries data only today — no consumer.** Wiring it into the graph expander's edge weighting is a future hop.

### `PACK_INTENT_TO_ENGINE: dict[str, Intent]`

Benchmark packs use a three-value vocabulary (`explain_behavior`, `trace_dependency`, `impact_analysis`); engine has seven. The mapping is conceptual — `trace_dependency` maps to `EXPLORATION` with `chase_chains=True` (the default for `EXPLORATION` in `INTENT_TRAVERSAL`), not a separate intent kind. Used by future benchmark overrides where the engine accepts a pack-annotated intent verbatim instead of running the text classifier.

### Derivation chain

```
INTENT_TRAVERSAL[i].chase_chains   →  unified_ranker._CHAIN_PURSUIT_INTENTS
INTENT_TRAVERSAL[i].doc_first      →  IntentClassifier._DOC_FIRST_INTENTS
INTENT_TRAVERSAL[i].tier_priority  →  IntentConfig.PRIORITY[i]
INTENT_ROLE_PROFILE                →  IntentClassifier._SECONDARY_INTENT_ROLES
```

`direction`, `max_depth`, `INTENT_EDGE_PRIORITY`, and `PACK_INTENT_TO_ENGINE` are data-only today; their consumers land in subsequent phases.

---

## Question-Shape Modulation

The intent enum tells the engine *what kind of question* it is. `QuestionShape` tells the engine *what the question literally asks for*. The base `TraversalShape` from `INTENT_TRAVERSAL` is then modulated by the question's signals so a single intent (EXPLORATION) can produce an "explain" or a "trace" shape on demand.

### `QuestionShape`

Eight orthogonal lexical features extracted from the raw query text. Each is independently checked so a misread of one signal shifts a single dimension downstream, not the whole intent.

| field | meaning | extraction |
|---|---|---|
| `entity_count` | distinct CamelCase / snake_case identifier tokens (excluding wh-stopwords) | regex |
| `has_flow_verb` | `flow` / `resolve` / `dispatch` / `route` / `wire` / `pass` / `propagate` / `forward` / `get from` / `goes through` | token + phrase match |
| `has_state_verb` | `work` / `behave` / `manage` / `decide` (single-mechanism explain shape) | token match |
| `scope` | `wide` when the query says `everywhere` / `across the codebase` / `throughout` / etc; `default` otherwise | phrase match |
| `direction_hint` | `definition` if `defined` / `implemented` / `located`; `usage` if `uses` / `calls` / `called by` / `imports` / `referenced` | phrase match |
| `has_failure_marker` | `fails` / `broken` / `doesn't work` / `throws` / `raised` / `error` / `wrong` | phrase match |
| `has_change_marker` | `if I change` / `if I rename` / `what breaks` / `what's affected` / `tests affected` | phrase match |
| `wh_word` | `where` / `how` / `why` / `what` / `which` | word-boundary regex |

### `modulate_shape(intent, q) -> TraversalShape`

Adjustment rule book — each rule modifies one dimension of the base `TraversalShape`, monotonically (an adjustment can only widen or specialise the base, never flip the intent's character):

| signal | adjustment |
|---|---|
| `entity_count >= 2` | `chase_chains = True`; `max_depth = max(base, base+2)` (chain across components) |
| `has_flow_verb` | `direction = ("forward",)`; `chase_chains = True` (flow verb names where the value goes) |
| `scope == "wide"` | `max_depth = max(base, 10)` (transitive) |
| `direction_hint == "definition"` | `direction = ("self",)` (definition only, no walk) |
| `direction_hint == "usage"` | `direction = ("backward",)` (callers / consumers only) |

`breadth`, `doc_first`, `tier_priority` are not modulated — they belong to the intent kind, not the question's surface form.

### Worked examples

| query | intent (classifier) | question signals | modulated shape (relative to base) |
|---|---|---|---|
| "Where is `HttpRouter` defined?" | NAVIGATION | `wh=where`, `direction_hint=definition`, `entity=1` | `direction = ("self",)` |
| "What uses `CacheManager`?" | NAVIGATION | `wh=what`, `direction_hint=usage`, `entity=1` | `direction = ("backward",)` (already base) |
| "How does `Context` manage state?" | EXPLORATION | `wh=how`, `state_verb`, `entity=1` | (no change — single-mechanism explain) |
| "How does dependency injection get resolved before the endpoint runs?" | EXPLORATION | `wh=how`, `flow_verb` (`resolved`), `entity=0` | `direction = ("forward",)`; `chase_chains` stays on |
| "How does `Controller` turn `DefaultRouter` routes into `HttpResponses`?" | EXPLORATION | `wh=how`, `flow_verb` (`turn`/`into` is not in the list but multi-entity is), `entity=3` | `chase_chains = True`; `max_depth += 2` |
| "How does `FastAPI` handle errors everywhere in the codebase?" | EXPLORATION | `wh=how`, `scope=wide`, `entity=1` | `max_depth = 10` |
| "If I change `ParamType`, what tests are affected?" | IMPACT_ANALYSIS | `change_marker`, `entity=1`, `wh=what` | (no change — base shape already backward-transitive) |

### Misread handling

The signal set is orthogonal by design: confusing `flow_verb` with `state_verb` only flips `direction` between `("forward",)` and the base both-ways walk, while leaving `chase_chains` / `max_depth` / `tier_priority` untouched. The earlier vocabulary-split attempt (`TRACE_DEPENDENCY` vs `EXPLAIN_BEHAVIOR` as separate enum values, classified from text) failed because the classifier had to pick one *intent* out of two for the same surface phrase. Modulation avoids that — every signal narrows a single dimension; mistakes degrade gracefully.

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

**Keywords matched:** "affected", "would be affected", "tests affected", "what tests", "test suites", "most likely to break", "most likely to be affected", "likely to break", "would break", "what would break", "what breaks", "are most likely"

**Ranker behavior:**
- Floor: 3000 minimum token budget
- Noise suppression: topic-related tests/examples keep `noise_factor = 1.0`; unrelated noisy candidates keep the standard penalty
- Priors: `symbol_prior = 0.3`, `doc_prior = 0.5` (emphasize dependencies + tests)
- Pass gate: **OR** semantics — either `role_recall ≥ 0.60` OR `file_recall ≥ 0.50` is sufficient (tests may not be indexed as symbols)

**Known gaps / current review findings:**
- `impact_analysis` classification now selects the highest score, with fixed precedence only as a tie-breaker. Keep regression tests for benchmark-style impact wording because `break`/`affected` phrases also overlap with debugging and refactor language.
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

**Current process:** deterministic keyword matching with phrase override pass.

1. Lowercase the query.
2. **Token pass** — match standalone keywords with word boundaries and phrase keywords by substring. Score each matching intent by keyword specificity: multi-word phrases score higher (1.5+) than long tokens (1.2) than short words (1.0).
3. **Phrase override pass** — scan `_PHRASE_OVERRIDES`, a list of `(phrase, intent, weight)` tuples. When a phrase matches the lowercased query, its weight is added directly to that intent's score regardless of token matches. Phrases cover cross-intent contexts where an action verb (add/remove) is instrumental rather than declarative, e.g. `"to understand why"` (+2.0 DEBUGGING), `"to fix"` (+1.5 DEBUGGING). Phrase weights are calibrated to beat single-token ambiguity.
4. The primary intent is the highest scorer. Ties are broken by the fixed precedence order.
5. Compute a normalized distribution across all matched intents.
6. Mark `ambiguous=true` when the second-best score is within 35% of the winner.
7. Default to `exploration` with confidence `0.0` when no keyword or phrase matches.

**Primary intent precedence (tie-breaker only):**

```
debugging → impact_analysis → refactor → new_feature → design_question → navigation → exploration
```

**Cross-intent false positive examples and their phrase fixes:**

| Query | Naive result | Phrase fix | Correct result |
|---|---|---|---|
| "add logging to understand why the function fails" | `exploration` (understand=1.2) | `"to understand why"` +2.0 DEBUGGING | `debugging` |
| "remove the old config to understand why startup is slow" | `exploration` | `"to understand why"` +2.0 DEBUGGING | `debugging` |
| "add caching to fix the slow query" | `new_feature` | `"to fix"` +1.5 DEBUGGING | `debugging` (ambiguous) |

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

Use the existing **`distribution`**, not only the primary intent. The current code already applies an initial `IntentPolicy` to blend tier order, priors, floor budget, doc-first mode, and supplemental roles. The remaining gap is a richer retrieval plan: graph direction preferences, evidence contracts, hard per-tier budget evaluation, and task-specific role requirements are still not first-class.

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

**Phase:** Phase 6.1 implemented; Phase 9.2 initial multi-label routing shipped.
**Reason:** The current keyword classifier is good enough for local routing and observability, and `IntentPolicy` now consumes the distribution for soft routing. Retrieval precision is now a higher priority than replacing the classifier.

**Implemented:**
- Seven intent labels: navigation, debugging, refactor, exploration, new_feature, design_question, impact_analysis.
- Keyword-based primary classification.
- Intent distribution, confidence, ambiguous flag, and matched keyword metadata.
- Intent Resolution Contract against the index-time `repository_profile`.
- Initial `IntentPolicy` consumption of `distribution` for blended tier order, secondary roles, priors, floor budget, and doc-first behavior.
- Prompt contract serialization of `effective_mode`, available capabilities, and risks.
- Prompt contract serialization under `intent_details`.
- Impact-analysis special handling in the ranker: higher floor, topic-sensitive test/example noise, and OR pass gate.

**Still deferred:**
- Hard per-tier budget routing from `intent.distribution`.
- `IntentPlanner` / `IntentPlan` retrieval contracts.
- First-class deep retrieval modes such as `trace_dependency`, `impact`, `find_usage`, `compare_design`, and `implement_change`.
- Shared impact retrieval contract between `/impact`, `AFFECTS`, and `impact_analysis` ranker mode.
- LLM fallback for ambiguous intent.
- Feedback-trained classifier.
- User-facing UI affordance when intent is ambiguous.
