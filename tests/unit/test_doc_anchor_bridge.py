from unittest.mock import MagicMock

from context_engine.axis.doc_anchor_bridge import _rank_bridge_neighbours, expand_doc_anchor_bridge
from context_engine.axis.graph_walk import Neighbour
from context_engine.axis.role_retrieval import RoleCandidate, WorkspaceScan


def test_rank_bridge_neighbours_drops_non_core_and_orders_by_reach():
    """Structural ranking: non-core tier excluded; reach (centrality) orders the rest."""
    rows = {
        "noise": {"file_tier": "example"},  # high reach but non-core -> dropped
        "core_lo": {"file_tier": "core"},
        "core_hi": {"file_tier": "core"},
    }
    neighbours = [
        Neighbour(uid="noise", name="X", file_path="a.ts", depth=1, reach=9),
        Neighbour(uid="core_lo", name="Y", file_path="b.ts", depth=1, reach=1),
        Neighbour(uid="core_hi", name="Z", file_path="c.ts", depth=1, reach=5),
    ]
    ranked = _rank_bridge_neighbours(neighbours, rows_by_uid=rows)
    assert [n.uid for n in ranked] == ["core_hi", "core_lo"]


def test_expand_doc_anchor_bridge_tier_filters_and_caps(monkeypatch):
    seed = RoleCandidate(
        uid="seed",
        name="Iface",
        file_path="iface.ts",
        role="doc_anchor",
        satisfying_contracts=(),
        satisfying_kinds=(),
        contract_count=0,
        kind_count=0,
        vector_distance=0.1,
        score=0.9,
    )

    def _fake_walk(_db, _ws, seed_uids, **kwargs):
        assert seed_uids == ["seed"]
        return [
            Neighbour("impl_hi", "Impl", "impl_hi.ts", 1, 3),
            Neighbour("impl_lo", "Impl2", "impl_lo.ts", 1, 1),
            Neighbour("noise", "Sample", "sample.ts", 1, 9),  # non-core -> dropped
        ]

    monkeypatch.setattr("context_engine.axis.doc_anchor_bridge.walk_neighbours", _fake_walk)
    monkeypatch.setattr(
        "context_engine.axis.doc_anchor_bridge._seed_idf_weights",
        lambda seed_uids, **kwargs: {uid: 1.0 for uid in seed_uids},
    )

    scan = WorkspaceScan(
        rows=[],
        vectors=None,
        rows_by_uid={
            "impl_hi": {"file_tier": "core"},
            "impl_lo": {"file_tier": "core"},
            "noise": {"file_tier": "example"},
        },
    )
    out = expand_doc_anchor_bridge(
        [seed],
        db=MagicMock(),
        workspace_id="ws",
        prescanned=scan,
        max_per_seed=2,
        max_total=10,
    )
    # noise tier-dropped; reach orders the two core impls; per-seed cap = 2
    assert [c.uid for c in out] == ["impl_hi", "impl_lo"]
    assert all(c.role == "doc_anchor_bridge" for c in out)
