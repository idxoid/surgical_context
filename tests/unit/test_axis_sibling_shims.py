"""Sibling-shim discovery — directory adjacency for re-export modules."""

from __future__ import annotations

import json
from typing import Any

from sidecar.axis.role_retrieval import RoleCandidate
from sidecar.axis.sibling_shims import expand_sibling_shims


WORKSPACE = "qa_repo/test@axis"


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


def _row(
    uid: str,
    file_path: str,
    *,
    kinds: list[str] | None = None,
    workspace_id: str = WORKSPACE,
) -> dict:
    return {
        "uid": uid,
        "name": file_path.rsplit("/", 1)[-1],
        "file_path": file_path,
        "axis_container_kinds_json": json.dumps(
            [{"kind": k} for k in (kinds or [])]
        ),
        "workspace_id": workspace_id,
    }


def _seed(uid: str, file_path: str) -> RoleCandidate:
    return RoleCandidate(
        uid=uid,
        name=file_path.rsplit("/", 1)[-1],
        file_path=file_path,
        role="routing_surface",
        satisfying_contracts=(),
        satisfying_kinds=(),
        contract_count=0,
        kind_count=0,
        vector_distance=None,
        score=0.5,
    )


def test_no_seeds_returns_empty():
    assert (
        expand_sibling_shims(
            [], lance=_FakeLance([]), workspace_id=WORKSPACE
        )
        == []
    )


def test_picks_empty_kinds_sibling_in_same_directory():
    """The headline case: ``fastapi/routing.py`` is the seed,
    ``fastapi/websockets.py`` is a re-export shim with empty kinds in
    the same directory — the pass surfaces it."""
    seed = _seed("u:routing", "/repo/fastapi/routing.py")
    rows = [
        _row(
            "u:wsmodule", "/repo/fastapi/websockets.py", kinds=[]
        ),  # the shim
        _row(
            "u:routing", "/repo/fastapi/routing.py",
            kinds=["registry_class"],  # the seed itself, not a shim
        ),
    ]
    out = expand_sibling_shims(
        [seed], lance=_FakeLance(rows), workspace_id=WORKSPACE,
    )
    assert len(out) == 1
    c = out[0]
    assert c.uid == "u:wsmodule"
    assert c.file_path == "/repo/fastapi/websockets.py"
    assert c.role == "structural_neighbour"
    assert c.satisfying_kinds == ("sibling_shim",)


def test_skips_files_with_axis_kinds():
    """Files with axis container_kinds are NOT shims — they have a
    body the kind classifier already chewed through. Only files with
    *empty* kinds qualify."""
    seed = _seed("u:routing", "/repo/fastapi/routing.py")
    rows = [
        _row(
            "u:other", "/repo/fastapi/applications.py",
            kinds=["registry_class"],
        ),
    ]
    out = expand_sibling_shims(
        [seed], lance=_FakeLance(rows), workspace_id=WORKSPACE,
    )
    assert out == []


def test_skips_other_workspaces():
    """A shim in a different workspace_id is not adjacent in any
    meaningful sense — the pass must filter by workspace."""
    seed = _seed("u:routing", "/repo/fastapi/routing.py")
    rows = [
        _row(
            "u:other_ws_shim",
            "/repo/fastapi/websockets.py",
            kinds=[],
            workspace_id="some_other_ws",
        ),
    ]
    out = expand_sibling_shims(
        [seed], lance=_FakeLance(rows), workspace_id=WORKSPACE,
    )
    assert out == []


def test_max_shims_cap_respected():
    seed = _seed("u:routing", "/repo/fastapi/routing.py")
    rows = [
        _row(f"u:shim{i}", f"/repo/fastapi/file_{i}.py", kinds=[])
        for i in range(10)
    ]
    out = expand_sibling_shims(
        [seed],
        lance=_FakeLance(rows),
        workspace_id=WORKSPACE,
        max_shims=3,
    )
    assert len(out) == 3


def test_seed_directory_only_no_cross_pollination():
    """A seed in ``fastapi/routing.py`` should NOT surface shims from
    ``celery/app/`` — the pass scopes strictly to seed directories."""
    seed = _seed("u:routing", "/repo/fastapi/routing.py")
    rows = [
        _row("u:far_shim", "/repo/celery/app/empty.py", kinds=[]),
    ]
    out = expand_sibling_shims(
        [seed], lance=_FakeLance(rows), workspace_id=WORKSPACE,
    )
    assert out == []


def test_multiple_seed_dirs_aggregate():
    """A seed in ``fastapi/`` and one in ``fastapi/middleware/`` should
    both contribute their directory to the search — shims from either
    may be surfaced."""
    seed_a = _seed("u:routing", "/repo/fastapi/routing.py")
    seed_b = _seed("u:asyncexitstack", "/repo/fastapi/middleware/asyncexitstack.py")
    rows = [
        _row("u:ws_shim", "/repo/fastapi/websockets.py", kinds=[]),
        _row("u:mw_init", "/repo/fastapi/middleware/__init__.py", kinds=[]),
    ]
    out = expand_sibling_shims(
        [seed_a, seed_b], lance=_FakeLance(rows), workspace_id=WORKSPACE,
    )
    paths = sorted(c.file_path for c in out)
    assert paths == [
        "/repo/fastapi/middleware/__init__.py",
        "/repo/fastapi/websockets.py",
    ]


def test_exclude_uids_dropped():
    seed = _seed("u:routing", "/repo/fastapi/routing.py")
    rows = [
        _row("u:wsmod", "/repo/fastapi/websockets.py", kinds=[]),
    ]
    out = expand_sibling_shims(
        [seed],
        lance=_FakeLance(rows),
        workspace_id=WORKSPACE,
        exclude_uids=["u:wsmod"],
    )
    assert out == []
