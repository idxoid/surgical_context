"""Multi-role intersection boost — graph proximity over candidates."""

from __future__ import annotations

import pytest

from sidecar.axis.cross_role_boost import (
    boost_by_cross_role_proximity,
    intersect_by_cross_role_proximity,
)
from sidecar.axis.role_retrieval import RoleCandidate

WORKSPACE = "qa_repo/test@axis"


class _Result:
    def __init__(self, records):
        self._records = list(records)

    def __iter__(self):
        return iter(self._records)


class _Session:
    def __init__(self, records_by_call: list[list[dict]]):
        self._records = list(records_by_call)
        self.runs: list[tuple[str, dict]] = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def run(self, query: str, **params):
        self.runs.append((query, dict(params)))
        records = self._records.pop(0) if self._records else []
        return _Result(records)


class _Driver:
    def __init__(self, session):
        self._session = session

    def session(self):
        return self._session


class _FakeDB:
    def __init__(self, records_by_call=None):
        self._session = _Session(records_by_call or [])
        self.driver = _Driver(self._session)


def _candidate(
    uid: str,
    name: str,
    *,
    role: str = "proxy_mechanism",
    score: float = 0.5,
) -> RoleCandidate:
    return RoleCandidate(
        uid=uid,
        name=name,
        file_path=f"/tmp/{name}.py",
        role=role,
        satisfying_contracts=(),
        satisfying_kinds=(role.split("_")[0],),
        contract_count=0,
        kind_count=1,
        vector_distance=None,
        score=score,
    )


def test_empty_primary_returns_empty():
    out = boost_by_cross_role_proximity(
        [],
        secondary_by_role={"routing_surface": [_candidate("u:r", "r")]},
        db=_FakeDB(),
        workspace_id=WORKSPACE,
    )
    assert out == []


def test_empty_secondary_returns_primary_unchanged():
    primary = [_candidate("u:p", "p", score=0.5)]
    out = boost_by_cross_role_proximity(
        primary,
        secondary_by_role={},
        db=_FakeDB(),
        workspace_id=WORKSPACE,
    )
    assert out == primary
    assert out[0].score == 0.5


def test_candidate_within_max_hops_gets_boosted():
    primary = [_candidate("u:proxy", "proxy", score=0.5)]
    secondary = {
        "routing_surface": [_candidate("u:route", "route", role="routing_surface")]
    }
    # Proximity query returns u:proxy → u:route reachable.
    db = _FakeDB([[{"primary_uid": "u:proxy", "reachable": ["u:route"]}]])

    out = boost_by_cross_role_proximity(
        primary,
        secondary_by_role=secondary,
        db=db,
        workspace_id=WORKSPACE,
        boost_per_role=0.2,
    )

    assert out[0].uid == "u:proxy"
    assert out[0].score == pytest.approx(0.7)


def test_candidate_not_in_proximity_keeps_score():
    primary = [
        _candidate("u:near", "near", score=0.5),
        _candidate("u:far", "far", score=0.4),
    ]
    secondary = {
        "routing_surface": [_candidate("u:r", "r", role="routing_surface")]
    }
    # Only u:near is reachable.
    db = _FakeDB([[{"primary_uid": "u:near", "reachable": ["u:r"]}]])

    out = boost_by_cross_role_proximity(
        primary,
        secondary_by_role=secondary,
        db=db,
        workspace_id=WORKSPACE,
        boost_per_role=0.2,
    )

    # u:near got +0.2 → 0.7, u:far stays at 0.4.
    assert out[0].uid == "u:near"
    assert out[0].score == pytest.approx(0.7)
    assert out[1].uid == "u:far"
    assert out[1].score == 0.4


def test_multiple_secondary_roles_stack_boost():
    primary = [_candidate("u:p", "p", score=0.4)]
    secondary = {
        "routing_surface": [_candidate("u:r", "r", role="routing_surface")],
        "dependency_solver": [_candidate("u:d", "d", role="dependency_solver")],
    }
    # u:p reaches BOTH u:r and u:d.
    db = _FakeDB([[{"primary_uid": "u:p", "reachable": ["u:r", "u:d"]}]])

    out = boost_by_cross_role_proximity(
        primary,
        secondary_by_role=secondary,
        db=db,
        workspace_id=WORKSPACE,
        boost_per_role=0.15,
    )

    assert out[0].score == pytest.approx(0.4 + 0.15 * 2)


