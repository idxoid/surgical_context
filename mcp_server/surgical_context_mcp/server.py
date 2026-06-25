"""MCP stdio server exposing surgical_context retrieval as the ``ask_code`` tool.

Wire into Claude Code via ``.mcp.json`` (see mcp_server/claude_mcp.example.json).
The tool returns code context — not an answer — for the host chat model to
reason over, the way ``/graphify query`` feeds a budgeted context block.
"""

from __future__ import annotations

import sys

from mcp.server.fastmcp import FastMCP

from surgical_context_mcp.config import resolve_workspace_id
from surgical_context_mcp.engine import AxisEngine

mcp = FastMCP("surgical-context")
_engine = AxisEngine()


@mcp.tool()
def ask_code(
    question: str,
    token_budget: int = 6000,
    workspace: str | None = None,
    roles: list[str] | None = None,
    render: str = "full",
) -> str:
    """Retrieve surgical code context for a natural-language question about the
    indexed codebase.

    Returns ranked, graph-expanded code bundles (the same role-intent retrieval
    the surgical_context ``/ask`` pipeline uses) for you to reason over — it does
    NOT itself produce an answer. Use it whenever you need to know how something
    works in this repo's code: which symbols implement a behaviour, what calls
    what, where a mechanism lives.

    Args:
        question: Plain-language question, e.g. "how does workspace scoping work".
        token_budget: Soft cap on the volume of code returned (default 6000).
        workspace: Optional base workspace id (e.g. "qa_repo/django@main") to
            target a specific indexed repo. Call ``list_workspaces`` to discover
            options. Defaults to SURGICAL_CONTEXT_WORKSPACE.
        roles: Optional explicit roles to drive retrieval, bypassing the
            embedding intent-classifier — supply when you can target the
            mechanism better than cosine similarity of role descriptions (the
            vector seeds still rerank, only role *selection* changes). Call
            ``list_roles`` for the vocabulary. Omit to auto-classify.
        render: "full" (default) = ranked code bundles to reason over. "names" =
            census view — one line per symbol (file :: name + role/depth, NO
            code) with no budget eviction, so far more coupling symbols/files
            surface per token. Use "names" to map structure/blast surface ("which
            symbols/files touch X"); "full" to read the actual code.
    """
    workspace_id = resolve_workspace_id(workspace)

    if render not in ("full", "names"):
        return f"Unknown render '{render}'. Use 'full' (code) or 'names' (census)."

    if roles:
        known = _engine.available_roles()
        invalid = [r for r in roles if r not in known]
        if invalid:
            return (
                f"Unknown role(s): {', '.join(invalid)}.\n"
                f"Valid roles: {', '.join(sorted(known))}.\n"
                "Call list_roles for descriptions."
            )

    result = _engine.ask(
        question, workspace_id, token_budget=token_budget, roles=roles, render=render
    )

    roles = ", ".join(f"{r}({s:.2f})" for r, s in result.intent)
    if not result.text:
        return (
            f"No code context found for: {question!r}\n"
            f"workspace: {workspace_id}\n"
            f"intent roles: {roles or '(none above threshold)'}\n\n"
            "The repo may not be indexed under the axis_python_v1 profile, the "
            "Neo4j/LanceDB backends may be down, or the question matched no role. "
            "Verify the index exists for this workspace."
        )

    header = (
        f"# Surgical context for: {question}\n"
        f"workspace: {workspace_id} · intent: {roles} · "
        f"{result.candidate_count} candidates · {len(result.files)} files\n"
    )
    return header + "\n" + result.text


