from unittest.mock import MagicMock

from sidecar.indexer.affects import AFFECTSIndexer


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
        {"dependency_uid": "a", "dependent_uid": "x"},
        {"dependency_uid": "a", "dependent_uid": "y"},
        {"dependency_uid": "b", "dependent_uid": "z"},
    ]
    indexer = AFFECTSIndexer(MagicMock())

    adjacency = indexer._load_reverse_adjacency(
        session,
        workspace_id="acme/repo@main",
    )

    assert adjacency == {"a": ["x", "y"], "b": ["z"]}
    params = session.run.call_args.kwargs
    assert params["workspace_id"] == "acme/repo@main"


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
