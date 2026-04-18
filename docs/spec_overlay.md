# In-Memory Overlay — Spec

## Overview

`sidecar/overlay.py` — holds unsaved file content in memory and re-parses symbols on demand. Enables the arbitrator to answer questions about code the user is currently editing, before it hits disk.

---

## Class: InMemoryOverlay

Process-level singleton in `sidecar/main.py`.

### State

```python
_files: dict[str, str]          # file_path → raw content
_extractor: SymbolExtractor      # tree-sitter extractor (auto-detects language per file)
```

### Methods

#### update(file_path: str, content: str)
Store or replace content for a file. Called on every keypress (`POST /overlay`).

#### clear(file_path: str)
Remove a file from the overlay. Called on file save or editor close (`DELETE /overlay`). No-op if path not present.

#### has(file_path: str) → bool
Returns `True` if this file has a dirty version in memory.

#### read_lines(file_path: str, start: int, end: int) → str
Returns lines `start..end` (1-indexed, inclusive) from the in-memory content. Used by `ContextArbitrator._read_code()`.

#### get_symbols(file_path: str) → dict[str, tuple[int, int]]
Re-parses the in-memory content via tree-sitter. Returns `{name: (start_line, end_line)}` for all symbols found. Used by `POST /overlay` response to tell VS Code what symbols exist in the dirty file.

Language is auto-detected from `file_path` extension — supports all languages registered in the adapter registry (Python, TypeScript, etc.).

---

## Lifetime

TTL = editor session. Content is cleared when:
- VS Code sends `DELETE /overlay` on file save
- VS Code sends `DELETE /overlay` on editor tab close

There is no automatic expiry timer.

---

## Limitations (current)

- No symbol diff tracking — `POST /overlay` always returns the full symbol set, not just added/removed since last update.

---

## Planned Extensions

- Return symbol diff (added/removed names) on `POST /overlay` to support VS Code outline updates
- Phase 3.5: support `IMPORTS` and `DEPENDS_ON` edge extraction in overlay (currently only `CALLS` edges)
