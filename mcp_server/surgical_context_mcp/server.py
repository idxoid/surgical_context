"""MCP stdio server exposing surgical_context retrieval as structured tools.

Wire into Claude Code via ``.mcp.json`` (see mcp_server/claude_mcp.example.json).
The tools return code *context* — not an answer — for the host chat model to
reason over, the way ``/graphify query`` feeds a budgeted context block.

OUTPUT CONTRACT — every tool returns a structured result (see ``schemas.py``):
a JSON ``structuredContent`` payload (advertised via ``outputSchema``) carrying
stable symbol ``uid`` IDs and machine-readable scores/provenance, plus the
human/LLM markdown render in the result's text content. Programmatic callers
read the fields; a chat host reads the markdown. ``_result`` packs both.
"""

from __future__ import annotations

import hashlib
import re
import sys
from typing import cast

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent
from pydantic import BaseModel

from surgical_context_mcp import schemas
from surgical_context_mcp.config import resolve_workspace_id
from surgical_context_mcp.engine import AxisEngine, _common_dir_prefix

SERVER_INSTRUCTIONS = """\
surgical_context exposes a code repository's **axis retrieval** graph: ask in
natural language or navigate by name, and get back ranked, graph-expanded code
*context* (not an answer) for you to reason over — the way `/graphify query`
feeds a budgeted block into the calling model. Retrieval is LLM-free.

Every tool returns BOTH a markdown render (read this) and a structured JSON
payload (`structuredContent`, per the tool's `outputSchema`) with stable symbol
`uid` IDs, machine-readable scores, and provenance — parse the fields when you
need to chain calls or sort/filter programmatically.

CHOOSING A TOOL — round-trips dominate cost (each call re-bills the whole
conversation as cache), so prefer one rich call over many granular drips:
  - Can you NAME the target? Use the cheap, precise primitives:
      find_definition / search_code  → locate a symbol
      read_symbol                    → its exact source
      file_outline                   → a file's symbol map
      callers / callees / path       → call edges & how two symbols connect
      impact                         → downstream blast radius (structural)
      explain / docs_for             → a concept card / its documentation
  - Can you NOT name it ("how does X work", "where is Y handled")? Use the
    semantic retrievers: ask_code (cross-file code bundles) or investigate
    (intent → context → blast surface, one planned round-trip).
  - Several known lookups at once? Wrap them in `batch` (one round-trip, code
    de-duplicated) instead of issuing them separately.
ask_code is the HEAVY tool (returns many bodies) — reach for it only when the
cheap tools can't, and use render="names" for a census without code.

WORKSPACE: tools default to SURGICAL_CONTEXT_WORKSPACE; pass workspace=<base id>
(see list_workspaces) to target another indexed repo. Start at list_workspaces →
list_files → file_outline → read_symbol when exploring an unfamiliar repo.

UNCOMMITTED EDITS: push a dirty buffer with set_overlay(path, content) so
impact/ask_code reflect a change you haven't committed; clear_overlay when done.
"""

mcp = FastMCP("surgical-context", instructions=SERVER_INSTRUCTIONS)
_engine = AxisEngine()


def _result[T: BaseModel](markdown: str, payload: T) -> T:
    """Pack a structured payload + its markdown render into the tool's result.

    Returns a ``CallToolResult`` whose text content is the clean markdown (what
    a chat host reads) and whose ``structuredContent`` is ``payload`` (what a
    program reads, validated against the tool's ``outputSchema``). FastMCP passes
    a returned ``CallToolResult`` through verbatim, so the markdown render is
    preserved instead of being replaced by a JSON dump of the model.

    Typed ``-> T`` so each tool's ``-> XxxOutput`` annotation drives schema
    generation and stays mypy-clean; the runtime object is the ``CallToolResult``
    (a single, contained cast — the symmetric ``_as_result`` undoes it in batch).
    """
    res = CallToolResult(
        content=[TextContent(type="text", text=markdown)],
        structuredContent=payload.model_dump(mode="json"),
    )
    return cast(T, res)


def _as_result(value: object) -> CallToolResult:
    """Recover the runtime ``CallToolResult`` a tool returned (see ``_result``)."""
    return cast(CallToolResult, value)


def _intent_roles(pairs: list[tuple[str, float]]) -> list[schemas.IntentRole]:
    return [schemas.IntentRole(role=r, score=round(float(s), 4)) for r, s in pairs]


def _intent_str(roles: list[schemas.IntentRole]) -> str:
    return ", ".join(f"{r.role}({r.score:.2f})" for r in roles)


