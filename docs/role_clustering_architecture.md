# Pass-1 role assignment — discriminator-first L1/L2 cascade

Decision record for how Pass-1 derives roles. Motivated by the collisions
in [role_signature_findings.md](role_signature_findings.md) and the role
vocabulary in [role_catalog.md](role_catalog.md).

Format per item: **what → how (where in code) → why it matters → decision**.

**Status:** implemented in `sidecar/indexer/role_clustering.py` +
`sidecar/indexer/role_cascade.py` (schema v3). Sections C1–D5 below include
historical rationale for the retired k-means + Pass-1 archetype tier.

---

## C1 — Fixed `k = 5..8` does not scale across repo composition 🔴 *(retired k-means)*
- **what:** the number of *clusters* is bounded to 5–8 regardless of how many
  *roles* are actually present in a repo.
- **how:** former `cluster_symbols(..., k_min=5, k_max=8)` + silhouette loop.
- **why:**
  - *Flask microservice* (~4–5 real topologies, no DI): k is forced ≥ 5, so a
    surplus centroid splits a real role (executors/DTOs) into a phantom cluster.
  - *NestJS monolith* (dozens of DI resolvers): many structurally-identical
    symbols of one role; silhouette may pick k=8 and shred that single role into
    several clusters on `call_fan_out` noise.
  - k-means answers "into how many groups do I cut" — but the question is "which
    roles from the vocabulary are present", a different problem.
- **decision:** stop deriving roles *from* cluster count. Roles are discrete
  structural predicates; clustering (if used at all) is for sub-variants within a
  known role. See D1.

## C2 — Fallback to `cluster[0]` manufactures phantom roles 🔴 *(retired)*
- **what:** when no cluster scores ≥ 0.35 for an archetype, the archetype is still
  attached to `cluster[0]`.
- **how:** former `build_role_catalog` phantom fallback on `taxonomy.clusters[0]`.
- **why:** a role that does not exist in the repo (e.g. `dependency_solver` in a
  plain Flask app) still appears in `role_catalog_json`. The ranker then resolves
  it to an arbitrary cluster and surfaces wrong candidates.
- **decision:** add a **presence gate** (D2): a role enters the workspace catalog
  only if its discriminator fires on ≥ `min_support` symbols. No phantom fallback.

## C3 — Per-level feature masking is necessary but not sufficient 🟡
- **what:** masking irrelevant axes per macro-group removes *competing* signals
  (e.g. not comparing `call_fan_out` against `type_fan_in` in one space).
- **why:** masking fixes within-level axis competition only. It does **not** fix
  forced k, phantom roles, role-instance multiplicity, or absent roles.
- **decision:** apply masking *inside* L1 buckets (D3) as hygiene, on top of the
  pipeline inversion — not as the primary fix.

---

## D1 — Decision: invert the pipeline (discriminator-first)
- **what:** assign roles by evaluating catalog discriminators as predicates, then
  optionally sub-cluster within a role.
- **target pipeline:**
  ```
  extract_symbol_rows()
    → filter_clustering_rows()            # drop unconnected (existing)
    → assign_l1(row)                      # rule-based macro topology
    → assign_l2(row, l1)                  # discriminator cascade per L1
    → detect_present_roles(counts)        # presence gate (C2)
    → optional subcluster_within_role()   # HDBSCAN per (l1,l2) if |members| large
    → persist (present roles only)
  ```
- **why:** roles are dragged out of an emergent cluster shape in the old design; making them
  explicit predicates removes C1/C2 and matches the catalog's own
  "single discriminating edge signal" framing
  ([role_catalog.md](role_catalog.md) Distinctiveness principle).
- **implemented:** `SymbolRow` + edge aggregation, `filter_clustering_rows`,
  `NOISE_PATH_PATTERNS`; L2 predicates in `role_cascade.py`; `present_roles` +
  per-symbol `derived_primary_role` / `derived_supporting_roles_json` on persist.

## D2 — Decision: presence gate before catalog entry
- **what:** `PRESENT(role) iff count(symbols matching discriminator) >= min_support`.
- **why:** removes phantom roles (C2); makes the workspace catalog reflect the
  repo's real composition; lets the ranker answer "is `dependency_solver` here?"
  honestly.
