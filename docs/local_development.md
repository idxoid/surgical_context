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
| `data/history/` | planned SQLite local history | No |
| `logs/neo4j/` | Neo4j logs | No |
| `logs/sidecar/` | sidecar logs | No |

The graph provider stores topology and metadata only. Source code stays on the filesystem. History persistence should remain metadata-first until the storage policy explicitly allows raw prompt text, response text, or source snippets.

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
- code indexing works against the fast default slice: `sidecar/context`
- docs indexing works against the fast default fixture: `tests/fixtures/smoke_docs`
- unified search returns a valid response
- `/ask` returns context and trace metadata
- `/impact` responds for the smoke symbol
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

Local default:

```dotenv
MODEL_PREFERENCE=auto
OLLAMA_MODEL=llama3
```

Optional cloud model routing:

```dotenv
ANTHROPIC_API_KEY=
ANTHROPIC_MODEL=
```

Cloud keys are not required for the local product. If no cloud key is configured, the sidecar should stay on the local/Ollama path or degrade clearly.
