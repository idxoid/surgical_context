# Intent Classifier — Spec

## Overview

**Purpose:** Detect the user's query intent and rank retrieval tiers (code, specs, architecture, concepts, ideas) accordingly. Enables adaptive payload assembly — different intents prioritize different content types.

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

**Process:** TBD — options include:
- Regex pattern matching on keywords ("where", "why", "change", "add", etc.)
- Lightweight LLM classification (small model or prompt-cached Claude)
- Hybrid (regex + LLM fallback)

**Output:** Intent label + metadata (confidence, matched keywords)

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

**Phase:** 6+ (post-Phase 5)  
**Reason:** Currently precision is low due to fixture scope, not BFS tuning. Fix retrieval quality first.

**When to implement:**
- After Phase 5 (AFFECTS index, typed edges) is stable
- Once doc-code semantic linking improves
- When retrieval precision reaches >60% on golden set
