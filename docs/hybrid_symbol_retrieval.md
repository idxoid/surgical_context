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

## Index lifecycle

Semantic chunks live in `<symbols_table>_semantic_chunks_v1`. Full and
incremental indexing populate the table; symbol tombstones, workspace resets,
and path-prefix deletes remove matching chunk rows. Existing indexes need one
reindex before semantic-chunk retrieval can return hits. Lexical retrieval is
immediately available because it uses the cached symbol metadata scan.

Index-time environment knobs:

- `AXIS_SEMANTIC_CHUNK_INDEX` (default `true`)
- `AXIS_SEMANTIC_CHUNK_TARGET_LINES` (default `24`)
- `AXIS_SEMANTIC_CHUNK_OVERLAP_LINES` (default `4`)
- `AXIS_SEMANTIC_CHUNK_MIN_SYMBOL_LINES` (default `10`)

The `/ask/axis` request and `AxisRetrievalConfig` expose
`lexical_retrieval`, `semantic_chunk_retrieval`, and `hybrid_seed_limit` for
A/B runs. `QA.axis_benchmark` mirrors these switches and reports file,
exact-symbol, and optional line/span recall at the retrieval/pool/bundle
layers.