@mcp.tool(structured_output=True)
def ask_code(
    question: str,
    token_budget: int = 4000,
    workspace: str | None = None,
    roles: list[str] | None = None,
    render: str = "full",
) -> schemas.AskCodeOutput:
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
        md = f"Unknown render '{render}'. Use 'full' (code) or 'names' (census)."
        return _result(
            md,
            schemas.AskCodeOutput(
                tool="ask_code",
                ok=False,
                workspace=workspace_id,
                markdown=md,
                question=question,
                render=render,
            ),
        )

    if roles:
        known = _engine.available_roles()
        invalid = [r for r in roles if r not in known]
        if invalid:
            md = (
                f"Unknown role(s): {', '.join(invalid)}.\n"
                f"Valid roles: {', '.join(sorted(known))}.\n"
                "Call list_roles for descriptions."
            )
            return _result(
                md,
                schemas.AskCodeOutput(
                    tool="ask_code",
                    ok=False,
                    workspace=workspace_id,
                    markdown=md,
                    question=question,
                    render=render,
                ),
            )

    result = _engine.ask(
        question, workspace_id, token_budget=token_budget, roles=roles, render=render
    )
    intent = _intent_roles(result.intent)
    roles_str = _intent_str(intent)
    symbols = [schemas.ContextItem(**row) for row in result.symbols]

    if not result.text:
        md = (
            f"No code context found for: {question!r}\n"
            f"workspace: {workspace_id}\n"
            f"intent roles: {roles_str or '(none above threshold)'}\n\n"
            "The repo may not be indexed under the axis_python_v1 profile, the "
            "Neo4j/LanceDB backends may be down, or the question matched no role. "
            "Verify the index exists for this workspace."
        )
        return _result(
            md,
            schemas.AskCodeOutput(
                tool="ask_code",
                ok=False,
                workspace=workspace_id,
                markdown=md,
                question=question,
                render=render,
                token_budget=token_budget,
                intent=intent,
                candidate_count=result.candidate_count,
            ),
        )

    header = (
        f"# Surgical context for: {question}\n"
        f"workspace: {workspace_id} · intent: {roles_str} · "
        f"{result.candidate_count} candidates · {len(result.files)} files\n"
    )
    md = header + "\n" + result.text
    if render == "full":
        # Footer nudges toward cheaper follow-ups instead of always maxing budget.
        md += (
            f"\n---\n_Shown under token_budget={token_budget}. More depth: raise "
            'token_budget. Cheap structural map: render="names". '
            "Targeted facts: read_symbol / callers / impact._\n"
        )
    return _result(
        md,
        schemas.AskCodeOutput(
            tool="ask_code",
            ok=True,
            workspace=workspace_id,
            markdown=md,
            question=question,
            render=render,
            token_budget=token_budget,
            intent=intent,
            candidate_count=result.candidate_count,
            files=result.files,
            symbols=symbols,
        ),
    )


@mcp.tool(structured_output=True)
def investigate(
    question: str,
    depth: str = "full",
    token_budget: int = 4000,
    workspace: str | None = None,
) -> schemas.InvestigateOutput:
    """One-call deep retrieval — a planned pipeline run server-side in ONE
    round-trip: intent → ranked code context → downstream blast surface of the
    top seeds, de-duplicated.

    Use this for diagnosis / "how does X work" / "what breaks if I change X"
    INSTEAD of dripping many granular calls: each host round-trip re-bills the
    whole context, so one planned call is far cheaper than N drilldowns. The
    pipeline's internal steps run in the server (no host-context replay).

    depth="full" (default) = code bundles + impact on the top 5 seeds — a
    self-contained first shot. depth="lean" = names-only context + impact on the
    top 3 (cheaper; may need one follow-up read_symbol).
    """
    workspace_id = resolve_workspace_id(workspace)
    if depth not in ("full", "lean"):
        md = f"Unknown depth '{depth}'. Use 'full' (code + blast) or 'lean' (names + blast)."
        return _result(
            md,
            schemas.InvestigateOutput(
                tool="investigate",
                ok=False,
                workspace=workspace_id,
                markdown=md,
                question=question,
                depth=depth,
            ),
        )

    r = _engine.investigate(question, workspace_id, depth=depth, token_budget=token_budget)
    intent = _intent_roles(r.intent)
    roles_str = _intent_str(intent)
    symbols = [schemas.ContextItem(**row) for row in r.symbols]
    blast_items = [
        schemas.BlastItem(
            seed=str(b.get("seed") or ""),
            name=str(b.get("name") or ""),
            file_path=b.get("file_path"),
            depth=b.get("depth"),
            kind=b.get("kind"),
        )
        for b in r.blast
    ]

    if not r.context_text and not r.blast:
        md = (
            f"No context found for: {question!r}\n"
            f"workspace: {workspace_id}\n"
            f"intent: {roles_str or '(none above threshold)'}\n"
            "The repo may not be indexed under axis_python_v1, or nothing matched."
        )
        return _result(
            md,
            schemas.InvestigateOutput(
                tool="investigate",
                ok=False,
                workspace=workspace_id,
                markdown=md,
                question=question,
                depth=depth,
                intent=intent,
                candidate_count=r.candidate_count,
            ),
        )

    out = [
        f"# Investigation: {question}",
        f"workspace: {workspace_id} · intent: {roles_str} · "
        f"{r.candidate_count} candidates · {len(r.files)} files · depth={depth}",
        "",
        "## Context (ranked code)",
        r.context_text or "(no context bundles)",
    ]
    if r.blast:
        prefix = _common_dir_prefix([b["file_path"] for b in r.blast if b.get("file_path")])
        out.append(
            "\n## Blast surface — downstream dependents of the top seeds (deduped, not shown above)"
        )
        if prefix:
            out.append(f"_paths relative to {prefix}_")
        for b in r.blast:
            fp = b.get("file_path") or ""
            rel = fp[len(prefix) :] if prefix and fp.startswith(prefix) else fp
            out.append(
                f"- {b.get('name')} — {rel} (←{b.get('seed')}, d{b.get('depth')}, {b.get('kind')})"
            )
    out.append(
        "\n---\n_one planned pipeline (intent → context → blast) in 1 round-trip. "
        "Follow up only for a specific missing body._"
    )
    md = "\n".join(out)
    return _result(
        md,
        schemas.InvestigateOutput(
            tool="investigate",
            ok=True,
            workspace=workspace_id,
            markdown=md,
            question=question,
            depth=depth,
            intent=intent,
            candidate_count=r.candidate_count,
            files=r.files,
            symbols=symbols,
            blast=blast_items,
        ),
    )


