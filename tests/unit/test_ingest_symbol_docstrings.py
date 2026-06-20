from unittest.mock import MagicMock

from context_engine.indexer.anchor import ingest_symbol_docstrings
from context_engine.parser.protocol import SymbolMetadata


def test_ingest_symbol_docstrings_writes_lance_and_covers():
    neo4j = MagicMock()
    session = MagicMock()
    neo4j.driver.session.return_value.__enter__.return_value = session
    session.run.return_value.single.return_value = {"uid": "uid-module"}

    lance = MagicMock()
    lance.upsert_symbol_docstring_rows.return_value = 1

    sym = SymbolMetadata(
        uid="uid-module",
        name="Module",
        kind="class",
        start_line=2,
        end_line=4,
        content_hash="abc",
        file_path="/repo/src/module.ts",
        docstring="Nest module metadata.",
    )

    stats = ingest_symbol_docstrings(
        neo4j,
        lance,
        [sym],
        workspace_id="acme/repo@main",
        allowed_prefixes=["/repo"],
    )

    assert stats == {"anchors": 1, "covers": 1, "rows": 1, "skipped_noise": 0}
    lance.upsert_symbol_docstring_rows.assert_called_once()
    rows = lance.upsert_symbol_docstring_rows.call_args.args[0]
    assert rows[0]["owner_uid"] == "uid-module"
    assert rows[0]["chunk"] == "Nest module metadata."
    assert session.execute_write.call_count == 2


def test_ingest_symbol_docstrings_skips_non_core_file_tiers():
    neo4j = MagicMock()
    session = MagicMock()
    neo4j.driver.session.return_value.__enter__.return_value = session
    session.run.return_value.single.return_value = {"uid": "uid-test"}

    lance = MagicMock()

    sym = SymbolMetadata(
        uid="uid-test",
        name="test_foo",
        kind="function",
        start_line=2,
        end_line=4,
        content_hash="abc",
        file_path="tests/test_models.py",
        docstring="Test helper docstring.",
    )

    stats = ingest_symbol_docstrings(
        neo4j,
        lance,
        [sym],
        workspace_id="acme/repo@main",
        allowed_prefixes=["/repo"],
        file_tier_by_path={"tests/test_models.py": "test"},
    )

    assert stats == {"anchors": 0, "covers": 0, "rows": 0, "skipped_noise": 1}
    lance.upsert_symbol_docstring_rows.assert_not_called()
