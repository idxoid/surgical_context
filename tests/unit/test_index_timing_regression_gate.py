"""Regression gate for indexer timing contracts.

Keeps the optimized project index path honest:
- ``timings_sec["total"]`` is always present (including noop exits)
- queue snapshots expose wait/process timings
- embedding cache stats expose get/set timings
"""

from __future__ import annotations

from context_engine.database.embedding_cache import EmbeddingCache, EmbeddingCacheKey
from context_engine.database.embedding_registry import compute_chunk_hash, compute_embedding_hash
from context_engine.index_profile import resolve_index_profile
from context_engine.indexer.fast.pipeline import _fast_indexing_initial_stats, _stamp_total
from context_engine.indexer.queue import IndexBatchQueue


def test_stamp_total_always_sets_timings_total():
    profile = resolve_index_profile("legacy")
    stats = _fast_indexing_initial_stats("/tmp/proj", "ws", "ws", profile, skip_affects=False)
    _stamp_total(stats, t0=0.0)
    assert "total" in stats["timings_sec"]
    assert isinstance(stats["timings_sec"]["total"], float)


def test_queue_snapshot_exposes_timings():
    batches = []
    queue = IndexBatchQueue(batches.append, debounce_ms=0, auto_start=False)
    queue.enqueue_file("/repo/a.py", workspace_id="ws")
    queue.process_ready_once()
    snap = queue.snapshot()
    assert "timings_ms" in snap
    assert "queue_wait_total" in snap["timings_ms"]
    assert "queue_process_total" in snap["timings_ms"]
    assert snap["batches_processed"] == 1


def test_embedding_cache_stats_expose_timings(tmp_path):
    cache = EmbeddingCache(str(tmp_path / "emb.sqlite3"))
    key = EmbeddingCacheKey("m", "1", compute_chunk_hash("x"))
    cache.set_many([(key, [1.0, 2.0], compute_embedding_hash([1.0, 2.0]))])
    cache.get_many([key])
    timings = cache.stats()["timings_ms"]
    assert timings["set_many_calls"] == 1
    assert timings["get_many_calls"] == 1
