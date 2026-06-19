from unittest.mock import MagicMock

from context_engine.axis.doc_anchor_bridge import _rank_bridge_neighbours, expand_doc_anchor_bridge
from context_engine.axis.graph_walk import Neighbour
from context_engine.axis.role_retrieval import RoleCandidate, WorkspaceScan


def test_rank_bridge_neighbours_prefers_packages_core_over_integration():
    rows = {
        "int": {"file_tier": "core"},
        "core": {"file_tier": "core"},
    }
    neighbours = [
        Neighbour(
            uid="int",
            name="Interceptor",
            file_path="/repo/integration/scopes/src/hello/interceptors/logging.interceptor.ts",
            depth=1,
            reach=1,
        ),
        Neighbour(
            uid="core",
            name="GuardsConsumer",
            file_path="/repo/packages/core/guards/guards-consumer.ts",
            depth=1,
            reach=1,
        ),
    ]
    ranked = _rank_bridge_neighbours(neighbours, rows_by_uid=rows)
    assert [n.uid for n in ranked] == ["core", "int"]


def test_expand_doc_anchor_bridge_caps_per_seed(monkeypatch):
    seed_a = RoleCandidate(
        uid="seed-a",
        name="CanActivate",
        file_path="iface.ts",
        role="doc_anchor",
        satisfying_contracts=(),
        satisfying_kinds=(),
        contract_count=0,
        kind_count=0,
        vector_distance=0.1,
        score=0.9,
    )
    seed_b = RoleCandidate(
        uid="seed-b",
        name="NestInterceptor",
        file_path="iface2.ts",
        role="doc_anchor",
        satisfying_contracts=(),
        satisfying_kinds=(),
        contract_count=0,
        kind_count=0,
        vector_distance=0.1,
        score=0.8,
    )

    def _fake_walk(_db, _ws, seed_uids, **kwargs):
        if seed_uids == ["seed-a"]:
            return [
                Neighbour("g1", "GuardsConsumer", "packages/core/guards/guards-consumer.ts", 1, 1),
                Neighbour("g2", "tryActivate", "packages/core/guards/guards-consumer.ts", 1, 1),
                Neighbour("g3", "GuardsContextCreator", "packages/core/guards/guards-context-creator.ts", 1, 1),
                Neighbour("g4", "extra", "packages/core/guards/extra.ts", 1, 1),
            ]
        return [
            Neighbour("i1", "InterceptorsConsumer", "packages/core/interceptors/interceptors-consumer.ts", 1, 1),
        ]

    monkeypatch.setattr("context_engine.axis.doc_anchor_bridge.walk_neighbours", _fake_walk)
    monkeypatch.setattr(
        "context_engine.axis.doc_anchor_bridge._seed_idf_weights",
        lambda seed_uids, **kwargs: {uid: 1.0 for uid in seed_uids},
    )

    out = expand_doc_anchor_bridge(
        [seed_a, seed_b],
        db=MagicMock(),
        workspace_id="ws",
        prescanned=WorkspaceScan(rows=[], vectors=None, rows_by_uid={}),
        max_per_seed=2,
        max_total=10,
    )
    assert len(out) == 3
    assert {c.uid for c in out} == {"g1", "g3", "i1"}
