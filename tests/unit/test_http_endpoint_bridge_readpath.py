from unittest.mock import MagicMock

from context_engine.axis.graph_walk import Neighbour
from context_engine.axis.http_endpoint_bridge import (
    _rank_http_callers,
    expand_http_endpoint_bridge,
)
from context_engine.axis.role_retrieval import RoleCandidate, WorkspaceScan


def test_rank_http_callers_prefers_view_provider_over_dashboard():
    rows = {
        "ask_handler": {"file_tier": "core"},
        "metrics": {"file_tier": "core"},
    }
    neighbours = [
        Neighbour(
            uid="metrics",
            name="loadMetrics",
            file_path="/repo/extension/src/panels/DashboardPanel.ts",
            depth=1,
            reach=1,
        ),
        Neighbour(
            uid="ask_handler",
            name="handleAsk",
            file_path="/repo/extension/src/providers/SurgicalContextViewProvider.ts",
            depth=1,
            reach=1,
        ),
    ]
    ranked = _rank_http_callers(neighbours, rows_by_uid=rows)
    assert [n.uid for n in ranked] == ["ask_handler", "metrics"]


def test_expand_http_endpoint_bridge_from_handler_seed(monkeypatch):
    handler = RoleCandidate(
        uid="handler-uid",
        name="ask",
        file_path="/repo/context_engine/main.py",
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
        lambda seed_uids, **kwargs: {uid: 1.0 for uid in seed_uids},
    )
    monkeypatch.setattr(
        "context_engine.axis.http_endpoint_bridge.walk_neighbours",
        lambda db, ws, client_uids, **kwargs: [
            Neighbour(
                "provider-ask",
                "handleAsk",
                "/repo/extension/src/providers/SurgicalContextViewProvider.ts",
                1,
                1,
            ),
            Neighbour(
                "activate",
                "activate",
                "/repo/extension/src/extension.ts",
                1,
                1,
            ),
        ],
    )

    out = expand_http_endpoint_bridge(
        [handler],
        db=MagicMock(),
        workspace_id="ws",
        prescanned=WorkspaceScan(rows=[], vectors=None, rows_by_uid={}),
    )
    assert [c.uid for c in out] == ["activate", "provider-ask"]
    assert out[0].role == "http_endpoint_bridge"
    assert out[0].satisfying_kinds == ("http_client_caller",)
