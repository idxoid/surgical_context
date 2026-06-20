# Spec — Learning Loop (Phase 10)

> **Status:** Feedback telemetry slice implemented; adaptive learning proposed. `/ask` and `/ask/stream` issue workspace/user-scoped tokens, append sanitized retrieval snapshots and feedback events to bounded JSONL logs, and expose joined examples. No online weight updates, classifier retraining, CO_RELEVANT edges, or feedback stats endpoint exist yet.

## 1. Problem

The system records explicit/implicit feedback, but retrieval still starts from the same structural policy and does not adapt from those events. Two failure modes remain:

- **Silent drift:** ranker weights that were correct last quarter are wrong now; we never find out.
- **Silent miss:** the right symbol scored 0.4, just under threshold; the user worked around it; we never learned.

Without a feedback signal, every improvement is manual — via offline eval harness runs — and every regression is invisible between runs. A retrieval system without a loop eventually loses to one with a loop, even if it starts smarter.

## 2. Design

### 2.1 Signal Types

Four feedback signals, roughly ordered by cost vs. value:

| Signal | Source | Interpretation |
|---|---|---|
| **Implicit-accept** | User sends follow-up query referencing a retrieved symbol | Retrieved symbol was useful |
| **Implicit-reject** | User asks same question with rephrasing / different symbol within N minutes | Retrieval missed |
| **Explicit-accept** | User clicks 👍 on response | Strong positive |
| **Explicit-reject** | User clicks 👎 or edits with missing-context annotation | Strong negative with detail |

Every retrieval emits a `feedback_token` — an opaque server-issued handle that binds the later signal to the exact retrieval ranking. Client sends it back on feedback events.

### 2.2 Feedback Event Schema

```json
POST /feedback
{
  "feedback_token": "fbk_abc123...",
  "kind": "explicit_reject",
  "details": {
    "missing_symbols": ["RateLimiter.check"],      // optional
    "wrong_symbols": ["legacy_charge_flow"],        // optional
    "correct_intent": "debugging",                  // optional
    "comment": "I was asking about timeout errors"  // optional
  },
  "timestamp": "2026-05-01T12:34:56Z"
}
```

Server writes to `.surgical_context/feedback.jsonl` and keeps retrieval snapshots in `.surgical_context/retrieval_snapshots.jsonl` by default. Paths and rotation limits are configurable with `FEEDBACK_LOG_PATH`, `FEEDBACK_SNAPSHOT_PATH`, `FEEDBACK_JSONL_MAX_BYTES`, and `FEEDBACK_JSONL_MAX_LINES`. Free-text comments are reduced to presence/length metadata before persistence.

### 2.3 Attribution

Each retrieval snapshot persisted at emit time with:

```python
@dataclass
class RetrievalSnapshot:
    feedback_token: str
    workspace_id: str
    user_id: str
    trace_id: str
    symbol: str
    intent: str
    mode: str
    question_hash: str
    question_tokens: int
    context_pipeline_version: str
    selected_candidates: list[dict]
    documentation: list[dict]
    context_metadata: dict
    timestamp: str
```

Feedback events join on `feedback_token` to produce labeled examples.

### 2.4 Signal Processing — Two Loops

**Fast loop (online, per user):**

Per-user exponential-moving-average adjustments to tier priors:

- Accept on a `debugging`-classified query with `cross_refs` tier dominating → bump this user's `debugging → cross_refs` prior by a small ε.
- Reject → bump down.

Adjustments stored as per-user deltas; global defaults untouched. Caps prevent any single user's preferences from dominating their own retrieval (max ±20% from default).

**Slow loop (offline, cross-user):**

Nightly job reads the feedback log, builds `(query, accepted_symbols, rejected_symbols)` tuples, runs:

1. **Weight sweep:** grid-search `α, β, γ, δ, ε` against the harness *augmented with new feedback as additional test cases*. Propose new global defaults if Pareto-better.
2. **Classifier retrain:** if ≥ 500 labeled queries accumulated, retrain the intent classifier (Phase 10 ML upgrade). Ship as a new model version alongside the heuristic — A/B gate before promotion.
3. **Anchor classifier drift check:** for chunks that participated in accepts/rejects, compare classifier confidence to outcome. Low-confidence accepts and high-confidence rejects flagged for manual review.

Nothing ships to production automatically from the slow loop. Every proposed weight change is a PR with a harness diff and gets human approval — the loop learns, humans deploy.

### 2.5 Boost / Downweight Table

A supplementary Neo4j relation stores learned per-pair weights:

```cypher
(s:Symbol)-[:CO_RELEVANT {
    boost: 0.3,           // positive boost or negative downweight
    source: "feedback",
    evidence_count: 17,
    updated_at: datetime
}]->(t:Symbol)
```

