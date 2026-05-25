#!/usr/bin/env bash
# Claude Code → Obsidian: extract chats → claude_to_obsidian.py → decisions extraction.
# Processors live in OBSIDIAN_SCRIPT_DIR (default ~/scripts). Same env naming as sync_cursor_obsidian.sh.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT_DIR="${OBSIDIAN_SCRIPT_DIR:-$HOME/scripts}"
VAULT_DIR="${VAULT_DIR:-$HOME/vault}"
PROJECT="${OBSIDIAN_VAULT_PROJECT:-surgical_context}"
LOG="${LOG:-$SCRIPT_DIR/sync.log}"
EXPORT_DIR="${CLAUDE_EXPORT_DIR:-$HOME/claude-exports}"

mkdir -p "$(dirname "$LOG")"
mkdir -p "$EXPORT_DIR/code" "$EXPORT_DIR/web"

{
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] Claude→Obsidian sync started (vault=$VAULT_DIR project=$PROJECT)"

  if command -v claude-extract &>/dev/null; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Extracting Claude Code chats..."
    claude-extract --all --output "$EXPORT_DIR/code" 2>&1 || true
  else
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] WARNING: claude-extract not found; skip export step"
  fi

  echo "[$(date '+%Y-%m-%d %H:%M:%S')] Processing chats → markdown..."
  python3 "$SCRIPT_DIR/claude_to_obsidian.py" \
    --export-dir "$EXPORT_DIR" \
    --vault-dir "$VAULT_DIR" \
    --project "$PROJECT" \
    --move 2>&1 || echo "[$(date '+%Y-%m-%d %H:%M:%S')] ERROR in claude_to_obsidian" >&2

  echo "[$(date '+%Y-%m-%d %H:%M:%S')] Extracting decisions from Claude Code chats..."
  python3 "$SCRIPT_DIR/extract_decisions_from_claude_code.py" \
    --vault-dir "$VAULT_DIR" \
    --project "$PROJECT" 2>&1 || echo "[$(date '+%Y-%m-%d %H:%M:%S')] ERROR in decision extraction" >&2

  echo "[$(date '+%Y-%m-%d %H:%M:%S')] Claude→Obsidian sync finished"
} >>"$LOG" 2>&1

echo "Log: $LOG"
