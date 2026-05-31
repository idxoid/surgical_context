# Engineering principles — the context engine

**Read this before touching role inference, the ranker, or any indexer edge.**
These are hard invariants. They were reached by *removing* a large amount of
framework-specific and answer-key code; re-adding any of it is a regression, not a
fix. If a change seems to need a banned construct, the design is wrong — fix the
engine instead (see the recipe at the end).

Guiding sentence: **the graph is a derivative of code and topology. YAML, configs,
and the benchmark are not the author of edges or roles.**

---

## P1 — The graph is derived from code, not from answers
Edges and roles come from **AST facts + call-graph topology**. The eval benchmark
exists to *measure*, never to *author*. Nothing in the engine may encode "for repo
X, symbol Y is role Z" or "query phrase Q → answer A".

## P2 — No fixtures / no answer-key tables
No hardcoded table that maps benchmark queries, symbol names, or file paths to
roles, mechanisms, or ranking bonuses.
- **Removed, do not reintroduce:** `_target_query_bonus` (query-keyword × role/kind
  table), `_GENERIC_AUTO_ROLE_PLANS`, `infer_identity_trace_roles` /
  `infer_ranker_fusion_roles` (per-repo answer-key role functions), the
  `worker_execution` mechanism, framework "recovery"/literal packs (e.g. celery
  `TRACE_*` literals).

## P3 — Roles are structural; no name/keyword/path patterns
Pass-1 role inference uses **call-graph topology + per-edge-type fan only**
(call / type / api / inject / depend / handle, weighted by edge confidence + the
`USES_TYPE` kind). Never assign a *semantic* role from a symbol name, a keyword, or
a file stem.
- **Removed, do not reintroduce:** `_semantic_tokens`, `symbol_name_matches_file_stem`,
  name-token role tests, keyword/name-pattern branches in role inference.
- **Allowed:** path patterns (`NOISE_PATH_PATTERNS`) **only** for impact
  partitioning (test-surface vs runtime-surface) and for excluding test fixtures
  from Pass-1 clustering — never to decide a semantic role.

## P4 — Fix the engine, not the symptom
When a role/retrieval is wrong, add the **missing structural signal**, not a patch:
a new derived edge (`HANDLES`, `DECORATED_BY`, `PROXY_OF`, `RE_EXPORTS`,
`INSTANTIATES`, …), a new feature, or a type-inference hop. Do **not** tune a magic
constant to pass one case, and do **not** add a per-framework branch.

## P5 — Derived edges are precision-over-recall AST facts
A derived edge resolves to an in-graph symbol or it is **not created** (stdlib /
external / unresolved → no edge). If a genuine signal requires dataflow (e.g.
construction through a reassigned/disjunction local, `v = a or b; v(...)`), **scope
it out honestly** and record it as a gap — never fake it with a guessing heuristic.

## P6 — Archetypes describe shape; they do not author roles
Archetype/template detection stays structural. Role **plans** are *adaptive* —
derived from the roles actually observed around the target
(`target_role_supply_counts`) — not preset per-framework tables.

## P7 — Validate empirically, never a-priori
New role/ranking logic is proven against the **indexed benchmark** (e.g. the
discriminator-first cascade vs the k-means baseline, `file_recall` deltas), and the
result — including regressions — is reported honestly. A claim like "L1/L2 beats
k-means" is only valid with the prototype numbers behind it.

## P8 — Don't special-case for benchmark coverage
Prefer removing a dead framework-specific branch over preserving it to keep a
benchmark number. ("сноси и не заморачивайся на покрытие бенчмарка.")

---

## How to add a signal correctly (the recipe)
1. State the structural fact in the source code that the engine currently can't see
   (an edge it doesn't extract, a feature it doesn't aggregate, a type it can't
   infer). Ground it in the actual code, not in the benchmark answer.
2. Decide the cost tier honestly: 🟢 AST-extractable · 🟡 AST but noisy/partial ·
   🟠 needs an intermediate identity node · 🔴 needs dataflow. If it's 🔴, scope the
   cheap part and record the rest as a gap (P5).
3. Extract it as a **derived edge/feature** that resolves to in-graph symbols
   (precision over recall), mirroring an existing extractor
   (`extract_decorators` / `extract_type_references` / `extract_reexports` are the
   templates) and its `link_*` resolver in `neo4j_client`.
4. Consume it as a **structural discriminator** (a feature / L2 predicate), never as
   a name match or a benchmark lookup.
5. **Measure** on the indexed benchmark before and after (P7); report regressions.

See [role_signature_findings.md](role_signature_findings.md),
[role_clustering_architecture.md](role_clustering_architecture.md), and
[role_catalog.md](role_catalog.md) for the current role vocabulary, the open
structural gaps, and the missing-edge inventory.
