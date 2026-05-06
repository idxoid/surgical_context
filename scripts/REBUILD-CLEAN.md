# rebuild-clean.sh — Clean Graphify & Obsidian Rebuild

One-command cleanup and fresh rebuild of graphify knowledge graph + vault exports, with QA/repos exclusion verification.

## Usage

```bash
bash scripts/rebuild-clean.sh
```

## What it does

1. **🧹 Cleanup Phase**
   - Removes `graphify-out/` (output artifacts)
   - Clears graphify cache directories (`~/.cache/graphify/`, `~/.graphify/`)
   - Removes vault chat exports (`~/vault/surgical_context/chats/`)
   - Verifies `.graphifyignore` file exists

2. **🔨 Rebuild Phase**
   - Runs `graphify update .` for fresh code extraction (210 files)
   - Generates new `graph.json` and `GRAPH_REPORT.md`
   - Respects `.graphifyignore` to exclude QA/repos

3. **✅ Verification Phase**
   - Checks that no QA/ source files made it into graph nodes
   - Scans AST cache for orphaned QA/ entries (warns if found, safe to ignore)
   - Reports final graph stats (nodes, edges, communities)

4. **🗂️ Vault Rebuild Phase**
   - Re-exports Cursor agent transcripts (if `scripts/cursor_agent_transcripts_to_obsidian.py` exists)
   - Re-exports Codex chats (if `scripts/codex_to_obsidian.py` exists)

## Why use this?

- ✅ **QA/repos isolation:** After changing `.graphifyignore`, ensures test repos don't pollute the graph
- ✅ **Cache issues:** Graphify's AST cache can linger if a directory was excluded after initial indexing
- ✅ **Fresh start:** Clean slate for graphify clusters and communities
- ✅ **Vault sync:** Rebuilds all chat exports in one shot

## Expected Output

```
🧹 Cleaning graphify-out, cache, and vault artifacts...
  ✓ Removed graphify-out/
  ✓ Cleaned graphify cache
  ✓ Removed vault chats/
  ✓ .graphifyignore present

🔨 Rebuilding graphify (clean)...
  AST extraction: 210/210 files (100%)
  Rebuilt: 2998 nodes, 5714 edges, 210 communities

✅ Verifying QA/repos exclusion...
  ✓ No QA/ nodes in graph.json (0 found)
  ✓ No QA/ entries in AST cache

📊 Graph Statistics:
  - 2998 nodes · 5714 edges · 204 communities detected

🗂️  Rebuilding vault chats...

✨ Clean rebuild complete!
```

## Exit codes

- `0` — Success, graph is clean and excludes QA/repos
- `1` — Fatal error: `.graphifyignore` missing OR QA/ nodes found in graph.json

## When to use this vs. `graphify update .`

| Scenario | Command |
|----------|---------|
| Modified source code in production modules | `graphify update .` |
| Changed `.graphifyignore` (add/remove exclusion) | `bash scripts/rebuild-clean.sh` |
| QA/repos accidentally indexed | `bash scripts/rebuild-clean.sh` |
| Debugging graph clustering/communities | `bash scripts/rebuild-clean.sh` |
| Vault exports are stale | `bash scripts/rebuild-clean.sh` |

## Files created/modified

- `graphify-out/graph.json` — Code dependency graph (~2998 nodes)
- `graphify-out/GRAPH_REPORT.md` — Graph analysis report
- `graphify-out/cache/ast/` — AST cache (for incremental updates)
- `~/vault/surgical_context/chats/` — Chat/transcript exports

## Performance

- Runtime: ~30-60 seconds (depends on system load and cache warmth)
- Graph size: 2998 nodes, 5714 edges (excludes QA/repos)
- No API calls required (AST-only extraction)
