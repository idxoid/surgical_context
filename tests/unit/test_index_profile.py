import pytest

from sidecar.index_profile import (
    AXIS_PYTHON_V1_PROFILE,
    INDEX_PROFILE_ENV,
    LEGACY_INDEX_PROFILE,
    active_index_profile,
    resolve_index_profile,
)


def test_legacy_profile_keeps_default_storage():
    profile = resolve_index_profile(LEGACY_INDEX_PROFILE)

    assert profile.workspace_id("local/repo@main") == "local/repo@main"
    assert profile.docs_table == "docs"
    assert profile.symbols_table == "symbols"
    assert profile.language_scope == "all"


def test_axis_python_profile_isolates_workspace_and_lancedb_tables():
    profile = resolve_index_profile(AXIS_PYTHON_V1_PROFILE)

    assert profile.workspace_id("local/repo@main") == "local/repo@main+axis_python_v1"
    assert profile.workspace_id("local/repo@main+axis_python_v1") == (
        "local/repo@main+axis_python_v1"
    )
    assert profile.docs_table == "docs_axis_python_v1"
    assert profile.symbols_table == "symbols_axis_python_v1"
    assert profile.language_scope == "python"
    assert profile.schema_version == 2


def test_active_profile_reads_environment(monkeypatch):
    monkeypatch.setenv(INDEX_PROFILE_ENV, "axis-python-v1")

    assert active_index_profile().name == AXIS_PYTHON_V1_PROFILE


def test_unknown_profile_is_rejected():
    with pytest.raises(ValueError, match="Unknown index profile"):
        resolve_index_profile("missing")
