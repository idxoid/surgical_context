# Spec — Branch / Workspace Isolation (Phase 8)

> **Status:** Implemented for graph reads/writes and dirty overlay isolation. Closes the SaaS correctness gap: the Phase 7 shared Aura graph collapses all branches and users into a single symbol set, guaranteeing drift across workspaces.

## 1. Problem

Phase 7 ships a multi-user cloud graph on Aura. Every user writes symbol nodes into the same global namespace. Real developer workflows immediately break this model:

- Alice is on `feature/new-pricing`; `process_payment` has a new signature.
- Bob is on `main`; `process_payment` still has the old signature.
- Both index into the same Aura. Whoever indexed last wins.
- Alice's `/ask` against `process_payment` returns Bob's version of the body. Silent wrong answer.

Secondary failures:
- Local uncommitted changes never propagate and aren't tenant-visible.
- AFFECTS edges cross branches — a delete on a feature branch appears to cascade into main.
- Doc chunks on a review branch leak into main-branch retrieval.

Multi-user without workspace isolation is worse than single-user — the appearance of shared knowledge hides divergent truth.

## 2. Design

Introduce a first-class **Workspace** axis on every graph node. A workspace is `(tenant, repo, ref)` where `ref` is a branch, tag, or commit SHA. All queries are workspace-scoped by default; cross-workspace queries require an explicit opt-in.

### 2.1 Data Model

```cypher
// New node
(:Workspace {
    id: "acme/surgical_context@main",    // canonical key
    tenant: "acme",
    repo: "surgical_context",
    ref: "main",
    ref_kind: "branch",                   // "branch" | "tag" | "commit"
    created_at: datetime,
    last_indexed: datetime
})

// Every mutation-producing node carries a workspace edge
(File)-[:IN_WORKSPACE]->(Workspace)
(Symbol)-[:IN_WORKSPACE]->(Workspace)
(DocAnchor)-[:IN_WORKSPACE]->(Workspace)
```

Symbol UIDs from [spec_uid_stability.md](spec_uid_stability.md) are NOT workspace-scoped — they are semantic identity. Two branches with the same `process_payment(int)` produce the same UID; the body may differ. This is correct: it lets us ask "which workspace has the green version?" and enables cross-branch diff views.

### 2.2 Body Storage

`Symbol` is still pure identity (ADR-001). The body is resolved via `File.range`, and `File` is workspace-scoped:

```cypher
(f:File {path, hash, workspace_id})-[:CONTAINS {range}]->(s:Symbol {uid})
```

Same symbol UID, different File per workspace, different `hash` per workspace. Retrieval always joins through the user's active workspace.

### 2.3 Query Scoping

All `/ask`, `/impact`, `/overlay` endpoints accept a workspace header or derive it from the request:

```
X-User-ID: alice
X-Workspace: acme/surgical_context@feature/new-pricing
```

For development compatibility, requests without `X-Workspace` fall back to `DEFAULT_WORKSPACE_ID` (`local/surgical_context@main` by default). The VS Code extension no longer ships a competing static default: when `surgicalContext.workspaceId` is blank, it derives `local/{workspace-folder-name}@{git-branch-or-short-sha}` and sends that; if no workspace folder is open, it omits the header and lets the sidecar fallback apply. Explicit user configuration always wins.

Cypher reads get a `WHERE` clause injected at the arbitrator layer:

```cypher
MATCH (s:Symbol {uid: $uid})-[:IN_WORKSPACE]->(w:Workspace {id: $workspace_id})
MATCH (s)-[r:CALLS_DIRECT|CALLS_DYNAMIC|CALLS_INFERRED]->(n:Symbol)
      -[:IN_WORKSPACE]->(w)
RETURN n, r
```

Workspace scoping is enforced **in Cypher**, not in Python filtering — a Python-side filter is always one `forgot-to-add-the-where` away from data leak.

### 2.4 AFFECTS and DocAnchor

- AFFECTS edges are workspace-local. Rebuilt per workspace on file change.
- DocAnchor `COVERS` edges are workspace-local. A spec on `feature/new-pricing` only covers symbols on that branch.
- Cross-workspace edges (e.g., "symbol X on main → diverges from → symbol X on feature") are **opt-in** relationship types (`DIFFERS_FROM`) computed on demand; not materialized by default.

### 2.5 Overlay

`InMemoryOverlay` was already per-connection. Upgraded to `(user_id, workspace_id)` key. Overlay on `feature/new-pricing` does not leak into `main` queries even when the same user switches branches.

