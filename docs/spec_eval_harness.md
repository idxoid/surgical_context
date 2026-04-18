# Spec — Evaluation Harness (Phase 2.5)

> **Status:** Proposed. Blocks Phase 4 (SaaS) and Phase 5 (launch) per ADR-006.

## 1. Purpose

Every claim in [architectura.md §1.3](architectura.md) — "60–80% fewer tokens", "equal or better answer quality", "<200ms assembly" — is currently unfalsifiable. Without numbers, Phase 4 scales an unverified product.

The harness turns each claim into a CI-enforceable metric on a known fixture set. It is the first piece of infrastructure built in Phase 2.5, because every subsequent decision (token-budget BFS tuning, embedding-model swap, re-ranker weights) needs a measuring stick.

## 2. Success Criteria

The harness is "done" when all four are true:

1. `pytest tests/` runs green on a golden fixture repo with ≥30 (question → expected_symbols) pairs.
2. `python QA/qa_benchmark.py --report` emits a JSON metrics bundle: `recall@k`, `precision@k`, `tokens_surgical`, `tokens_carpet_bomb`, `assembly_ms_p50/p95`.
3. GitHub Actions runs the bundle on every PR and posts a delta comment (regressions block merge).
4. A single baseline row exists in `QA/baselines.jsonl` — without it, deltas are meaningless.

## 3. Fixture Design

### 3.1 Golden repo — `tests/fixtures/sample_project/`

A small but realistic Python project (~15 files, ~80 symbols) hand-crafted to exercise every retrieval edge case. **Not** auto-generated — hand-crafted, because the expected answers have to be trustworthy.

Required topologies:
- A symbol called by 5+ callers (fan-in) — tests caller-preference re-ranking.
- A symbol calling a 3-hop chain — tests depth budget.
- A decorated function (`@cached`, `@app.route`) — tests non-`CALLS` relationships.
- A class with inheritance across files — tests `DEPENDS_ON` edges.
- A symbol covered by two conflicting doc chunks — tests RAG re-ranking.
- A module-level constant referenced by 10+ symbols — tests constant pruning.
- A file with a syntax error — tests graceful indexer failure.

### 3.2 Question set — `tests/fixtures/questions.yaml`

```yaml
- id: q001
  symbol: process_payment
  question: "What validates the amount before the DB write?"
  expected_symbols: [validate_amount, PaymentError]
  expected_doc_chunks: ["docs/payments.md::2"]
  difficulty: easy
  intent: trace_dependency
```

Each entry: `id`, `symbol`, `question`, `expected_symbols` (must appear in `graph_context`), `expected_doc_chunks` (must appear in `documentation`), `difficulty` (easy/medium/hard), `intent` (`trace_dependency`, `explain_behavior`, `find_caller`, `impact_analysis`).

Target: 30 entries for v1, evenly split across intents. Grow to 100 before Phase 4.

## 4. Metrics

| Metric | Formula | Failure threshold |
|---|---|---|
| `recall@k` | `|retrieved ∩ expected| / |expected|` at k=5 graph deps | <0.80 blocks merge |
| `precision@k` | `|retrieved ∩ expected| / |retrieved|` | <0.60 blocks merge |
| `tokens_surgical` | tiktoken count of `to_system_prompt()` output | regression >10% blocks |
| `tokens_carpet_bomb` | tiktoken count of all files touched by any expected symbol | baseline only |
| `reduction_ratio` | `1 - tokens_surgical / tokens_carpet_bomb` | <0.50 blocks (target 0.60–0.80 per [architectura.md §1.3](architectura.md)) |
| `assembly_ms_p50` | wall-clock of `ContextArbitrator.get_context_for_symbol` | >200ms blocks |
| `assembly_ms_p95` | same, p95 | >500ms blocks |

Quality metric (answer correctness) is **intentionally deferred** — it requires an LLM judge, which introduces noise and cost. Recall@k is a proxy: if the right symbols are in the context, quality is the model's problem, not ours.

## 5. Module Layout

```
tests/
  conftest.py                    # pytest fixtures: temp Neo4j, temp LanceDB
  fixtures/
    sample_project/              # golden repo (committed)
    questions.yaml               # curated Q&A pairs
    expected_graph.json          # materialized expected Symbol/CALLS/DEPENDS_ON
  unit/
    test_parser.py               # tree-sitter extraction stability
    test_arbitrator_bfs.py       # BFS correctness on fixture
    test_overlay.py              # dirty-state reads
    test_indexer.py              # hash-based incremental upserts
  integration/
    test_ask_endpoint.py         # full /ask flow, LLM stubbed
    test_index_endpoint.py       # full /index flow on sample_project
QA/
  qa_benchmark.py                # reframed: loads questions.yaml, emits metrics JSON
  baselines.jsonl                # one row per commit on main
  judges/
    recall.py
    tokens.py
    latency.py
```

## 6. CI Integration

`.github/workflows/eval.yml` runs on every PR:

1. Boot Neo4j + LanceDB in services.
2. `pytest tests/` — unit + integration.
3. `python QA/qa_benchmark.py --report out.json`.
4. Load latest `baselines.jsonl` row from main.
5. Diff: any metric worse than its failure threshold fails the job.
6. Post comment: `recall@5 0.84 → 0.87 ✅ | tokens_surgical 1.2k → 1.5k ⚠️ (+25%)`.

On merge to main, a follow-up job appends a new row to `baselines.jsonl` with the commit SHA.

## 7. Non-Goals

- **Not** an answer-quality evaluator. LLM-as-judge is deferred to Phase 5 when Anthropic SDK is wired — cost and noise make it premature now.
- **Not** a load test. Latency is measured on a single-threaded synthetic workload; real concurrency testing lives in Phase 4.
- **Not** a regression suite for Ollama output. The LLM is stubbed in integration tests — we measure what *we* ship (the context), not what the model does with it.

## 8. Open Questions

- **Should fixtures include TypeScript?** Yes in v1.1 — the language adapter (ADR-005) is untested without a second language in CI.
- **Stub LLM vs. real Ollama in CI?** Stub. Real LLM means flaky CI and license risk on a shared runner. Real-LLM runs happen locally via `make eval-full`.
- **Where does the "carpet-bomb" baseline come from?** For each question, union the files of all expected symbols — that's a charitable approximation of what a naive tool would send.

## 9. Related

- [road_map.md](road_map.md) — Phase 2.5 checklist.
- [spec_token_budget_bfs.md](spec_token_budget_bfs.md) — depends on this harness for tuning.
- [architectura.md §2.4](architectura.md) — observability requirements this harness satisfies.
- ADR-006 — the blocking rationale.
