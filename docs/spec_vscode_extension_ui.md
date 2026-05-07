# VS Code Extension UI — Spec

`extension/ui/*`, `extension/webview/*` — defines the developer-facing UI contract for the Surgical Context VS Code extension. This spec describes the proposed extension UI based on the approved mockup direction: chat-first layout, evidence inspection, and graph-aware navigation for code exploration. The main UX changes requested for the chat panel are a bottom-docked composer, an auto-expanding response area above it, and collapsed info groups by default. fileciteturn0file0L139-L161 fileciteturn1file0L10-L18

## Overview

The Surgical Context extension adds a VS Code-native UI for asking questions about the current symbol, inspecting the context sent to the model, and exploring impact across code and docs. The extension is not a generic chatbot. Its primary value is transparent context assembly: the user can see what code symbols, documentation chunks, and metadata were included in a request. The extension UI should expose that value without overwhelming the user. fileciteturn0file0L8-L18 fileciteturn0file0L139-L161

Primary surfaces:

1. **Chat Panel** — ask questions about the current symbol and read streaming answers.
2. **Context Inspector** — inspect primary source, graph context, docs, prompt JSON, and token allocation.
3. **Impact Explorer** — inspect calls, reverse dependencies, docs covering a symbol, and likely blast radius.
4. **Dashboard** — inspect health, indexing status, token savings, and recent system activity. fileciteturn0file0L20-L31 fileciteturn0file0L54-L67

## Design

### Why this UI exists

The backend already supports graph expansion, doc retrieval, dirty-state overlays, streaming responses, impact lookup, health checks, cloud status, and operational signals. A thin chat-only panel would hide the strongest part of the system: explainable context selection. The UI therefore treats the answer and the evidence as first-class peers. fileciteturn0file0L20-L31 fileciteturn0file0L125-L148

### Why the chat panel uses a bottom composer

The approved mockup revision moves the prompt area to the bottom of the panel, keeps the response area above it, and collapses secondary info groups by default. This matches common chat application behavior, reduces visual noise, and keeps the user focused on the active conversation instead of configuration. The answer area should grow naturally with message height; metadata should stay available but unobtrusive. fileciteturn1file0L10-L18

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
- The current symbol is inferred from the active editor selection when possible. If no symbol is available, the Ask action falls back to standard mode or prompts the user to select a symbol. fileciteturn0file0L139-L148

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

The inspector consumes the `context` payload returned by `/ask` or `/ask/stream`. The payload includes `primary_source`, `graph_context`, and `documentation`. Implemented metadata already includes `mode`, `intent`, `tiers_used`, `tier_tokens`, `depth`, `direction`, and `relevance_score`. fileciteturn0file0L149-L173

#### Primary use case

A developer asks a question in the Chat Panel, then opens the inspector to verify:

- which symbol was treated as the seed
- which related symbols were added through graph traversal
- which doc chunks were matched semantically
- how much token budget code vs. docs consumed

### 3. Impact Explorer

The Impact Explorer visualizes likely change impact for the selected symbol.

#### Sections

- `Calls`
- `Called By`
- `Depends On`
- `Docs Covering`
- `Affects`

These sections map directly to the underlying graph model and retrieval strategy. The backend already models `CALLS_*`, `DEPENDS_ON`, `AFFECTS`, `FROM`, and `COVERS` relationships. fileciteturn0file0L180-L197

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

These metrics align with the existing architecture goals and planned observability layer. fileciteturn0file0L41-L52 fileciteturn0file0L54-L67

## State Model

The extension UI should use four state domains.

### Session state

| Field | Type | Purpose |
|---|---|---|
| `conversationId` | `string` | Current thread identity inside the panel |
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
  | { type: 'context.openInspector' }
  | { type: 'impact.open'; symbol?: string }
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
| `GET` | `/impact` | Impact Explorer |
| `POST` | `/overlay` | Dirty-file sync |
| `DELETE` | `/overlay` | Clear overlay on save/close |
| `POST` | `/index/file` | Reindex current file action |
| `GET` | `/status/cloud` | Cloud/local mode indicator |
| `GET` | `/audit/actions` | Dashboard recent activity |
| `GET` | `/metrics` | Dashboard metrics when implemented |

These endpoints are already implemented or planned in the architecture document. `/metrics` is explicitly marked planned. fileciteturn0file0L20-L31

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
9. The user can open the Context Inspector for full evidence. fileciteturn0file0L118-L123 fileciteturn0file0L139-L148

### Flow 2: Inspect context

1. The user clicks `Inspect Context`.
2. The webview switches to the inspector view.
3. The inspector reads the cached `PromptContext` from the most recent completed ask.
4. The UI renders tabs for primary source, graph context, documentation, and token breakdown.
5. The user can open a selected symbol in the editor.

### Flow 3: Show impact

1. The user clicks `Impact` from the action row or an inline CodeLens action.
2. The extension host calls `GET /impact` for the selected symbol.
3. The UI renders grouped sections for `Calls`, `Called By`, `Depends On`, `Docs Covering`, and `Affects`.
4. The user opens a follow-up ask or related file from the result set.

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
    symbol: 'GraphExpander.expand',
  });

  // The extension host would proxy /ask/stream here.
  panel.webview.postMessage({
    type: 'chat.streamChunk',
    requestId: 'req-123',
    chunk: 'GraphExpander.expand() performs a bounded expansion...',
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
    "mode": "surgical",
    "symbol": "GraphExpander.expand()"
  },
  "contextSummary": {
    "primary": "GraphExpander.expand",
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
- `/metrics` is planned, so the Dashboard must degrade gracefully when metrics are unavailable. fileciteturn0file0L20-L31
- Project/workspace/branch metadata in the prompt contract is still planned, so some inspector fields may need placeholder handling. fileciteturn0file0L149-L173
- Mass editor events and backpressure for indexing are still hardening items; the UI should not assume every dirty update is indexed immediately. fileciteturn0file0L175-L179

## Planned Extensions

- Add session-persistent accordion preferences per workspace.
- Add keyboard-first chat actions (`Enter` to send, `Shift+Enter` for newline, `Cmd/Ctrl+L` to focus composer).
- Add inline diff-aware ask mode for dirty symbols.
- Add richer token breakdown visualizations in the inspector.
- Add explicit degraded-state banners for cloud fallback and stale index conditions.
- Add dashboard cards for prompt-contract observability once runtime metrics ship. fileciteturn0file0L54-L67
