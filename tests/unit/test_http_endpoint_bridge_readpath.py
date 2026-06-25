from unittest.mock import MagicMock

from context_engine.axis.graph_walk import Neighbour
from context_engine.axis.http_endpoint_bridge import (
    _rank_http_callers,
    expand_http_endpoint_bridge,
)
from context_engine.axis.role_retrieval import RoleCandidate, WorkspaceScan


def test_rank_http_callers_drops_non_core_and_orders_by_reach():
    """Structural ranking: non-core tier excluded; reach (centrality) orders the rest."""
    rows = {
        "noise": {"file_tier": "example"},  # high reach but non-core -> dropped
        "caller_lo": {"file_tier": "core"},
        "caller_hi": {"file_tier": "core"},
    }
    neighbours = [
        Neighbour(uid="noise", name="X", file_path="a.ts", depth=1, reach=9),
        Neighbour(uid="caller_lo", name="Y", file_path="b.ts", depth=1, reach=1),
        Neighbour(uid="caller_hi", name="Z", file_path="c.ts", depth=1, reach=4),
    ]
    ranked = _rank_http_callers(neighbours, rows_by_uid=rows)
    assert [n.uid for n in ranked] == ["caller_hi", "caller_lo"]


def test_expand_http_endpoint_bridge_from_handler_seed(monkeypatch):
    handler = RoleCandidate(
        uid="handler-uid",
        name="ask",
        file_path="handler.py",
        role="routing_surface",
        satisfying_contracts=(),
        satisfying_kinds=(),
        contract_count=0,
        kind_count=0,
        vector_distance=0.2,
        score=0.8,
    )

    monkeypatch.setattr(
        "context_engine.axis.http_endpoint_bridge._collect_http_clients",
        lambda db, workspace_id, seed_uids: {"handler-uid": ["client-uid"]},
    )
    monkeypatch.setattr(
        "context_engine.axis.http_endpoint_bridge._seed_idf_weights",
        lambda seed_uids, **kwargs: dict.fromkeys(seed_uids, 1.0),
    )
    monkeypatch.setattr(
        "context_engine.axis.http_endpoint_bridge.walk_neighbours",
        lambda db, ws, client_uids, **kwargs: [
            Neighbour("caller_hi", "entrypoint", "caller_hi.ts", 1, 3),
            Neighbour("caller_lo", "helper", "caller_lo.ts", 1, 1),
            Neighbour("noise", "Sample", "sample.ts", 1, 9),  # non-core -> dropped
        ],
    )

    scan = WorkspaceScan(
        rows=[],
        vectors=None,
        rows_by_uid={
            "caller_hi": {"file_tier": "core"},
            "caller_lo": {"file_tier": "core"},
            "noise": {"file_tier": "example"},
        },
    )
    out = expand_http_endpoint_bridge(
        [handler],
        db=MagicMock(),
        workspace_id="ws",
        prescanned=scan,
    )
    # noise tier-dropped; reach orders the two core callers
    assert [c.uid for c in out] == ["caller_hi", "caller_lo"]
    assert out[0].role == "http_endpoint_bridge"
    assert out[0].satisfying_kinds == ("http_client_caller",)
