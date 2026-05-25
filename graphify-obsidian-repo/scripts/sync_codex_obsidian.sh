#!/usr/bin/env bash
# Codex (VS Code extension) sessions → Obsidian: codex_to_obsidian.py → decisions extraction.
# Same env naming as sync_cursor_obsidian.sh / sync_claude_obsidian.sh.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT_DIR="${OBSIDIAN_SCRIPT_DIR:-$HOME/scripts}"
VAULT_DIR="${VAULT_DIR:-$HOME/vault}"
PROJECT="${OBSIDIAN_VAULT_PROJECT:-surgical_context}"
LOG="${LOG:-$SCRIPT_DIR/sync_codex.log}"
CODEX_DIR="${CODEX_DIR:-$HOME/.codex}"

mkdir -p "$(dirname "$LOG")"
mkdir -p "$CODEX_DIR/sessions"

{
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] Codex→Obsidian sync started (vault=$VAULT_DIR project=$PROJECT)"

  echo "[$(date '+%Y-%m-%d %H:%M:%S')] Processing Codex sessions..."
  python3 "$SCRIPT_DIR/codex_to_obsidian.py" \
    --codex-dir "$CODEX_DIR" \
    --vault-dir "$VAULT_DIR" \
    --project "$PROJECT" 2>&1 || echo "[$(date '+%Y-%m-%d %H:%M:%S')] ERROR in codex_to_obsidian" >&2

  echo "[$(date '+%Y-%m-%d %H:%M:%S')] Extracting decisions from Codex chats..."
  python3 "$SCRIPT_DIR/extract_decisions_from_codex.py" \
    --vault-dir "$VAULT_DIR" \
    --project "$PROJECT" 2>&1 || echo "[$(date '+%Y-%m-%d %H:%M:%S')] ERROR in decision extraction" >&2

  echo "[$(date '+%Y-%m-%d %H:%M:%S')] Codex→Obsidian sync finished"
} >>"$LOG" 2>&1

echo "Log: $LOG"
