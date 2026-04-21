"""Unit tests for retrieval cache primitives."""

from sidecar.cache.layered import CachedBody, LayeredCache
from sidecar.context.types import Subgraph, SubgraphNode


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
