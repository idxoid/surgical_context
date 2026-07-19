from unittest.mock import MagicMock

from context_engine.indexer.affects import AFFECTSIndexer


def test_rebuild_affects_batches_delete_compute_and_merge():
    db = MagicMock()
    session = db.driver.session.return_value.__enter__.return_value
    indexer = AFFECTSIndexer(db)

    progress: list[int] = []

    indexer._load_reverse_adjacency = MagicMock(return_value={"x": ["y"]})
    indexer._compute_affected_pairs = MagicMock(
        side_effect=[
            [{"source_uid": "a", "target_uid": "x"}],
            [{"source_uid": "c", "target_uid": "y"}],
        ]
    )
    indexer.REBUILD_BATCH_SIZE = 2

    indexer.rebuild_affects(
        ["a", "b", "c"],
        workspace_id="acme/repo@main",
        progress_callback=progress.append,
    )

    assert session.run.call_count == 4
    delete_query = session.run.call_args_list[0].args[0]
    delete_params = session.run.call_args_list[0].kwargs
    assert "UNWIND $uids AS uid" in delete_query
    assert delete_params["uids"] == ["a", "b", "c"]

    merge_query = session.run.call_args_list[1].args[0]
    merge_params = session.run.call_args_list[1].kwargs
    assert "UNWIND $pairs AS pair" in merge_query
    assert merge_params["pairs"] == [{"source_uid": "a", "target_uid": "x"}]

    second_merge_params = session.run.call_args_list[2].kwargs
    assert second_merge_params["pairs"] == [{"source_uid": "c", "target_uid": "y"}]

    version_bump_query = session.run.call_args_list[-1].args[0]
    assert "graph_version" in version_bump_query
    assert progress == [2, 1]


def test_load_reverse_adjacency_groups_edges_by_dependency():
    session = MagicMock()
    session.run.return_value = [
        {"dependency_uid": "a", "dependent_uid": "x", "dependent_qn": "pkg.x"},
        {"dependency_uid": "a", "dependent_uid": "y", "dependent_qn": "pkg.y"},
        {"dependency_uid": "b", "dependent_uid": "z", "dependent_qn": "pkg.z"},
    ]
    indexer = AFFECTSIndexer(MagicMock())

    adjacency = indexer._load_reverse_adjacency(
        session,
        workspace_id="acme/repo@main",
    )

    assert adjacency == {"a": ["x", "y"], "b": ["z"]}
    params = session.run.call_args.kwargs
    assert params["workspace_id"] == "acme/repo@main"


def test_load_reverse_adjacency_orders_by_qualified_name_not_uid():
    """The fanout cap consumes this order — it must not depend on uids,
    which mix workspace_id and reshuffle on every fresh ref."""
    session = MagicMock()
    session.run.return_value = [
        {"dependency_uid": "dep", "dependent_uid": "uid_9", "dependent_qn": "pkg.alpha"},
        {"dependency_uid": "dep", "dependent_uid": "uid_1", "dependent_qn": "pkg.zeta"},
        {"dependency_uid": "dep", "dependent_uid": "uid_5", "dependent_qn": "pkg.mid"},
    ]
    indexer = AFFECTSIndexer(MagicMock())

    adjacency = indexer._load_reverse_adjacency(session, workspace_id="acme/repo@main")

    assert adjacency == {"dep": ["uid_9", "uid_5", "uid_1"]}


def test_affected_set_is_invariant_under_uid_renaming():
    """Two workspaces with identical content but different uid schemes must
    select the same dependents through the fanout cap."""

    def build_rows(uid_of):
        graph = {
            "root": ["pkg.a", "pkg.b", "pkg.c"],
            "pkg.a": ["pkg.d"],
            "pkg.b": ["pkg.e"],
            "pkg.c": ["pkg.f"],
        }
        rows = []
        for dep_qn, dependents in graph.items():
            for qn in dependents:
                rows.append(
                    {
                        "dependency_uid": uid_of(dep_qn),
                        "dependent_uid": uid_of(qn),
                        "dependent_qn": qn,
                    }
                )
        return rows

    def affected_qns(uid_of):
        session = MagicMock()
        session.run.return_value = build_rows(uid_of)
        indexer = AFFECTSIndexer(MagicMock())
        indexer.MAX_AFFECTS_DEPTH = 2
        indexer.MAX_FANOUT_PER_LEVEL = 2
        adjacency = indexer._load_reverse_adjacency(session, workspace_id="ws")
        pairs = indexer._compute_affected_pairs(adjacency, [uid_of("root")])
        qn_of = {
            uid_of(qn): qn for qn in ("root", "pkg.a", "pkg.b", "pkg.c", "pkg.d", "pkg.e", "pkg.f")
        }
        return {qn_of[p["target_uid"]] for p in pairs}

    ws1 = affected_qns(lambda qn: f"ws1_{hash(qn) % 97:02d}_{qn}")
    ws2 = affected_qns(lambda qn: f"ws2_{hash(qn[::-1]) % 89:02d}_{qn}")
    assert ws1 == ws2
    assert ws1 == {"pkg.a", "pkg.b", "pkg.d", "pkg.e"}


def test_compute_affected_pairs_uses_depth_and_fanout_limits():
    indexer = AFFECTSIndexer(MagicMock())
    indexer.MAX_AFFECTS_DEPTH = 2
    indexer.MAX_FANOUT_PER_LEVEL = 2

    pairs = indexer._compute_affected_pairs(
        {
            "a": ["b", "c", "d"],
            "b": ["e"],
            "c": ["f"],
            "d": ["g"],
        },
        ["a"],
    )

    assert pairs == [
        {"source_uid": "a", "target_uid": "b"},
        {"source_uid": "a", "target_uid": "c"},
        {"source_uid": "a", "target_uid": "e"},
        {"source_uid": "a", "target_uid": "f"},
    ]
