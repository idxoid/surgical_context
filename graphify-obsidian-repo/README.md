# graphify-obsidian-repo

This directory is prepared as a standalone repository for Graphify/Obsidian
tooling that previously lived in the main `surgical_context` repo.

Included:

- `scripts/cursor_agent_transcripts_to_obsidian.py`
- `scripts/sync_cursor_obsidian.sh`
- `.graphifyignore`

To publish as a separate git repository:

1. Split history for this directory:
   `git subtree split --prefix=graphify-obsidian-repo -b graphify-obsidian-split`
2. Push to a new remote repository from that branch.
3. (Optional) add it back here as a submodule.
