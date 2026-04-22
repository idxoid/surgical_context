# Spec - Storage Provider Connectors

> **Status:** Planned, staged. Local v0.1 needs provider boundaries around the default implementations first: Neo4j, LanceDB, and SQLite. Alternate graph/vector/history backends are Team/Enterprise horizon work, not a blocker for the local release.

## 1. Problem

Different users have different storage constraints:

- Solo developers want local Docker and local files.
- Teams may use company-owned Neo4j/Aura, Qdrant, Postgres, or another managed service.
- Enterprise customers may require a dedicated deployment in their own cloud account.
- Some organizations may require centralized user activity retention, while others may require local-only or disabled history.

The product should not bind retrieval correctness or privacy policy to a specific database vendor.

## 2. Design

Introduce a storage provider layer with three independent connector families:

```text
GraphProvider
VectorProvider
HistoryProvider
```

Each connector implements a stable product contract. Privacy and retention policy sit above the connectors, so swapping databases changes where data lives but not what data is allowed to be stored.

The first implementation slice should be narrow: wrap the existing Neo4j and LanceDB clients, then add SQLite local history. Do not add real alternate backends until the default providers have conformance tests.

## 3. Provider Families

### GraphProvider

Stores topology and metadata:

- Workspaces, files, symbols, ranges, content hashes.
- Calls/imports/dependencies/AFFECTS relationships.
- DocAnchor topology.
- Tenant API contract links from published manifests.

Default implementation: Neo4j.

Future implementations:

| Provider | Mode | Notes |
|---|---|---|
| `neo4j` | `local_docker` | Default solo/local development mode |
| `neo4j` | `customer_managed` | Company-owned Neo4j or Aura endpoint |
| `neo4j` | `dedicated_managed` | Single-tenant managed deployment |
| `nebula` | `customer_managed` | Alternative graph backend |
| `memgraph` | `local_docker` / `customer_managed` | Alternative Cypher-like backend |

### VectorProvider

Stores semantic retrieval indexes:

- Documentation chunks.
- Optional symbol-body embeddings.
- Embedding metadata and model version.
- Pending DocAnchor references.

Default implementation: LanceDB.

Future implementations:

| Provider | Mode | Notes |
|---|---|---|
| `lancedb` | `local` | Default local vector store |
| `qdrant` | `local_docker` / `customer_managed` | Team/customer vector service |
| `weaviate` | `customer_managed` | Managed semantic index |
| `pgvector` | `customer_managed` | Postgres-backed vector index |
| `pinecone` | `customer_managed` | Only where customer policy allows it |

### HistoryProvider

Stores user-facing product state:

- Conversations and messages.
- Ask snapshots.
- Inspector prompt-context snapshots.
- Prompt-derived impact snapshots.
- Optional refreshed `/impact` result snapshots.
- Trace IDs, feedback tokens, model route, token counts, timestamps.

Default implementation: SQLite local.

Future implementations:

| Provider | Mode | Notes |
|---|---|---|
| `sqlite` | `local` | Default user history store |
| `sqlite_encrypted` | `local` | Local encrypted history |
| `postgres` | `customer_managed` | Team or enterprise history/audit store |
| `audit_store` | `enterprise_audit` | Corporate retention connector |
| `memory` | `ephemeral` | Session-only history |
| `disabled` | `disabled` | No persisted dialog history |

## 4. Policy Boundary

Connectors do not decide whether sensitive data may be stored. A storage policy layer classifies payloads before they reach a connector.

Policy decides:

- Can raw prompt text be stored?
- Can response text be stored?
- Can source snippets or full prompt bodies be stored?
- Can data leave the local machine?
- Can history be shared with a team?
- What is the retention window?
- Is redaction required before persistence?

Connectors receive already-approved payloads:

```text
TopologyFacts
VectorChunks
HistorySnapshot
FeedbackMetadata
AuditEvent
```

## 5. Configuration Shape

Local solo default:

```yaml
storage:
  graph:
    provider: neo4j
    mode: local_docker
  vector:
    provider: lancedb
    mode: local
  history:
    provider: sqlite
    mode: local
    retention_days: 30
    store_prompt_text: false
    store_response_text: true
```

Customer-managed team:

```yaml
storage:
  graph:
    provider: neo4j
    mode: customer_managed
    workspace_scope_required: true
  vector:
    provider: qdrant
    mode: customer_managed
  history:
    provider: sqlite
    mode: local
    retention_days: 30
```

Enterprise audit mode:

```yaml
storage:
  graph:
    provider: nebula
    mode: customer_managed
  vector:
    provider: pgvector
    mode: customer_managed
  history:
    provider: postgres
    mode: enterprise_audit
    retention_days: 180
    store_prompt_text: true
    store_response_text: true
    redaction_required: true
```

## 6. Interface Sketch

```python
class GraphProvider(Protocol):
    def upsert_file_structure(self, file_path: str, file_hash: str, symbols: list, workspace_id: str) -> None: ...
    def link_calls(self, calls: list[dict], workspace_id: str) -> None: ...
    def expand_symbol(self, symbol: str, workspace_id: str, budget: int) -> dict: ...
    def impact(self, symbol: str, workspace_id: str) -> dict: ...

class VectorProvider(Protocol):
    def upsert_chunks(self, file_path: str, chunks: list, workspace_id: str) -> None: ...
    def upsert_symbol_embeddings(self, symbols: list, workspace_id: str) -> None: ...
    def search_docs(self, query: str, workspace_id: str, limit: int) -> list[dict]: ...
    def search_symbols(self, query: str, workspace_id: str, limit: int) -> list[dict]: ...

class HistoryProvider(Protocol):
    def create_conversation(self, workspace_id: str, user_id: str) -> str: ...
    def append_message(self, conversation_id: str, message: dict) -> str: ...
    def save_ask_snapshot(self, message_id: str, snapshot: dict) -> None: ...
    def list_conversations(self, workspace_id: str, user_id: str, limit: int) -> list[dict]: ...
```

The actual Python interfaces can be narrower at first. This sketch names the ownership boundaries.

## 7. Migration Plan

1. Keep `Neo4jClient` and `LanceDBClient` as concrete defaults.
2. Introduce `GraphProvider` and `VectorProvider` protocols around the methods the sidecar already calls.
3. Add `HistoryProvider` with SQLite as the first implementation.
4. Move storage selection into configuration.
5. Add provider capability checks, for example `supports_relationship_properties`, `supports_vector_metadata_filter`, and `supports_transactions`.
6. Add conformance tests that run against fake/in-memory providers before adding real alternate backends.
7. Defer `customer_managed`, `dedicated_managed`, and `enterprise_audit` implementations until local defaults are stable.

## 8. Tests

- Provider config selects defaults when no config is present.
- Privacy policy blocks raw prompt/code storage before the HistoryProvider receives payloads.
- GraphProvider conformance covers workspace scoping, symbol lookup, call linking, and impact traversal.
- VectorProvider conformance covers doc search, symbol search, metadata filters, and embedding version guards.
- HistoryProvider conformance covers conversation listing, ask snapshots, retention deletion, and disabled/ephemeral modes.
- Alternate providers cannot bypass tenant/workspace scoping.

## 9. Related

- [spec_storage.md](spec_storage.md) - current concrete Neo4j/LanceDB storage behavior.
- [spec_tenant_api_graph.md](spec_tenant_api_graph.md) - tenant API links stored through the graph provider.
- [spec_branch_isolation.md](spec_branch_isolation.md) - workspace and tenant scoping.
- [spec_learning_loop.md](spec_learning_loop.md) - feedback metadata boundaries.