@mcp.tool(structured_output=True)
def impact(
    symbol: str,
    file_path: str | None = None,
    max_depth: int = 3,
    workspace: str | None = None,
) -> schemas.ImpactOutput:
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
        md = (
            f"Symbol '{symbol}'{hint} not found in the committed index or any "
            f"overlay buffer (workspace: {workspace_id}). It may be unindexed, "
            "or the name/path is off."
        )
        return _result(
            md,
            schemas.ImpactOutput(
                tool="impact",
                ok=False,
                found=False,
                workspace=workspace_id,
                markdown=md,
                symbol=symbol,
            ),
        )

    items = [
        schemas.ImpactItem(
            uid=row.get("uid"),
            name=str(row.get("name") or row.get("symbol") or "?"),
            file_path=row.get("file_path"),
            depth=row.get("depth"),
            kind=row.get("kind") or None,
            severity=row.get("severity") or None,
            provenance="overlay" if row.get("degraded") else "committed",
        )
        for row in r.affected_symbols
    ]

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
    else:
        lines.append("## Affected files")
        lines.extend(f"- {f}" for f in r.affected_files)
        lines.append("")
        lines.append("## Affected symbols")
        for it in items:
            tag = " [overlay]" if it.provenance == "overlay" else ""
            lines.append(
                f"- {it.name} — {it.file_path} (depth={it.depth}, {it.kind or ''}, "
                f"sev={it.severity or ''}){tag}"
            )
    md = "\n".join(lines) + "\n"
    return _result(
        md,
        schemas.ImpactOutput(
            tool="impact",
            ok=True,
            found=True,
            workspace=workspace_id,
            markdown=md,
            symbol=symbol,
            symbol_uid=r.symbol_uid,
            file_path=r.file_path,
            max_depth=r.max_depth,
            degraded=r.degraded,
            overlay_count=r.overlay_count,
            affected_files=r.affected_files,
            affected_symbols=items,
        ),
    )


@mcp.tool(structured_output=True)
def list_workspaces() -> schemas.ListWorkspacesOutput:
    """List the indexed workspaces (repos) you can query with ``ask_code`` /
    ``impact``. Returns base workspace ids to pass as the ``workspace=`` argument.
    """
    rows = _engine.list_workspaces()
    if not rows:
        md = "No indexed workspaces found. Is the index built and Neo4j up?"
        return _result(
            md, schemas.ListWorkspacesOutput(tool="list_workspaces", ok=False, markdown=md)
        )
    items = [schemas.WorkspaceItem(base=w.base, indexed=w.indexed, files=w.files) for w in rows]
    lines = ["# Indexed workspaces — pass `base` as workspace=", ""]
    lines.extend(f"- {w.base}  ({w.files} files)" for w in items)
    md = "\n".join(lines) + "\n"
    return _result(
        md, schemas.ListWorkspacesOutput(tool="list_workspaces", markdown=md, workspaces=items)
    )


