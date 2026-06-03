# Spec — Multi-Label Intent (Phase 9)

> **Status:** Initially implemented for local retrieval. `IntentSignal.distribution` is converted into an `IntentPolicy` that blends tier priority, adds secondary-intent role requirements, blends symbol/doc priors, adjusts floor budget, and exposes policy metadata in the prompt contract. Explicit hard per-tier budget buckets and learned classification remain future work.

## 1. Problem

Current classifier returns one of 7 labels: `navigation | debugging | refactor | exploration | new_feature | design_question | impact_analysis`.

Real queries are mixtures:

| Query | True decomposition |
|---|---|
| "Why does `process_payment` fail after the refactor?" | debugging 0.6, refactor 0.3, exploration 0.1 |
| "How should I add retry logic here?" | new_feature 0.5, design_question 0.3, exploration 0.2 |
| "Where is the code that handles currency conversion, and does it still match the spec?" | navigation 0.5, design_question 0.4, exploration 0.1 |

Forcing a single label throws away signal the tier-filling logic would use. Example: a debugging+refactor query should surface callees (debug signal) **and** reverse dependencies (refactor signal). Pure debugging drops AFFECTS; pure refactor drops callees. Either loses half the answer.

## 2. Design

### 2.1 Intent Distribution

Classifier returns a probability distribution over labels:

```python
@dataclass
class IntentDistribution:
    weights: dict[str, float]  # {label: weight}, sums to 1.0

    def top(self) -> str:
        return max(self.weights, key=self.weights.get)

    def is_ambiguous(self) -> bool:
        return self.top_weight() < 0.5  # no single label > half
```

Sum-to-1 invariant enforced on construction. Missing labels default to 0.

Current implementation name: `IntentSignal.distribution`. It is paired with
`primary`, `confidence`, `ambiguous`, and `matched_keywords`.

### 2.2 Classifier Output

Keyword heuristics v2 produces partial scores per label, applies phrase
overrides, then normalizes:

```python
def classify(query: str) -> IntentDistribution:
    raw = {label: 0.0 for label in LABELS}

    for keyword, (label, weight) in KEYWORD_TABLE.items():
        if keyword in query.lower():
            raw[label] += weight

    total = sum(raw.values())
    return IntentDistribution({k: v / total for k, v in raw.items()})
```

Single-label queries still concentrate mass (~0.9 on one label). Multi-intent queries spread naturally across the keywords they match.

If nothing matches, the runtime classifier returns `exploration` with confidence
`0.0` instead of smoothing every query with a non-zero exploration baseline.

### 2.3 Tier Priority as Weighted Sum

Each single intent has a 6-tier priority vector from the existing `IntentConfig`. Weighted distribution produces a blended tier score:

```python
# Per-tier score across all labels:
tier_score[tier] = sum(
    distribution.weights[label] * tier_rank_of(tier, label)
    for label in distribution.weights
)
```

`tier_rank_of(tier, label)` returns the tier's priority rank for that label (higher = more preferred). The final tier order is `sorted(tiers, key=tier_score, desc=True)`.

Example: `{debugging: 0.6, refactor: 0.3, exploration: 0.1}` blends:
- `code` (high for debug + refactor) → dominates.
- `cross_refs` (high for debug, medium for refactor) → strong second.
- `architecture` (medium for refactor) → minor boost.
- `concept` (low everywhere) → tail.

Cleaner outcome than "pick debugging, ignore the rest".

### 2.4 Budget Allocation

Token budget is split across tiers in proportion to blended tier scores:

```python
total_score = sum(tier_score.values())
tier_budget[tier] = int(remaining_budget * tier_score[tier] / total_score)
```

Soft cap: a floor (100 tokens) on every tier with score > 0 prevents narrow distributions from starving adjacent tiers entirely. Tiers with zero score get zero budget.

Current implementation uses this as a soft policy rather than a hard allocator:
- `IntentPolicy.tier_order` drives doc tier ordering in `PromptCompiler`.
- strong secondary intents add supplemental retrieval roles before ranker selection.
- symbol/doc priors are blended across active intents.
- the ranker floor is the weighted floor across active intents, never below the primary intent floor.
- doc-first behavior activates when `new_feature`, `design_question`, or `impact_analysis` has meaningful active share.

