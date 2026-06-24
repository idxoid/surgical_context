# surgical_context MCP server

Exposes surgical_context's **axis retrieval** to any LLM chat (Claude Code,
Cursor, Codex, Claude Desktop) over MCP stdio — the same way `/graphify query`
feeds a budgeted context block into the calling model.

It is a thin, **in-process** wrapper over
`context_engine.axis.pipeline.run_axis_retrieval` — the exact read path the
`/ask/axis` HTTP route runs and the `QA/axis_benchmark` harness replays. No
uvicorn: the server holds one long-lived Neo4j + LanceDB handle and calls the
pipeline directly.

## Tools

- **`ask_code(question, token_budget=6000, workspace=None)`** — natural-language
  question → ranked, graph-expanded code bundles for the host model to reason
  over. Returns *context, not an answer* (LLM-free retrieval).
- **`impact(symbol, file_path=None, max_depth=3, workspace=None)`** — downstream
  blast radius of a change to `symbol` (reverse callers, structural
  API/inheritance, AFFECTS closure). Committed index surface only (no overlay).
- **`list_workspaces()`** — indexed repos you can target via `workspace=`.

`workspace` is an optional base id (e.g. `qa_repo/django@main`); the
`+axis_python_v1` index suffix is added automatically. Omit it to use
`SURGICAL_CONTEXT_WORKSPACE`.

## Prerequisites

1. **Backends up**: Neo4j + LanceDB (the repo's `docker-compose.yml`).
2. **Repo indexed** under the `axis_python_v1` profile for the target
   workspace. The dogfood repo is indexed as `qa_repo/surgical_context@main`.
3. The sibling `context_engine` package (this repo) importable — run from the
   repo root `.venv`.

## Install

```bash
# from the repo root, into the repo venv:
.venv/bin/pip install -r mcp_server/requirements.txt
```

The pins in that file keep `mcp` from upgrading `starlette`/`sse-starlette` past
what the core server's `fastapi==0.110.0` allows (a naive `pip install mcp`
breaks the FastAPI server). The MCP server uses the stdio transport only.

## Wire into Claude Code

Copy `claude_mcp.example.json` to `.mcp.json` at the repo root (or merge into an
existing one), adjusting absolute paths if the repo moved:

```json
{
  "mcpServers": {
    "surgical-context": {
      "command": "/home/idxoid/surgical_context/.venv/bin/python",
      "args": ["-m", "surgical_context_mcp"],
      "env": {
        "PYTHONPATH": "/home/idxoid/surgical_context:/home/idxoid/surgical_context/mcp_server",
        "SURGICAL_CONTEXT_WORKSPACE": "qa_repo/surgical_context@main"
      }
    }
  }
}
```

`PYTHONPATH` makes both `context_engine` (repo root) and `surgical_context_mcp`
(this folder) importable without an editable install. `SURGICAL_CONTEXT_WORKSPACE`
is the client-facing base workspace id; the `+axis_python_v1` index suffix is
added automatically.

Restart Claude Code, then ask it something about the code — it should call
`ask_code`.

## Smoke test (without a chat host)

```bash
PYTHONPATH=..:. ../.venv/bin/python -c \
  "from surgical_context_mcp.engine import AxisEngine; \
   from surgical_context_mcp.config import resolve_workspace_id; \
   r = AxisEngine().ask('how does workspace scoping work', resolve_workspace_id()); \
   print(r.intent); print(r.files); print(r.text[:800])"
```

## Limitations / next steps

- **Workspace** defaults to `SURGICAL_CONTEXT_WORKSPACE`; callers can target any
  indexed repo via `workspace=` (discover with `list_workspaces`). Auto-resolve
  from the chat's cwd (graphify's `graphify-out/`-detect analog) is still TODO.
- **Python-only** (`axis_python_v1` is `language_scope="python"`).
- **Caller-supplied intent** (skip the embedding role-classifier, let the host
  model pick roles) — proposed; see notes. `search_code` (cheap,
  `with_context=False`) is another easy follow-on.
