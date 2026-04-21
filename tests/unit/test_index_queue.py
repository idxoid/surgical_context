"""Unit tests for bounded coalescing indexing queue."""

from sidecar.indexer.queue import IndexBatchQueue


def test_queue_coalesces_duplicate_file_saves():
    batches = []
    queue = IndexBatchQueue(batches.append, debounce_ms=0, auto_start=False)

    first = queue.enqueue_file("/repo/app.py", workspace_id="repo@main")
    second = queue.enqueue_file("/repo/app.py", workspace_id="repo@main")

    assert first.status == "queued"
    assert second.status == "coalesced"
    assert second.generation == 2
    assert queue.snapshot()["pending"] == 1


def test_queue_rejects_when_full():
    batches = []
    queue = IndexBatchQueue(batches.append, max_pending=1, debounce_ms=0, auto_start=False)

    accepted = queue.enqueue_file("/repo/a.py", workspace_id="repo@main")
    rejected = queue.enqueue_file("/repo/b.py", workspace_id="repo@main")

    assert accepted.accepted is True
    assert rejected.accepted is False
    assert rejected.status == "rejected"
    assert rejected.reason == "queue_full"
    assert queue.snapshot()["pending"] == 1


def test_process_ready_once_batches_ready_items():
    batches = []
    queue = IndexBatchQueue(batches.append, debounce_ms=0, batch_size=10, auto_start=False)

    queue.enqueue_file("/repo/a.py", workspace_id="repo@main")
    queue.enqueue_file("/repo/b.py", workspace_id="repo@main")

    processed = queue.process_ready_once()

    assert processed == 2
    assert [[item.file_path for item in batch] for batch in batches] == [
        ["/repo/a.py", "/repo/b.py"]
    ]
    assert queue.snapshot()["pending"] == 0
    assert queue.snapshot()["processed"] == 2


def test_process_ready_once_respects_batch_size():
    batches = []
    queue = IndexBatchQueue(batches.append, debounce_ms=0, batch_size=1, auto_start=False)

    queue.enqueue_file("/repo/a.py", workspace_id="repo@main")
    queue.enqueue_file("/repo/b.py", workspace_id="repo@main")

    assert queue.process_ready_once() == 1
    assert queue.process_ready_once() == 1
    assert [[item.file_path for item in batch] for batch in batches] == [
        ["/repo/a.py"],
        ["/repo/b.py"],
    ]
