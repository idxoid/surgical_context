from __future__ import annotations

import numpy as np

from context_engine.axis.context_builder import (
    ContextBundle,
    ContextSymbol,
    _apply_span_line_rerank,
)
from context_engine.axis.role_retrieval import (
    WorkspaceScan,
    find_seeds_by_lexical,
    find_seeds_by_semantic_chunk,
    fuse_seed_channels,
)
from context_engine.search.lexical import FieldedBM25Index, identifier_tokens


def _scan() -> WorkspaceScan:
    return WorkspaceScan(
        rows=[
            {
                "uid": "poller",
                "name": "run_once",
                "qualified_name": "worker.AsyncPoller.run_once",
                "file_path": "/repo/worker/poller.py",
                "symbol_kind": "method",
                "file_tier": "core",
                "_contracts": set(),
                "_kinds": set(),
            },
            {
                "uid": "dispatch",
                "name": "dispatch_alert",
                "qualified_name": "alerts.dispatch_alert",
                "file_path": "/repo/alerts.py",
                "symbol_kind": "function",
                "file_tier": "core",
                "_contracts": set(),
                "_kinds": set(),
            },
        ],
        vectors=np.zeros((2, 3), dtype=np.float32),
    )


def test_fielded_bm25_preserves_full_and_split_code_identifiers():
    tokens = identifier_tokens("AsyncPoller.run_once")
    assert "asyncpoller.run_once" in tokens
    assert {"async", "poller", "run", "once"}.issubset(tokens)

    index = FieldedBM25Index(_scan().rows)
    [hit, *_] = index.search("inside `worker.AsyncPoller.run_once`", limit=2)
    assert hit.row_index == 0
    assert hit.exact is True


def test_lexical_seed_marks_exact_symbol_match():
    [seed, *_] = find_seeds_by_lexical(
        "ws",
        "Where is `run_once` invoked?",
        limit=4,
        prescanned=_scan(),
    )
    assert seed.uid == "poller"
    assert seed.exact_symbol_match is True
    assert seed.retrieval_channels == ("lexical",)


def test_semantic_chunks_aggregate_owner_spans_and_rrf_provenance():
    class FakeLance:
        def search_symbol_chunks_by_vector(self, _vector, **_kwargs):
            return [
                {
                    "owner_uid": "poller",
                    "distance": 0.3,
                    "start_line": 120,
                    "end_line": 128,
                },
                {
                    "owner_uid": "poller",
                    "distance": 0.4,
                    "start_line": 126,
                    "end_line": 134,
                },
                {
                    "owner_uid": "dispatch",
                    "distance": 0.8,
                    "start_line": 20,
                    "end_line": 25,
                },
            ]

    scan = _scan()
    chunks = find_seeds_by_semantic_chunk(
        "ws",
        "ordered connector pipeline",
        embed_fn=lambda _text: [0.0, 0.0, 0.0],
        limit=2,
        prescanned=scan,
        lance=FakeLance(),
    )
    assert chunks[0].uid == "poller"
    assert chunks[0].retrieval_spans == ((120, 128), (126, 134))

    lexical = find_seeds_by_lexical(
        "ws",
        "`run_once` connector pipeline",
        limit=2,
        prescanned=scan,
    )
    [hybrid, *_] = fuse_seed_channels(
        {"lexical": lexical, "semantic_chunk": chunks},
        limit=2,
    )
    assert hybrid.uid == "poller"
    assert hybrid.retrieval_channels == ("lexical", "semantic_chunk")
    assert hybrid.retrieval_spans == ((120, 128), (126, 134))


def test_semantic_chunk_span_is_a_line_rerank_prior_not_a_render_claim():
    code = "\n".join(
        [
            "def run_once(self):",
            "    prepare()",
            "    unrelated_a()",
            "    unrelated_b()",
            "    unrelated_c()",
            "    answer_stage()",
            "    unrelated_d()",
            "    finish()",
        ]
    )
    symbol = ContextSymbol(
        uid="poller",
        name="run_once",
        file_path="/repo/poller.py",
        role="hybrid_seed",
        distance_from_seed=0,
        expansion_step=None,
        code=code,
        start_line=100,
        end_line=107,
        retrieval_spans=((105, 105),),
    )
    [rendered] = _apply_span_line_rerank(
        [ContextBundle(role="hybrid_seed", seed=symbol)],
        query_text="pipeline stage",
        score_fn=lambda texts: [0.0] * len(texts),
        max_candidates_per_symbol=8,
        max_body_lines=2,
    )
    assert "answer_stage()" in (rendered.seed.code or "")
    assert any(start <= 105 <= end for start, end in rendered.seed.effective_rendered_spans())


def test_rare_body_tokens_join_the_index() -> None:
    from context_engine.search.lexical import FieldedBM25Index

    rows = [
        {"name": "check_all_models", "qualified_name": "django.core.checks.check_all_models"},
        {"name": "handler", "qualified_name": "django.core.handlers.handler"},
    ]
    bodies = [
        "errors.append(Error(id='models.E028'))",
        "return handler(request)",
    ]
    index = FieldedBM25Index(rows, bodies=bodies)
    hits = index.search("db_table clash raises models.E028 for different apps")
    assert hits, "rare body token must be searchable"
    assert hits[0].row_index == 0


def test_common_body_tokens_stay_out_via_df_ceiling() -> None:
    from context_engine.search.lexical import _BODY_TOKEN_DF_CEILING, FieldedBM25Index

    n = _BODY_TOKEN_DF_CEILING + 5
    rows = [{"name": f"fn{i}", "qualified_name": f"pkg.fn{i}"} for i in range(n)]
    bodies = ["everywhere_token = compute()" for _ in range(n)]
    index = FieldedBM25Index(rows, bodies=bodies)
    assert not index.search("everywhere_token")


def test_bodies_none_is_identical_to_metadata_only() -> None:
    from context_engine.search.lexical import FieldedBM25Index

    rows = [{"name": "alpha", "qualified_name": "pkg.alpha"}]
    with_none = FieldedBM25Index(rows, bodies=None).search("alpha")
    plain = FieldedBM25Index(rows).search("alpha")
    assert [(h.row_index, h.score) for h in with_none] == [(h.row_index, h.score) for h in plain]
