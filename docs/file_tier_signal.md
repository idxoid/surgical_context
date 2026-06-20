# File-tier signal — spec

> **Type:** A — Spec (✅ implemented). Index-time structural signal; not a cascade remnant.

One **index-time structural signal** that unifies two retrieval problems
the three-layer benchmark exposed as the *same* missing notion:

- **Class 2 — noise pollution.** Seed retrieval drowns in non-answer
  files: flask's `examples/` apps bury `src/flask/blueprints.py`
  (`flask_q02` seed 0.00), every celery question tops with
  `celery/__init__.py` (a re-export), pydantic with `pydantic_core/*.pyi`
  stubs.
- **Class 3 — scope/test expectation.** The *same* file families
  (`tests/`, `docs/`) are sometimes the **expected answer** — "what tests
  are affected if X changes?" wants the test surface.

They are one signal with two signs: a file's **tier** (its role in the
repository's structure, not its semantics), applied by the ranker with a
sign chosen by intent — **demote** non-core tiers for behaviour
questions, **promote** the relevant tier for impact/trace modes.

This is invariant-clean: tier is derived purely from **path topology +
file shape**, never from semantic content; intent only chooses the
**weight sign** (resource management, like `apply_intent_axis_boost`),
never which files are structurally valid.

**Code:** [context_engine/indexer/file_tier.py](../context_engine/indexer/file_tier.py)
(derivation), [context_engine/axis/role_retrieval.py](../context_engine/axis/role_retrieval.py)
(seed/role ranking weights).

**See also:** [axis_terminology.md](axis_terminology.md),
[engineering_principles.md](engineering_principles.md),
[spec_indexer.md](spec_indexer.md) (embedding batch).

**Status:** derivation + LanceDB materialization + ranker weights are
implemented. Step 5 (retire `is_test_path` / `include_tests` shims in
graph walks) is still open — tier weights apply to seed/role retrieval;
walks still use the binary test filter.

## Taxonomy + derivation (index-time, structural)

| tier | rule (path segment ∪ file shape) |
|---|---|
| `test` | segment ∈ {tests,test,t,qa,__tests__,testfixtures,__testfixtures__} ∨ name ∈ {test_*.py, *_test.py, *.spec.*, *.test.*, conftest.py} |
| `example` | segment ∈ {examples,example,tutorial,tutorials,demo,demos,samples,docs_src,benchmarks,codemods} |
| `doc` | segment ∈ {docs,doc} ∨ ext ∈ {.md,.rst,.txt} |
| `stub` | ext == .pyi |
| `reexport` | `.py` whose body is only import / `__all__` / docstring |
| `core` | default — the library/answer code |

Precedence: path tiers (`test` → `example` → `doc`) before shape tiers
(`stub` → `reexport`), else `core`. First match wins.

## Storage

`file_tier` on each LanceDB symbol row (schema v5+), materialized **once at
index time** in the embedding batch (alongside vector upsert). Path is
relative to the indexed project root so infra prefixes (`QA/repos/…`) do not
false-trigger tier rules. Ranker scans read `file_tier` from
`scan_workspace_rows` / `find_seeds_by_vector`.

## Application (axis ranker, query-time, intent-signed)

A tier weight multiplies the candidate `score` before the top-k cut, in
both `find_symbols_by_roles` and `find_seeds_by_vector`. The sign comes
from a coarse **two-class** intent table (mode vs non-mode) — a ranking
weight, **not** a query→file map:

| tier | non-mode intents (behaviour/explain/…) | mode intents (impact_analysis / trace_dependency) |
|---|---|---|
| core | 1.0 | 1.0 |
| reexport | 0.5 | 0.5 |
| stub | 0.5 | 0.5 |
| doc | 0.3 | 0.6 |
| example | 0.2 | 0.6 |
| test | hard-fenced (as today, `is_test_path`) | 1.0 (tests are the answer) |

`test` stays a hard fence for non-mode intents to preserve current
behaviour exactly; the soft demotions (`example`/`doc`/`stub`/`reexport`)
are the new lever. Weights echo the legacy `NOISE_FACTOR`
(0.15) / `EXPLORATION_NOISE_FACTOR` (0.3) — tune empirically.

## Subsumes (3 → 1)

- `axis/test_file_filter.is_test_path` → the `test` tier, now native.
- **`context_engine/indexer/signal_constants.NOISE_PATH_PATTERNS`** (class B, moved
  out of the deleted cascade into the indexer, 2026-06-15) → its path patterns
  become the `example`/`doc` derivation.
  This *closes the class-B migration item* — the indexer is its home.
- Scattered checks (`indexer/anchor.py` `/examples/`·`/tutorial/`) → one derive.

## Implementation steps

1. ✅ **`context_engine/indexer/file_tier.py`** — `classify_file_tier(path,
   pure_reexport)` + `is_pure_reexport_source(src)`.
2. ✅ Indexer fast-pipeline — stamp `file_tier` on each Lance symbol row at
   embed time. Requires reindex for existing workspaces.
3. ✅ `scan_workspace_rows` — surface `file_tier` in scanned rows.
4. ✅ `find_symbols_by_roles` + `find_seeds_by_vector` — intent-signed tier
   multiplier on `score` before top-k.
5. ⏳ Retire the binary `include_tests` / `is_test_path` query shim; graph
   fences (lookahead/cross_role/structural) read the tier label.

## Measurement target (three-layer benchmark)

`seed` 0.817 → higher (flask_q02 0.00→, pydantic seed-0 cases);
`masked_by_pool_expander` shrinks (less for the walk to rescue); `bundle`
≥ 0.983; zero regression on core questions; impact (Class 3) holds the
test surface via the mode promote.
