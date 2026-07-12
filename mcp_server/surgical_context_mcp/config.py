"""Workspace / index resolution for the MCP server.

MVP: a single workspace, taken from ``SURGICAL_CONTEXT_WORKSPACE`` (the
client-facing base id, e.g. ``local/surgical_context@main``) and mapped to the
physical index namespace by the axis_python_v1 profile — exactly how
``QA/axis_benchmark`` composes ``{tenant}/{repo}@{ref}`` → ``workspace_id``.

Multi-repo resolution (cwd → workspace, index-freshness check) is the next
step; see mcp_server/README.md.
"""

from __future__ import annotations

import os

from context_engine.index_profile import AXIS_PYTHON_V1_PROFILE, resolve_index_profile

# Client-facing base workspace id. Environment-specific — in this dev/dogfood
# box the repos are indexed under the ``qa_repo`` tenant (benchmark checkouts);
# a production index may use ``local`` or another tenant. Override via the env.
DEFAULT_BASE_WORKSPACE = os.getenv("SURGICAL_CONTEXT_WORKSPACE", "qa_repo/surgical_context@main")
DEFAULT_TOKEN_BUDGET = 4000
# structuredContent density for chat hosts (Claude Code forwards structured JSON
# and drops TextContent). ``lean`` = markdown + envelope, no fat symbol rows;
# ``full`` = include symbols/blast for programmatic agents / ContextBench.
DETAIL_ENV = "SURGICAL_CONTEXT_MCP_DETAIL"
DEFAULT_DETAIL = "lean"


def resolve_workspace_id(base: str | None = None) -> str:
    """Map a client-facing base workspace id to the axis_python_v1 namespace."""
    base_ws = (base or DEFAULT_BASE_WORKSPACE).strip()
    profile = resolve_index_profile(AXIS_PYTHON_V1_PROFILE)
    return profile.workspace_id(base_ws)


def resolve_detail(explicit: str | None = None) -> str:
    """Resolve structuredContent density: tool arg > env > lean default."""
    if explicit in ("lean", "full"):
        return explicit
    env = os.getenv(DETAIL_ENV, DEFAULT_DETAIL).strip().lower()
    if env in ("lean", "full"):
        return env
    return DEFAULT_DETAIL
