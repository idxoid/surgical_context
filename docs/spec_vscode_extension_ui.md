# VS Code Extension UI — Spec

> **Status:** Implemented baseline with remaining synchronization and accessibility work.

`extension/src/providers/`, `extension/src/panels/`, and
`extension/src/webview/` define the developer-facing UI. The current direction
is chat-first with evidence inspection, graph-aware navigation, a bottom-docked
composer, an expanding response area, and collapsed secondary groups.

Exact host/webview DTOs live in `extension/src/webview/shared/protocol.ts`; code
wins when the illustrative message unions below lag implementation.

## Overview

The Surgical Context extension adds a VS Code-native UI for asking questions about the current symbol, inspecting the context sent to the model, and exploring impact across code and docs. The extension is not a generic chatbot. Its primary value is transparent context assembly: the user can see what code symbols, documentation chunks, and metadata were included in a request.

Primary surfaces:

1. **Chat Panel** — ask questions about the current symbol and read streaming answers.
2. **Context Inspector** — inspect primary source, graph context, docs, prompt JSON, and token allocation.
3. **Impact Explorer** — inspect bounded reverse reachability for a symbol, affected files, and prompt-context impact.
4. **Dashboard** — inspect health, indexing status, token signals, and recent system activity.

## Design

### Why this UI exists

The backend supports graph expansion, vector/doc search, dirty-state overlays, streaming responses, impact lookup, health checks, cloud status, and operational signals. A thin chat-only panel would hide the strongest part of the system: explainable context selection. The UI therefore treats the answer and the evidence as first-class peers.

### Why the chat panel uses a bottom composer

The prompt area sits at the bottom of the panel, the response area stays above it, and secondary info groups are collapsed by default. The answer area grows with message height while metadata stays available but unobtrusive.

### Main trade-offs

- **Gain:** familiar chat layout reduces onboarding friction.
- **Gain:** collapsed info groups preserve transparency without making the panel visually dense.
- **Gain:** separate inspector view keeps the chat panel readable.
- **Cost:** some metadata becomes one click farther away.
- **Cost:** multiple surfaces require explicit state synchronization.

## UI Surfaces

### 1. Chat Panel

The Chat Panel is the default entry point.

#### Layout

Top to bottom:

1. **Header**
   - Title: `Surgical Context`
   - Optional icon controls: pin, overflow menu, close

2. **Action row**
   - `Ask`
   - `Inspect Context`
   - `Impact`
   - `Search`

3. **Conversation area**
   - Streaming answer cards
   - Prior question/answer thread
   - Feedback actions (`thumbs up`, `thumbs down`, `copy`, optional retry)

4. **Collapsed info groups**
   - `Environment`
   - `Context Summary`
   - `Advanced Info`

5. **Composer** (bottom-docked)
   - Multiline textarea
   - Auto-expands with input height
   - Send button aligned right
   - Placeholder: `Ask about this symbol, its behavior, dependencies...`

6. **Status chips**
   - `dirty-aware`
   - `graph-first`
   - `doc-linked`

#### Behavior

- The composer stays anchored to the bottom edge of the panel.
- The conversation scrolls independently above the composer.
- The response card grows with content height; it must not force the composer upward until panel height is exhausted.
- Info groups are collapsed by default and persist their open/closed state per session.
- The current symbol is inferred from the active editor selection when possible. If no symbol is available, the Ask action uses workspace/direct fallback behavior.

#### Minimal component tree

```text
ChatPanel
├─ Header
├─ ActionBar
├─ ConversationList
│  ├─ MessageCard[]
│  └─ StreamingState
├─ AccordionGroup(Environment)
├─ AccordionGroup(ContextSummary)
├─ AccordionGroup(AdvancedInfo)
├─ Composer
└─ StatusChipRow
```

### 2. Context Inspector

The Context Inspector explains why the model saw specific files, symbols, and documentation.

#### Tabs