@mcp.tool(structured_output=True)
def list_files(
    workspace: str | None = None,
    path_prefix: str | None = None,
    with_counts: bool = False,
    limit: int = 400,
) -> schemas.ListFilesOutput:
    """List indexed files of a workspace — the navigation entry point:
    list_workspaces → **list_files** → file_outline(path) → read_symbol(name).

    The only way to enumerate a NON-local workspace (workspace="qa_repo/django@main")
    the host's file tools can't see; for the local repo the host's Glob is usually
    cheaper. Cheap — paths from the index, no code.

    Args:
        workspace: base workspace id; defaults to SURGICAL_CONTEXT_WORKSPACE.
        path_prefix: substring filter on the file path (e.g. "api/routes").
        with_counts: also show per-file indexed-symbol count.
        limit: max files returned (default 400).
    """
    workspace_id = resolve_workspace_id(workspace)
    rows = _engine.list_files(
        workspace_id, path_prefix=path_prefix, with_counts=with_counts, limit=limit
    )
    if not rows:
        where = f" under {path_prefix!r}" if path_prefix else ""
        md = f"No indexed files{where} in {workspace_id}."
        return _result(
            md,
            schemas.ListFilesOutput(
                tool="list_files",
                ok=False,
                workspace=workspace_id,
                markdown=md,
                path_prefix=path_prefix,
            ),
        )
    items = [
        schemas.FileItem(path=r.path, symbols=r.symbols if with_counts else None) for r in rows
    ]
    prefix = _common_dir_prefix([r.path for r in rows])
    lines = [f"# {len(rows)} files · {workspace_id}"]
    if prefix:
        lines.append(f"_paths relative to {prefix}_")
    lines.append("")
    for r in rows:
        rel = r.path[len(prefix) :] if prefix and r.path.startswith(prefix) else r.path
        lines.append(f"- {rel}" + (f"  ({r.symbols} symbols)" if with_counts else ""))
    md = "\n".join(lines) + "\n"
    return _result(
        md,
        schemas.ListFilesOutput(
            tool="list_files",
            workspace=workspace_id,
            markdown=md,
            path_prefix=path_prefix,
            files=items,
        ),
    )


@mcp.tool(structured_output=True)
def classify_intent(question: str, top_roles: int = 5) -> schemas.ClassifyIntentOutput:
    """Preview which structural roles the embedding intent-classifier maps a
    question to (cosine of question vs role descriptions), WITHOUT running
    retrieval. Cheap (embedding only — no graph). Use it to decide whether to
    override ``ask_code(roles=[...])``: see the auto-picked roles + similarity,
    then refine. Closes the loop list_roles → classify_intent → ask_code.
    """
    matches = _engine.classify_intent(question, top_roles=top_roles)
    if not matches:
        md = (
            f"No role above threshold for: {question!r}.\n"
            "Try a more specific question, or call list_roles for the vocabulary."
        )
        return _result(
            md,
            schemas.ClassifyIntentOutput(
                tool="classify_intent",
                ok=False,
                markdown=md,
                question=question,
            ),
        )
    intent = [
        schemas.IntentRole(role=role, score=round(float(sim), 4), description=desc)
        for role, sim, desc in matches
    ]
    lines = [f"# Intent for: {question}", ""]
    lines.extend(f"- {r.role} ({r.score:.2f}) — {r.description}" for r in intent)
    md = "\n".join(lines) + "\n"
    return _result(
        md,
        schemas.ClassifyIntentOutput(
            tool="classify_intent",
            markdown=md,
            question=question,
            intent=intent,
        ),
    )


@mcp.tool(structured_output=True)
def list_roles() -> schemas.ListRolesOutput:
    """List the structural roles you can pass to ``ask_code(roles=[...])`` to
    bypass the embedding intent-classifier and target retrieval yourself.
    """
    roles = _engine.available_roles()
    if not roles:
        md = "No roles available."
        return _result(md, schemas.ListRolesOutput(tool="list_roles", ok=False, markdown=md))
    items = [schemas.RoleItem(role=role, description=desc) for role, desc in sorted(roles.items())]
    lines = ["# Axis roles — pass to ask_code(roles=[...])", ""]
    lines.extend(f"- **{it.role}** — {it.description}" for it in items)
    md = "\n".join(lines) + "\n"
    return _result(md, schemas.ListRolesOutput(tool="list_roles", markdown=md, roles=items))


