#!/usr/bin/env bash
# Single workspace-level sync: ONE graphify update over ~/dathund/ + Obsidian exporters.
# Wired into ~/dathund/.claude/settings.local.json PostToolUse(git commit*).
# Lower levels (dathund/, dathund_paid/) MUST NOT run `graphify update .` themselves —
# they share ~/dathund/graphify-out/ via symlinks; a partial scan from a sub-repo
# would clobber the merged graph with incomplete data.

set -uo pipefail

# Find workspace root: prefer DATHUND_WORKSPACE, fall back to script's grandparent
# (graphify-obsidian-repo/scripts/sync_all.sh → ../../).
ENV_FILE_DEFAULT="${HOME}/dathund/dathund/scripts/export_dathund_workspace_env.sh"
if [[ -f "$ENV_FILE_DEFAULT" ]]; then
  # shellcheck disable=SC1090
  . "$ENV_FILE_DEFAULT"
fi
WORKSPACE="${DATHUND_WORKSPACE:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
LOG_DIR="${DATHUND_LOG_DIR:-$WORKSPACE/.dathund/logs}"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/sync_all.log"
LEGACY_LOG="$(dirname "${BASH_SOURCE[0]}")/../.sync_all.log"
ln -sfn "$LOG" "$LEGACY_LOG" 2>/dev/null || true

ts() { date -Is; }
log() { echo "[$(ts)] $*" | tee -a "$LOG"; }
run() { log "+ $*"; "$@" >>"$LOG" 2>&1; local rc=$?; log "  exit=$rc"; return $rc; }

log "=== sync_all start (WORKSPACE=$WORKSPACE) ==="

if ! command -v graphify >/dev/null 2>&1; then
  log "graphify CLI not on PATH — skipping graph update"
else
  run graphify update "$WORKSPACE" || log "graphify update failed (rc above)"
fi

EXPORT_DIR="$(dirname "${BASH_SOURCE[0]}")"
for sub in sync_cursor_obsidian.sh sync_codex_obsidian.sh sync_claude_obsidian.sh; do
  if [[ -x "$EXPORT_DIR/$sub" ]]; then
    run bash "$EXPORT_DIR/$sub" || log "$sub failed (non-fatal)"
  fi
done

log "=== sync_all done ==="