- **decision:** `min_support = 2` for common roles, `1` for rare boundary roles
  (gateway); store per-role support counts in `role_catalog_json`.

## D3 — Decision: two-level model (L1 macro / L2 micro)
- **L1 (rule-based, 4–5 buckets, mostly single-label with soft scores):**
  | L1 | gate | feature subspace |
  |---|---|---|
  | Control Flow | `call_fan_out > call_fan_in` | call_fan_out, cross_pkg_out, handle_fan_out, depth |
  | Compute/Leaf | high `call_fan_in` + leaf | call_fan_in, call_leaf_score, handle_fan_in, depth |
  | State & Types | high `type_fan_in`/`depend_fan_in` | type_fan_in/out, depend_fan_in, api_fan_in, is_class |
  | Routing/Wrap | `proxy_of`/`decorated_in`/`handle_*` | handle_fan_out/in, decorated_in, proxy_of |
  | (noise) | zero in-degree across all families | — |
- **L2 (discriminator cascade per L1):** ordered predicate match → first hit is
  primary. Example (Control Flow): `handle_fan_out>0` → `registration_step`
  (clean — `handle_fan_out` is decoration-only, so it is the decorator/registry,
  not a runtime router; see F1); else `call_fan_out>in + cross_pkg` →
  orchestrator/composition; else isinstance-dispatch → dependency_solver; else
  generic_control. (`request_router` is **not** in this cascade — it has no
  `handle_fan_out` and dispatches dynamically; see F1, deferred.)
- **why:** L1 overlap is *by design* (resolves F2/F3 from
  [role_signature_findings.md](role_signature_findings.md)); the real work is the
  L2 cascade. Removes ~70% of the flat-clustering "collisions".
- **open issues:** (a) 4 buckets may be too few — `public_entrypoint`,
  `gateway`, `abstract_contract` straddle; consider a 5th `Surface/Entry` bucket.
  (b) straddlers (factory = Control+State) need soft L1 scores or multi-parent.
  (c) impact roles (§7) stay a query-time overlay, outside L1/L2.

## D4 — Decision: sub-cluster only within a role, adaptive density
- **what:** clustering survives only as optional Step 3 — HDBSCAN
  (`min_cluster_size`) inside one (L1, L2) role to find *variants*, not roles.
- **why:** 40 NestJS providers = one role, maybe a few module-variant
  sub-clusters; k is not fixed, so no forced split (C1) and a natural noise label.
- **decision:** run only when `|members| > threshold` and a consumer needs the
  sub-split; otherwise the role is atomic.

