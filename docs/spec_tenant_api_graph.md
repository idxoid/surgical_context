# Spec - Tenant API Contract Graph

> **Status:** Future Team/Enterprise layer. Extends project-owned indexes with tenant-level API contract links. This is not required for the Local Developer Product, not cross-project source scanning, and not live API invocation.

## 1. Problem

Microservice systems drift at the service boundary: endpoints are renamed, schemas change, generated clients lag behind servers, and ownership is often unclear. A local project graph can explain internal code flow, but it cannot answer tenant-level questions like:

- Which service owns `POST /alerts/{id}/ack`?
- What clients call this endpoint?
- Will changing `AlertChannel` break a neighboring system?
- After this external response enters the current project, where is it transformed or persisted?

The system needs cross-project awareness without violating project ownership or privacy.

## 2. Boundary

Each project indexes itself. The tenant graph only links published project facts.

### In Scope

- Index API contracts and call sites that belong to the current project.
- Publish safe contract metadata from each project into a tenant-scoped graph.
- Link services, endpoints, schemas, events, and dependency edges across project manifests.
- Use direction-aware weighting and bounded tenant-link traversal during retrieval.

### Out of Scope

- Scanning sibling repositories from another project's sidecar.
- Reading source code from a neighboring project unless that project's own index explicitly published safe metadata.
- Invoking live external APIs during indexing or retrieval.
- Storing raw request/response payloads, secrets, auth tokens, or raw prompts in the tenant graph.

## 3. Project-Owned Indexing

The project indexer is responsible only for facts observable inside its workspace:

| Source | Examples | Output |
|---|---|---|
| OpenAPI / Swagger | `openapi.yaml`, generated route specs | endpoints, operations, schemas |
| GraphQL | SDL files, resolver declarations | queries, mutations, types |
| gRPC / protobuf | `.proto` files, generated clients | services, RPC methods, messages |
| AsyncAPI / events | event specs, topic manifests | topics, producers, consumers |
| Route declarations | FastAPI decorators, Express routes, controller annotations | implemented endpoints |
| Client usage | generated SDK calls, typed HTTP clients, gateway clients | outbound dependency edges |
| Service metadata | gateway config, service catalog, IaC manifests | service ownership and aliases |

The local sidecar emits a `ContractManifest` for the current workspace. Tenant linking consumes these manifests; it does not pull source from other projects.

## 4. Data Model

### Nodes

| Label | Key Properties | Description |
|---|---|---|
| `Service` | `service_id, tenant_id, workspace_id, name, owner, repo, version` | Project-published service identity |
| `ApiEndpoint` | `operation_id, method, path, protocol, version, deprecated` | HTTP/GraphQL/RPC operation |
| `ApiSchema` | `schema_id, name, format, version, schema_hash` | Request/response/event schema |
| `ApiField` | `field_id, name, type, required, sensitivity` | Optional schema-field granularity |
| `EventTopic` | `topic_id, name, broker, version` | Published/consumed event stream |
| `ExternalSystem` | `system_id, tenant_id, name, kind` | SaaS/vendor/system without a project index |
| `ContractManifest` | `manifest_id, workspace_id, graph_version, published_at` | Immutable publication unit |

### Relationships

| Type | Pattern | Description |
|---|---|---|
| `PUBLISHES_SERVICE` | `(Workspace)->(Service)` | Workspace owns/publishes a service manifest |
| `EXPOSES_ENDPOINT` | `(Service)->(ApiEndpoint)` | Service offers an operation |
| `IMPLEMENTS_ENDPOINT` | `(Symbol|File)->(ApiEndpoint)` | Current project code implements an endpoint |
| `CALLS_ENDPOINT` | `(Symbol|File|Service)->(ApiEndpoint)` | Current project calls an operation |
| `USES_SCHEMA` | `(ApiEndpoint|EventTopic)->(ApiSchema)` | Operation/topic uses a schema |
| `HAS_FIELD` | `(ApiSchema)->(ApiField)` | Field-level schema structure |
| `PRODUCES_EVENT` | `(Service)->(EventTopic)` | Service publishes an event |
| `CONSUMES_EVENT` | `(Service)->(EventTopic)` | Service consumes an event |
| `DEPENDS_ON_SERVICE` | `(Service)->(Service|ExternalSystem)` | Derived service dependency |
| `VERSION_OF` | `(ApiEndpoint|ApiSchema)->(ApiEndpoint|ApiSchema)` | Contract version lineage |
| `BREAKS_CONTRACT` | `(ContractManifest)->(ApiEndpoint|ApiSchema)` | Compatibility warning from contract diff |

Edges are tenant-scoped and carry `workspace_id`, `tenant_id`, `confidence`, `resolver`, and `published_at` where applicable.

## 5. Tenant Linking

Tenant linking matches project-published facts by stable contract fingerprints:

