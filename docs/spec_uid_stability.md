# Spec — UID Stability (Phase 8)

> **Status:** Implemented. Replaces the old `sha256(file_path:name)` scheme with a qualified-name + signature derivation in `sidecar/parser/uid.py`.

## 1. Problem

Current UID:

```python
uid = sha256(f"{file_path}:{name}").hexdigest()
```

This is a positional identity, not a semantic one. It breaks under every common refactor:

| Scenario | Effect | Damage |
|---|---|---|
| Rename function | New UID → old history detached | AFFECTS edges point at a ghost; DocAnchor `COVERS` rewires randomly |
| Move function between files | New UID | Same as rename |
| Overloads (TS, future Java) | Identical UIDs for distinct symbols | Silent call-graph collision |
| Nested / inner functions | `:name` collides across outer scopes | BFS walks into the wrong body |
| Method vs. standalone `process()` | Collides if both live in same file | Wrong body returned for query |

Because UID is the join key across Neo4j, LanceDB, the overlay, and AFFECTS — drift here corrupts every downstream system. It is the single biggest latent correctness bug in the MVP.

## 2. Design

### 2.1 New Derivation

```python
uid = sha256(f"{language}:{qualified_name}|{normalized_signature}").hexdigest()[:16]
```

Where:

- `qualified_name` — dotted path from module root to symbol, e.g. `sidecar.indexer.code.CodeIndexer.run_indexing`.
- `signature_hash` — SHA-256 over the normalized signature (parameter names dropped; types kept where available).

**Absolute file path is no longer part of the UID.** The implementation derives a module-like qualified name without machine-specific roots, then combines it with the normalized signature.

### 2.2 Signature Normalization

Python example: `process_payment(user_id: int, amount: float, *, currency: str = "USD") -> Receipt`

Normalized to: `process_payment(int,float,*,str)->Receipt`

Normalization rules:
- Parameter names stripped (names rename without semantic change).
- Default values stripped.
- Keyword-only marker `*` kept.
- `**kwargs` kept as literal.
- Return type kept.
- Untyped params serialize as `_`.

TypeScript: same shape, with `?` preserved for optional params and union types sorted alphabetically.

### 2.3 Overload Disambiguation

Languages that allow overloads (TS declaration merging, future Java/C#) disambiguate purely through `signature_hash`. Two `parse(x: string)` / `parse(x: Buffer)` overloads get distinct UIDs because their signature strings differ.

### 2.4 Nested Symbols

Qualified name includes all enclosing scopes:

```python
def outer():
    def inner():  # qualified_name = "module.outer.<locals>.inner"
        ...
```

`<locals>` is a reserved token used for function-scope nesting; classes use their class name directly (`module.Outer.Inner`).

### 2.5 Unknown Signatures

When the parser cannot resolve a signature (malformed source, incomplete type info):
- Fall back to `qualified_name|<unresolved>` with a warning logged.
- `symbol.signature_status = "unresolved"` stored on the node.
- These symbols remain indexable but should not be used as AFFECTS targets until re-parsed cleanly.

## 3. Migration

UID changes are destructive to the existing graph. Migration is a one-shot rebuild:

1. **Freeze** `/index`, `/overlay`, `/ask` (maintenance mode).
2. **Snapshot** existing Neo4j DB (pg-style backup).
3. **Drop + rebuild** Symbol nodes with new UIDs; all edges rebuilt from parsed source.
4. **Migration CLI** reads the snapshot, emits a CSV `old_uid → new_uid` for any external consumers (audit log back-references).
5. **Re-run** DocAnchor `COVERS` resolution via LanceDB — content-hash similarity means old links mostly re-establish.
6. **Unfreeze.**

No online migration path. The cost of an inconsistent hybrid is higher than the downtime.

## 4. API / Interface

```python
# sidecar/parser/uid.py

def compute_uid(
    qualified_name: str,
    signature: str | None,
    language: str = "python",
) -> str:
    """Return 16-hex-char UID.

    Args:
        qualified_name: dotted path from module root (e.g. "pkg.mod.Class.method")
        signature: normalized signature string; None → "<unresolved>"
        language: source language (affects normalization rules)

    Returns:
        16-character hex string (first 16 of SHA-256)
    """

def normalize_signature(raw: str, language: str) -> str:
    """Strip param names and defaults per language rules."""

def qualified_name_for(node: tree_sitter.Node, file_module: str) -> str:
    """Walk AST parents to build dotted path. Uses <locals> for function nesting."""
```

## 5. Examples

```python
# Python — standalone function
compute_uid("sidecar.indexer.code.run_indexing", "run_indexing(str,bool)->None")
# → "a4f9c1e2b7d83f56"

# Python — method
compute_uid(
    "sidecar.context.arbitrator.ContextArbitrator.get_context_for_symbol",
    "get_context_for_symbol(str,str)->PromptContext",
)
# → "1b3e8c02af47d9e1"

# Python — nested
compute_uid("sidecar.cli.main.<locals>.handler", "handler(Request)->Response")
# → "f012c3d4a5b6e789"

# TypeScript — overload
compute_uid("parser.parse", "parse(string)->AST")    # UID A
compute_uid("parser.parse", "parse(Buffer)->AST")    # UID B ≠ A
```

## 6. Limitations (current)

- Signature parsing depends on the language adapter — partial type info degrades to `<unresolved>`. Python untyped params are common; expect 20–40% of user code to have partially-unresolved signatures initially.
- Renaming **and** changing the signature simultaneously still breaks identity. No fix at this layer; the learning loop (Phase 10) could approximate recovery via embedding similarity.
- Truncation to 16 hex chars = 64 bits of entropy. Collision probability at 1M symbols ≈ 2.7×10⁻⁸. Acceptable; widen to 20 chars if a large tenant approaches that scale.

## 7. Planned Extensions

- **Rename detection** (Phase 10): when an old UID disappears and a new one appears with high code-similarity + same signature shape, treat as rename and rewire AFFECTS/DocAnchor edges.
- **Cross-language UIDs** for polyglot repos: prefix qualified name with language tag (`py:`, `ts:`) to prevent accidental collisions when two modules have the same dotted path.

## 8. Related

- [spec_parser.md](spec_parser.md) — qualified-name extraction lives here.
- [spec_affects_index.md](spec_affects_index.md) — every AFFECTS edge depends on UID stability.
- [spec_doc_anchor.md](spec_doc_anchor.md) — DocAnchor `COVERS` links by UID.
- ADR-001 — symbols are pure identity; body lives on File + range.
