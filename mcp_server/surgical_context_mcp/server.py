"""MCP stdio server exposing surgical_context retrieval as the ``ask_code`` tool.

Wire into Claude Code via ``.mcp.json`` (see mcp_server/claude_mcp.example.json).
The tool returns code context — not an answer — for the host chat model to
reason over, the way ``/graphify query`` feeds a budgeted context block.
"""

from __future__ import annotations

import hashlib
import re
import sys

from mcp.server.fastmcp import FastMCP

from surgical_context_mcp.config import resolve_workspace_id
from surgical_context_mcp.engine import AxisEngine

mcp = FastMCP("surgical-context")
_engine = AxisEngine()


@mcp.tool()
def ask_code(
    question: str,
    token_budget: int = 4000,
    workspace: str | None = None,
    roles: list[str] | None = None,
    render: str = "full",
) -> str:
    """Semantic, cross-file code retrieval: a natural-language question → ranked,
    graph-expanded code bundles to reason over. Returns *context, not an answer*.

    COST — this is the HEAVY tool (it returns many symbols' code). Prefer the
    cheap, targeted tools when you can name what you want:
      - file_outline(path)            — a file's symbol map
      - read_symbol(name)             — one symbol's code
      - callers(name) / callees(name) — direct call edges
      - find_definition / search_code — locate a symbol by name or text
      - impact(symbol)                — downstream blast radius (structural, cheap)
      - classify_intent(question)     — preview roles only (no retrieval)
    Reach for ask_code only when you need semantic retrieval ACROSS files and
    cannot name the targets (e.g. "how does X work", "where is Y handled").

    ROUND-TRIPS: each tool call re-bills the whole conversation context (cache),
    so at large context FEWER rich calls beat MANY granular ones. If answering
    would otherwise take many targeted lookups, one ask_code is cheaper overall —
    batch, don't drip.

    Args:
        question: Plain-language question, e.g. "how does workspace scoping work".
        token_budget: Soft cap on the volume of code returned (default 4000).
            Raise it for more depth; lower it (or render="names") to stay cheap.
        workspace: Optional base workspace id (e.g. "qa_repo/django@main"); call
            list_workspaces for options. Defaults to SURGICAL_CONTEXT_WORKSPACE.
        roles: Optional explicit roles, bypassing the embedding intent-classifier
            (vector seeds still rerank). Call list_roles for the vocabulary.
        render: "full" (default) = ranked code bundles. "names" = cheap structural
            census (one line per symbol, no code) — use to map "which symbols /
            files touch X" without paying for bodies.
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

    roles_str = ", ".join(f"{r}({s:.2f})" for r, s in result.intent)
    if not result.text:
        return (
            f"No code context found for: {question!r}\n"
            f"workspace: {workspace_id}\n"
            f"intent roles: {roles_str or '(none above threshold)'}\n\n"
            "The repo may not be indexed under the axis_python_v1 profile, the "
            "Neo4j/LanceDB backends may be down, or the question matched no role. "
            "Verify the index exists for this workspace."
        )

    header = (
        f"# Surgical context for: {question}\n"
        f"workspace: {workspace_id} · intent: {roles_str} · "
        f"{result.candidate_count} candidates · {len(result.files)} files\n"
    )
    body = header + "\n" + result.text
    if render == "full":
        # Footer nudges toward cheaper follow-ups instead of always maxing budget.
        body += (
            f"\n---\n_Shown under token_budget={token_budget}. More depth: raise "
            'token_budget. Cheap structural map: render="names". '
            "Targeted facts: read_symbol / callers / impact._\n"
        )
    return body


@mcp.tool()
def impact(
    symbol: str,
    file_path: str | None = None,
    max_depth: int = 3,
    workspace: str | None = None,
) -> str:
    """Downstream blast radius of a change to ``symbol``: which symbols and files
    depend on it (reverse callers, structural API/inheritance, then AFFECTS
    closure). The committed index is the authoritative surface; any uncommitted
    edits pushed via ``set_overlay`` add degraded ``overlay_caller`` rows (and a
    brand-new symbol typed into an overlay still resolves). Use before changing
    or removing a symbol, or to scope a refactor.

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
            f"Symbol '{symbol}'{hint} not found in the committed index or any "
            f"overlay buffer (workspace: {workspace_id}). It may be unindexed, "
            "or the name/path is off."
        )

    flags = " · degraded (overlay-augmented)" if r.degraded else ""
    overlay_note = f" · {r.overlay_count} from uncommitted edits" if r.overlay_count else ""
    lines = [
        f"# Impact of `{symbol}` ({r.file_path})",
        f"workspace: {workspace_id} · uid: {r.symbol_uid} · depth={r.max_depth} · "
        f"{len(r.affected_symbols)} affected symbols across {len(r.affected_files)} files"
        f"{overlay_note}{flags}",
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
        tag = " [overlay]" if row.get("degraded") else ""
        lines.append(f"- {name} — {fp} (depth={depth}, {kind}, sev={sev}){tag}")
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


@mcp.tool()
def read_symbol(
    name: str,
    file_path: str | None = None,
    workspace: str | None = None,
) -> str:
    """Exact source of a single named symbol (function/class/method) — the full,
    untrimmed body straight from disk. Use this when you need to READ specific
    code precisely rather than the budget-trimmed bundles ``ask_code`` returns:
    e.g. "show me the body of run_axis_retrieval".

    Args:
        name: Symbol name, e.g. "build_impact_surface".
        file_path: Optional file to disambiguate a non-unique name. Call
            ``find_definition`` first if a name resolves to several files.
        workspace: Optional base workspace id; defaults to SURGICAL_CONTEXT_WORKSPACE.
    """
    workspace_id = resolve_workspace_id(workspace)
    r = _engine.read_symbol(name, workspace_id, file_path=file_path)
    if not r.found:
        hint = f" in {file_path}" if file_path else ""
        return (
            f"Symbol '{name}'{hint} not found in the index (workspace: {workspace_id}). "
            f"Try ``find_definition('{name}')`` or check the spelling/path."
        )
    header = (
        f"# {r.name}  ({r.file_path}:{r.start_line}-{r.end_line})\n"
        f"workspace: {workspace_id} · uid: {r.uid}\n"
    )
    if not r.code:
        return (
            header
            + "\n(Source unavailable — the file is outside the indexed workspace root "
            "or could not be read. The span above still locates the definition.)\n"
        )
    return header + "\n```python\n" + r.code.rstrip() + "\n```\n"


@mcp.tool()
def search_code(
    query: str,
    limit: int = 10,
    kind: str = "symbol",
    workspace: str | None = None,
) -> str:
    """Cheap semantic search for symbols or doc chunks — ranked hits with NO
    graph expansion. Use it to LOCATE things fast ("which symbol embeds vector
    seeds", "find the workspace-scoping doc") before a deeper ``ask_code`` or a
    precise ``read_symbol``. Lighter than ``ask_code``: vectors only, no traversal.

    Args:
        query: Free-text search, e.g. "token credit budget packer".
        limit: Max hits (default 10).
        kind: "symbol" (default) = code symbols; "doc" = documentation chunks.
        workspace: Optional base workspace id; defaults to SURGICAL_CONTEXT_WORKSPACE.
    """
    workspace_id = resolve_workspace_id(workspace)
    if kind not in ("symbol", "doc"):
        return f"Unknown kind '{kind}'. Use 'symbol' or 'doc'."
    rows = _engine.search_code(query, workspace_id, limit=limit, kind=kind)
    if not rows:
        return f"No {kind} hits for: {query!r} (workspace: {workspace_id})."
    lines = [f"# {kind} search: {query}", f"workspace: {workspace_id} · {len(rows)} hits", ""]
    if kind == "symbol":
        for r in rows:
            score = r.get("score")
            score_s = f" · score={score:.2f}" if isinstance(score, (int, float)) else ""
            lines.append(f"- {r.get('name', '?')} — {r.get('file_path', '')}{score_s}")
    else:
        for r in rows:
            fp = r.get("file_path") or r.get("id") or ""
            snippet = " ".join(str(r.get("chunk") or "").split())[:160]
            lines.append(f"- {fp}\n  {snippet}")
    return "\n".join(lines) + "\n"


@mcp.tool()
def callers(
    symbol: str,
    file_path: str | None = None,
    max_hops: int = 1,
    limit: int = 50,
    workspace: str | None = None,
) -> str:
    """Who calls ``symbol`` — incoming CALLS edges (reverse walk). Cheaper and
    more precise than ``impact``: a direct "what invokes this function" rather
    than the full AFFECTS blast closure. Use it to trace control flow upward.

    Args:
        symbol: Symbol name, e.g. "run_axis_retrieval".
        file_path: Optional file to disambiguate a non-unique name.
        max_hops: Call-chain depth, 1-4 (default 1 = direct callers).
        limit: Max rows (default 50).
        workspace: Optional base workspace id; defaults to SURGICAL_CONTEXT_WORKSPACE.
    """
    return _format_neighbours("callers", symbol, file_path, max_hops, limit, workspace)


@mcp.tool()
def callees(
    symbol: str,
    file_path: str | None = None,
    max_hops: int = 1,
    limit: int = 50,
    workspace: str | None = None,
) -> str:
    """What ``symbol`` calls — outgoing CALLS edges (forward walk). The
    dependency view: which functions this one invokes. Use it to trace control
    flow downward without the full ``ask_code`` expansion.

    Args:
        symbol: Symbol name, e.g. "run_axis_retrieval".
        file_path: Optional file to disambiguate a non-unique name.
        max_hops: Call-chain depth, 1-4 (default 1 = direct callees).
        limit: Max rows (default 50).
        workspace: Optional base workspace id; defaults to SURGICAL_CONTEXT_WORKSPACE.
    """
    return _format_neighbours("callees", symbol, file_path, max_hops, limit, workspace)


def _format_neighbours(
    which: str,
    symbol: str,
    file_path: str | None,
    max_hops: int,
    limit: int,
    workspace: str | None,
) -> str:
    workspace_id = resolve_workspace_id(workspace)
    direction = "reverse" if which == "callers" else "forward"
    depth = max_hops if isinstance(max_hops, int) and 1 <= max_hops <= 4 else 1
    r = _engine.call_neighbours(
        symbol, workspace_id, direction=direction, file_path=file_path, max_hops=depth, limit=limit
    )
    if not r.found:
        hint = f" in {file_path}" if file_path else ""
        return (
            f"Symbol '{symbol}'{hint} not found in the index (workspace: {workspace_id}). "
            f"Try ``find_definition('{symbol}')``."
        )
    rel = "called by" if which == "callers" else "calls"
    lines = [
        f"# {symbol} — {which} (max_hops={depth})",
        f"workspace: {workspace_id} · uid: {r.symbol_uid} · {len(r.rows)} {which}",
        "",
    ]
    if not r.rows:
        lines.append(f"No {which} found ({'entry point' if which == 'callers' else 'leaf'}).")
        return "\n".join(lines) + "\n"
    for row in r.rows:
        lines.append(f"- {row.name} — {row.file_path} (depth={row.depth}) [{rel}]")
    return "\n".join(lines) + "\n"


@mcp.tool()
def find_definition(name: str, limit: int = 20, workspace: str | None = None) -> str:
    """Locate where a name is defined: every symbol called ``name`` with its
    file and start line (go-to-definition, including collisions/overloads).
    Cheap Neo4j lookup — use it to pick the right ``file_path`` before
    ``read_symbol``/``callers`` when a name is ambiguous.

    Args:
        name: Symbol name to locate.
        limit: Max definitions (default 20).
        workspace: Optional base workspace id; defaults to SURGICAL_CONTEXT_WORKSPACE.
    """
    workspace_id = resolve_workspace_id(workspace)
    hits = _engine.find_definition(name, workspace_id, limit=limit)
    if not hits:
        return f"No definition of '{name}' in the index (workspace: {workspace_id})."
    lines = [f"# Definitions of `{name}`", f"workspace: {workspace_id} · {len(hits)} found", ""]
    for h in hits:
        kind = f" ({h.kind})" if h.kind else ""
        lines.append(f"- {h.file_path}:{h.start_line}{kind}")
    return "\n".join(lines) + "\n"


@mcp.tool()
def file_outline(file_path: str, limit: int = 400, workspace: str | None = None) -> str:
    """Symbol map of one file: every defined symbol with kind and start line,
    top-to-bottom (no code). Use it to understand a file's structure before
    reading specific symbols, or to find what to ``read_symbol`` next.

    Args:
        file_path: File path (absolute, or a suffix like "axis/pipeline.py").
        limit: Max symbols (default 400).
        workspace: Optional base workspace id; defaults to SURGICAL_CONTEXT_WORKSPACE.
    """
    workspace_id = resolve_workspace_id(workspace)
    r = _engine.file_outline(file_path, workspace_id, limit=limit)
    if not r.found:
        return (
            f"No indexed file matching '{file_path}' (workspace: {workspace_id}). "
            "Pass a longer path suffix, or check the file is indexed."
        )
    lines = [f"# Outline: {r.file_path}", f"workspace: {workspace_id} · {len(r.rows)} symbols", ""]
    for row in r.rows:
        kind = f"{row.kind} " if row.kind else ""
        lines.append(f"- L{row.start_line}: {kind}{row.name}")
    return "\n".join(lines) + "\n"


@mcp.tool()
def path(
    symbol_a: str,
    symbol_b: str,
    file_a: str | None = None,
    file_b: str | None = None,
    max_hops: int = 6,
    workspace: str | None = None,
) -> str:
    """How two symbols relate: the shortest connecting path across ALL edge
    types (calls, inheritance, API, type refs, …). Answers "how does A reach
    B" / "what links these two". Use it to discover indirect coupling that
    ``callers``/``callees`` (CALLS-only) miss.

    Args:
        symbol_a: First symbol name.
        symbol_b: Second symbol name.
        file_a: Optional file to disambiguate ``symbol_a``.
        file_b: Optional file to disambiguate ``symbol_b``.
        max_hops: Max path length, 1-10 (default 6).
        workspace: Optional base workspace id; defaults to SURGICAL_CONTEXT_WORKSPACE.
    """
    workspace_id = resolve_workspace_id(workspace)
    r = _engine.path(
        symbol_a, symbol_b, workspace_id, file_a=file_a, file_b=file_b, max_hops=max_hops
    )
    if not r.found:
        return (
            f"No path from '{symbol_a}' to '{symbol_b}' "
            f"(workspace: {workspace_id}): {r.reason}."
        )
    # Interleave node names with the edge type that links each consecutive pair.
    parts = [r.node_names[0]] if r.node_names else []
    for i, rel in enumerate(r.rel_types):
        nxt = r.node_names[i + 1] if i + 1 < len(r.node_names) else "?"
        parts.append(f"--[{rel}]--> {nxt}")
    chain = " ".join(parts)
    return (
        f"# Path: {symbol_a} → {symbol_b}\n"
        f"workspace: {workspace_id} · {len(r.rel_types)} hops\n\n"
        f"{chain}\n"
    )


@mcp.tool()
def docs_for(
    symbol: str,
    file_path: str | None = None,
    limit: int = 20,
    workspace: str | None = None,
) -> str:
    """Documentation anchored to ``symbol`` via DocAnchor COVERS edges: which
    doc chunks describe it, their anchor type (definition/example/warning/…)
    and confidence, and the source doc files. Use it to find prose explaining a
    symbol's contract before reading code.

    Args:
        symbol: Symbol name.
        file_path: Optional file to disambiguate a non-unique name.
        limit: Max doc anchors (default 20).
        workspace: Optional base workspace id; defaults to SURGICAL_CONTEXT_WORKSPACE.
    """
    workspace_id = resolve_workspace_id(workspace)
    r = _engine.docs_for(symbol, workspace_id, file_path=file_path, limit=limit)
    if not r.found:
        hint = f" in {file_path}" if file_path else ""
        return (
            f"Symbol '{symbol}'{hint} not found in the index (workspace: {workspace_id}). "
            f"Try ``find_definition('{symbol}')``."
        )
    if not r.rows:
        return (
            f"No documentation anchors cover `{symbol}` (workspace: {workspace_id}). "
            "The symbol may be undocumented, or docs aren't indexed for this workspace."
        )
    lines = [f"# Docs for `{symbol}`", f"workspace: {workspace_id} · {len(r.rows)} anchors", ""]
    for row in r.rows:
        files = ", ".join(row.files) if row.files else "(source file unknown)"
        lines.append(f"- [{row.anchor_type or 'doc'}] conf={row.confidence:.2f} — {files}")
    return "\n".join(lines) + "\n"


@mcp.tool()
def set_overlay(file_path: str, content: str, workspace: str | None = None) -> str:
    """Stash an UNCOMMITTED edit of ``file_path`` (the new full file content) so
    subsequent ``impact`` and ``ask_code`` calls reflect it. This is how you
    check the blast radius of a change you just made BEFORE committing: push the
    edited file here, then call ``impact(symbol)`` — callers you just typed show
    up as degraded ``overlay`` rows, and ``ask_code`` reads your buffer over the
    indexed code. Clear it with ``clear_overlay`` when done.

    Args:
        file_path: Path of the edited file (use the same path you'll reference
            in ``impact``/``read_symbol`` — absolute paths match the index).
        content: The full, edited file content (the unsaved buffer).
        workspace: Optional base workspace id; defaults to SURGICAL_CONTEXT_WORKSPACE.
    """
    workspace_id = resolve_workspace_id(workspace)
    symbols = _engine.set_overlay(file_path, content, workspace_id)
    head = (
        f"# Overlay set: {file_path}\n"
        f"workspace: {workspace_id} · {len(symbols)} symbols parsed\n"
    )
    if symbols:
        head += "\n" + "\n".join(f"- {s}" for s in symbols[:50])
        if len(symbols) > 50:
            head += f"\n… (+{len(symbols) - 50} more)"
    else:
        head += "\n(No symbols parsed — config/data file, or no language adapter.)"
    return head + "\n\nimpact / ask_code now reflect this buffer until clear_overlay.\n"


@mcp.tool()
def clear_overlay(file_path: str, workspace: str | None = None) -> str:
    """Drop a buffer previously pushed with ``set_overlay`` so ``impact`` /
    ``ask_code`` return to the committed index for that file.

    Args:
        file_path: Path of the overlay buffer to clear.
        workspace: Optional base workspace id; defaults to SURGICAL_CONTEXT_WORKSPACE.
    """
    workspace_id = resolve_workspace_id(workspace)
    cleared = _engine.clear_overlay(file_path, workspace_id)
    if cleared:
        return f"Cleared overlay for {file_path} (workspace: {workspace_id})."
    return f"No overlay buffer was set for {file_path} (workspace: {workspace_id})."


@mcp.tool()
def explain(concept: str, file_path: str | None = None, workspace: str | None = None) -> str:
    """Concept card for ``concept``: resolve it to a symbol, then lay out its
    one-hop connections grouped by relationship (calls / called by, uses type,
    instantiates, decorated by, inherits, …) plus its documentation and
    signature. The "what is X and how does it sit in the codebase" overview —
    a structured map for you to narrate, complementary to ``ask_code`` (which
    returns code) and ``impact`` (downstream AFFECTS closure, excluded here).

    Args:
        concept: A symbol name (exact match preferred) or free-text concept
            (resolved to the nearest symbol by embedding).
        file_path: Optional file to disambiguate an exact symbol name.
        workspace: Optional base workspace id; defaults to SURGICAL_CONTEXT_WORKSPACE.
    """
    workspace_id = resolve_workspace_id(workspace)
    r = _engine.explain(concept, workspace_id, file_path=file_path)
    if not r.found:
        return (
            f"Could not resolve '{concept}' to a symbol (workspace: {workspace_id}). "
            "Try an exact symbol name, or ``search_code`` / ``find_definition`` first."
        )
    via = " (nearest match)" if r.resolved_via == "vector" else ""
    lines = [
        f"# {r.seed_name}{via}",
        f"{r.seed_file} · workspace: {workspace_id} · uid: {r.seed_uid}",
        "",
    ]
    if r.signature:
        lines += ["```python", r.signature.rstrip(), "```", ""]
    if r.groups:
        lines.append("## Connections")
        for g in r.groups:
            names = ", ".join(f"{c.name}" for c in g.rows)
            lines.append(f"- **{g.label}** ({len(g.rows)}): {names}")
        lines.append("")
    else:
        lines.append("## Connections\n(No structural connections found.)\n")
    if r.docs:
        lines.append("## Documentation")
        for d in r.docs:
            files = ", ".join(d.files) if d.files else "(source unknown)"
            lines.append(f"- [{d.anchor_type or 'doc'}] conf={d.confidence:.2f} — {files}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


_FENCE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)

# Read/nav tools safe to batch. Excludes ask_code (already one rich call),
# overlay mutations, and trivial list_* tools.
_BATCHABLE = {
    "read_symbol": read_symbol,
    "callers": callers,
    "callees": callees,
    "impact": impact,
    "file_outline": file_outline,
    "search_code": search_code,
    "find_definition": find_definition,
    "docs_for": docs_for,
    "path": path,
    "classify_intent": classify_intent,
}


def _dedup_code_blocks(text: str, seen: dict[str, bool]) -> tuple[str, int]:
    """Collapse fenced code blocks already emitted earlier in the batch.

    Keys substantial blocks by content hash; a repeat becomes a one-line ref so
    a symbol's body is paid for once even when several ops surface it. Small
    blocks (signatures/stubs) are left alone — the ref wouldn't be shorter.
    """
    n = 0

    def repl(m: re.Match) -> str:
        nonlocal n
        body = m.group(1)
        if len(body) < 120:
            return m.group(0)
        key = hashlib.md5(body.encode("utf-8")).hexdigest()  # noqa: S324 — dedup key, not security
        if key in seen:
            n += 1
            return "```\n(identical code collapsed — shown above in this batch)\n```"
        seen[key] = True
        return m.group(0)

    return _FENCE.sub(repl, text), n


@mcp.tool()
def batch(ops: list[dict]) -> str:
    """Run several read/nav ops in ONE call (one round-trip), de-duplicating
    repeated code across results.

    Use this for multi-step questions. Each tool round-trip re-bills the whole
    conversation context (cache), so N granular calls = N replays; ``batch``
    collapses them into one. A symbol's code body is emitted once even if several
    ops surface it.

    ``ops``: list of ``{"tool": <name>, ...args}``. Supported tools:
    read_symbol, callers, callees, impact, file_outline, find_definition,
    search_code, docs_for, path, classify_intent. (ask_code is already one rich
    call; overlay mutations and list_* are excluded.)

    Example:
        [{"tool": "read_symbol", "name": "create_app"},
         {"tool": "callers", "symbol": "require_main"},
         {"tool": "impact", "symbol": "build_context_engine_state"}]
    """
    if not isinstance(ops, list) or not ops:
        return (
            'batch: pass a non-empty list of ops, e.g. '
            '[{"tool": "read_symbol", "name": "create_app"}].'
        )

    seen: dict[str, bool] = {}
    collapsed = 0
    parts: list[str] = []
    for i, op in enumerate(ops, 1):
        if not isinstance(op, dict) or "tool" not in op:
            parts.append(f"## op{i} — invalid (need a dict with a 'tool' key)")
            continue
        tool = op["tool"]
        fn = _BATCHABLE.get(tool)
        if fn is None:
            parts.append(
                f"## op{i} {tool} — not batchable. "
                f"Allowed: {', '.join(sorted(_BATCHABLE))}."
            )
            continue
        args = {k: v for k, v in op.items() if k != "tool"}
        argstr = ", ".join(f"{k}={v!r}" for k, v in args.items())
        try:
            out = fn(**args)
        except TypeError as e:
            parts.append(f"## op{i} — {tool}({argstr}) — bad args: {e}")
            continue
        except Exception as e:  # one bad op shouldn't sink the batch
            parts.append(f"## op{i} — {tool}({argstr}) — error: {type(e).__name__}: {e}")
            continue
        out, n = _dedup_code_blocks(out, seen)
        collapsed += n
        parts.append(f"## op{i} — {tool}({argstr})\n{out}")

    footer = f"\n---\n_batch: {len(ops)} ops in 1 round-trip"
    if collapsed:
        footer += f"; {collapsed} duplicate code block(s) collapsed"
    footer += "._"
    return "\n\n".join(parts) + footer


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