- `Primary Source`
- `Graph Context`
- `Documentation`
- `Prompt JSON`
- `Token Breakdown`

#### Required data

The inspector consumes the `context` payload returned by `/ask` or `/ask/stream`. The payload includes `primary_source`, `graph_context`, and `documentation`. The serializer includes mode, intent, tiers, depth/direction, scores, provenance, route, trace, and workspace fields, although the active axis adapter still leaves some richer values sparse.

#### Primary use case

A developer asks a question in the Chat Panel, then opens the inspector to verify:

- which symbol was treated as the seed
- which related symbols were added through graph traversal
- which doc chunks were matched semantically
- how much token budget code vs. docs consumed

### 3. Impact Explorer

The Impact Explorer visualizes likely change impact for the selected symbol. The current product scope is intentionally shallow: it uses the sidecar's bounded `AFFECTS` graph plus the selected ask's prompt context. It is not a full framework-aware blast-radius engine across codegen, templates, runtime dispatch, and tests.

#### Sections

- `Affects`
- `Files`
- Summary metrics: affected symbols, affected files, max traversal depth, source (`live graph` or `prompt context`)

Current implementation note: `Affects` maps to materialized `AFFECTS` edges. The prompt-context source is derived from the selected ask's `graph_context` and documentation files, so Inspect and Impact stay attached to the same request. First-class `CALLS_*`, `DEPENDS_ON`, `FROM`, and `COVERS` Impact groups are a later iteration, not the current UI contract.

These sections map directly to the underlying graph model and retrieval strategy. The backend models `CALLS_*`, `DEPENDS_ON`, `AFFECTS`, `FROM`, and `COVERS` relationships.

#### Actions

- `Open related files`
- `Ask follow-up`
- `Create refactor plan`

### 4. Dashboard

The Dashboard is an operational view, not a second chat surface.

#### Metrics to show

- sidecar health
- cloud status
- indexed files
- indexed symbols
- doc chunks
- last indexing job state
- average ask latency
- token savings vs. naive context
- fallback rate
- recent audit events

These metrics align with the existing architecture goals and observability layer.

## State Model

The extension UI should use four state domains.

### Session state

| Field | Type | Purpose |
|---|---|---|
| `conversationId` | `string` | Current thread identity inside the panel |
| `selectedRequestId` | `string \| null` | Completed ask selected as the source for Inspector and prompt-context Impact |
| `selectedSymbol` | `SymbolRef \| null` | Active symbol for ask/inspect/impact actions |
| `expandedGroups` | `Record<string, boolean>` | Accordion open state |
| `pinnedItems` | `PinnedContextItem[]` | Manually pinned symbols or docs |

### Editor state

| Field | Type | Purpose |
|---|---|---|
| `activeFile` | `string \| null` | Current editor file |
| `cursorRange` | `Range \| null` | Used to infer symbol under cursor |
| `isDirty` | `boolean` | Indicates unsaved file state |
| `overlaySynced` | `boolean` | Whether overlay content was sent to the sidecar |

### Backend state

| Field | Type | Purpose |
|---|---|---|
| `sidecarHealth` | `'up' \| 'down' \| 'degraded'` | Health indicator |
| `cloudStatus` | `'connected' \| 'fallback-local' \| 'offline'` | Provider/backend mode |
| `workspaceId` | `string` | Current workspace scope. Blank setting means derive from VS Code workspace folder + Git branch; explicit setting overrides derivation. |
| `authState` | `'ready' \| 'missing-token' \| 'expired'` | Auth-related UX |

### Request state

| Field | Type | Purpose |
|---|---|---|
| `status` | `'idle' \| 'collecting' \| 'streaming' \| 'done' \| 'error'` | Request lifecycle |
| `lastRequest` | `LastRequest \| null` | Host-side selected/completed request: request id, symbol, question, answer, and prompt context |
| `mode` | `'surgical' \| 'standard'` | Ask mode |
| `intent` | `string \| null` | Request intent classification |
| `contextSummary` | `ContextSummary \| null` | Compact display data for accordions |

