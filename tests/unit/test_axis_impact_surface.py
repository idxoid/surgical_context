from __future__ import annotations

from context_engine.axis import impact_surface
from context_engine.axis.role_retrieval import RoleCandidate


def _candidate(uid: str = "uid:caller") -> RoleCandidate:
    return RoleCandidate(
        uid=uid,
        name="caller",
        file_path="/repo/caller.py",
        role="impact_analysis",
        satisfying_contracts=(),
        satisfying_kinds=("reverse_calls",),
        contract_count=0,
        kind_count=1,
        vector_distance=None,
        score=0.8,
        depth=1,
        edge_type="CALLS_*",
        utility_score=0.95,
    )


def test_build_impact_surface_adds_symbol_navigation_lines(monkeypatch):
    candidate = _candidate()

    class DB:
        def get_symbol_spans_by_uids(self, uids, workspace_id):
            assert uids == [candidate.uid]
            assert workspace_id == "workspace"
            return {
                candidate.uid: {
                    "start_line": 37,
                    "end_line": 52,
                }
            }

    monkeypatch.setattr(
        impact_surface,
        "expand_impact_neighbourhood",
        lambda *args, **kwargs: [candidate],
    )

    surface = impact_surface.build_impact_surface(
        db=DB(),
        symbol_uid="uid:target",
        symbol_name="target",
        file_path="/repo/target.py",
        workspace_id="workspace",
    )

    row = surface["affected_symbols"][0]
    assert row["start_line"] == 37
    assert row["end_line"] == 52


def test_build_impact_surface_keeps_compatibility_without_span_lookup(monkeypatch):
    candidate = _candidate()
    monkeypatch.setattr(
        impact_surface,
        "expand_impact_neighbourhood",
        lambda *args, **kwargs: [candidate],
    )

    surface = impact_surface.build_impact_surface(
        db=object(),
        symbol_uid="uid:target",
        symbol_name="target",
        file_path="/repo/target.py",
        workspace_id="workspace",
    )

    row = surface["affected_symbols"][0]
    assert "start_line" not in row
    assert "end_line" not in row
