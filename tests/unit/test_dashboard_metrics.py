from unittest.mock import MagicMock

from context_engine.database import lancedb_client
from context_engine.database.lancedb_client import LanceDBClient
from context_engine.database.neo4j_client import Neo4jClient


def test_workspace_dashboard_counts_are_scoped_and_aggregated():
    session = MagicMock()
    session.run.return_value.single.return_value = {
        "files": 12,
        "symbols": 47,
        "symbols_with_docs": 9,
    }
    driver = MagicMock()
    driver.session.return_value.__enter__.return_value = session
    client = Neo4jClient.__new__(Neo4jClient)
    client.driver = driver

    counts = client.get_workspace_dashboard_counts("workspace-1")

    assert counts == {"files": 12, "symbols": 47, "symbols_with_docs": 9}
    assert session.run.call_args.kwargs == {"workspace_id": "workspace-1"}


def test_lancedb_storage_size_counts_nested_files(monkeypatch, tmp_path):
    (tmp_path / "table").mkdir()
    (tmp_path / "table" / "data.lance").write_bytes(b"12345")
    (tmp_path / "manifest").write_bytes(b"123")
    monkeypatch.setattr(lancedb_client, "DB_PATH", str(tmp_path))

    assert LanceDBClient.storage_size_bytes() == 8
