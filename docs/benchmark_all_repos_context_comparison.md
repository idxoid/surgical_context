# All-repos context comparison (index)

> **Status:** Index only. Detailed tables live in linked docs below.

This filename is kept for bookmarks and older notes. The comparison content was split into:

| Document | What it covers |
|---|---|
| [benchmark_mechanism_coverage.md](benchmark_mechanism_coverage.md) | Harness pass rate, `role_recall` / `file_recall` / `precision@5` per repo, impact stop-reasons, mechanism-coverage framing |
| [benchmark_path1_vs_path2.md](benchmark_path1_vs_path2.md) | LLM Judgment: Surgical Context (P1) vs first-time repo read (P2) on the full 65-question pack |

## Latest harness snapshot (2026-05-24)

Full pack: `tests/fixtures/real_repo_question_pack.yaml`, `--no-index`, local indexes.

| Repo | Questions | Pass |
|---|---:|---:|
| fastapi | 8 | 8/8 |
| pydantic | 8 | 8/8 |
| redux_toolkit | 8 | 8/8 |
| django | 5 | 5/5 |
| flask | 5 | 5/5 |
| express | 4 | 4/4 |
| nestjs | 4 | 4/4 |
| sqlalchemy | 4 | 4/4 |
| vue | 4 | 4/4 |
| surgical_context | 7 | 7/7 |
| dathund | 8 | 8/8 |
| **Total** | **65** | **65/65** |

Pass means benchmark gates satisfied; **precision tails** (low `@5`, missing impact symbols) are still tracked in [benchmark_mechanism_coverage.md](benchmark_mechanism_coverage.md) § Latest Real-Repo Run.

## Reproduce

```bash
PYTHONPATH=. .venv/bin/python QA/qa_benchmark.py \
  --questions tests/fixtures/real_repo_question_pack.yaml \
  --repo <repo_id> \
  --no-index
```

Log: `QA/benchmark_runs.jsonl`. See [spec_eval_harness.md](spec_eval_harness.md).
