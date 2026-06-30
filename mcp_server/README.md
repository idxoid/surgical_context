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

- **`ask_code(question, token_budget=4000, workspace=None, roles=None, render="full")`**
  — natural-language question → ranked, graph-expanded code bundles for the host
  model to reason over. Returns *context, not an answer* (LLM-free retrieval).
  `roles=[...]` overrides the embedding intent-classifier (see `list_roles`).
  `render="names"` = census view: one line per symbol (file :: name + role/depth,
  no code) with eviction disabled, so far more coupling symbols/files surface per
  token (~−40% tokens, ~30% more symbols on coupling questions) — use it to map
  structure/blast surface, `"full"` to read code.
- **`investigate(question, depth="full", token_budget=4000, workspace=None)`**
  — one planned retrieval round-trip: intent → ranked context → downstream
  blast surface of the top seeds. Use `depth="lean"` for a cheaper names-only
  context plus a smaller blast pass.
- **`impact(symbol, file_path=None, max_depth=3, workspace=None)`** — downstream
  blast radius of a change to `symbol` (reverse callers, structural
  API/inheritance, AFFECTS closure). The committed index is authoritative;
  dirty buffers pushed through `set_overlay` add degraded overlay rows and can
  resolve brand-new overlay-only symbols.
- **`list_workspaces()`** — indexed repos you can target via `workspace=`.
- **`list_files(workspace=None, path_prefix=None, with_counts=False, limit=400)`**
  — indexed files of a workspace; the navigation entry point
  (`list_workspaces → list_files → file_outline → read_symbol`). The only way to
  enumerate a NON-local workspace the host's Glob can't see; for the local repo
  the host's Glob is usually cheaper.
- **`list_roles()`** / **`classify_intent(question, top_roles=5)`** — inspect
  the structural role vocabulary and preview the intent classifier before
  overriding `ask_code(roles=[...])`.

### Navigation & read tools (P0/P1)

Thin, mostly Neo4j-only wrappers over the same read path — precise locate/read/
navigate primitives the budget-trimmed `ask_code` can't guarantee. `search_code`
pulls the embedding model; `explain` does too only when an exact symbol-name
lookup misses and it falls back to vector search. The other tools in this
section are graph + filesystem.

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
  file (name, kind, start line; no bodies). If a short suffix such as
  `__init__.py` matches several indexed files, the tool returns an ambiguity
  response with candidate paths instead of mixing symbols from different files.
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

### Batching (cost)

- **`batch(ops)`** — run several read/nav ops in ONE call, de-duplicating
  repeated code across results. `ops=[{"tool": <name>, ...args}]` over
  read_symbol/callers/callees/impact/file_outline/find_definition/search_code/
  docs_for/path/classify_intent/list_files.

Why it matters: each tool round-trip re-bills the whole conversation context
(cache_read), measured as the dominant token cost — so at large context FEWER
rich calls beat MANY granular ones. For a multi-step question, one `batch`
collapses N round-trips into 1 and emits each symbol's body once. (`ask_code`'s
docstring carries the same "batch, don't drip" note.)

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

## Wire into Codex (project-scoped)

Codex layers config: `$CODEX_HOME/config.toml` (the user layer, default
`~/.codex/config.toml`), then — only when the user layer marks the project root
`trusted` — the **project layer** at `<repo>/.codex/config.toml`. So scope the
server to this repo in two steps:

1. In `~/.codex/config.toml`, trust the repo so its project layer loads:

   ```toml
   [projects."/home/idxoid/surgical_context"]
   trust_level = "trusted"
   ```

2. Copy `codex_config.example.toml` to `<repo>/.codex/config.toml` (adjust the
   absolute paths if the repo moved). It carries the `[mcp_servers.surgical-context]`
   block — same `command`/`args`/`PYTHONPATH`/`SURGICAL_CONTEXT_WORKSPACE` as the
   Claude wiring, plus a `tool_timeout_sec` cushion for the first call's embedding
   cold-start.

(Or skip the project layer and paste the `[mcp_servers.*]` blocks straight into
`~/.codex/config.toml` — same effect, just not scoped to the repo.)

The server also ships **MCP `instructions`** (set on the `FastMCP` constructor in
`server.py`) — a server-level orientation, sent to the host at initialize, on
what the toolset is and which tool to reach for; it complements the per-tool
docstrings/`outputSchema`.

## Smoke test (without a chat host)

```bash
PYTHONPATH=..:. ../.venv/bin/python -c \
  "from surgical_context_mcp.engine import AxisEngine; \
   from surgical_context_mcp.config import resolve_workspace_id; \
   r = AxisEngine().ask('how does workspace scoping work', resolve_workspace_id()); \
   print(r.intent); print(r.files); print(r.text[:800])"
```

## Type-checking

Run mypy **from the repo root** so it picks up the shared `[tool.mypy]` config
(`pyproject.toml`) — which adds `mcp_server` as a package base via `mypy_path`
so `surgical_context_mcp` resolves, and treats `context_engine` as a typed
package (it ships a `py.typed` marker):

```bash
# from the repo root, in the repo venv — this is what CI runs:
PYTHONPATH=. mypy context_engine/ mcp_server/surgical_context_mcp tests/
```

The `PYTHONPATH=..:.` shown elsewhere is only for **running** the server
(making both packages importable without an editable install); it is not the
right context for mypy — invoke mypy from the repo root as above.

## Limitations / next steps

- **Workspace** defaults to `SURGICAL_CONTEXT_WORKSPACE`; callers can target any
  indexed repo via `workspace=` (discover with `list_workspaces`). Auto-resolve
  from the chat's cwd (graphify's `graphify-out/`-detect analog) is still TODO.
- **Python-only** (`axis_python_v1` is `language_scope="python"`).
- **Embedding cold-start** applies to `ask_code`, `investigate`,
  `classify_intent`, `search_code`, and `explain` when exact resolution misses.
  Pure graph/navigation calls (`list_workspaces`, `list_files`, `read_symbol`,
  `callers`, `callees`, `impact`, `find_definition`, `file_outline`, `path`,
  `docs_for`) stay on the Neo4j/filesystem path.
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
- **Bounds**: MCP tools clamp user-facing integer knobs before they reach Neo4j
  or LanceDB (`token_budget`, `limit`, `top_roles`, `max_hops`). `batch` accepts
  at most 20 sub-operations, and `set_overlay` rejects buffers over 1 MB.
- **Concept explain**: `explain` is symbol-centric (resolve → connections +
  docs). Free-text concepts resolve to the nearest symbol by embedding, which
  can be approximate (flagged "nearest match"); a true abstract-concept graph
  (cross-file doc-anchor concept links) is out of scope.
