# Spec — Evaluation Harness (Phase 2.5)

> **Status:** Implemented locally and used actively for retrieval tuning. The remaining gap is CI automation of benchmark deltas; the harness, reports, real-repo pack, and baseline appends already exist in the repo.

## 1. Purpose

Every claim in [architectura.md §1.3](architectura.md) — "60–80% fewer tokens", "equal or better answer quality", "<200ms assembly" — is currently unfalsifiable. Without numbers, Phase 4 scales an unverified product.

The harness turns each claim into a CI-enforceable metric on a known fixture set. It is the first piece of infrastructure built in Phase 2.5, because every subsequent decision (token-budget BFS tuning, embedding-model swap, re-ranker weights) needs a measuring stick.

## 2. Success Criteria

The harness is "fully productized" when all four are true:

1. `pytest tests/` runs green on a golden fixture repo with ≥30 (question → expected_symbols) pairs.
2. `python QA/qa_benchmark.py` always emits a JSON metrics bundle and prints its path at the end of the run. `--report` is now an explicit output-path override, not the switch that enables report writing. The bundle includes `recall@k`, `precision`, `role_recall`, `file_recall`, `tokens_surgical`, `tokens_carpet_bomb`, `assembly_ms_avg`, and per-question `ready_context`.
3. Each run also appends a compact pointer row to `QA/benchmark_runs.jsonl` unless `--no-snapshot-manifest` is passed. The row records repo, core12 flag, commit, branch, report path, pass rate, precision, recall, tokens, reduction, and assembly time so `/tmp` reports remain discoverable. `QA/benchmark_runs.py` prints the recent rows, compares the latest rows for a repo, and audits `ready_context.contract.pruned[]` reasons from the referenced full reports.
4. GitHub Actions runs the bundle on every PR and posts a delta comment (regressions block merge).
5. A baseline row exists in `QA/baselines.jsonl` — without it, deltas are meaningless.

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

### 3.2 Question set — `tests/fixtures/real_repo_question_pack.yaml`

```yaml
- id: fastapi_q06
  repo: fastapi
  symbol: serialize_response
  question: "If I change response model serialization behavior, what parts of the framework and tests are most likely to break?"
  expected_mode: symbol
  mechanism: fastapi_serialization_impact
  required_roles: [affected_runtime, affected_public_api, affected_tests]
  expected_symbols: [...]
  expected_files: [...]
  difficulty: medium
  intent: impact_analysis
```

Each entry: `id`, `repo`, `symbol`, `question`, `expected_mode` (`symbol` or `workspace`), **`mechanism`** (code relationship type), **`required_roles`** (list of roles ranker must fulfill), `expected_symbols`, `expected_files`, `difficulty`, `intent`.

**Current additions beyond the original Phase 2.5 design:**
- **`mechanism`**: Classifies which code relationship is being tested (e.g., `fastapi_route_registration`, `pydantic_validation_core_bridge`, `rtk_slice_generation`). Enables diagnosing architectural gaps vs. ranking noise.
- **`required_roles`**: List of code roles the ranker must find. The YAML may use legacy names, but benchmark scoring normalizes them into the canonical role taxonomy before computing `role_recall`.
- **`expected_mode`**: Either `symbol` (should find by name) or `workspace` (correct answer is "not found" — used for negative test cases like nonexistent symbols).

Target: 30 entries for Phase 2.5 (fixture), 20+ for Phase 4 (real-repo pack, FastAPI/Pydantic/Redux Toolkit).

## 4. Metrics

### 4.1 Symbol Retrieval Metrics (legacy, used for fixture pack)

| Metric | Formula | Failure threshold |
|---|---|---|
| `recall@k` | `|retrieved ∩ expected| / |expected|` at k=5 graph deps | <0.80 blocks merge |
| `precision@k` | `|retrieved ∩ expected| / |retrieved|` | <0.60 blocks merge |

### 4.2 Mechanism-Aware Metrics (Phase 4, used for real-repo pack)

| Metric | Formula | Semantics |
|---|---|---|
| **`role_recall`** | `normalize(required_roles - missing_roles) / len(normalize(required_roles))` | Fraction of required code roles the ranker fulfilled on the canonical role scale. Diagnostic for code relationship discovery gaps. |
| **`file_recall`** | `|retrieved_files ∩ expected_files| / |expected_files|` | Fraction of expected files included. Tests ranking noise and code coverage. |
| **Intent-stratified pass gate** | See table below | Different intents have different acceptable thresholds. |

