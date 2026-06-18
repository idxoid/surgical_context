# Local Development

This is the local-first setup path for the open-source developer product. It starts from one machine, one repo, local Neo4j, local LanceDB, local history paths, and the VS Code extension dev host.

## Prerequisites

- Python 3.12 with project dependencies installed.
- Docker with Docker Compose.
- Node.js and npm.
- VS Code with the `code` CLI on `PATH`.
- Ollama is optional for local LLM answers. Cloud model keys are optional.

## One-Time Bootstrap

From the repository root:

```bash
python scripts/local_dev.py doctor
python scripts/local_dev.py bootstrap
```

`bootstrap` does the local setup work:

- creates ignored local paths under `data/`, `logs/`, `import/`, and `plugins/`
- creates `.env` from `.env.example` when `.env` does not exist
- starts local Neo4j with `docker compose up -d neo4j`
- runs `npm install` in `extension/` when needed
- runs `npm run compile` for the extension
- prints the next sidecar and VS Code commands

Use `--dry-run` to inspect commands without changing anything:

```bash
python scripts/local_dev.py bootstrap --dry-run
```

## Daily Run

Terminal 1:

```bash
python scripts/local_dev.py sidecar --reload
```

Terminal 2:

```bash
python scripts/local_dev.py code
```

Or run the sidecar and launch VS Code from one terminal:

```bash
python scripts/local_dev.py up --launch-code --reload
```

Stop the sidecar with `Ctrl+C`.

## Local Storage

| Path | Purpose | Tracked |
|---|---|---|
| `data/neo4j/` | Neo4j Docker data | No |
| `data/lancedb/` | LanceDB vector tables | No |
| `data/history/` | SQLite local history | No |
| `logs/neo4j/` | Neo4j logs | No |
| `logs/sidecar/` | sidecar logs | No |

The graph provider stores topology and metadata only. Source code stays on the filesystem. History persistence should remain metadata-first until the storage policy explicitly allows raw prompt text, response text, or source snippets.

## Workspace Scope

The sidecar uses `DEFAULT_WORKSPACE_ID=local/surgical_context@main` when a request
does not include `X-Workspace`. The VS Code extension leaves
`surgicalContext.workspaceId` blank by default; in normal local development it
derives the header from the first open workspace folder and active Git branch:

```text
local/<workspace-folder-name>@<git-branch-or-short-sha>
```

Set `surgicalContext.workspaceId` only when you need to force a specific scope
such as `acme/surgical_context@review-branch`. Leaving it blank avoids the old
`local/default@main` mismatch and keeps extension requests aligned with the
sidecar's workspace model.

### Project root and path sandboxing

The sidecar only reads or indexes files **under the registered project root** for
that workspace. The root is set when you run `POST /index` (manifest field
`project_path`). Until then, `/index/file` and `/ask` with `file_path` return
`400` (“no registered project root”).

| Situation | HTTP |
|---|---|
| Path outside the indexed project tree | `403` |
| Incremental index / file fallback before first full index | `400` |

Relative paths in API requests are resolved under the project root. The smoke
test and `local_dev.py` index `sidecar/axis` (or the full repo in
`--full-repo` mode) so the default workspace has a registered root.

With `AUTH_REQUIRED=false` (local default), this prevents other local processes
from using the sidecar as a generic file reader. See
[spec_sidecar_api.md](spec_sidecar_api.md#filesystem-path-sandboxing).

History controls are environment-driven for the local sidecar:

```dotenv
HISTORY_MODE=local
HISTORY_DB_PATH=./data/history/surgical_context.sqlite3
HISTORY_RETENTION_DAYS=
```

`HISTORY_MODE=disabled` makes `/history/ask` a no-op and returns empty history lists. `HISTORY_MODE=ephemeral` uses a temporary SQLite database for the current sidecar process only. Set `HISTORY_RETENTION_DAYS` to a non-negative integer to prune conversations older than that many days.

## Useful Checks

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/status/cloud
curl http://127.0.0.1:8000/metrics
```

## Docker Compose Troubleshooting

If Docker reports that `surgical-network` has an incorrect Compose label, it is a stale network from an older local setup. Remove it once:

```bash
docker network rm surgical-network
```

Then rerun:

```bash
python scripts/local_dev.py up
```

If Neo4j is already running and you only want the sidecar/extension flow:

```bash
python scripts/local_dev.py up --skip-storage
```

If Docker reports permission denied for `/var/run/docker.sock`, start Docker or run the command from a user that is allowed to access the Docker daemon.

## Local Smoke Test

After `bootstrap`, use:

```bash
python scripts/local_dev.py smoke
```

The smoke test checks the local daily-driver path:

- extension bundles exist
- local data/log paths exist
- local Neo4j starts through Docker Compose unless `--skip-storage` is passed
- sidecar `/health` responds; if no sidecar is running, the smoke test starts a temporary sidecar and stops it at the end
- graph provider status responds
- code indexing works against the fast default slice: `sidecar/axis`
- docs indexing works against `docs/local_development.md` by default (full `docs/` with `--full-repo`)
- unified search returns a valid response
- `/ask` returns context and trace metadata
- `/impact` responds for the smoke symbol (`run_axis_retrieval` by default)
- `/metrics` returns dashboard-ready sidecar metrics

The default smoke test is intentionally small. Use the full repo mode only when you want a heavier verification pass:

```bash
python scripts/local_dev.py smoke --full-repo
```

If the repo is already indexed and you only want to check retrieval/API health:

```bash
python scripts/local_dev.py smoke --skip-index --skip-docs
```

If Neo4j is already running and you do not want the smoke test to manage Docker:

```bash
python scripts/local_dev.py smoke --skip-storage
```

If you specifically want to require an already running sidecar:

```bash
python scripts/local_dev.py smoke --no-start-sidecar
```

Inside VS Code, use:

- `Surgical Context: Index Workspace`
- `Surgical Context: Index Current File`
- `Surgical Context: Open Settings`
- `Surgical Context: Move to Secondary Side Bar`

## Optional Model Configuration

Local default (prompts stay on Ollama; `ANTHROPIC_API_KEY` alone does not enable cloud):

```dotenv
MODEL_PREFERENCE=ollama
OLLAMA_MODEL=llama3
ALLOW_CLOUD_LLM=false
```

Optional cloud model routing (explicit opt-in):

```dotenv
ALLOW_CLOUD_LLM=true
MODEL_PREFERENCE=auto
ANTHROPIC_API_KEY=sk-ant-...
# Optional; default is claude-sonnet-4-6 (do not use retired claude-sonnet-4-20250514)
ANTHROPIC_MODEL=
```

With `ALLOW_CLOUD_LLM=false`, `MODEL_PREFERENCE=auto` never sends assembled context to Anthropic even when a key is present. Set `MODEL_PREFERENCE=claude` only together with `ALLOW_CLOUD_LLM=true`.

**Safety defaults (sidecar):** after `POST /index`, path sandboxing applies to API and graph reads. Search `limit` is capped at 50; `/ask` `token_budget` at 32 000. Details: [spec_sidecar_api.md](spec_sidecar_api.md#filesystem-path-sandboxing) and [spec_sidecar_api.md](spec_sidecar_api.md#request-validation-bounds).
