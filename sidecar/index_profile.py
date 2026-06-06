"""Index profile selection for isolating incompatible indexed data.

Feature flags can switch query/ranker code, but they cannot make already
materialized graph/vector rows compatible. An index profile names the physical
storage contract for one indexing approach.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

LEGACY_INDEX_PROFILE = "legacy"
AXIS_PYTHON_V1_PROFILE = "axis_python_v1"
INDEX_PROFILE_ENV = "INDEX_PROFILE"


@dataclass(frozen=True)
class IndexProfile:
    """Physical storage namespace for one index contract."""

    name: str
    schema_version: int
    docs_table: str
    symbols_table: str
    workspace_suffix: str = ""
    language_scope: str = "all"

    def workspace_id(self, base_workspace_id: str) -> str:
        """Return the workspace namespace for this profile."""
        base = base_workspace_id.strip()
        if not self.workspace_suffix or base.endswith(self.workspace_suffix):
            return base
        return f"{base}{self.workspace_suffix}"

    def manifest_fields(self) -> dict[str, str | int]:
        return {
            "index_profile": self.name,
            "index_profile_schema_version": self.schema_version,
            "index_profile_language_scope": self.language_scope,
            "lancedb_docs_table": self.docs_table,
            "lancedb_symbols_table": self.symbols_table,
        }


_PROFILES: dict[str, IndexProfile] = {
    LEGACY_INDEX_PROFILE: IndexProfile(
        name=LEGACY_INDEX_PROFILE,
        schema_version=1,
        docs_table="docs",
        symbols_table="symbols",
        language_scope="all",
    ),
    AXIS_PYTHON_V1_PROFILE: IndexProfile(
        name=AXIS_PYTHON_V1_PROFILE,
        schema_version=3,
        docs_table="docs_axis_python_v1",
        symbols_table="symbols_axis_python_v1",
        workspace_suffix="+axis_python_v1",
        language_scope="python",
    ),
}


def normalize_index_profile_name(value: str | None) -> str:
    raw = (value or LEGACY_INDEX_PROFILE).strip().lower().replace("-", "_")
    return raw or LEGACY_INDEX_PROFILE


def resolve_index_profile(value: str | None = None) -> IndexProfile:
    name = normalize_index_profile_name(value)
    try:
        return _PROFILES[name]
    except KeyError as exc:
        known = ", ".join(sorted(_PROFILES))
        raise ValueError(f"Unknown index profile '{name}'. Known profiles: {known}") from exc


def active_index_profile() -> IndexProfile:
    return resolve_index_profile(os.getenv(INDEX_PROFILE_ENV))