**Validity caveat:** `role_recall = 1.00` on every positive question means the
ranker covers every role currently encoded in the pack; it does not, by itself,
prove that the role annotations are complete or independent. Because some roles
were refined during benchmark debugging, reports should present `role_recall`
as "coverage of the current formalized mechanism taxonomy" and pair it with
`precision_at_5`, `file_recall`, `ready_context`, and `pruned[]` inspection. A
stronger claim requires a pre-registered or independently reviewed role-label
pass before ranker tuning.

**Intent-Stratified Pass Gates (Phase 4):**

| Intent | role_recall floor | file_recall floor | Gate semantics |
|---|---|---|---|
| `explain_behavior` | 0.70 | 0.50 | **AND** (both required) |
| `trace_dependency` | 0.80 | 0.70 | **AND** (both required) |
| `impact_analysis` | 0.60 | 0.50 | **OR** (either sufficient) |

**Rationale:**
- **Explanation**: Moderate role coverage + moderate file coverage = good answer
- **Tracing**: Deep understanding (80% roles) + broad coverage (70% files) required
- **Impact**: Either test coverage (files) OR symbol coverage (roles) proves cascade exposure; don't need both

### 4.3 Token and Assembly Metrics (all packs)

| Metric | Formula | Failure threshold |
|---|---|---|
| `tokens_surgical` | tiktoken count of `to_system_prompt()` output | regression >10% blocks |
| `tokens_carpet_bomb` | tiktoken count of all files touched by any expected symbol | baseline only |
| `reduction_ratio` | `1 - tokens_surgical / tokens_carpet_bomb` | <0.50 blocks (target 0.60–0.80 per [architectura.md §1.3](architectura.md)) |
| `assembly_ms_avg` | mean wall-clock of `ContextArbitrator.get_context_for_symbol` across the run | >200ms is a local-release warning |

**Per-question report fields now include:**

- `precision` as an alias alongside `precision_at_k`
- `ready_context.token_count`
- `ready_context.contract` (serialized prompt contract)
- `ready_context.system_prompt`
- `expected_roles` (canonical normalized required roles from the question pack)
- `missing_expected_roles` (only the expected roles that remained unfilled)

Current local retrieval snapshot after the UnifiedRanker hardening pass:

| Repo | Command shape | Result |
|---|---|---|
| FastAPI | `QA/qa_benchmark.py --repo fastapi --no-index` | 8/8 pass, `fastapi_q03` and `fastapi_q06` stop with `context_complete_below_floor` instead of floor failure |
| Pydantic | `QA/qa_benchmark.py --repo pydantic --no-index` | 8/8 pass; `pydantic_q05` resolves `v1` through module fallback instead of "Symbol not found" |
| Redux Toolkit | `QA/qa_benchmark.py --repo redux_toolkit --no-index` | 8/8 pass; broad RTK precision remains a tuning target |

### 4.4 Console diagnostics for mechanism-aware runs

The per-question console line now prints role diagnostics explicitly:

- `expected_roles=...` — canonical expected role set for that question
- `missing_roles=...` — unfilled subset of `expected_roles`
- trailing `missing: ...` — raw ranker-internal `ctx.missing_roles` (debug-only, may contain non-pack roles)

Use `missing_roles` as the pass-gate indicator. Treat raw trailing `missing:` as
internal telemetry for tuning recovery and role-planning behavior.

**Note:** Quality metric (answer correctness) is **intentionally deferred** — it requires an LLM judge, which introduces noise and cost. Recall@k and role_recall are proxies: if the right symbols and roles are in the context, quality is the model's problem, not ours.

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
  benchmark_runs.py              # inspect benchmark_runs.jsonl and audit pruned[] reasons
  baselines.jsonl                # one row per commit on main
  judges/
    recall.py
    tokens.py
    latency.py
```

## 6. CI Integration

Target end-state:

`.github/workflows/eval.yml` runs on every PR:

1. Boot Neo4j + LanceDB in services.
2. `pytest tests/` — unit + integration.
3. `python QA/qa_benchmark.py --report out.json`.
4. Load latest `baselines.jsonl` row from main.
5. Diff: any metric worse than its failure threshold fails the job.
6. Post comment: `recall@5 0.84 → 0.87 ✅ | tokens_surgical 1.2k → 1.5k ⚠️ (+25%)`.

On merge to main, a follow-up job appends a new row to `baselines.jsonl` with the commit SHA.

Current repo truth: CI runs the unit suite only; benchmark-diff automation remains future work.

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
