"""Unit tests for retrieval cache primitives."""

from context_engine.cache.layered import CachedBody, InMemoryResponseCache, LayeredCache
from context_engine.context_types import Subgraph, SubgraphNode


def test_body_cache_is_keyed_by_file_hash():
    cache = LayeredCache()
    cache.put_body("/repo/app.py", (1, 3), "hash-a", CachedBody("old", 3))

    assert cache.get_body("/repo/app.py", (1, 3), "hash-a").code == "old"
    assert cache.get_body("/repo/app.py", (1, 3), "hash-b") is None


def test_subgraph_cache_is_keyed_by_workspace_and_graph_version():
    cache = LayeredCache()
    subgraph = Subgraph(
        primary=SubgraphNode(
            uid="u1",
            name="process",
            file_path="/repo/app.py",
            range=[1, 3],
            token_estimate=24,
            relation="target",
            direction="primary",
            depth=0,
            relevance_score=1.0,
            file_hash="hash-a",
        ),
        nodes=[],
        budget={"limit": 4000, "spent": 124, "reserved": 100, "pruned": 0},
    )

    cache.put_subgraph("u1", "intent", 4000, "acme/repo@main", 7, subgraph)

    assert cache.get_subgraph("u1", "intent", 4000, "acme/repo@main", 7) is subgraph
    assert cache.get_subgraph("u1", "intent", 4000, "acme/repo@main", 8) is None
    assert cache.get_subgraph("u1", "intent", 4000, "acme/repo@feature", 7) is None


# ---------------------------------------------------------------------------
# L3 response cache invalidation
# ---------------------------------------------------------------------------


def test_response_cache_returns_entry_after_put():
    cache = InMemoryResponseCache()
    cache.put("hash-a", "ws1", "answer", {}, file_paths=["/repo/app.py"])
    assert cache.get("hash-a", "ws1").answer == "answer"


def test_response_cache_invalidate_files_evicts_tagged_entry():
    cache = InMemoryResponseCache()
    cache.put("hash-a", "ws1", "answer", {}, file_paths=["/repo/app.py"])
    removed = cache.invalidate_files(["/repo/app.py"])
    assert removed == 1
    assert cache.get("hash-a", "ws1") is None


def test_response_cache_invalidate_files_leaves_unrelated_entries():
    cache = InMemoryResponseCache()
    cache.put("hash-a", "ws1", "answer-a", {}, file_paths=["/repo/app.py"])
    cache.put("hash-b", "ws1", "answer-b", {}, file_paths=["/repo/other.py"])
    cache.invalidate_files(["/repo/app.py"])
    assert cache.get("hash-a", "ws1") is None
    assert cache.get("hash-b", "ws1").answer == "answer-b"


def test_response_cache_invalidate_entry_shared_across_files():
    # A response referencing two files is evicted when either file changes.
    cache = InMemoryResponseCache()
    cache.put(
        "hash-ab",
        "ws1",
        "answer",
        {},
        file_paths=["/repo/a.py", "/repo/b.py"],
    )
    cache.invalidate_files(["/repo/b.py"])
    assert cache.get("hash-ab", "ws1") is None


def test_response_cache_invalidate_unknown_file_is_noop():
    cache = InMemoryResponseCache()
    cache.put("hash-a", "ws1", "answer", {}, file_paths=["/repo/app.py"])
    removed = cache.invalidate_files(["/repo/nonexistent.py"])
    assert removed == 0
    assert cache.get("hash-a", "ws1").answer == "answer"


def test_layered_cache_invalidate_files_clears_l1_and_l3():
    cache = LayeredCache()
    cache.put_body("/repo/app.py", (1, 10), "hash-x", CachedBody("code", 40))
    cache.put_response("hash-q", "ws1", "answer", {}, file_paths=["/repo/app.py"])

    cache.invalidate_files(["/repo/app.py"], workspace_id="ws1")

    assert cache.get_body("/repo/app.py", (1, 10), "hash-x") is None
    assert cache.get_response("hash-q", "ws1") is None


def test_layered_cache_invalidate_files_does_not_touch_other_workspace_responses():
    cache = LayeredCache()
    # Two workspaces sharing the same file — invalidating for ws1 must not
    # evict ws2's entry; workspace isolation must be preserved.
    cache.put_response("hash-a", "ws1", "ans-a", {}, file_paths=["/repo/app.py"])
    cache.put_response("hash-b", "ws2", "ans-b", {}, file_paths=["/repo/app.py"])
    cache.invalidate_files(["/repo/app.py"], workspace_id="ws1")
    assert cache.get_response("hash-a", "ws1") is None
    assert cache.get_response("hash-b", "ws2").answer == "ans-b"


def test_response_cache_put_overwrite_reindexes_file_paths():
    cache = InMemoryResponseCache()
    cache.put("hash-a", "ws1", "answer-v1", {}, file_paths=["/repo/old.py"])
    cache.put("hash-a", "ws1", "answer-v2", {}, file_paths=["/repo/new.py"])

    assert cache.get("hash-a", "ws1").answer == "answer-v2"
    assert cache.invalidate_files(["/repo/old.py"], workspace_id="ws1") == 0
    assert cache.get("hash-a", "ws1").answer == "answer-v2"
    assert cache.invalidate_files(["/repo/new.py"], workspace_id="ws1") == 1
    assert cache.get("hash-a", "ws1") is None
    assert "/repo/old.py" not in cache._file_index
    assert "/repo/new.py" not in cache._file_index


def test_response_cache_lru_eviction_unindexes_stale_file_paths():
    cache = InMemoryResponseCache(capacity=2)
    cache.put("hash-a", "ws1", "a", {}, file_paths=["/repo/a.py"])
    cache.put("hash-b", "ws1", "b", {}, file_paths=["/repo/b.py"])
    cache.put("hash-c", "ws1", "c", {}, file_paths=["/repo/c.py"])

    assert cache.get("hash-a", "ws1") is None
    assert cache.invalidate_files(["/repo/a.py"], workspace_id="ws1") == 0
    assert "/repo/a.py" not in cache._file_index
