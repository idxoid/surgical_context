"""Lance client selection for axis retrieval."""

from __future__ import annotations

from context_engine.ask.context_builder import AskContextBuilder
from context_engine.database.lancedb_client import LanceDBClient
from context_engine.index_profile import AXIS_PYTHON_V1_PROFILE, LEGACY_INDEX_PROFILE
from context_engine.overlay import InMemoryOverlay


def test_lance_for_index_workspace_reuses_axis_client_for_axis_namespace():
    legacy = LanceDBClient(index_profile=LEGACY_INDEX_PROFILE)
    builder = AskContextBuilder(overlay=InMemoryOverlay(), vector_db=legacy)

    lance = builder.lance_for_index_workspace("qa_repo/surgical_context@main+axis_python_v1")

    assert lance.index_profile_name == AXIS_PYTHON_V1_PROFILE
    assert lance is builder.lance_for_index_workspace("qa_repo/surgical_context@main+axis_python_v1")


def test_lance_for_index_workspace_uses_process_client_when_profiles_match():
    axis = LanceDBClient(index_profile=AXIS_PYTHON_V1_PROFILE)
    builder = AskContextBuilder(overlay=InMemoryOverlay(), vector_db=axis)

    lance = builder.lance_for_index_workspace("local/surgical_context@main+axis_python_v1")

    assert lance is axis