@mcp.tool(structured_output=True)
def read_symbol(
    name: str,
    file_path: str | None = None,
    workspace: str | None = None,
) -> schemas.ReadSymbolOutput:
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
        md = (
            f"Symbol '{name}'{hint} not found in the index (workspace: {workspace_id}). "
            f"Try ``find_definition('{name}')`` or check the spelling/path."
        )
        return _result(
            md,
            schemas.ReadSymbolOutput(
                tool="read_symbol",
                ok=False,
                found=False,
                workspace=workspace_id,
                markdown=md,
                name=name,
            ),
        )
    header = (
        f"# {r.name}  ({r.file_path}:{r.start_line}-{r.end_line})\n"
        f"workspace: {workspace_id} · uid: {r.uid}\n"
    )
    if not r.code:
        md = (
            header + "\n(Source unavailable — the file is outside the indexed workspace root "
            "or could not be read. The span above still locates the definition.)\n"
        )
        return _result(
            md,
            schemas.ReadSymbolOutput(
                tool="read_symbol",
                ok=True,
                found=True,
                workspace=workspace_id,
                markdown=md,
                name=r.name,
                uid=r.uid,
                file_path=r.file_path,
                start_line=r.start_line,
                end_line=r.end_line,
            ),
        )
    md = header + "\n```python\n" + r.code.rstrip() + "\n```\n"
    return _result(
        md,
        schemas.ReadSymbolOutput(
            tool="read_symbol",
            ok=True,
            found=True,
            workspace=workspace_id,
            markdown=md,
            name=r.name,
            uid=r.uid,
            file_path=r.file_path,
            start_line=r.start_line,
            end_line=r.end_line,
            language="python",
            code=r.code.rstrip(),
        ),
    )


@mcp.tool(structured_output=True)
def search_code(
    query: str,
    limit: int = 10,
    kind: str = "symbol",
    workspace: str | None = None,
) -> schemas.SearchCodeOutput:
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
        md = f"Unknown kind '{kind}'. Use 'symbol' or 'doc'."
        return _result(
            md,
            schemas.SearchCodeOutput(
                tool="search_code",
                ok=False,
                workspace=workspace_id,
                markdown=md,
                query=query,
                kind=kind,
            ),
        )
    rows = _engine.search_code(query, workspace_id, limit=limit, kind=kind)
    if not rows:
        md = f"No {kind} hits for: {query!r} (workspace: {workspace_id})."
        return _result(
            md,
            schemas.SearchCodeOutput(
                tool="search_code",
                ok=False,
                workspace=workspace_id,
                markdown=md,
                query=query,
                kind=kind,
            ),
        )
    lines = [f"# {kind} search: {query}", f"workspace: {workspace_id} · {len(rows)} hits", ""]
    hits: list[schemas.SearchItem] = []
    if kind == "symbol":
        for r in rows:
            score = r.get("score")
            dist = r.get("distance")
            hits.append(
                schemas.SearchItem(
                    uid=r.get("uid"),
                    name=r.get("name"),
                    file_path=r.get("file_path"),
                    score=float(score) if isinstance(score, (int, float)) else None,
                    distance=float(dist) if isinstance(dist, (int, float)) else None,
                )
            )
            score_s = f" · score={score:.2f}" if isinstance(score, (int, float)) else ""
            lines.append(f"- {r.get('name', '?')} — {r.get('file_path', '')}{score_s}")
    else:
        for r in rows:
            fp = r.get("file_path") or r.get("id") or ""
            snippet = " ".join(str(r.get("chunk") or "").split())[:160]
            hits.append(schemas.SearchItem(uid=r.get("id"), file_path=str(fp), snippet=snippet))
            lines.append(f"- {fp}\n  {snippet}")
    md = "\n".join(lines) + "\n"
    return _result(
        md,
        schemas.SearchCodeOutput(
            tool="search_code",
            workspace=workspace_id,
            markdown=md,
            query=query,
            kind=kind,
            hits=hits,
        ),
    )


@mcp.tool(structured_output=True)
def callers(
    symbol: str,
    file_path: str | None = None,
    max_hops: int = 1,
    limit: int = 50,
    workspace: str | None = None,
) -> schemas.NeighboursOutput:
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


