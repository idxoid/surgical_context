#!/bin/bash
set -e

# Clean Graphify & Obsidian rebuild with QA/repos exclusion check

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "🧹 Cleaning graphify-out, cache, and vault artifacts..."

# 1. Clean graphify output and cache
rm -rf graphify-out/
rm -rf ~/.cache/graphify/  2>/dev/null || true
rm -rf ~/.graphify/        2>/dev/null || true
echo "  ✓ Removed graphify-out/"
echo "  ✓ Cleaned graphify cache (~/.cache/graphify, ~/.graphify)"

# 2. Clean vault chats (keep architecture/decisions/data/logs)
rm -rf ~/vault/surgical_context/chats/
echo "  ✓ Removed vault chats/"

# 3. Verify .graphifyignore exists
if [ ! -f .graphifyignore ]; then
    echo "❌ ERROR: .graphifyignore not found!"
    exit 1
fi
echo "  ✓ .graphifyignore present"

# 4. Rebuild graphify from scratch
echo ""
echo "🔨 Rebuilding graphify (clean)..."
graphify update . 2>&1 | tail -10

# 5. Verify QA/repos NOT in graph
echo ""
echo "✅ Verifying QA/repos exclusion..."

# Check for QA source files in graph nodes
QA_COUNT_GRAPH=$(jq '[.nodes[] | select(.source_file | contains("QA/"))] | length' graphify-out/graph.json 2>/dev/null || echo 0)
if [ "$QA_COUNT_GRAPH" -gt 0 ]; then
    echo "❌ ERROR: Found $QA_COUNT_GRAPH nodes from QA/ in graph.json!"
    exit 1
fi
echo "  ✓ No QA/ nodes in graph.json ($QA_COUNT_GRAPH found)"

# Check for QA source files in AST cache
QA_COUNT_CACHE=$(find graphify-out/cache/ast -type f -exec jq '.nodes[] | select(.source_file | contains("QA/"))' {} \; 2>/dev/null | wc -l)
if [ "$QA_COUNT_CACHE" -gt 0 ]; then
    echo "⚠️  WARNING: Found $QA_COUNT_CACHE QA/ entries in AST cache (orphaned)"
    echo "  → This is safe (cache entries don't affect graph.json), but can be cleaned:"
    echo "  → rm -rf graphify-out/cache/"
else
    echo "  ✓ No QA/ entries in AST cache"
fi

# 6. Report stats
echo ""
echo "📊 Graph Statistics:"
jq '.stats // "N/A"' graphify-out/GRAPH_REPORT.md 2>/dev/null || \
  jq '.[] | select(.communities) | "\(.nodes) nodes, \(.edges) edges, \(.communities) communities"' graphify-out/graph.json 2>/dev/null || \
  grep -E "nodes|edges|communities" graphify-out/GRAPH_REPORT.md | head -1

# 7. Rebuild vault (if codex/code export scripts exist)
echo ""
echo "🗂️  Rebuilding vault chats..."

if [ -f scripts/cursor_agent_transcripts_to_obsidian.py ]; then
    python3 scripts/cursor_agent_transcripts_to_obsidian.py --vault-dir ~/vault --project-substring surgical-context 2>&1 | tail -3 || echo "  (Cursor export skipped)"
fi

if [ -f scripts/codex_to_obsidian.py ]; then
    python3 scripts/codex_to_obsidian.py --vault-dir ~/vault 2>&1 | tail -3 || echo "  (Codex export skipped)"
fi

echo ""
echo "✨ Clean rebuild complete!"