def test_score_capped_at_ceiling():
    primary = [_candidate("u:p", "p", score=0.9)]
    secondary = {
        "routing_surface": [_candidate("u:r", "r", role="routing_surface")],
        "dependency_solver": [_candidate("u:d", "d", role="dependency_solver")],
        "data_model_surface": [_candidate("u:dm", "dm", role="data_model_surface")],
    }
    # u:p reaches all three secondary roles → would push score to 1.35,
    # but the ceiling caps it.
    db = _FakeDB(
        [[{"primary_uid": "u:p", "reachable": ["u:r", "u:d", "u:dm"]}]]
    )

    out = boost_by_cross_role_proximity(
        primary,
        secondary_by_role=secondary,
        db=db,
        workspace_id=WORKSPACE,
        boost_per_role=0.15,
        score_ceiling=1.0,
    )

    assert out[0].score == 1.0


def test_results_resorted_after_boost():
    """A primary with a low base score plus a multi-role boost can
    overtake a higher-base-score primary with no boost — that's the
    whole point of the intersection.
    """
    primary = [
        _candidate("u:high", "high", score=0.7),
        _candidate("u:boosted", "boosted", score=0.5),
    ]
    secondary = {
        "routing_surface": [_candidate("u:r", "r", role="routing_surface")],
        "dependency_solver": [_candidate("u:d", "d", role="dependency_solver")],
    }
    # Only u:boosted has cross-role neighbours, on both secondaries.
    db = _FakeDB(
        [[{"primary_uid": "u:boosted", "reachable": ["u:r", "u:d"]}]]
    )

    out = boost_by_cross_role_proximity(
        primary,
        secondary_by_role=secondary,
        db=db,
        workspace_id=WORKSPACE,
        boost_per_role=0.15,
    )

    # u:boosted: 0.5 + 0.3 = 0.8 > u:high (0.7).
    assert [c.uid for c in out] == ["u:boosted", "u:high"]


@pytest.mark.parametrize("bad_hops", [0, -1, 1.5, "2", True])
def test_cross_role_rejects_unsafe_max_hops(bad_hops):
    primary = [_candidate("u:p", "p")]
    secondary = {
        "routing_surface": [_candidate("u:r", "r", role="routing_surface")]
    }

    with pytest.raises(ValueError, match="max_hops"):
        boost_by_cross_role_proximity(
            primary,
            secondary_by_role=secondary,
            db=_FakeDB(),
            workspace_id=WORKSPACE,
            max_hops=bad_hops,  # type: ignore[arg-type]
        )


def test_intersect_drops_candidates_without_cross_role_neighbours():
    """The intersection variant *filters* — a primary candidate that
    has no secondary-role neighbour is removed entirely, not just
    left at its base score. This is the structural meaning of the
    multi-intent conjunction.
    """
    primary = [
        _candidate("u:in", "in", score=0.5),
        _candidate("u:out", "out", score=0.9),
    ]
    secondary = {
        "routing_surface": [_candidate("u:r", "r", role="routing_surface")]
    }
    # Only u:in has a cross-role neighbour.
    db = _FakeDB([[{"primary_uid": "u:in", "reachable": ["u:r"]}]])

    out = intersect_by_cross_role_proximity(
        primary,
        secondary_by_role=secondary,
        db=db,
        workspace_id=WORKSPACE,
        boost_per_role=0.15,
    )

    assert [c.uid for c in out] == ["u:in"]
    # And the survivor gets the boost.
    assert out[0].score == pytest.approx(0.65)


def test_intersect_empty_falls_back_to_primary():
    """If *no* primary candidate has a cross-role neighbour, the
    fallback returns the original primary list. Without this guard a
    too-narrow intent would zero out the response — defeating the
    purpose of having a fallback intent at all.
    """
    primary = [_candidate("u:a", "a", score=0.5)]
    secondary = {
        "routing_surface": [_candidate("u:r", "r", role="routing_surface")]
    }
    # Empty record set → no proximity.
    db = _FakeDB([[]])

    out = intersect_by_cross_role_proximity(
        primary,
        secondary_by_role=secondary,
        db=db,
        workspace_id=WORKSPACE,
    )

    assert out == primary


def test_intersect_empty_no_fallback_returns_empty():
    primary = [_candidate("u:a", "a", score=0.5)]
    secondary = {
        "routing_surface": [_candidate("u:r", "r", role="routing_surface")]
    }
    db = _FakeDB([[]])

    out = intersect_by_cross_role_proximity(
        primary,
        secondary_by_role=secondary,
        db=db,
        workspace_id=WORKSPACE,
        fallback_on_empty=False,
    )

    assert out == []


def test_unsafe_edge_pattern_rejected():
    """``_safe_rel_pattern`` defends against injection by validating
    every edge type at construction time. Cypher injection through the
    proximity walk is impossible by construction.
    """
    from sidecar.axis.cross_role_boost import _safe_rel_pattern

    with pytest.raises(ValueError, match="unsafe edge type"):
        _safe_rel_pattern(["CALLS", "DROP TABLE"])