@mcp.tool(structured_output=True)
def callees(
    symbol: str,
    file_path: str | None = None,
    max_hops: int = 1,
    limit: int = 50,
    workspace: str | None = None,
) -> schemas.NeighboursOutput:
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
) -> schemas.NeighboursOutput:
    workspace_id = resolve_workspace_id(workspace)
    direction = "reverse" if which == "callers" else "forward"
    depth = max_hops if isinstance(max_hops, int) and 1 <= max_hops <= 4 else 1
    r = _engine.call_neighbours(
        symbol, workspace_id, direction=direction, file_path=file_path, max_hops=depth, limit=limit
    )
    if not r.found:
        hint = f" in {file_path}" if file_path else ""
        md = (
            f"Symbol '{symbol}'{hint} not found in the index (workspace: {workspace_id}). "
            f"Try ``find_definition('{symbol}')``."
        )
        return _result(
            md,
            schemas.NeighboursOutput(
                tool=which,
                ok=False,
                found=False,
                workspace=workspace_id,
                markdown=md,
                symbol=symbol,
                relation=which,
                max_hops=depth,
            ),
        )
    rel = "called by" if which == "callers" else "calls"
    items = [
        schemas.NeighbourItem(uid=row.uid, name=row.name, file_path=row.file_path, depth=row.depth)
        for row in r.rows
    ]
    lines = [
        f"# {symbol} — {which} (max_hops={depth})",
        f"workspace: {workspace_id} · uid: {r.symbol_uid} · {len(r.rows)} {which}",
        "",
    ]
    if not r.rows:
        lines.append(f"No {which} found ({'entry point' if which == 'callers' else 'leaf'}).")
    else:
        for it in items:
            lines.append(f"- {it.name} — {it.file_path} (depth={it.depth}) [{rel}]")
    md = "\n".join(lines) + "\n"
    return _result(
        md,
        schemas.NeighboursOutput(
            tool=which,
            ok=True,
            found=True,
            workspace=workspace_id,
            markdown=md,
            symbol=symbol,
            relation=which,
            symbol_uid=r.symbol_uid,
            max_hops=depth,
            neighbours=items,
        ),
    )


@mcp.tool(structured_output=True)
def find_definition(
    name: str, limit: int = 20, workspace: str | None = None
) -> schemas.FindDefinitionOutput:
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
        md = f"No definition of '{name}' in the index (workspace: {workspace_id})."
        return _result(
            md,
            schemas.FindDefinitionOutput(
                tool="find_definition",
                ok=False,
                workspace=workspace_id,
                markdown=md,
                name=name,
            ),
        )
    items = [
        schemas.DefinitionItem(
            uid=h.uid,
            name=h.name,
            file_path=h.file_path,
            kind=h.kind or None,
            start_line=h.start_line,
        )
        for h in hits
    ]
    lines = [f"# Definitions of `{name}`", f"workspace: {workspace_id} · {len(hits)} found", ""]
    for h in hits:
        kind = f" ({h.kind})" if h.kind else ""
        lines.append(f"- {h.file_path}:{h.start_line}{kind}")
    md = "\n".join(lines) + "\n"
    return _result(
        md,
        schemas.FindDefinitionOutput(
            tool="find_definition",
            workspace=workspace_id,
            markdown=md,
            name=name,
            definitions=items,
        ),
    )


@mcp.tool(structured_output=True)
def file_outline(
    file_path: str, limit: int = 400, workspace: str | None = None
) -> schemas.FileOutlineOutput:
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
        md = (
            f"No indexed file matching '{file_path}' (workspace: {workspace_id}). "
            "Pass a longer path suffix, or check the file is indexed."
        )
        return _result(
            md,
            schemas.FileOutlineOutput(
                tool="file_outline",
                ok=False,
                found=False,
                workspace=workspace_id,
                markdown=md,
                requested_path=file_path,
            ),
        )
    items = [
        schemas.OutlineItem(name=row.name, kind=row.kind or None, start_line=row.start_line)
        for row in r.rows
    ]
    lines = [f"# Outline: {r.file_path}", f"workspace: {workspace_id} · {len(r.rows)} symbols", ""]
    for row in r.rows:
        kind = f"{row.kind} " if row.kind else ""
        lines.append(f"- L{row.start_line}: {kind}{row.name}")
    md = "\n".join(lines) + "\n"
    return _result(
        md,
        schemas.FileOutlineOutput(
            tool="file_outline",
            ok=True,
            found=True,
            workspace=workspace_id,
            markdown=md,
            requested_path=file_path,
            file_path=r.file_path,
            symbols=items,
        ),
    )


@mcp.tool(structured_output=True)
def path(
    symbol_a: str,
    symbol_b: str,
    file_a: str | None = None,
    file_b: str | None = None,
    max_hops: int = 6,
    workspace: str | None = None,
) -> schemas.PathOutput:
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
        md = f"No path from '{symbol_a}' to '{symbol_b}' (workspace: {workspace_id}): {r.reason}."
        return _result(
            md,
            schemas.PathOutput(
                tool="path",
                ok=False,
                found=False,
                workspace=workspace_id,
                markdown=md,
                symbol_a=symbol_a,
                symbol_b=symbol_b,
                reason=r.reason,
            ),
        )
    # Interleave node names with the edge type that links each consecutive pair.
    parts = [r.node_names[0]] if r.node_names else []
    for i, rel in enumerate(r.rel_types):
        nxt = r.node_names[i + 1] if i + 1 < len(r.node_names) else "?"
        parts.append(f"--[{rel}]--> {nxt}")
    chain = " ".join(parts)
    md = (
        f"# Path: {symbol_a} → {symbol_b}\n"
        f"workspace: {workspace_id} · {len(r.rel_types)} hops\n\n"
        f"{chain}\n"
    )
    return _result(
        md,
        schemas.PathOutput(
            tool="path",
            ok=True,
            found=True,
            workspace=workspace_id,
            markdown=md,
            symbol_a=symbol_a,
            symbol_b=symbol_b,
            hops=len(r.rel_types),
            node_names=r.node_names,
            rel_types=r.rel_types,
        ),
    )