### 2.6 Lifecycle

- **Create workspace:** implicit on first `/index` call with a new `ref`.
- **Switch workspace:** client passes a new `X-Workspace`; server validates membership, serves from that namespace.
- **Delete workspace:** `DELETE /workspace/{id}` — cascades to all `IN_WORKSPACE`-linked nodes. Audit log records who deleted what.
- **Branch rebased / force-pushed:** treated as a new workspace; old one garbage-collected after TTL (default 14 days).

## 3. API / Interface

```python
# sidecar/workspace.py

@dataclass
class Workspace:
    id: str         # "{tenant}/{repo}@{ref}"
    tenant: str
    repo: str
    ref: str
    ref_kind: str   # "branch" | "tag" | "commit"

class WorkspaceResolver:
    def from_request(self, headers: dict) -> Workspace:
        """Parse X-Workspace header, validate format, ensure user has access.
        Raises WorkspaceNotFound / WorkspaceAccessDenied."""

    def ensure_exists(self, ws: Workspace) -> None:
        """Create Workspace node if absent (idempotent)."""

    def list_for_user(self, user_id: str) -> list[Workspace]:
        """Return all workspaces the user has touched."""
```

Arbitrator gets a workspace parameter threaded through every query method. Signature becomes:

```python
def get_context_for_symbol(
    self,
    symbol: str,
    workspace: Workspace,        # NEW — required
    question: str = "",
    ...
) -> PromptContext: ...
```

## 4. Examples

```bash
# Alice indexes feature branch
curl -X POST http://localhost:8000/index \
  -H "X-User-ID: alice" \
  -H "X-Workspace: acme/surgical_context@feature/new-pricing" \
  -d '{"path": "/repo"}'

# Alice asks a question
curl -X POST http://localhost:8000/ask \
  -H "X-User-ID: alice" \
  -H "X-Workspace: acme/surgical_context@feature/new-pricing" \
  -d '{"symbol": "process_payment", "question": "How does pricing work?"}'
# → sees Alice's branch version

# Bob on main queries the same symbol
curl -X POST http://localhost:8000/ask \
  -H "X-User-ID: bob" \
  -H "X-Workspace: acme/surgical_context@main" \
  -d '{"symbol": "process_payment"}'
# → sees main version — NOT Alice's
```

## 5. Migration

Existing Phase 7 Aura graphs have no workspace metadata. Migration:

1. Create a single `Workspace {id: "local/surgical_context@main", ...}` node, or another operator-supplied `DEFAULT_WORKSPACE_ID`.
2. Link every existing File / Symbol / DocAnchor to it.
3. Require new writes to specify a workspace.
4. Emit deprecation warnings on reads without `X-Workspace`; after 30 days, require the header.

No destructive rebuild needed — additive schema change.

## 6. Limitations (current)

- **Storage cost scales with branch count.** An active team with 20 feature branches duplicates File/Symbol data 20×. Mitigation: branches auto-purge after N days of inactivity (configurable; default 14).
- **Cross-workspace queries are unindexed.** Comparing `process_payment` across branches requires a linear scan. Cheap at low branch count; revisit if teams keep >50 active branches.
- **No commit-granularity time travel.** `ref_kind: "commit"` works for pinning a workspace to a SHA, but we don't preserve history *within* a workspace. "Show me this function as it was yesterday" is out of scope here; that's a separate versioning project.

## 7. Planned Extensions

- Shared-ancestor optimization: when two workspaces share an unchanged file hash, point both `File` nodes at the same `Symbol` rows (structural sharing). Shrinks storage on low-divergence branches.
- Workspace-level RBAC: `acme/surgical_context@main` visible to all, `acme/surgical_context@private-spike` visible to one team. Pairs with the deferred Phase 7 RBAC work.
- `DIFFERS_FROM` materialization for PR review: pre-compute per-PR the set of symbols whose bodies differ between base and head. Powers a "PR surgical review" mode.

## 8. Related

- [spec_uid_stability.md](spec_uid_stability.md) — UIDs must be semantic (not path-based) for cross-workspace identity to work.
- [spec_overlay.md](spec_overlay.md) — overlay key upgrades to `(user, workspace)`.
- [spec_sidecar_api.md](spec_sidecar_api.md) — `X-Workspace` header added to every endpoint.
- ADR-003 — cloud-first SaaS model.