Hard per-tier token buckets are still deferred until real-repo calibration proves the soft policy is under-serving mixed queries.

### 2.5 Mode Determination

The existing `mode` field ("surgical_full" | "surgical_doc_only" | "standard") generalizes:

- `surgical_full`: code tier filled + at least one cross-ref or doc tier filled.
- `surgical_doc_only`: no code available, doc tiers filled.
- `standard`: all tiers empty (fallback).

Multi-label distribution does not change mode semantics, only which tiers qualify as "filled".

### 2.6 Ambiguity Signal

`IntentDistribution.is_ambiguous()` exposed in the contract. Clients can:
- Render "did you mean X or Y?" disambiguation UI.
- Choose a deeper LLM model for ambiguous queries (route to Claude regardless of token size).
- Fall back to `exploration` priors as a conservative default.

Ambiguity is observability, not an error.

## 3. API / Interface

```python
# sidecar/context/intent_classifier.py (extended)

class IntentClassifier:
    def classify(self, query: str) -> IntentDistribution:
        """Return weighted distribution over intent labels."""

    def classify_label(self, query: str) -> str:
        """Back-compat: return single top label. Calls classify() internally."""
```

`PromptCompiler.compile_with_intent()` accepts either `str` (legacy) or `IntentDistribution` (new). Internally normalized to distribution.

`IntentConfig` stays as-is — the tier-rank mapping is data the blender reads.

Runtime compatibility note: `PromptCompiler.compile_with_intent()` still accepts
the primary `Intent`; `ContextArbitrator` passes `IntentPolicy.tier_order` into
the compiler and stores policy metadata in the prompt contract. The ranker also
uses the policy for supplemental roles, blended priors, and floor budget.

## 4. Prompt Contract Impact

The prompt contract keeps the legacy string `intent` field and adds structured
metadata under `intent_details`:

```json
{
  "intent": "debugging",
  "intent_details": {
    "primary": "debugging",
    "distribution": {
      "debugging": 0.6,
      "refactor": 0.3,
      "exploration": 0.1
    },
    "ambiguous": false
  }
}
```

`budget.intent_policy` additionally exposes `active_intents`,
`secondary_intents`, `budget_share`, `tier_order`, `supplemental_roles`, and
`doc_first`.

## 5. Examples

```python
classifier = IntentClassifier()

d = classifier.classify("why does process_payment fail after refactor?")
# IntentDistribution(weights={
#     "debugging": 0.55,
#     "refactor": 0.30,
#     "exploration": 0.15,
#     "navigation": 0.0,
#     "new_feature": 0.0,
#     "design_question": 0.0,
#     "impact_analysis": 0.0,
# })
# d.top() → "debugging"
# d.is_ambiguous() → False

d2 = classifier.classify("what's in this file")
# {"navigation": 0.45, "exploration": 0.45, ...rest tiny}
# d2.is_ambiguous() → True   (top < 0.5)
```

## 6. Limitations (current)

- Keyword heuristics cannot disambiguate word-sense. "Fix" in "fix the bug" vs "fix the spec" both score debugging; ML classifier (Phase 10) fixes this.
- Distribution is flat across a query — a long query that mixes intents sentence-by-sentence collapses to one distribution. Per-sentence decomposition is out of scope.
- Weight normalization to sum=1 hides absolute confidence. A weakly-classified query looks identical to a confidently-split one. Mitigation: keep the raw pre-normalization sum as a `confidence` field in the contract for clients that want it.

## 7. Planned Extensions

- **Learned classifier** (Phase 10): small transformer trained on (query, accepted_tiers) pairs from the feedback loop.
- **Temporal intent shift:** if the user's last three queries were debugging-heavy, give debugging a prior bump on the next query.
- **Query rewriting:** for ambiguous queries, generate clarifying rephrases and show the user before retrieval — trades latency for precision on hard cases.

## 8. Related

- [spec_intent_classifier.md](spec_intent_classifier.md) — the single-label classifier this extends.
- [spec_unified_ranking.md](spec_unified_ranking.md) — consumes `intent_weight(c)` derived from the distribution.
- [spec_eval_harness.md](spec_eval_harness.md) — intent distribution accuracy is measurable per-label; add precision/recall-per-label metrics.
- [spec_learning_loop.md](spec_learning_loop.md) — feedback on accepted retrievals refines the classifier.
