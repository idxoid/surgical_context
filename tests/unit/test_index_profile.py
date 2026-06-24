import pytest

from context_engine.index_profile import (
    AXIS_PYTHON_V1_PROFILE,
    INDEX_PROFILE_ENV,
    LEGACY_INDEX_PROFILE,
    active_index_profile,
    base_workspace_id,
    effective_index_workspace_id,
    index_workspace_lookup_order,
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
    assert profile.schema_version == 5


def test_active_profile_reads_environment(monkeypatch):
    monkeypatch.setenv(INDEX_PROFILE_ENV, "axis-python-v1")

    assert active_index_profile().name == AXIS_PYTHON_V1_PROFILE


def test_unknown_profile_is_rejected():
    with pytest.raises(ValueError, match="Unknown index profile"):
        resolve_index_profile("missing")


def test_effective_index_workspace_id_applies_active_profile(monkeypatch):
    monkeypatch.setenv(INDEX_PROFILE_ENV, AXIS_PYTHON_V1_PROFILE)
    assert effective_index_workspace_id("local/repo@main") == "local/repo@main+axis_python_v1"


def test_effective_index_workspace_id_is_idempotent_for_suffixed_input(monkeypatch):
    monkeypatch.setenv(INDEX_PROFILE_ENV, AXIS_PYTHON_V1_PROFILE)
    suffixed = "local/repo@main+axis_python_v1"
    assert effective_index_workspace_id(suffixed) == suffixed


def test_base_workspace_id_strips_profile_suffix():
    assert base_workspace_id("local/repo@main+axis_python_v1") == "local/repo@main"
    assert base_workspace_id("local/repo@main") == "local/repo@main"


def test_index_workspace_lookup_order_tries_axis_when_legacy_active(monkeypatch):
    monkeypatch.delenv(INDEX_PROFILE_ENV, raising=False)

    order = index_workspace_lookup_order("local/repo@main")

    assert order == [
        "local/repo@main",
        "local/repo@main+axis_python_v1",
    ]


def test_index_workspace_lookup_order_dedupes_when_axis_active(monkeypatch):
    monkeypatch.setenv(INDEX_PROFILE_ENV, AXIS_PYTHON_V1_PROFILE)

    order = index_workspace_lookup_order("local/repo@main")

    assert order[0] == "local/repo@main+axis_python_v1"
    assert "local/repo@main" in order
