#!/usr/bin/env bash
# Export Cursor agent transcripts (Composer) → Obsidian vault chats/cursor/
# Related: sync_codex_obsidian.sh, sync_claude_obsidian.sh (also in this directory)

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$ROOT/scripts/cursor_agent_transcripts_to_obsidian.py"
VAULT_DIR="${VAULT_DIR:-$HOME/vault}"
LOG="${LOG:-$HOME/scripts/sync_cursor.log}"

mkdir -p "$(dirname "$LOG")"
{
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] Cursor transcript export started"
  # Optional: limit to repos whose Cursor slug contains this substring
  # export CURSOR_PROJECT_SUBSTRING="surgical-context"
  EXTRA=()
  if [[ -n "${CURSOR_PROJECT_SUBSTRING:-}" ]]; then
    EXTRA=(--project-substring "$CURSOR_PROJECT_SUBSTRING")
  fi
  python3 "$SCRIPT" --vault-dir "$VAULT_DIR" "${EXTRA[@]}"
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] Cursor transcript export finished"
} >>"$LOG" 2>&1

echo "Log: $LOG"
