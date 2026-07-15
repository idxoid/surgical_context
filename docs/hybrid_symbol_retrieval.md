# Hybrid symbol retrieval

The axis `/ask` path has three retrieval-only signals before its first graph
lookahead:

1. the existing whole-symbol body/signature vector floor;
2. fielded BM25 over `name`, `qualified_name`, `file_path`, and `symbol_kind`,
   with explicit exact-identifier boosts;
3. overlapping Python-AST-aligned semantic chunks.

The channels are deduplicated by owner symbol and combined with weighted
reciprocal-rank fusion (`hybrid_seed`, default cap 12). Chunks are not graph
nodes: their `owner_uid` is the graph seed, while their absolute
`start_line..end_line` intervals become priors for the optional within-symbol
line reranker. This keeps graph fan-out symbol-granular and source attribution
honest.

The shipped `vector_seed` channel remains separate, so
`AXIS_VSEED_CONN_MIN` still applies after pool expansion. This branch defaults
the threshold to `1`, which executes the index-seek `(v:Symbol {uid: vu})`
query; set it to `0` for the off-arm. Setting both
`lexical_retrieval=false` and `semantic_chunk_retrieval=false` preserves the
previous role-lookahead → vector/doc-seed order.

## Pre-graph seed selection

Before the expensive per-seed context walk, retrieval occurrences are
aggregated by symbol UID. The aggregate preserves independent evidence as
`supporting_roles`, `retrieval_channels`, `retrieval_spans`, and
`exact_symbol_match`, while retaining the first occurrence's score and role so
downstream Token Credit ordering does not drift.

Production uses an evidence-aware soft cap of seven candidates per source
role. The selector preserves the established ranked top seven and then dedupes
across roles. An explicit anchor is always retained; at most one exact-symbol
candidate per role is admitted when it would otherwise miss the graph seed
set. Ambiguous additional exact matches, semantic spans, and multi-channel or
multi-role consensus are recorded as telemetry but do not displace the ranked
tail until dedicated symbol/span gold supports that policy.

`context_seeds_per_role=None` remains the uncapped diagnostic mode. The QA
benchmark defaults to the production value and exposes
`--uncapped-context-seeds` for the historical full-pool arm. Per-question JSONL
rows include `seed_selection`, and audited candidates include
`supporting_roles` plus `selection_reasons`.

## Query-time lexical span probe (experimental)

When semantic chunk materialization is disabled, metadata BM25 can identify an
owner symbol but cannot honestly claim which body lines answer the question.
The opt-in `pregraph_lexical_span_probe` fills that gap without reindexing:

1. round-robin the ranked role slices that can reach the seed selector;
2. fetch their symbol payloads as one bounded exact-UID batch;
3. hydrate missing Lance bodies from persisted symbol spans/source files (no
   neighbourhood traversal);
4. rank six-line body windows by query-term IDF and explicit line hints;
5. attach only non-zero matching windows as `retrieval_spans` with channel
   `lexical_span`.

`lexical_span_score` is kept separate from the existing structural/vector
score. A report-only gold audit measures its AUC; it does not receive a hard
reserve. `lexical_span_utility_weight` can apply a small additive Token Credit
prior after selection, and defaults to `0.0`.

On the 98-question pack, the probe produced `10.30%` pool line recall versus
`2.63%` at the seed layer, with score AUC `0.728` (`0.173` mean score for exact
gold owners vs `0.085` for other selected candidates). On the version-aligned
repos specifically, pool line recall moved from `0%` without the probe to
`10.58%` with it. It cost `103 ms` mean / `344 ms` max. A `0.15` utility arm
changed bundle line recall by only `+0.31 pp` (5 wins, 1 loss) and left token
precision effectively flat, so both the probe and its utility weight remain
off by default.

## Index lifecycle

Semantic chunks live in `<symbols_table>_semantic_chunks_v1`. Their
materialization is opt-in because a cold index can require many additional
embeddings. When enabled, full and incremental indexing populate the table;
symbol tombstones, workspace resets, and path-prefix deletes remove matching
chunk rows. Existing indexes need one enabled reindex before semantic-chunk
retrieval can return hits. Lexical retrieval is immediately available because
it uses the cached symbol metadata scan.

Index-time environment knobs:

- `AXIS_SEMANTIC_CHUNK_INDEX` (default `false`; set `true` for span-signal experiments)
- `AXIS_SEMANTIC_CHUNK_TARGET_LINES` (default `24`)
- `AXIS_SEMANTIC_CHUNK_OVERLAP_LINES` (default `4`)
- `AXIS_SEMANTIC_CHUNK_MIN_SYMBOL_LINES` (default `10`)

The `/ask/axis` request and `AxisRetrievalConfig` expose
`lexical_retrieval`, `semantic_chunk_retrieval`, `hybrid_seed_limit`,
`pregraph_lexical_span_probe`, its bounded window knobs, and
`lexical_span_utility_weight` for A/B runs. `QA.axis_benchmark` mirrors these
switches and reports file, exact-symbol, exact span-owner, and line recall at
the retrieval/pool/bundle layers plus probe latency and score AUC.