@mcp.tool(structured_output=True)
def docs_for(
    symbol: str,
    file_path: str | None = None,
    limit: int = 20,
    workspace: str | None = None,
) -> schemas.DocsForOutput:
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
        md = (
            f"Symbol '{symbol}'{hint} not found in the index (workspace: {workspace_id}). "
            f"Try ``find_definition('{symbol}')``."
        )
        return _result(
            md,
            schemas.DocsForOutput(
                tool="docs_for",
                ok=False,
                found=False,
                workspace=workspace_id,
                markdown=md,
                symbol=symbol,
            ),
        )
    items = [
        schemas.DocAnchorItem(
            chunk_id=row.chunk_id or None,
            anchor_type=row.anchor_type or None,
            confidence=row.confidence,
            files=row.files,
        )
        for row in r.rows
    ]
    if not r.rows:
        md = (
            f"No documentation anchors cover `{symbol}` (workspace: {workspace_id}). "
            "The symbol may be undocumented, or docs aren't indexed for this workspace."
        )
        return _result(
            md,
            schemas.DocsForOutput(
                tool="docs_for",
                ok=True,
                found=True,
                workspace=workspace_id,
                markdown=md,
                symbol=symbol,
                symbol_uid=r.symbol_uid,
            ),
        )
    lines = [f"# Docs for `{symbol}`", f"workspace: {workspace_id} · {len(r.rows)} anchors", ""]
    for row in r.rows:
        files = ", ".join(row.files) if row.files else "(source file unknown)"
        lines.append(f"- [{row.anchor_type or 'doc'}] conf={row.confidence:.2f} — {files}")
    md = "\n".join(lines) + "\n"
    return _result(
        md,
        schemas.DocsForOutput(
            tool="docs_for",
            ok=True,
            found=True,
            workspace=workspace_id,
            markdown=md,
            symbol=symbol,
            symbol_uid=r.symbol_uid,
            anchors=items,
        ),
    )


@mcp.tool(structured_output=True)
def set_overlay(
    file_path: str, content: str, workspace: str | None = None
) -> schemas.SetOverlayOutput:
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
        f"# Overlay set: {file_path}\nworkspace: {workspace_id} · {len(symbols)} symbols parsed\n"
    )
    if symbols:
        head += "\n" + "\n".join(f"- {s}" for s in symbols[:50])
        if len(symbols) > 50:
            head += f"\n… (+{len(symbols) - 50} more)"
    else:
        head += "\n(No symbols parsed — config/data file, or no language adapter.)"
    md = head + "\n\nimpact / ask_code now reflect this buffer until clear_overlay.\n"
    return _result(
        md,
        schemas.SetOverlayOutput(
            tool="set_overlay",
            workspace=workspace_id,
            markdown=md,
            file_path=file_path,
            symbols=symbols,
        ),
    )


@mcp.tool(structured_output=True)
def clear_overlay(file_path: str, workspace: str | None = None) -> schemas.ClearOverlayOutput:
    """Drop a buffer previously pushed with ``set_overlay`` so ``impact`` /
    ``ask_code`` return to the committed index for that file.

    Args:
        file_path: Path of the overlay buffer to clear.
        workspace: Optional base workspace id; defaults to SURGICAL_CONTEXT_WORKSPACE.
    """
    workspace_id = resolve_workspace_id(workspace)
    cleared = _engine.clear_overlay(file_path, workspace_id)
    if cleared:
        md = f"Cleared overlay for {file_path} (workspace: {workspace_id})."
    else:
        md = f"No overlay buffer was set for {file_path} (workspace: {workspace_id})."
    return _result(
        md,
        schemas.ClearOverlayOutput(
            tool="clear_overlay",
            ok=cleared,
            workspace=workspace_id,
            markdown=md,
            file_path=file_path,
            cleared=cleared,
        ),
    )


