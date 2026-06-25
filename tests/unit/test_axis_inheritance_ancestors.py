"""Upward inheritance walk — surface abstract bases via DEPENDS_ON."""

from __future__ import annotations

from context_engine.axis.inheritance_ancestors import expand_inheritance_ancestors
from tests.unit.axis_helpers import (
    AXIS_TEST_WORKSPACE,
    FakeNeo4jDB,
    graph_row,
    make_role_candidate,
)


def test_no_seeds_returns_empty():
    assert (
        expand_inheritance_ancestors([], db=FakeNeo4jDB(), workspace_id=AXIS_TEST_WORKSPACE) == []
    )


def test_surfaces_ancestor_in_different_file():
    """The canonical case: ``concurrency/prefork.py:TaskPool`` walks
    ``DEPENDS_ON`` to ``concurrency/base.py:BasePool``; the ancestor
    lives in a different file, so the pass surfaces it."""
    seed = make_role_candidate("u:taskpool", file_path="/repo/celery/concurrency/prefork.py")
    db = FakeNeo4jDB(
        [
            graph_row(
                "u:basepool",
                "BasePool",
                "/repo/celery/concurrency/base.py",
                depth=1,
            )
        ]
    )
    out = expand_inheritance_ancestors(
        [seed],
        db=db,
        workspace_id=AXIS_TEST_WORKSPACE,
    )
    assert len(out) == 1
    c = out[0]
    assert c.uid == "u:basepool"
    assert c.file_path == "/repo/celery/concurrency/base.py"
    assert c.role == "structural_neighbour"
    assert c.satisfying_kinds == ("inheritance_ancestor",)


def test_skips_ancestor_in_seed_file():
    """An ancestor in the seed's own file is already represented by
    the seed — no need to re-add it under a different role tag."""
    seed = make_role_candidate("u:concrete", file_path="/repo/celery/concurrency/prefork.py")
    db = FakeNeo4jDB(
        [
            graph_row(
                "u:also_in_prefork",
                "Helper",
                "/repo/celery/concurrency/prefork.py",
                depth=1,
            )
        ]
    )
    out = expand_inheritance_ancestors(
        [seed],
        db=db,
        workspace_id=AXIS_TEST_WORKSPACE,
    )
    assert out == []


def test_max_files_caps_distinct_file_count():
    seed = make_role_candidate("u:concrete", file_path="/repo/celery/concurrency/prefork.py")
    rows = [
        graph_row(f"u:base{i}", f"Base{i}", f"/repo/celery/base_{i}.py", depth=1) for i in range(8)
    ]
    db = FakeNeo4jDB(rows)
    out = expand_inheritance_ancestors(
        [seed],
        db=db,
        workspace_id=AXIS_TEST_WORKSPACE,
        max_files=3,
        max_total=10,
    )
    assert len({c.file_path for c in out}) == 3


def test_max_total_caps_pool_size():
    seed = make_role_candidate("u:concrete", file_path="/repo/celery/concurrency/prefork.py")
    rows = [
        graph_row(f"u:base{i}", f"Base{i}", f"/repo/celery/base_{i}.py", depth=1) for i in range(20)
    ]
    db = FakeNeo4jDB(rows)
    out = expand_inheritance_ancestors(
        [seed],
        db=db,
        workspace_id=AXIS_TEST_WORKSPACE,
        max_files=50,
        max_total=4,
    )
    assert len(out) == 4


def test_exclude_uids_dropped():
    seed = make_role_candidate("u:concrete", file_path="/repo/celery/concurrency/prefork.py")
    db = FakeNeo4jDB(
        [
            graph_row("u:dup", "Dup", "/repo/celery/concurrency/base.py", depth=1),
            graph_row("u:other", "Other", "/repo/celery/other/base.py", depth=2),
        ]
    )
    out = expand_inheritance_ancestors(
        [seed],
        db=db,
        workspace_id=AXIS_TEST_WORKSPACE,
        exclude_uids=["u:dup"],
    )
    assert [c.uid for c in out] == ["u:other"]


# Cypher-injection defence now lives in the shared graph_walk core —
# see tests/unit/test_axis_graph_walk.py::test_safe_rel_pattern_rejects_injection.
