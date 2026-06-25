"""File-level structural-neighbour expansion via AFFECTS."""

from __future__ import annotations

from context_engine.axis.structural_neighbours import expand_structural_neighbours
from tests.unit.axis_helpers import (
    AXIS_TEST_WORKSPACE,
    FakeNeo4jDB,
    graph_row,
    make_role_candidate,
)


def test_no_seeds_returns_empty():
    assert (
        expand_structural_neighbours([], db=FakeNeo4jDB(), workspace_id=AXIS_TEST_WORKSPACE) == []
    )


def test_reaches_new_file_via_affects():
    """Concrete case the user named: seed in ``routing.py`` reaches
    ``concurrency.contextmanager_in_threadpool`` via an undirected
    AFFECTS walk; the symbol is from a *different* file, so it gets
    included."""
    seed = make_role_candidate(
        "u:run", file_path="/repo/fastapi/routing.py", role="routing_surface"
    )
    db = FakeNeo4jDB(
        [
            graph_row(
                "u:ctx",
                "contextmanager_in_threadpool",
                "/repo/fastapi/concurrency.py",
                depth=4,
            ),
        ]
    )
    out = expand_structural_neighbours(
        [seed],
        db=db,
        workspace_id=AXIS_TEST_WORKSPACE,
    )
    assert len(out) == 1
    c = out[0]
    assert c.uid == "u:ctx"
    assert c.role == "structural_neighbour"
    assert c.satisfying_kinds == ("affects_bridge",)
    assert c.file_path == "/repo/fastapi/concurrency.py"


def test_skips_symbols_in_seed_files():
    """The point of this pass is *new* files. A neighbour that lives
    in a seed's own file is already represented by the seed."""
    seed = make_role_candidate(
        "u:run", file_path="/repo/fastapi/routing.py", role="routing_surface"
    )
    db = FakeNeo4jDB(
        [
            graph_row("u:also_in_routing", "n", "/repo/fastapi/routing.py", depth=1),
            graph_row("u:elsewhere", "n", "/repo/fastapi/encoders.py", depth=2),
        ]
    )
    out = expand_structural_neighbours(
        [seed],
        db=db,
        workspace_id=AXIS_TEST_WORKSPACE,
    )
    assert [c.uid for c in out] == ["u:elsewhere"]


def test_max_files_caps_distinct_file_count():
    seed = make_role_candidate("u:run", file_path="/repo/a.py", role="routing_surface")
    rows = [graph_row(f"u:n{i}", f"n{i}", f"/repo/file_{i}.py", depth=i + 1) for i in range(8)]
    db = FakeNeo4jDB(rows)
    out = expand_structural_neighbours(
        [seed],
        db=db,
        workspace_id=AXIS_TEST_WORKSPACE,
        max_files=3,
        max_per_file=1,
        max_total=20,
    )
    assert len({c.file_path for c in out}) == 3


def test_max_per_file_caps_symbols_per_file():
    seed = make_role_candidate("u:run", file_path="/repo/a.py", role="routing_surface")
    rows = [graph_row(f"u:n{i}", f"n{i}", "/repo/shared.py", depth=i + 1) for i in range(5)]
    db = FakeNeo4jDB(rows)
    out = expand_structural_neighbours(
        [seed],
        db=db,
        workspace_id=AXIS_TEST_WORKSPACE,
        max_per_file=2,
    )
    assert len(out) == 2


def test_max_total_caps_pool_size():
    seed = make_role_candidate("u:run", file_path="/repo/a.py", role="routing_surface")
    rows = [graph_row(f"u:n{i}", f"n{i}", f"/repo/file_{i}.py", depth=1) for i in range(20)]
    db = FakeNeo4jDB(rows)
    out = expand_structural_neighbours(
        [seed],
        db=db,
        workspace_id=AXIS_TEST_WORKSPACE,
        max_files=50,
        max_per_file=1,
        max_total=5,
    )
    assert len(out) == 5


def test_exclude_uids_dropped():
    seed = make_role_candidate("u:run", file_path="/repo/a.py", role="routing_surface")
    db = FakeNeo4jDB(
        [
            graph_row("u:dup", "n", "/repo/b.py", depth=1),
            graph_row("u:other", "n2", "/repo/c.py", depth=1),
        ]
    )
    out = expand_structural_neighbours(
        [seed],
        db=db,
        workspace_id=AXIS_TEST_WORKSPACE,
        exclude_uids=["u:dup"],
    )
    assert [c.uid for c in out] == ["u:other"]