@mcp.tool(structured_output=True)
def explain(
    concept: str, file_path: str | None = None, workspace: str | None = None
) -> schemas.ExplainOutput:
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
        md = (
            f"Could not resolve '{concept}' to a symbol (workspace: {workspace_id}). "
            "Try an exact symbol name, or ``search_code`` / ``find_definition`` first."
        )
        return _result(
            md,
            schemas.ExplainOutput(
                tool="explain",
                ok=False,
                found=False,
                workspace=workspace_id,
                markdown=md,
                concept=concept,
            ),
        )
    via = " (nearest match)" if r.resolved_via == "vector" else ""
    groups = [
        schemas.ConnectionGroup(label=g.label, names=[c.name for c in g.rows]) for g in r.groups
    ]
    docs = [
        schemas.DocAnchorItem(
            chunk_id=d.chunk_id or None,
            anchor_type=d.anchor_type or None,
            confidence=d.confidence,
            files=d.files,
        )
        for d in r.docs
    ]
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
    md = "\n".join(lines).rstrip() + "\n"
    return _result(
        md,
        schemas.ExplainOutput(
            tool="explain",
            ok=True,
            found=True,
            workspace=workspace_id,
            markdown=md,
            concept=concept,
            resolved_via=r.resolved_via,
            seed_name=r.seed_name,
            seed_uid=r.seed_uid,
            seed_file=r.seed_file,
            signature=r.signature or None,
            groups=groups,
            docs=docs,
        ),
    )


_FENCE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)

# Read/nav tools safe to batch. Excludes ask_code (already one rich call),
# overlay mutations, and trivial list_* tools.
_BATCHABLE = {
    "list_files": list_files,
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
            return str(m.group(0))
        key = hashlib.sha256(body.encode("utf-8")).hexdigest()
        if key in seen:
            n += 1
            return "```\n(identical code collapsed — shown above in this batch)\n```"
        seen[key] = True
        return str(m.group(0))

    return _FENCE.sub(repl, text), n


@mcp.tool(structured_output=True)
def batch(ops: list[dict]) -> schemas.BatchOutput:
    """Run several read/nav ops in ONE call (one round-trip), de-duplicating
    repeated code across results.

    Use this for multi-step questions. Each tool round-trip re-bills the whole
    conversation context (cache), so N granular calls = N replays; ``batch``
    collapses them into one. A symbol's code body is emitted once even if several
    ops surface it. Each op's structured payload is returned in ``results[]``;
    the combined markdown render is in ``markdown``.

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
        md = (
            "batch: pass a non-empty list of ops, e.g. "
            '[{"tool": "read_symbol", "name": "create_app"}].'
        )
        return _result(md, schemas.BatchOutput(tool="batch", ok=False, markdown=md))

    seen: dict[str, bool] = {}
    collapsed = 0
    parts: list[str] = []
    results: list[schemas.BatchOpResult] = []
    for i, op in enumerate(ops, 1):
        if not isinstance(op, dict) or "tool" not in op:
            parts.append(f"## op{i} — invalid (need a dict with a 'tool' key)")
            results.append(
                schemas.BatchOpResult(
                    index=i, tool="?", ok=False, error="invalid op (need a dict with a 'tool' key)"
                )
            )
            continue
        tool = str(op["tool"])
        fn = _BATCHABLE.get(tool)
        if fn is None:
            err = f"not batchable. Allowed: {', '.join(sorted(_BATCHABLE))}."
            parts.append(f"## op{i} {tool} — {err}")
            results.append(schemas.BatchOpResult(index=i, tool=tool, ok=False, error=err))
            continue
        args = {k: v for k, v in op.items() if k != "tool"}
        argstr = ", ".join(f"{k}={v!r}" for k, v in args.items())
        try:
            sub = _as_result(fn(**args))
        except TypeError as e:
            err = f"bad args: {e}"
            parts.append(f"## op{i} — {tool}({argstr}) — {err}")
            results.append(schemas.BatchOpResult(index=i, tool=tool, ok=False, error=err))
            continue
        except Exception as e:  # one bad op shouldn't sink the batch
            err = f"error: {type(e).__name__}: {e}"
            parts.append(f"## op{i} — {tool}({argstr}) — {err}")
            results.append(schemas.BatchOpResult(index=i, tool=tool, ok=False, error=err))
            continue
        out = sub.content[0].text if sub.content and isinstance(sub.content[0], TextContent) else ""
        out, n = _dedup_code_blocks(out, seen)
        collapsed += n
        parts.append(f"## op{i} — {tool}({argstr})\n{out}")
        sub_struct = sub.structuredContent
        results.append(
            schemas.BatchOpResult(
                index=i, tool=tool, ok=bool((sub_struct or {}).get("ok", True)), result=sub_struct
            )
        )

    footer = f"\n---\n_batch: {len(ops)} ops in 1 round-trip"
    if collapsed:
        footer += f"; {collapsed} duplicate code block(s) collapsed"
    footer += "._"
    md = "\n\n".join(parts) + footer
    return _result(
        md,
        schemas.BatchOutput(
            tool="batch",
            ok=True,
            markdown=md,
            op_count=len(ops),
            collapsed_code_blocks=collapsed,
            results=results,
        ),
    )


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