## API / Interface

The UI has two boundaries:

1. **Webview ↔ extension host**
2. **Extension host ↔ sidecar HTTP API**

### Webview → extension host messages

```ts
export type WebviewToExtensionMessage =
  | { type: 'chat.ask'; prompt: string }
  | { type: 'chat.retry'; messageId: string }
  | { type: 'request.selected'; requestId: string; symbol?: string; question?: string; answer?: string; context: PromptContextDto }
  | { type: 'context.openInspector' }
  | { type: 'impact.open'; symbol?: string }
  | { type: 'impact.openFiles'; filePaths: string[] }
  | { type: 'accordion.toggle'; group: 'environment' | 'contextSummary' | 'advancedInfo'; expanded: boolean }
  | { type: 'composer.resize'; height: number }
  | { type: 'feedback.submit'; messageId: string; rating: 'up' | 'down' };
```

### Extension host → webview messages

```ts
export type ExtensionToWebviewMessage =
  | { type: 'chat.requestStarted'; requestId: string; symbol?: string }
  | { type: 'chat.streamChunk'; requestId: string; chunk: string }
  | { type: 'chat.completed'; requestId: string; answer: string; context: PromptContextDto }
  | { type: 'chat.failed'; requestId: string; error: string }
  | { type: 'state.editor'; activeFile: string | null; symbol: string | null; isDirty: boolean }
  | { type: 'state.backend'; sidecarHealth: string; cloudStatus: string; workspaceId: string }
  | { type: 'impact.loaded'; data: ImpactViewDto }
  | { type: 'dashboard.loaded'; data: DashboardDto };
```

### Sidecar endpoints used by this UI

| Method | Path | UI use |
|---|---|---|
| `GET` | `/health` | Header and dashboard health badges |
| `POST` | `/ask` | Non-streaming fallback |
| `POST` | `/ask/stream` | Primary chat flow |
| `POST` | `/search` | Search surface |
| `POST` | `/search/unified` | Mixed symbol/doc/graph search |
| `GET` | `/impact` | Impact Explorer |
| `POST` | `/overlay` | Dirty-file sync |
| `DELETE` | `/overlay` | Clear overlay on save/close |
| `POST` | `/index` | Index workspace action |
| `POST` | `/index/file` | Reindex current file action |
| `POST` | `/index/files` | Batched save/refactor updates |
| `POST` | `/index/docs` | Index repository documentation |
| `GET` | `/index/queue` | Dashboard queue state |
| `POST` | `/history/ask` | Persist sanitized request/history snapshot |
| `POST` | `/feedback` | Accept/reject feedback |
| `POST` | `/auth/token` | Local workspace-scoped token bootstrap |
| `GET` | `/status/cloud` | Cloud/local mode indicator |
| `GET` | `/audit/actions` | Dashboard recent activity |
| `GET` | `/metrics` | Dashboard metrics |

All endpoints in this table, including `/metrics`, are implemented. Optional
providers may still be degraded or unavailable, so the UI must preserve partial
health states.

## Interaction Flows

### Flow 1: Ask about current symbol

1. The user places the cursor inside a symbol.
2. The extension host resolves the current symbol.
3. If the file is dirty, the extension sends `POST /overlay` with in-memory content.
4. The webview sends `chat.ask`.
5. The extension host calls `POST /ask/stream`.
6. The Chat Panel renders streaming chunks into the latest response card.
7. When the request completes, the host stores the returned `context` payload.
8. The `Context Summary` accordion becomes populated.
9. The user can open the Context Inspector for the evidence carried by the current prompt contract.

### Flow 2: Inspect context

1. The user clicks `Inspect Context`.
2. The webview switches to the inspector view.
3. The inspector reads the selected request's `PromptContext`; if no request is selected, it falls back to the most recent completed ask.
4. The UI renders tabs for primary source, graph context, documentation, and token breakdown.
5. The user can open a selected symbol in the editor.

