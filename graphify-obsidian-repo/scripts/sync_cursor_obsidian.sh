#!/usr/bin/env bash
# Export Cursor agent transcripts → Obsidian vault <OBSIDIAN_VAULT_PROJECT>/chats/cursor/
# Same JSONL layout for all Cursor agent models; optional slug filter via CURSOR_PROJECT_SUBSTRING.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$ROOT/scripts/cursor_agent_transcripts_to_obsidian.py"
VAULT_DIR="${VAULT_DIR:-$HOME/vault}"
LOG="${LOG:-$HOME/scripts/sync_cursor.log}"

mkdir -p "$(dirname "$LOG")"

EXTRA=()
if [[ -n "${CURSOR_PROJECT_SUBSTRING:-}" ]]; then
  EXTRA+=(--project-substring "$CURSOR_PROJECT_SUBSTRING")
fi

# Optional: limit vault subfolder (e.g. dathund vs surgical_context). Python also reads OBSIDIAN_VAULT_PROJECT.
if [[ -n "${OBSIDIAN_VAULT_PROJECT:-}" ]]; then
  EXTRA+=(--vault-project "$OBSIDIAN_VAULT_PROJECT")
fi

{
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] Cursor transcript export started (vault=${VAULT_DIR})"
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] OBSIDIAN_VAULT_PROJECT=${OBSIDIAN_VAULT_PROJECT:-<default from env or surgical_context>}"
  python3 "$SCRIPT" --vault-dir "$VAULT_DIR" "${EXTRA[@]}"
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] Cursor transcript export finished"
} >>"$LOG" 2>&1

echo "Log: $LOG"
