# Week 1: Documentation & Synchronization

> **Goal:** Clarify product thesis, update architecture docs for Phase 9 status, and synchronize Ask/Inspect/Impact to share request context.
>
> **Duration:** 3 days (Mon–Wed)
>
> **Status:** In progress

---

## 1. Product Documentation Clarification

### 1.1 Current Status
- ✅ `concept.md` and `product_direction_memo.md` align well with local-first thesis
- ✅ `road_map.md` is accurate and detailed
- ✅ `local_development.md` setup path is clear

### 1.2 Updates Needed
- [ ] Update `architectura.md` § 2.4 (Observability) to reflect Phase 9.1 unified ranker completion
  - Add unified ranker score blending formula
  - Clarify current state of prompt-contract observability
  - List what's completed vs. deferred in Phase 9.4
- [ ] Update `architectura.md` § 4.1 (Prompt Lifecycle) to note mechanism-aware routing (Phase 4 output)
- [ ] Add Phase 9 status section to `road_map.md` confirming 9.1 complete, 9.2–9.4 in progress

### 1.3 Why This Matters
The architecture doc was last updated Apr 20. Since then:
- Phase 4 launched mechanism-aware evaluation with role-recall metrics
- Phase 9.1 unified ranker (graph + semantic) shipped
- Phase 9.3 (doc-anchor confidence/type) completed
- Phase 9.4 (prompt-contract observability) is ~70% done

Stale docs hide real progress and make reviews harder.

---

## 2. Ask / Inspect / Impact Synchronization

### 2.1 Current Problem
Each surface loads its own context:
```
Ask (webview)
  ↓
  POST /ask → context + answer
  ↓
  stateManager.lastContext = new context

Inspector (modal)
  ↓
  pushInspectorContext() → loads context again
  ↓
  Can diverge if overlay changes between calls

Impact (modal)
  ↓
  loadImpact() → POST /impact → new request
  ↓
  Unrelated to Ask context
```

**Result:** If user edits code, then clicks Inspector → Inspector shows different context than the Ask that was just run.

### 2.2 Target Architecture
```
Ask (webview) → POST /ask
  ↓
  Stores response in stateManager.lastRequest = {
    symbol, question, timestamp,
    context (full contract), answer, tokens
  }
  ↓
  Inspector reads stateManager.lastRequest.context
  Inspector shows same context that Ask used
  
  Impact (webview) → POST /impact
    ↓
    Uses stateManager.lastRequest.symbol
    Shows AFFECTS relationship for that symbol
```

**Key:** All three surfaces are stateless — they read from the shared `lastRequest` state.

### 2.3 Implementation Tasks
1. [ ] Extend `ExtensionState` to track `lastRequest` (symbol, question, timestamp, context, answer)
2. [ ] Update `SurgicalContextViewProvider.loadChat()` to store full response in `stateManager.lastRequest`
3. [ ] Update `pushInspectorContext()` to read from `stateManager.lastRequest.context` instead of fetching new context
4. [ ] Update `loadImpact()` to use `stateManager.lastRequest.symbol` as the target
5. [ ] Add 15min TTL to `lastRequest` (or clear on new file selection)
6. [ ] Test: Ask → Inspect → Edit file → Ask again → verify Inspector updates

### 2.4 Webview Changes
Chat Panel will now signal when a request completes:
```typescript
// In chat.html message handler:
case 'chat.done':
  // Ask completed. Inspector/Impact now have access to shared state.
  updateViews(['inspector', 'impact']);
  break;
```

---

## 3. Retrieval Metadata Visibility

### 3.1 Current State
Metadata is rich in the sidecar response:
- `context.scores` (graph + semantic per candidate)
- `context.provenance` (why each symbol was chosen)
- `context.metadata.ranker.*` (weights, intent, cache hits)
- `context.metadata.pruning_reasons` (budget overflow reasons)

**Problem:** Not visible in the webview. Users see answer + summary, but not the detailed scores.

### 3.2 Inspector Panel Enhancement
Add a collapsible "Ranking Details" section:

```
Retrieval Summary
├─ Intent: explain_behavior
├─ Mode: surgical_full
├─ Role Recall: 0.87
├─ Token Budget: 4000 / 4000 used
│
Ranking Scores (per symbol)
├─ [1.0] process_payment (graph + semantic: 0.92 + 0.98 overlap bonus)
├─ [0.92] charge_card (graph: 0.90, semantic: 0.60)
├─ [0.75] validate_amount (doc coverage, no graph match)
│
Cache & Assembly
├─ Cache hits: 2 (graph subgraph, embedding vector)
├─ Phases: extract (5ms) → rank (12ms) → deduplicate (2ms) → resolve (8ms) → compile (18ms)
└─ Trace ID: abc123...
```

### 3.3 Implementation
1. [ ] Add `RankingDetails` webview component
2. [ ] Update Inspector to render collapsible scoring data from `context.scores`
3. [ ] Add "show/hide" toggle in Inspector settings
4. [ ] Test on real repo: verify scores make sense for a few Ask queries

---

## 4. Immediate Next Steps (by day)

### Monday
- [ ] Update `architectura.md` with Phase 9 status
- [ ] Add Phase 9 section to `road_map.md`
- [ ] Review code for Ask/Inspect/Impact state flow

### Tuesday
- [ ] Implement `lastRequest` state tracking
- [ ] Update Inspector to read from shared state
- [ ] Test Ask → Inspect → Impact flow

### Wednesday
- [ ] Add RankingDetails component
- [ ] Full end-to-end test on sample repo
- [ ] Prepare branch for Week 2 (real-repo benchmark runs)

---

## 5. Acceptance Criteria

✅ **Docs**
- [ ] `architectura.md` reflects Phase 9.1/9.4 completion
- [ ] No stale language about "planned" features that are shipped
- [ ] Phase transitions are clear (Phase 9 → Phase 10 candidate next)

✅ **Ask/Inspect/Impact Sync**
- [ ] Ask response stored in `stateManager.lastRequest`
- [ ] Inspector reads from shared state (no new fetch)
- [ ] Impact uses shared symbol (no caller selection needed)
- [ ] TTL prevents stale context after edits

✅ **Metadata Visibility**
- [ ] Inspector shows collapsible Ranking Details
- [ ] Scores align with sidecar's `context.scores` output
- [ ] Cache hits and phase latencies visible
- [ ] Verified on at least one real Ask query

---

## 6. Notes

**Out of scope for Week 1:**
- Keyboard shortcuts (Phase 10.5)
- Full accessibility audit (Phase 10.5)
- Settings UI for model preference / workspace (Phase 10.5)

**Risk:** Metadata visibility requires webview component work; if complex, defer display to Week 2 + focus on state sync.

**Decision gate:** If Ask/Inspect/Impact sync reveals architectural issues (e.g., overlay state colliding between surfaces), escalate before moving to real-repo testing.
