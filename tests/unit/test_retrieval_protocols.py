"""Contract checks for retrieval provider protocols (no database)."""

from __future__ import annotations

from unittest.mock import MagicMock

from sidecar.context.arbitrator import ContextArbitrator
from sidecar.context.ranker.candidate_pool import VectorSearcher
from sidecar.retrieval.adapters import Neo4jWorkspaceMetaAdapter
from sidecar.retrieval.fakes import (
    FakeGraphDriverProvider,
    FakeVectorSearchProvider,
    FakeWorkspaceMetaProvider,
)
from sidecar.retrieval.protocols import (
    GraphDriverProvider,
    VectorSearchProvider,
    WorkspaceMetaProvider,
)


def test_fake_vector_provider_isinstance_and_returns_rows():
    ws = "local/test@main"
    fake = FakeVectorSearchProvider(
        docs_by_workspace={
            ws: [
                {
                    "chunk_id": "c1",
                    "file_path": "/a.md",
                    "content": "hello",
                    "score": 0.9,
                }
            ]
        },
        symbols_by_workspace={ws: [{"uid": "u1", "name": "foo"}]},
    )
    assert isinstance(fake, VectorSearchProvider)
    docs = fake.search_docs("q", 10, workspace_id=ws)
    assert len(docs) == 1 and docs[0]["chunk_id"] == "c1"
    syms = fake.search_symbols("q", 10, workspace_id=ws)
    assert syms[0]["uid"] == "u1"


def test_vector_searcher_wraps_lancedb_and_satisfies_protocol():
    inner = MagicMock()
    inner.search.return_value = []
    inner.search_symbols.return_value = []
    vs = VectorSearcher(inner)
    assert isinstance(vs, VectorSearchProvider)


def test_fake_workspace_meta_provider():
    meta = FakeWorkspaceMetaProvider(
        profiles={"ws": {"strategy_profile": {"x": 1}}},
        graph_versions={"ws": 7},
    )
    assert isinstance(meta, WorkspaceMetaProvider)
    assert meta.repository_profile("ws")["strategy_profile"]["x"] == 1
    assert meta.graph_version("ws") == 7
    assert meta.graph_version("missing") == 0


def test_fake_graph_driver_provider():
    drv = MagicMock()
    g = FakeGraphDriverProvider(drv)
    assert isinstance(g, GraphDriverProvider)
    assert g.driver is drv


def test_neo4j_workspace_meta_adapter_delegates():
    db = MagicMock()
    db.get_repository_profile.return_value = {"strategy_profile": {"tier": "full"}}
    db.get_workspace_graph_version.return_value = 11
    ad = Neo4jWorkspaceMetaAdapter(db)
    assert isinstance(ad, WorkspaceMetaProvider)
    assert ad.repository_profile("ws1")["strategy_profile"]["tier"] == "full"
    db.get_repository_profile.assert_called_once_with(workspace_id="ws1")
    assert ad.graph_version("ws1") == 11


def test_arbitrator_workspace_meta_overrides_profile_and_graph_version():
    meta = FakeWorkspaceMetaProvider(
        profiles={"local/x": {"capabilities": {"impact_analysis": True}}},
        graph_versions={"local/x": 99},
    )
    db = MagicMock()
    arb = ContextArbitrator(db, workspace_id="local/x", workspace_meta=meta)
    assert arb._repository_profile() == {"capabilities": {"impact_analysis": True}}
    assert arb._graph_version() == 99
    db.get_repository_profile.assert_not_called()