## D5 — Decision: retire the archetype tier
- **what:** drop the `archetype` layer (`_ARCHETYPE_TEMPLATES`,
  `_ROLE_TO_ARCHETYPES`, `_score_cluster_for_archetype`, cluster-based
  `resolve_role_clusters`, and the ranker's `_cluster_role_membership`) as a
  distinct construct. See the tautology analysis in F11 of
  [role_signature_findings.md](role_signature_findings.md).
- **why it existed (retired):** k-means cluster ids were unstable across re-index;
  the archetype layer mapped names to centroids to paper over that. It was also a
  third name-normalisation tier (`framework-alias → canonical role → archetype`) on
  top of `ROLE_ALIASES`, duplicating what L1 buckets do now.
- **why it dies under D1:** once roles are assigned by predicates there are no
  unstable cluster ids to stabilise, and the macro tier is L1. Of the 7
  archetypes: 3 (`active_entrypoint`, `passive_api_surface`, `runtime_handle`)
  are genuine macro-shapes → become L1 buckets; 4 (`orchestrator`, `executor`,
  `representation_surface`, `config_surface`) are string-identical to roles →
  collapse into their eponymous L2 role (`config_surface` is a pure 1:1
  tautology, used by no other role).
- **dies vs survives:**
  | archetype element | fate |
  |---|---|
  | `_ARCHETYPE_TEMPLATES` (feature-weight centroids) | dies — L2 is a predicate, not centroid proximity |
  | `_ROLE_TO_ARCHETYPES` (preference map) | dies — no intermediate tier |
  | `_score_cluster_for_archetype` + confidence | relocates → per-predicate confidence (M3) |
  | `resolve_role_clusters` (cluster lookup) | replaced → "symbols by role predicate" |
  | `_cluster_role_membership` (ranker) | dies → per-symbol predicate set (M1) |
  | 3 clean archetypes | survive → L1 buckets (rule gates, renamed) |
  | 4 role-name archetypes | collapse → L2 roles |
  | blend decomposition (`core_runtime = runtime_handle + executor`) | dies — was a surrogate for a missing predicate, not real information |
- **preserve consciously:** graded membership (a symbol leaning toward several
  macro-shapes) must not be lost — it relocates to **soft L1 scores +
  per-role confidence**, not to a centroid blend.
- **invariant:** **two name tiers, not three** — `framework-alias → role (L2)`
  plus an orthogonal `L1 bucket`. The name sets of L1 and L2 must be disjoint
  (this is the tautology test).
- **cost:** `role_catalog_json` schema v3 — `present_roles` only; ranker reads
  per-symbol roles via `role_fulfilment.py` (no `_cluster_to_role` /
  `_cluster_role_membership`).

---

## Multi-label fit

The codebase has **two** multi-label axes; the pipeline inversion repairs the
first and better serves the second.

### M1 — Symbol roles: per-symbol predicate set *(implemented)*
- **was (k-means):** one `cluster_id` per symbol; supporting roles from cluster
  co-membership — multi-label was often centroid confusion.
- **now:** each L2 role is an independent predicate; a symbol gets primary +
  supporting roles from `role_cascade.py`. Straddlers in F8
  ([role_signature_findings.md](role_signature_findings.md)) are first-class
  multi-label rows (`factory + orchestrator`, …).
- **consumer:** `roles_of` / `supporting_roles_of` in `role_fulfilment.py` read
  persisted Pass-1 assignments.

### M2 — Query intent: mechanically unchanged, better served
- **what:** `IntentDistribution` ([spec_multi_label_intent.md](spec_multi_label_intent.md))
  is query-side and does not change.
- **why better:** a mixed intent (e.g. debugging 0.6 + refactor 0.3) demands
  roles from different intents; `candidate_matches_any_role` now matches against a
  symbol's *genuine* multi-role set. The old flat-clustering path could drop mixed
  roles behind a single label.

### M3 — Risks introduced by independent predicates
| risk | mitigation |
|---|---|
| label explosion (symbol fires 5 roles) | per-role confidence threshold + cap top-K supporting (the `[:3]` cap already used in `build_role_catalog`) |
| primary tie-break (two strong L2 hits) | L1 dominance decides; tie → edge-specificity order `handle > type > call` |
| per-role calibration | `min_support` (D2) + confidence floor |
| correlated predicates (factory∧orchestrator) | document as expected co-fire, not a bug |
| back-compat | `derived_role_id` retired; schema v3 uses string roles on Symbol |

---

## Decision summary

| # | Item | Severity | Decision |
|---|---|---|---|
| C1 | fixed k=5..8 | 🔴 | stop deriving roles from cluster count |
| C2 | cluster[0] fallback | 🔴 | presence gate (D2) |
| C3 | masking insufficient | 🟡 | apply inside L1 as hygiene only |
| D1 | discriminator-first pipeline | — | invert: predicates assign roles |
| D2 | presence gate | — | role in catalog iff support ≥ min |
| D3 | L1/L2 model | — | rule-based macro + cascade micro |
| D4 | sub-cluster within role | — | optional HDBSCAN, adaptive |
| D5 | retire archetype tier | — | drop templates/map; 3→L1, 4→L2; two name tiers |
| M1 | symbol multi-label | — | per-symbol predicate set |
| M2 | intent multi-label | — | unchanged; better matched |
| M3 | multi-label risks | 🟡 | threshold + tie-break + cap |

**Critical path (remaining):** naming fixes (F5/F6). Honest dataflow residuals:
`request_router` dynamic dispatch (F1) and `self.<attr>`-only construction (P5
residual). Re-validate with `QA/prototype_role_cascade.py` after re-index.

## Related
- [role_catalog.md](role_catalog.md) — role vocabulary and discriminators.
- [role_signature_findings.md](role_signature_findings.md) — the collisions that motivate this.
- [spec_unified_ranking.md](spec_unified_ranking.md) — consumes derived roles.
- [spec_multi_label_intent.md](spec_multi_label_intent.md) — query-side multi-label.
