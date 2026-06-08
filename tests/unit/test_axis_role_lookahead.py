"""Cross-role lookahead — graph-evidenced candidate expansion."""

from __future__ import annotations

import json
from typing import Any

import pytest

from sidecar.axis.role_lookahead import expand_candidates_via_neighbourhood
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


class _FakeLanceTable:
    def __init__(self, rows: list[dict[str, Any]]):
        self._rows = rows

    def to_lance(self):
        outer = self

        class _Lance:
            def to_table(self, columns=None):
                class _Arrow:
                    def to_pylist(self_inner):
                        return list(outer._rows)

                return _Arrow()

        return _Lance()


class _FakeLance:
    def __init__(self, rows: list[dict[str, Any]]):
        self._sym_table = _FakeLanceTable(rows)


def _candidate(
    uid: str,
    *,
    role: str,
    name: str | None = None,
    score: float = 0.6,
) -> RoleCandidate:
    return RoleCandidate(
        uid=uid,
        name=name or uid.split(":")[-1],
        file_path=f"/tmp/{uid}.py",
        role=role,
        satisfying_contracts=(),
        satisfying_kinds=(),
        contract_count=0,
        kind_count=0,
        vector_distance=None,
        score=score,
    )


def _lance_row(uid: str, *, kinds: list[str], name: str = "n") -> dict[str, Any]:
    return {
        "uid": uid,
        "name": name,
        "file_path": f"/tmp/{name}.py",
        "axis_container_kinds_json": json.dumps(
            [{"kind": k} for k in kinds]
        ),
        "workspace_id": WORKSPACE,
    }


def test_no_intent_roles_returns_unchanged():
    """Edge case: an intent ranking with no known roles produces no
    kind-to-role mapping, so there is nothing the lookahead can attribute
    a neighbour to."""
    cands = {"unknown_role": [_candidate("u:a", role="unknown_role")]}
    out = expand_candidates_via_neighbourhood(
        ["unknown_role"],
        cands,
        db=_FakeDB(),
        lance=_FakeLance([]),
        workspace_id=WORKSPACE,
    )
    assert out == cands


def test_neighbour_backing_other_role_is_injected():
    """The headline case. ``proxy_mechanism`` seed's K-hop neighbour
    carries ``keyed_dispatch_callable`` (which backs ``dispatch_surface``);
    the lookahead injects it into the ``dispatch_surface`` pool."""
    seed = _candidate("u:proxy", role="proxy_mechanism")
    cands = {"proxy_mechanism": [seed], "dispatch_surface": []}

    # Neo4j: u:proxy reaches u:dispatcher.
    db = _FakeDB([[{"seed_uid": "u:proxy", "neighbours": ["u:dispatcher"]}]])
    lance = _FakeLance(
        [
            _lance_row(
                "u:dispatcher",
                kinds=["keyed_dispatch_callable"],
                name="dispatch_request",
            )
        ]
    )

    out = expand_candidates_via_neighbourhood(
        ["proxy_mechanism", "dispatch_surface"],
        cands,
        db=db,
        lance=lance,
        workspace_id=WORKSPACE,
    )

    injected = out["dispatch_surface"]
    assert len(injected) == 1
    assert injected[0].uid == "u:dispatcher"
    assert injected[0].role == "dispatch_surface"
    assert "keyed_dispatch_callable" in injected[0].satisfying_kinds
    assert injected[0].kind_count == 1
    # Proxy seed pool is untouched.
    assert [c.uid for c in out["proxy_mechanism"]] == ["u:proxy"]


def test_neighbour_already_in_target_role_is_not_duplicated():
    """A neighbour that is *already* a vector-retrieved candidate for the
    target role should not be re-injected — duplicate uids would inflate
    counts and break consumer dedup."""
    seed = _candidate("u:proxy", role="proxy_mechanism")
    pre_existing = _candidate("u:dispatcher", role="dispatch_surface", score=0.9)
    cands = {
        "proxy_mechanism": [seed],
        "dispatch_surface": [pre_existing],
    }
    db = _FakeDB([[{"seed_uid": "u:proxy", "neighbours": ["u:dispatcher"]}]])
    lance = _FakeLance(
        [_lance_row("u:dispatcher", kinds=["keyed_dispatch_callable"])]
    )

    out = expand_candidates_via_neighbourhood(
        ["proxy_mechanism", "dispatch_surface"],
        cands,
        db=db,
        lance=lance,
        workspace_id=WORKSPACE,
    )

    assert [c.uid for c in out["dispatch_surface"]] == ["u:dispatcher"]
    # And the original score is preserved — lookahead must not overwrite
    # the higher-confidence vector entry with its base_score.
    assert out["dispatch_surface"][0].score == 0.9