### Flow 3: Show impact

1. The user clicks `Impact` from the action row or an inline CodeLens action.
2. If the user came from a selected ask, the tab first renders prompt-context impact from that request.
3. If live graph impact is requested, the extension host calls `GET /impact` for the selected symbol.
4. The UI renders `Affects`, `Files`, summary counts, and depth/source metadata.
5. The user opens a related file, opens up to 12 related files, starts a follow-up ask, or creates a refactor-plan prompt.

## Layout Rules

### Chat Panel sizing

- Minimum recommended width: `360px`
- Comfortable width: `420px–520px`
- Composer minimum height: `72px`
- Composer maximum auto-expanded height before internal scroll: `220px`
- Action row should wrap only as a last resort

### Vertical priorities

When panel height is constrained, preserve this order:

1. composer
2. latest response card
3. action row
4. collapsed groups
5. decorative chips

### Accordion rules

- All secondary groups start collapsed.
- Only one group may auto-open after a completed ask: `Context Summary`.
- Group titles must stay short and descriptive.
- Group contents should use dense rows instead of large cards.

## Examples

### TypeScript: send a streamed ask request

```ts
import * as vscode from 'vscode';

export async function askAboutCurrentSymbol(panel: vscode.WebviewPanel, prompt: string) {
  panel.webview.postMessage({
    type: 'chat.requestStarted',
    requestId: 'req-123',
    symbol: 'run_axis_retrieval',
  });

  // The extension host would proxy /ask/stream here.
  panel.webview.postMessage({
    type: 'chat.streamChunk',
    requestId: 'req-123',
    chunk: 'run_axis_retrieval() assembles ranked axis bundles...',
  });
}
```

### TypeScript: keep the composer anchored to the bottom

```ts
function layoutChatPanel(root: HTMLElement) {
  const composer = root.querySelector('[data-role="composer"]') as HTMLElement;
  const conversation = root.querySelector('[data-role="conversation"]') as HTMLElement;

  root.style.display = 'grid';
  root.style.gridTemplateRows = 'auto auto minmax(0, 1fr) auto auto';

  conversation.style.minHeight = '0';
  conversation.style.overflow = 'auto';
  composer.style.position = 'sticky';
  composer.style.bottom = '0';
}
```

### JSON: compact context summary for accordion rendering

```json
{
  "environment": {
    "workspace": "local/surgical_context@context-engine-refocus",
    "cloud": "connected",
    "mode": "surgical_full",
    "symbol": "run_axis_retrieval"
  },
  "contextSummary": {
    "primary": "run_axis_retrieval",
    "graphSymbols": 6,
    "docChunks": 2,
    "tokens": "3.4k vs est. 14.2k full-open-files"
  },
  "advancedInfo": {
    "intent": "exploration",
    "tiersUsed": ["code", "docs"],
    "isDirty": true
  }
}
```

## Limitations (current)

- The mockup and this spec define the target UX, not the current shipped extension behavior.
- The chat panel assumes reliable symbol resolution from the active editor; edge cases for unsupported files are still product work.
- `/metrics` exists, but the Dashboard must degrade gracefully when the sidecar or an optional provider is unavailable.
- `workspace_id` is populated; branch identity is encoded inside it rather than exposed as a separate field. Several axis score/intent/pruning fields may still be empty defaults.
- The bounded queue coalesces mass editor events; the UI should still distinguish queued, coalesced, rejected, and completed work rather than assume every dirty update is indexed immediately.

## Planned Extensions

- Add session-persistent accordion preferences per workspace.
- Add keyboard-first chat actions (`Enter` to send, `Shift+Enter` for newline, `Cmd/Ctrl+L` to focus composer).
- Add inline diff-aware ask mode for dirty symbols.
- Add richer token breakdown visualizations in the inspector.
- Add explicit degraded-state banners for cloud fallback and stale index conditions.
- Extend dashboard cards as richer axis prompt-contract diagnostics become populated.