- Endpoint fingerprint: `protocol + method/rpc + normalized_path_or_name`.
- Schema fingerprint: canonical schema hash with ignored formatting/order noise.
- Event fingerprint: broker namespace + topic name + schema hash.
- Service alias: declared service name, gateway host, package namespace, or service catalog ID.

When multiple projects publish matching facts, the tenant graph records both sides and confidence rather than choosing a winner silently.

## 6. Retrieval Policy

The retrieval ladder becomes:

```text
symbol -> file -> workspace -> tenant_api_graph -> direct_llm
```

The tenant API graph contributes only published contract/dependency context. It does not traverse or read neighboring project source.

### Direction

`api_direction` controls which tenant links get boosted:

| Value | Boosts | Use Case |
|---|---|---|
| `outbound_dependencies` | APIs this project calls, remote schemas, owners, contract changes | Understanding external calls |
| `inbound_consumers` | Services that call this project's published APIs | Impact on clients |
| `contract_impact` | endpoints/schemas/events affected by a local change | Change review |
| `internal_processing` | current-project symbols after external data enters | Debugging local transformations |
| `bidirectional_contract` | both callers and callees around a contract | Cross-service debugging |

### Link Depth

`tenant_link_depth` limits graph traversal across published tenant links:

| Depth | Meaning |
|---|---|
| `0` | Current project only |
| `1` | Direct linked services/contracts |
| `2` | One additional service dependency hop, only for explicit impact/debug views |

Depth is a traversal limit over tenant contract links, not permission to scan another project.

### Scoring

```text
score =
  base_relevance
  * edge_type_weight
  * direction_weight
  * scope_weight
  * depth_decay
  * confidence
```

Default scope priority:

```text
current_symbol > current_file > current_workspace > tenant_api_neighbor > direct_llm
```

## 7. Prompt Contract Impact

Tenant API candidates should appear as a separate context tier, for example:

```json
{
  "tenant_api_context": [
    {
      "kind": "endpoint",
      "service": "alerts-service",
      "operation": "POST /alerts/{id}/ack",
      "direction": "outbound_dependencies",
      "tenant_link_depth": 1,
      "workspace_id": "acme/alerts@main",
      "scores": {
        "base_relevance": 0.82,
        "direction_weight": 1.25,
        "depth_decay": 0.75,
        "confidence": 0.91,
        "blended_score": 0.70
      },
      "provenance": [
        "tenant_api:CALLS_ENDPOINT,depth=1",
        "manifest:acme/alerts@main#2026-04-22"
      ]
    }
  ]
}
```

For privacy, provenance may expose service and contract IDs, but not neighboring file paths, raw code, prompts, payloads, or secrets.

## 8. API / Interface

Planned sidecar additions:

```python
POST /index/api-contracts
```

Indexes API contracts and call sites for the current workspace only.

```python
GET /tenant/api/impact?operation_id=...&direction=...&tenant_link_depth=1
```

Returns linked services/contracts from published tenant facts.

```python
POST /ask
{
  "symbol": "optional",
  "file_path": "optional",
  "question": "string",
  "api_direction": "outbound_dependencies",
  "tenant_link_depth": 1
}
```

`api_direction` and `tenant_link_depth` are advisory retrieval policy inputs. The server may clamp `tenant_link_depth` by user permissions and endpoint type.

## 9. Privacy and Permissions

- Tenant API traversal requires the caller's tenant and workspace identity.
- Cross-project retrieval reads only published manifests and tenant graph facts.
- Project-private source paths are hidden unless that project explicitly publishes them as safe metadata.
- Raw prompts, code bodies, payload examples, secrets, credentials, and auth headers are not stored in the tenant graph.
- Field-level sensitivity can be stored as labels or enums, never as sample values.
- RBAC decides whether a user can see service names, endpoint names, schema fields, or only anonymized dependency counts.

## 10. Tests

- Project A cannot trigger indexing of Project B.
- Tenant traversal returns direct links at `tenant_link_depth=1` and clamps unsupported deeper traversal.
- `outbound_dependencies` boosts called endpoints above inbound consumers.
- `inbound_consumers` boosts callers of the current service above outbound dependencies.
- Missing or ambiguous contract fingerprints produce low-confidence candidates, not hard failures.
- Prompt contracts never include raw payloads, secrets, or neighboring source paths.

## 11. Related

- [architectura.md](architectura.md) - system architecture and ADRs.
- [road_map.md](road_map.md) - planned Phase 11 work.
- [spec_unified_ranking.md](spec_unified_ranking.md) - blended ranking model this extends.
- [spec_branch_isolation.md](spec_branch_isolation.md) - workspace and tenant scoping.
- [spec_prompt_contract_observability.md](spec_prompt_contract_observability.md) - scores and provenance in the prompt contract.