def test_neighbour_that_is_another_roles_seed_is_not_injected():
    """When the K-hop walk reaches a node that is *already* a seed for
    some role in the intent, it is part of the existing candidate set
    and must not be re-injected through the lookahead."""
    proxy_seed = _candidate("u:proxy", role="proxy_mechanism")
    dispatch_seed = _candidate("u:dispatcher", role="dispatch_surface")
    cands = {
        "proxy_mechanism": [proxy_seed],
        "dispatch_surface": [dispatch_seed],
    }
    # Proxy reaches dispatcher (which is already a dispatch seed) AND
    # an entirely new uid u:other carrying middleware_chain.
    db = _FakeDB(
        [
            [
                {
                    "seed_uid": "u:proxy",
                    "neighbours": ["u:dispatcher", "u:other"],
                }
            ],
            # second iteration: dispatch_surface seed walks too
            [{"seed_uid": "u:dispatcher", "neighbours": []}],
        ]
    )
    lance = _FakeLance(
        [
            _lance_row("u:dispatcher", kinds=["keyed_dispatch_callable"]),
            _lance_row("u:other", kinds=["middleware_chain"]),
        ]
    )

    out = expand_candidates_via_neighbourhood(
        ["proxy_mechanism", "dispatch_surface"],
        cands,
        db=db,
        lance=lance,
        workspace_id=WORKSPACE,
    )

    # u:dispatcher stays the only dispatch seed (already there);
    # u:other gets injected (middleware_chain → dispatch_surface).
    uids = [c.uid for c in out["dispatch_surface"]]
    assert uids == ["u:dispatcher", "u:other"]


def test_neighbour_kind_not_in_intent_is_ignored():
    """A neighbour whose kind backs only roles the intent classifier did
    NOT shout is irrelevant. Injecting it would expand into roles the
    question never gestured at — exactly what intersection guards
    against."""
    seed = _candidate("u:proxy", role="proxy_mechanism")
    cands = {"proxy_mechanism": [seed], "dispatch_surface": []}
    db = _FakeDB([[{"seed_uid": "u:proxy", "neighbours": ["u:cfg"]}]])
    # config_carrier backs configuration_surface, NOT dispatch_surface
    # — and configuration_surface is not in the intent ranking.
    lance = _FakeLance([_lance_row("u:cfg", kinds=["config_carrier"])])

    out = expand_candidates_via_neighbourhood(
        ["proxy_mechanism", "dispatch_surface"],
        cands,
        db=db,
        lance=lance,
        workspace_id=WORKSPACE,
    )

    assert out["dispatch_surface"] == []


def test_max_injected_per_role_cap_respected():
    """Dense graphs can reach many neighbours; the cap keeps the
    graph-derived pool from drowning the vector-derived signal."""
    seed = _candidate("u:proxy", role="proxy_mechanism")
    cands = {"proxy_mechanism": [seed], "dispatch_surface": []}
    neighbours = [f"u:n{i}" for i in range(20)]
    db = _FakeDB([[{"seed_uid": "u:proxy", "neighbours": neighbours}]])
    lance = _FakeLance(
        [_lance_row(u, kinds=["middleware_chain"]) for u in neighbours]
    )

    out = expand_candidates_via_neighbourhood(
        ["proxy_mechanism", "dispatch_surface"],
        cands,
        db=db,
        lance=lance,
        workspace_id=WORKSPACE,
        max_injected_per_role=5,
    )

    assert len(out["dispatch_surface"]) == 5


def test_workspace_isolation_blocks_neighbour_kind_lookup():
    """The Lance fetch must filter by workspace_id so a neighbour uid
    that collides across workspaces is not attributed cross-workspace."""
    seed = _candidate("u:proxy", role="proxy_mechanism")
    cands = {"proxy_mechanism": [seed], "dispatch_surface": []}
    db = _FakeDB([[{"seed_uid": "u:proxy", "neighbours": ["u:other_ws"]}]])
    # Same uid lives in a different workspace.
    row = {
        "uid": "u:other_ws",
        "name": "x",
        "file_path": "/tmp/x.py",
        "axis_container_kinds_json": json.dumps(
            [{"kind": "keyed_dispatch_callable"}]
        ),
        "workspace_id": "some_other_workspace",
    }
    lance = _FakeLance([row])

    out = expand_candidates_via_neighbourhood(
        ["proxy_mechanism", "dispatch_surface"],
        cands,
        db=db,
        lance=lance,
        workspace_id=WORKSPACE,
    )

    assert out["dispatch_surface"] == []


