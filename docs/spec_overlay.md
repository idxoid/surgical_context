# In-Memory Overlay — Spec


## Overview

`context_engine/overlay.py` — holds unsaved file content in memory and re-parses symbols on demand. Enables the axis context builder (`context_engine/axis/overlay_context.py`) to answer questions about code the user is currently editing, before it hits disk.

`POST /overlay` and `DELETE /overlay` validate `file_path` under the workspace project root before touching overlay state (see [spec_context_engine_api.md](spec_context_engine_api.md#filesystem-path-sandboxing)).

---

## Class: InMemoryOverlay

Process-level service owned by `SidecarState` (`context_engine/api/state.py`).

### State

```python
_files: dict[tuple[str, str, str], _OverlayEntry]
# key = (workspace_id, user_id, file_path)
# entry = content, dirty flag, monotonic updated_at
_extractor: SymbolExtractor
```

### Methods

#### update(file_path, content, workspace_id, user_id, dirty=True)
Store or replace content for one user/workspace/file scope. The extension sends
updates from `onDidChangeTextDocument` when overlay sync is enabled.

#### clear(file_path, workspace_id, user_id)
Remove a file from the overlay. Called on file save or editor close (`DELETE /overlay`). No-op if path not present.

#### has(file_path, workspace_id, user_id) → bool
Returns `True` if this file has a dirty version in memory.

#### read_lines(file_path, start, end, workspace_id, user_id) → str
Returns lines `start..end` (1-indexed, inclusive) from the in-memory content. Used by `overlay_context._overlay_code_for_symbol()` when assembling axis bundles.

#### get_symbols(file_path, workspace_id, user_id) → dict[str, tuple[int, int]]
Re-parses the in-memory content via tree-sitter. Returns `{name: (start_line, end_line)}` for all symbols found. Used by `POST /overlay` response to tell VS Code what symbols exist in the dirty file.

Language is auto-detected from `file_path` extension — supports all languages registered in the adapter registry (Python, TypeScript, etc.).

---

## Lifetime and bounds

Content is cleared when:
- VS Code sends `DELETE /overlay` on file save
- VS Code sends `DELETE /overlay` on editor tab close

The context_engine also evicts entries lazily during overlay access/update:

- `OVERLAY_MAX_ENTRIES` defaults to `256`; inserting beyond the cap evicts the least-recently-used entry.
- `OVERLAY_TTL_SECONDS` defaults to `86400`; idle entries older than the TTL are removed. A value `<=0` disables TTL eviction.
- Prometheus counters distinguish explicit clear, cap, and TTL evictions; gauges expose entry and byte counts.

---

## Limitations (current)

- No symbol diff tracking — `POST /overlay` always returns the full symbol set, not just added/removed since last update.
- Eviction is access-triggered rather than driven by a dedicated timer thread.

---

## Planned Extensions

- Return symbol diff (added/removed names) on `POST /overlay` to support VS Code outline updates
- Phase 3.5: support `IMPORTS` and `DEPENDS_ON` edge extraction in overlay (currently only `CALLS` edges)