Indicates "users who asked about `s` found `t` useful" with confidence proportional to evidence count. Retrieved via the unified ranker as an additional candidate source — adds candidates the pure graph didn't reach.

This is the part of the system that can get **smarter over time without changing code**.

### 2.6 Privacy & Workspace Scoping

- Feedback is per-workspace. Accepts on `acme/repo@feature-x` do not influence retrieval on `other-tenant/their-repo`.
- User-level deltas opt-out configurable — users can disable personalization if uncomfortable.
- Explicit feedback comments never feed ML training without a redaction pass (PII, credentials). Implicit signals are structural — safe.

### 2.7 Metrics

Dashboard surfaces:
- Feedback rate (explicit per 1k queries).
- Accept / reject ratio, trend.
- Coverage: fraction of queries receiving any feedback within 10 minutes.
- Impact: harness score on the original golden set vs. on golden + feedback-derived cases.

Low coverage means the loop is inert — signals aren't enough to trust. Triage accordingly.

## 3. API / Interface

```python
# context_engine/feedback/store.py

@dataclass
class FeedbackEvent:
    feedback_token: str
    kind: str           # "implicit_accept" | "implicit_reject" | "explicit_accept" | "explicit_reject"
    details: dict
    timestamp: datetime

class FeedbackStore:
    def record_snapshot(self, snapshot: RetrievalSnapshot) -> None: ...
    def get_snapshot(self, token: str) -> RetrievalSnapshot | None: ...
    def record_feedback(self, event: FeedbackEvent) -> None: ...
    def feedback_examples(self, limit: int = 200) -> list[dict]: ...
```

```python
# context_engine/feedback/ranker_update.py

class PersonalizedWeights:
    """Per-user deltas over the global ranker weights."""
    def get_for(self, user_id: str, workspace_id: str) -> dict[str, float]: ...
    def apply_feedback(self, event: FeedbackEvent, snapshot: RetrievalSnapshot) -> None: ...
```

Endpoints:

```
POST /feedback                   # record a feedback event
GET  /feedback/stats?window=24h  # planned, not implemented
```

## 4. Examples

```python
# 1. Retrieval happens
response = requests.post("/ask", json={
    "symbol": "process_payment",
    "question": "why slow?"
}).json()
# response["feedback_token"] == "fbk_9f2a1..."

# 2. User clicks 👎 and notes "I was looking for the timeout logic"
requests.post("/feedback", json={
    "feedback_token": "fbk_9f2a1...",
    "kind": "explicit_reject",
    "details": {"missing_symbols": ["RequestTimeout.apply"], "comment": "timeout logic"}
})

# 3. Snapshot + sanitized feedback event are appended to JSONL.
# 4. FeedbackStore.feedback_examples() can join the token to a labeled example.
# Automated tuning and learned edges are future work.
```

## 5. Limitations (current)

- Implicit signals are noisy — "follow-up query" heuristics misread exploration as rejection. Require N ≥ 20 events before trusting any inferred pattern.
- Cross-user generalization is weak at low scale — a 5-person team produces thin gradients. Personalized deltas work fine; global weight updates need more traffic.
- Feedback loop cannot recover from a *missing* candidate: if the right symbol never made the pool, no signal tells us about it. Mitigation: the `CO_RELEVANT` table can seed future pools from outside the graph's structural reach.
- Adversarial risk: a malicious user could spam 👎 to degrade retrieval. Mitigate by rate-limiting feedback per user and requiring quorum (N different users) before a signal affects global weights.
- Current telemetry is append-only local JSONL and is not consumed by the live axis ranker.

## 6. Planned Extensions

- **Active learning:** when the ranker is uncertain (close scores, wide intent distribution), surface a "which of these was relevant?" prompt post-answer to cheaply acquire labels.
- **Contrastive training:** use rejected+accepted pairs to train a small cross-encoder that reranks the top-20 candidates.
- **Cross-tenant knowledge transfer (opt-in):** allow a user to contribute anonymized labels to a shared model — paid tier for organizations that want to benefit from the collective.
- **Negative feature learning:** if a class of symbols (e.g., test fixtures) consistently gets rejected, down-weight them systemically.

## 7. Related

- [spec_prompt_contract_observability.md](spec_prompt_contract_observability.md) — `feedback_token` and score snapshots originate here.
- spec_unified_ranking.md (removed) — consumer of learned weights.
- spec_multi_label_intent.md (removed) — the ML classifier retrained in the slow loop replaces keyword heuristics.
- [spec_eval_harness.md](spec_eval_harness.md) — harness is the gatekeeper; no learned weights ship without passing it.