@mcp.tool()
def impact(
    symbol: str,
    file_path: str | None = None,
    max_depth: int = 3,
    workspace: str | None = None,
) -> str:
    """Downstream blast radius of a change to ``symbol``: which symbols and files
    depend on it (reverse callers, structural API/inheritance, then AFFECTS
    closure). Committed index surface only. Use before changing or removing a
    symbol, or to scope a refactor.

    Args:
        symbol: Symbol name (function/class/method), e.g. "run_axis_retrieval".
        file_path: Optional file to disambiguate when the name is non-unique.
        max_depth: Traversal depth, 1-4 (default 3).
        workspace: Optional base workspace id; defaults to SURGICAL_CONTEXT_WORKSPACE.
    """
    workspace_id = resolve_workspace_id(workspace)
    r = _engine.impact(symbol, workspace_id, file_path=file_path, max_depth=max_depth)

    if not r.found:
        hint = f" in {file_path}" if file_path else ""
        return (
            f"Symbol '{symbol}'{hint} not found in the committed index "
            f"(workspace: {workspace_id}). It may be unindexed, or the name/path is off."
        )

    lines = [
        f"# Impact of `{symbol}` ({r.file_path})",
        f"workspace: {workspace_id} · uid: {r.symbol_uid} · depth={r.max_depth} · "
        f"{len(r.affected_symbols)} affected symbols across {len(r.affected_files)} files",
        "",
    ]
    if not r.affected_symbols:
        lines.append("No downstream dependents found (leaf symbol).")
        return "\n".join(lines) + "\n"

    lines.append("## Affected files")
    lines.extend(f"- {f}" for f in r.affected_files)
    lines.append("")
    lines.append("## Affected symbols")
    for row in r.affected_symbols:
        name = row.get("name") or row.get("symbol") or "?"
        fp = row.get("file_path") or ""
        depth = row.get("depth")
        kind = row.get("kind") or ""
        sev = row.get("severity") or ""
        lines.append(f"- {name} — {fp} (depth={depth}, {kind}, sev={sev})")
    return "\n".join(lines) + "\n"


@mcp.tool()
def list_workspaces() -> str:
    """List the indexed workspaces (repos) you can query with ``ask_code`` /
    ``impact``. Returns base workspace ids to pass as the ``workspace=`` argument.
    """
    rows = _engine.list_workspaces()
    if not rows:
        return "No indexed workspaces found. Is the index built and Neo4j up?"
    lines = ["# Indexed workspaces — pass `base` as workspace=", ""]
    lines.extend(f"- {w.base}  ({w.files} files)" for w in rows)
    return "\n".join(lines) + "\n"


@mcp.tool()
def classify_intent(question: str, top_roles: int = 5) -> str:
    """Preview which structural roles the embedding intent-classifier maps a
    question to (cosine of question vs role descriptions), WITHOUT running
    retrieval. Cheap (embedding only — no graph). Use it to decide whether to
    override ``ask_code(roles=[...])``: see the auto-picked roles + similarity,
    then refine. Closes the loop list_roles → classify_intent → ask_code.
    """
    matches = _engine.classify_intent(question, top_roles=top_roles)
    if not matches:
        return (
            f"No role above threshold for: {question!r}.\n"
            "Try a more specific question, or call list_roles for the vocabulary."
        )
    lines = [f"# Intent for: {question}", ""]
    lines.extend(f"- {role} ({sim:.2f}) — {desc}" for role, sim, desc in matches)
    return "\n".join(lines) + "\n"


@mcp.tool()
def list_roles() -> str:
    """List the structural roles you can pass to ``ask_code(roles=[...])`` to
    bypass the embedding intent-classifier and target retrieval yourself.
    """
    roles = _engine.available_roles()
    if not roles:
        return "No roles available."
    lines = ["# Axis roles — pass to ask_code(roles=[...])", ""]
    lines.extend(f"- **{role}** — {desc}" for role, desc in sorted(roles.items()))
    return "\n".join(lines) + "\n"


def main() -> None:
    # Startup banner goes to stderr — stdout is the MCP JSON-RPC channel.
    print(
        f"[surgical-context-mcp] workspace={resolve_workspace_id()}",
        file=sys.stderr,
        flush=True,
    )
    mcp.run()


if __name__ == "__main__":
    main()