def test_auto_promote_creates_non_intent_role_pool():
    """When the intent classifier missed a role but the graph proves
    its relevance — at least ``auto_promote_min_hits`` distinct
    neighbours back it — the role is promoted into the output dict as
    its own pool of synthesised candidates.

    This is the case the user named: a question that primes
    ``proxy_mechanism`` but whose mechanism actually lives in
    ``dispatch_surface``-tagged dispatchers. The intent classifier
    cannot see this — only the graph can.
    """
    seed = _candidate("u:proxy", role="proxy_mechanism")
    cands = {"proxy_mechanism": [seed]}
    db = _FakeDB(
        [
            [
                {
                    "seed_uid": "u:proxy",
                    "neighbours": ["u:d1", "u:d2", "u:d3", "u:other"],
                }
            ]
        ]
    )
    lance = _FakeLance(
        [
            _lance_row("u:d1", kinds=["keyed_dispatch_callable"], name="dispatch_request"),
            _lance_row("u:d2", kinds=["keyed_dispatch_callable"], name="wsgi_app"),
            _lance_row("u:d3", kinds=["keyed_dispatch_callable"], name="url_for"),
            _lance_row("u:other", kinds=["config_carrier"], name="cfg"),
        ]
    )

    out = expand_candidates_via_neighbourhood(
        ["proxy_mechanism"],  # only one intent role
        cands,
        db=db,
        lance=lance,
        workspace_id=WORKSPACE,
        auto_promote_min_hits=3,
    )

    # dispatch_surface was promoted (3 hits).
    assert "dispatch_surface" in out
    promoted_uids = sorted(c.uid for c in out["dispatch_surface"])
    assert promoted_uids == ["u:d1", "u:d2", "u:d3"]
    # configuration_surface had only 1 hit → not promoted.
    assert "configuration_surface" not in out


def test_auto_promote_threshold_blocks_weak_evidence():
    """A single neighbour does not justify promoting a brand-new role.
    The threshold guards against intent inflation."""
    seed = _candidate("u:proxy", role="proxy_mechanism")
    cands = {"proxy_mechanism": [seed]}
    db = _FakeDB([[{"seed_uid": "u:proxy", "neighbours": ["u:single"]}]])
    lance = _FakeLance(
        [_lance_row("u:single", kinds=["keyed_dispatch_callable"])]
    )

    out = expand_candidates_via_neighbourhood(
        ["proxy_mechanism"],
        cands,
        db=db,
        lance=lance,
        workspace_id=WORKSPACE,
        auto_promote_min_hits=3,
    )

    assert "dispatch_surface" not in out


def test_auto_promote_pool_filter_restricts_eligible_roles():
    """Callers can narrow which roles are eligible for promotion to
    avoid e.g. ``binding_surface`` umbrella inflation."""
    seed = _candidate("u:proxy", role="proxy_mechanism")
    cands = {"proxy_mechanism": [seed]}
    db = _FakeDB(
        [
            [
                {
                    "seed_uid": "u:proxy",
                    "neighbours": ["u:d1", "u:d2", "u:d3"],
                }
            ]
        ]
    )
    lance = _FakeLance(
        [
            _lance_row(u, kinds=["keyed_dispatch_callable"])
            for u in ("u:d1", "u:d2", "u:d3")
        ]
    )

    out = expand_candidates_via_neighbourhood(
        ["proxy_mechanism"],
        cands,
        db=db,
        lance=lance,
        workspace_id=WORKSPACE,
        auto_promote_min_hits=3,
        # Only configuration_surface is eligible for promotion, which
        # ``keyed_dispatch_callable`` does not back.
        auto_promote_role_pool=["configuration_surface"],
    )

    assert "dispatch_surface" not in out
    assert "configuration_surface" not in out


def test_unsafe_edge_pattern_rejected():
    """Cypher injection through the proximity walk is impossible: every
    edge type goes through the safe-pattern validator."""
    from sidecar.axis.role_lookahead import _safe_rel_pattern

    with pytest.raises(ValueError, match="unsafe edge type"):
        _safe_rel_pattern(["CALLS", "DROP TABLE"])
