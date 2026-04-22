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
