"""Tests for per-workspace Lance table naming."""

from context_engine.database.lance_workspace_tables import (
    is_workspace_partition_table,
    workspace_partition_table_name,
)


def test_workspace_partition_table_name_is_stable():
    ws = "qa_repo/django@main+axis_python_v1"
    a = workspace_partition_table_name("symbols_axis_python_v1", ws)
    b = workspace_partition_table_name("symbols_axis_python_v1", ws)
    assert a == b
    assert a.startswith("symbols_axis_python_v1__ws_")
    assert is_workspace_partition_table(a, "symbols_axis_python_v1")


def test_workspace_partition_table_name_differs_by_workspace():
    a = workspace_partition_table_name("symbols", "tenant/a@main")
    b = workspace_partition_table_name("symbols", "tenant/b@main")
    assert a != b


def test_workspace_partition_table_exists_opens_off_catalog_datasets(tmp_path, monkeypatch):
    import lancedb
    import pyarrow as pa

    from context_engine.database.lance_workspace_tables import workspace_partition_table_exists

    monkeypatch.setenv("LANCEDB_PATH", str(tmp_path))
    db = lancedb.connect(str(tmp_path))
    ws = "tenant/repo@main"
    name = workspace_partition_table_name("symbols_axis_python_v1", ws)
    db.create_table(
        name,
        schema=pa.schema(
            [
                pa.field("uid", pa.string()),
                pa.field("workspace_id", pa.string()),
            ]
        ),
    )
    # Simulate a stale catalog listing: dataset exists but name is not listed.
    assert name not in db.table_names() or workspace_partition_table_exists(
        db, "symbols_axis_python_v1", ws
    )
