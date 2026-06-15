# All-repos context comparison (index)

> **Historical snapshot.** This document predates the cascade removal (2026-06-15): the legacy ranking cascade it references is gone — axis is the sole context path (see `cascade_cleanup_inventory.md`). Kept as a dated record; the findings/benchmarks below are as-of their date.


> **Status:** Index only. Detailed tables live in linked docs below.

This filename is kept for bookmarks and older notes. The comparison content was split into:

| Document | What it covers |
|---|---|
| [benchmark_mechanism_coverage.md](benchmark_mechanism_coverage.md) | Harness pass rate, `role_recall` / `file_recall` / `precision@5` per repo, impact stop-reasons, mechanism-coverage framing |
| [benchmark_path1_vs_path2.md](benchmark_path1_vs_path2.md) | LLM Judgment: Surgical Context (P1) vs first-time repo read (P2) on the full 65-question pack |

## Latest harness snapshot (2026-05-26)

Full pack: `tests/fixtures/real_repo_question_pack.yaml`, `--no-index`, local indexes.

| Repo | Q | Pass | P@5 fill | P@5 prompt | P@5 score | R@5 | Role | File |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| fastapi | 8 | 8/8 | 0.23 | 0.18 | 0.20 | 0.32 | 1.00 | 0.81 |
| pydantic | 8 | 8/8 | 0.18 | 0.18 | 0.18 | 0.42 | 1.00 | 0.81 |
| redux_toolkit | 8 | 8/8 | 0.20 | 0.18 | 0.20 | 0.62 | 1.00 | 0.78 |
| django | 5 | 5/5 | 0.24 | 0.24 | 0.24 | 0.35 | 1.00 | 0.80 |
| flask | 5 | 5/5 | 0.20 | 0.16 | 0.20 | 0.28 | 1.00 | 1.00 |
| express | 4 | 4/4 | 0.30 | 0.30 | 0.30 | 0.42 | 1.00 | 1.00 |
| nestjs | 4 | 4/4 | 0.25 | 0.40 | 0.40 | 0.31 | 1.00 | 0.88 |
| sqlalchemy | 4 | 4/4 | 0.20 | 0.23 | 0.20 | 0.27 | 1.00 | 0.88 |
| vue | 4 | 4/4 | 0.25 | 0.25 | 0.30 | 0.35 | 1.00 | 1.00 |
| surgical_context | 7 | 7/7 | 0.29 | 0.29 | 0.31 | 0.37 | 1.00 | 0.79 |
| dathund | 8 | 8/8 | 0.25 | 0.28 | 0.30 | 0.34 | 1.00 | 0.83 |
| **Total** | **65** | **65/65** | **0.23** | **0.24** | **0.26** | **0.37** | **1.00** | **0.87** |

Pass means benchmark gates satisfied. **P@5** is reported three ways: budget-fill order, LLM prompt order (`ordered_graph_context`), and blended-score order. Top-k diagnostics: [benchmark_mechanism_coverage.md](benchmark_mechanism_coverage.md) § Latest Real-Repo Run.

**Role recall:** **1.00** on all 65 questions (May 2026). Last tail was `surgical_context_q02` (`factory_surface`); closed via builtin `surgical_context_ranker_fusion` mechanism + `rank`/`_fuse`/`BudgetPruner` backfill.

## Reproduce

```bash
PYTHONPATH=. .venv/bin/python QA/qa_benchmark.py \
  --questions tests/fixtures/real_repo_question_pack.yaml \
  --repo <repo_id> \
  --no-index
```

Log: `QA/benchmark_runs.jsonl`. See [spec_eval_harness.md](spec_eval_harness.md).

**Related (ops):** path sandboxing, API bounds, and queued `/index` root registration — [spec_sidecar_api.md](spec_sidecar_api.md). Product status — [road_map.md](road_map.md).
