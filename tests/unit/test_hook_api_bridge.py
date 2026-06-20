from unittest.mock import MagicMock

from context_engine.axis.graph_walk import Neighbour
from context_engine.axis.hook_api_bridge import expand_hook_api_bridge
from context_engine.axis.role_retrieval import RoleCandidate, WorkspaceScan


def _seed(uid: str) -> RoleCandidate:
    return RoleCandidate(
        uid=uid,
        name=uid,
        file_path=f"{uid}.py",
        role="vector_seed",
        satisfying_contracts=(),
        satisfying_kinds=(),
        contract_count=0,
        kind_count=0,
        vector_distance=0.2,
        score=0.8,
    )


def _walk_router(by_edges: dict[tuple[str, ...], list[Neighbour]]):
    """Dispatch a mocked walk_neighbours by its ``edges`` argument."""

    def _walk(_db, _ws, _seeds, *, edges, **kwargs):
        return list(by_edges.get(tuple(edges), []))

    return _walk


def _scan(rows_by_uid):
    return WorkspaceScan(rows=[], vectors=None, rows_by_uid=rows_by_uid)


def test_bridge_reaches_dispatch_api_via_event_then_hook(monkeypatch):
    """seed → (EVENT site) → (HOOK_CONFIG api); gate + ranking are structural."""
    monkeypatch.setattr(
        "context_engine.axis.hook_api_bridge.walk_neighbours",
        _walk_router(
            {
                ("HAS_API", "INHERITED_API"): [],
                ("EVENT_SUB", "EVENT_PUB"): [Neighbour("site", "handler", "site.py", 1, 1)],
                ("HOOK_CONFIG", "HOOK_EXEC"): [Neighbour("api", "register", "api.py", 1, 2)],
            }
        ),
    )
    out = expand_hook_api_bridge(
        [_seed("topic")],
        db=MagicMock(),
        workspace_id="ws",
        prescanned=_scan({"api": {"file_tier": "core"}}),
    )
    assert [c.uid for c in out] == ["api"]
    assert out[0].role == "hook_api_bridge"
    assert out[0].edge_type == "HOOK_CONFIG"


def test_bridge_skips_when_no_event_sites(monkeypatch):
    """No EVENT_SUB/EVENT_PUB reach → not an event topic → nothing emitted."""
    monkeypatch.setattr(
        "context_engine.axis.hook_api_bridge.walk_neighbours",
        _walk_router(
            {
                ("HAS_API", "INHERITED_API"): [],
                ("EVENT_SUB", "EVENT_PUB"): [],
                ("HOOK_CONFIG", "HOOK_EXEC"): [Neighbour("api", "register", "api.py", 1, 2)],
            }
        ),
    )
    out = expand_hook_api_bridge(
        [_seed("plain")],
        db=MagicMock(),
        workspace_id="ws",
        prescanned=_scan({}),
    )
    assert out == []


def test_bridge_reaches_api_through_member_surface(monkeypatch):
    """EVENT edges hang off a HAS_API member of the topic, not the class node."""
    monkeypatch.setattr(
        "context_engine.axis.hook_api_bridge.walk_neighbours",
        _walk_router(
            {
                ("HAS_API", "INHERITED_API"): [Neighbour("member", "on_x", "member.py", 1, 1)],
                ("EVENT_SUB", "EVENT_PUB"): [Neighbour("site", "handler", "site.py", 1, 1)],
                ("HOOK_CONFIG", "HOOK_EXEC"): [Neighbour("api", "register", "api.py", 1, 3)],
            }
        ),
    )
    out = expand_hook_api_bridge(
        [_seed("topicclass")],
        db=MagicMock(),
        workspace_id="ws",
        prescanned=_scan({"api": {"file_tier": "core"}}),
    )
    assert [c.uid for c in out] == ["api"]


def test_bridge_drops_non_core_tier_api(monkeypatch):
    """The registration API is kept only when its file-tier is core."""
    monkeypatch.setattr(
        "context_engine.axis.hook_api_bridge.walk_neighbours",
        _walk_router(
            {
                ("HAS_API", "INHERITED_API"): [],
                ("EVENT_SUB", "EVENT_PUB"): [Neighbour("site", "h", "site.py", 1, 1)],
                ("HOOK_CONFIG", "HOOK_EXEC"): [Neighbour("api", "register", "api.py", 1, 2)],
            }
        ),
    )
    out = expand_hook_api_bridge(
        [_seed("topic")],
        db=MagicMock(),
        workspace_id="ws",
        prescanned=_scan({"api": {"file_tier": "test"}}),
    )
    assert out == []
