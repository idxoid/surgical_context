# Spec Update Checklist — Phase 4 Changes

Phase 4 (mechanism-aware benchmark evaluation) introduced new concepts and changes that should be reflected in related specs. This checklist tracks which specs need updating and what sections are affected.

## Critical Updates Required

### 1. ✅ spec_intent_classifier.md
**Status:** NEEDS UPDATE

**Changes needed:**
- Add `IMPACT_ANALYSIS` intent type with:
  - Question pattern examples
  - Priority order: `cross_refs → code → specs → architecture → concept → idea`
  - Rationale: tests are load-bearing for change impact; need to see what breaks
  - Special handling: impact analysis questions are not penalized for hitting test files
  - Keyword examples: "most likely to break", "what parts", "what breaks", "are most likely"

**Why:** The spec currently covers 6 intents but IMPACT_ANALYSIS was added in Phase 4.

**Location:** Add new section after "Design Question" (§5.5 or similar)

---

### 2. ✅ spec_eval_harness.md
**Status:** NEEDS UPDATE

**Changes needed:**
- Update question set specification to include mechanism classification:
  - Add `mechanism` field (e.g., `fastapi_route_registration`)
  - Add `required_roles` field (list of code roles that must be fulfilled)
  - Add `expected_mode` field (`symbol` or `workspace`)
  
- Update metrics section to include:
  - `role_recall` metric definition: `(required_roles not in missing_roles) / len(required_roles)`
  - Intent-stratified pass gates table:
    | Intent | role_recall floor | file_recall floor | Gate semantics |
    | explain_behavior | 0.70 | 0.50 | AND |
    | trace_dependency | 0.80 | 0.70 | AND |
    | impact_analysis | 0.60 | 0.50 | OR |
  
- Add benchmark output format:
  - Per-question display: `[intent] | role={role_recall:.2f} | file={file_recall:.2f}`
  - Summary: include `avg_role_recall` metric

**Why:** The harness spec was written before mechanism classification existed. Phase 4 adds these as first-class evaluation concepts.

**Location:** Update §3 (Fixture Design), §4 (Metrics), and add example output format

---

### 3. ⚠️ spec_unified_ranking.md
**Status:** PARTIAL — CHECK INTENT INTEGRATION

**Current state:** Spec exists and was recently added (commit d49f01f)

**Verify:**
- Does it document intent-aware noise suppression for IMPACT_ANALYSIS?
- Does it explain how noise_factor is computed per intent?
- Does it document intent-specific token floors and priors?

**If missing, add:**
- Section on intent-aware noise filtering:
  - IMPACT_ANALYSIS: `noise_factor = 1.0` (no penalty)
  - Other intents: `noise_factor = compute_noise_factor(file_path)` (std 0.15 for tests)
  - IMPACT_ANALYSIS floors: 3000-token minimum floor
  - Priors: IMPACT_ANALYSIS gets `symbol=0.3, doc=0.5`

**Why:** The Phase 4 ranker modifications for IMPACT_ANALYSIS intent should be spec'd out for future maintenance.

**Location:** Add subsection in §2 (Design) or new §2.8 "Intent-Aware Noise Filtering"

---

## Important Reference Updates

### 4. spec_prompt_contract_observability.md
**Status:** CHECK IF mechanism FIELD IS DOCUMENTED

**Check:**
- Does the full contract example in §2.1 include `mechanism` field?
- Does it document `missing_roles` array?
- Does it explain the role_recall metric?

**If missing, add to metadata block:**
```json
{
  "metadata": {
    "mechanism": "fastapi_route_registration",
    "missing_roles": ["route_object", "handler_or_lifecycle"],
    "pruned_details": [
      {"uid": "...", "reason": "pool_exhausted", "token_cost": 320}
    ],
    "stopped_reason": "pool_exhausted",
    "ranker": "unified",
    "ranker_weights": {"alpha": 1.0, "beta": 0.8, "gamma": 0.4, "delta": 0.5, "epsilon": 0.5}
  }
}
```

**Why:** Phase 4 adds these fields to PromptContext; the observability spec should document them in the contract.

**Location:** Update §2.1 (Full Contract) and add explanation in §3 (Design)

---

### 5. spec_affects_index.md
**Status:** REFERENCE — CONSIDER ENHANCEMENT

**Current state:** Exists and documents Phase 5 work

**Enhancement idea (not blocking):**
- Could use role_recall to improve AFFECTS edge scoring/weighting
- Impact analysis questions could use AFFECTS to surface affected test files
- Consider adding example: "If we change validate_amount, what tests break?" → uses AFFECTS + role_recall

**Why:** AFFECTS and role_recall work well together for impact analysis, but this is future optimization.

**Location:** §2 (Design) or new subsection "Synergy with Impact Analysis (Phase 4+)"

---

## Documentation-Only Updates

### 6. docs/architectura.md
**Status:** ✅ ALREADY UPDATED (commit 8021e43)

Sections updated:
- §2.4 Observability: mechanism-aware metrics and role_recall
- §4.1 Prompt Lifecycle: IMPACT_ANALYSIS intent and unified ranker
- §5.3 JSON Prompt Contract: new metadata fields documented

---

### 7. PHASE_4_SUMMARY.md
**Status:** ✅ CREATED (commit 265d4fd)

Comprehensive summary of all Phase 4 changes for future reference.

---

## Timeline and Precedence

**Immediate (before Phase 5):**
1. ✅ spec_intent_classifier.md — add IMPACT_ANALYSIS
2. ✅ spec_eval_harness.md — add mechanism classification, role_recall, intent-stratified gates
3. ⚠️ spec_unified_ranking.md — verify/add intent-aware noise filtering

**Soon (Phase 5 planning):**
4. spec_prompt_contract_observability.md — document mechanism and missing_roles in contract
5. spec_affects_index.md — note synergy with Phase 4 role_recall (enhancement, not blocking)

**Already Done:**
- ✅ docs/architectura.md (main architecture doc)
- ✅ PHASE_4_SUMMARY.md (implementation summary)

---

## Summary Table

| Spec | Status | Priority | Est. Work |
|---|---|---|---|
| spec_intent_classifier.md | NEEDS UPDATE | High | 30 min |
| spec_eval_harness.md | NEEDS UPDATE | High | 45 min |
| spec_unified_ranking.md | CHECK INTENT | High | 20 min |
| spec_prompt_contract_observability.md | CHECK mechanism | Medium | 20 min |
| spec_affects_index.md | REFERENCE | Low | 10 min |
| docs/architectura.md | ✅ DONE | — | — |
| PHASE_4_SUMMARY.md | ✅ DONE | — | — |

**Total estimated effort:** ~2 hours to complete all spec updates.
