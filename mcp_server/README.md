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

- **`ask_code(question, token_budget=6000, workspace=None, roles=None, render="full")`**
  — natural-language question → ranked, graph-expanded code bundles for the host
  model to reason over. Returns *context, not an answer* (LLM-free retrieval).
  `roles=[...]` overrides the embedding intent-classifier (see `list_roles`).
  `render="names"` = census view: one line per symbol (file :: name + role/depth,
  no code) with eviction disabled, so far more coupling symbols/files surface per
  token (~−40% tokens, ~30% more symbols on coupling questions) — use it to map
  structure/blast surface, `"full"` to read code.
- **`impact(symbol, file_path=None, max_depth=3, workspace=None)`** — downstream
  blast radius of a change to `symbol` (reverse callers, structural
  API/inheritance, AFFECTS closure). Committed index surface only (no overlay).
- **`list_workspaces()`** — indexed repos you can target via `workspace=`.

### Navigation & read tools (P0/P1)

Thin, mostly Neo4j-only wrappers over the same read path — precise locate/read/
navigate primitives the budget-trimmed `ask_code` can't guarantee. Only
`search_code` pulls the embedding model; everything else is graph + filesystem.

- **`read_symbol(name, file_path=None, workspace=None)`** — exact, untrimmed
  on-disk source of one symbol (resolve uid → Neo4j line span → sandboxed disk
  read). Use when you must READ specific code precisely.
- **`search_code(query, limit=10, kind="symbol", workspace=None)`** — cheap
  vector search (no graph expansion). `kind="symbol"` reuses the axis seed
  recall (`find_seeds_by_vector`); `kind="doc"` searches doc chunks. LOCATE fast.
- **`callers(symbol, file_path=None, max_hops=1, limit=50, workspace=None)`** —
  incoming CALLS edges ("who calls X"); cheaper/narrower than `impact`.
- **`callees(symbol, file_path=None, max_hops=1, limit=50, workspace=None)`** —
  outgoing CALLS edges ("what X calls").
- **`find_definition(name, limit=20, workspace=None)`** — every symbol named
  `name` with file:line (go-to-definition, incl. collisions). Disambiguate
  before `read_symbol`/`callers`.
- **`file_outline(file_path, limit=400, workspace=None)`** — symbol map of one
  file (name, kind, start line; no bodies).
- **`path(symbol_a, symbol_b, file_a=None, file_b=None, max_hops=6, workspace=None)`**
  — shortest connecting path across ALL edge types (calls, inheritance, API,
  type refs…); surfaces indirect coupling `callers`/`callees` miss.
- **`docs_for(symbol, file_path=None, limit=20, workspace=None)`** — doc chunks
  anchored to `symbol` via DocAnchor `COVERS` edges (anchor type, confidence,
  source files).
- **`explain(concept, file_path=None, workspace=None)`** — concept card: resolve
  `concept` to a symbol (exact name, else nearest by embedding), then its
  signature + one-hop connections grouped by relationship (calls/called by, uses
  type, instantiates, decorated by, inherits…) + documentation. The graphify
  `explain` analog. AFFECTS is excluded (too broad — use `impact`).

### Uncommitted-edit tools (P2 — overlay)

Let the host LLM check the blast radius / read code of a change it has NOT yet
committed, mirroring the HTTP server's live-buffer augmentation. Backed by an
in-process `InMemoryOverlay` (no DB, no model).

- **`set_overlay(file_path, content, workspace=None)`** — stash the edited full
  file content. Afterwards `impact` adds degraded `overlay_caller` rows (and
  resolves brand-new symbols), and `ask_code` reads the buffer over indexed code.
- **`clear_overlay(file_path, workspace=None)`** — drop the buffer; `impact`/
  `ask_code` return to the committed index.

`impact` flags overlay-augmented results as `degraded` and tags overlay rows
`[overlay]`. Typical loop: edit → `set_overlay` → `impact(symbol)` to see what
your change breaks → `clear_overlay`.

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
- **Docs** (`search_code(kind="doc")`, `docs_for`) need the docstring/anchor
  pass (fast-pipeline Stage 7 `ingest_symbol_docstrings` → `DocAnchor`-`COVERS`
  edges over in-code docstrings / out-of-function doc comments). A workspace
  indexed without that stage (e.g. the current dogfood axis index, 0 anchors)
  yields empty — the queries are correct, there's just no data until reindex.
- **Write/verify loop**: overlay-aware `impact`/`ask` over an uncommitted buffer
  is wired (`set_overlay`/`clear_overlay`). The overlay is per-server-process and
  single-user (`user_id="mcp"`) — it does not share buffers with the HTTP server.
  Persisting an edit still goes through the normal save (HTTP `/overlay`) → git
  commit (post-commit `graphify`/index) path; there is no `reindex_file` tool
  (redundant with that flow).
- **Concept explain**: `explain` is symbol-centric (resolve → connections +
  docs). Free-text concepts resolve to the nearest symbol by embedding, which
  can be approximate (flagged "nearest match"); a true abstract-concept graph
  (cross-file doc-anchor concept links) is out of scope.
