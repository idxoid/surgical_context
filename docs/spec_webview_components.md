# Webview Component Model — Spec

`src/webview/*`, `src/ui/*`, `src/protocol/*` — defines the component model, layout rules, local state, and message protocol for Surgical Context webview surfaces. This spec covers the Chat Panel, Context Inspector, Impact Explorer, and Dashboard, with special focus on the approved chat layout: bottom-docked composer, response area above it, and collapsed info groups by default.

## Overview

The Surgical Context extension uses webviews for UI that exceeds native VS Code view capabilities. Webviews render rich layouts for streaming answers, evidence inspection, token breakdowns, impact summaries, and operational dashboards.

The webview layer must stay thin. Business logic remains in the extension host and context_engine. Webviews are responsible for rendering state, capturing user input, and sending typed messages to the extension host.

## Design

### Why webviews are needed

The extension needs UI patterns that tree views and simple quick picks do not handle well:

- streaming answer cards
- resizable multiline composer
- evidence tables with filters and snippet previews
- rich dashboards with metric cards and charts
- impact groups with badges and contextual actions

### Why the chat layout is fixed

The approved mockup revision makes three behaviors non-negotiable:

- the composer is docked to the bottom
- the response area sits above the composer and grows with content
- metadata groups are collapsed by default

This layout keeps the active task visually primary and makes transparency optional but available.

### Main trade-offs

- **Gain:** flexible layout and interaction model.
- **Gain:** message protocol decouples rendering from backend operations.
- **Cost:** webview lifecycle and state restoration add complexity.
- **Cost:** accessibility and theme parity require explicit work.

## Component Model

### Shared shell

Every webview surface should use the same shell primitives:

```text
WebviewShell
├─ SurfaceHeader
├─ SurfaceToolbar
├─ SurfaceBody
├─ SurfaceFooter
└─ ToastLayer
```

Shared responsibilities:

- apply VS Code theme variables
- host loading, empty, and error states
- expose telemetry hooks
- handle message bridge setup

### Chat Panel

```text
ChatPanel
├─ ChatHeader
├─ ActionRow
├─ ConversationViewport
│  ├─ MessageList
│  │  ├─ UserMessageCard[]
│  │  ├─ AssistantMessageCard[]
│  │  └─ StreamCursor
│  └─ ScrollAnchor
├─ InfoAccordionGroup
│  ├─ EnvironmentAccordion
│  ├─ ContextSummaryAccordion
│  └─ AdvancedInfoAccordion
├─ ComposerDock
│  ├─ AutoGrowTextarea
│  ├─ ComposerActions
│  └─ SendButton
└─ StatusChipRow
```

#### Layout rules

- `ComposerDock` is pinned to the bottom edge.
- `ConversationViewport` consumes remaining height above the dock.
- `AutoGrowTextarea` expands until a maximum height, then becomes scrollable.
- Accordion groups render collapsed on first load.
- Status chips remain compact and never displace the composer.

#### Height contract

| Element | Rule |
|---|---|
| Composer minimum height | single-line plus padding |
| Composer maximum height | about 30–35% of panel height |
| Conversation viewport | fills remaining vertical space |
| Accordion section | collapsed by default, content lazy-mounted |

### Context Inspector

```text
ContextInspector
├─ InspectorHeader
├─ InspectorTabs
├─ SummaryBar
├─ InspectorBody
│  ├─ PrimarySourceTab
│  ├─ GraphContextTab
│  ├─ DocumentationTab
│  ├─ PromptJsonTab
│  └─ TokenBreakdownTab
└─ InspectorFooter
```

#### `GraphContextTab`

```text
GraphContextTab
├─ WhyIncludedBanner
├─ FilterBar
├─ SymbolTable
├─ SelectedSymbolPreview
└─ LinkedDocsTable
```

Required fields in the symbol table:

- symbol name
- relation type
- depth
- relevance score
- dirty flag
- file path

### Impact Explorer

```text
ImpactExplorer
├─ ImpactHeader
├─ SymbolSummaryCard
├─ ActionButtonRow
├─ ImpactGroupList
│  ├─ CallsGroup
│  ├─ CalledByGroup
│  ├─ DependsOnGroup
│  ├─ DocsCoveringGroup
│  └─ AffectsGroup
└─ ImpactFooter
```

Each group supports collapsed and expanded states. Expanded rows may include file path, relation badge, and quick-open action.

### Dashboard

```text
Dashboard
├─ DashboardHeader
├─ MetricCardGrid
├─ SavingsChartCard
├─ RecentIndexJobsCard
├─ AuditEventsCard
└─ DashboardFooter
```

## State

### Local UI state

| Field | Type | Surface | Purpose |
|---|---|---|---|
| `composerText` | `string` | Chat | Current unsent prompt |
| `composerHeightPx` | `number` | Chat | Auto-grow height |
| `expandedAccordions` | `Record<string, boolean>` | Chat, Impact | Open/closed UI state |
| `activeTab` | `InspectorTabId` | Inspector | Current tab |
| `selectedGraphRowId` | `string \| null` | Inspector | Selected symbol row |
| `selectedImpactGroup` | `string \| null` | Impact | Highlighted group |
| `chartRange` | `'24h' \| '7d' \| '30d'` | Dashboard | Time range filter |

### Restored state

Persist only UI state that improves continuity:

- active inspector tab
- expanded accordion groups
- composer draft text
- selected graph row
- dashboard time range

Do not persist transient streaming buffers after dispose.

## API / Interface

### Message protocol principles

- All messages are typed.
- Webviews never call sidecar HTTP APIs directly.
- The extension host is the only bridge to VS Code APIs and the context_engine.
- Every request message has a response, progress, or error path.

### Webview → extension host messages

```ts
export type WebviewToExtensionMessage =
  | { type: 'chat.ask'; prompt: string; symbol?: string }
  | { type: 'chat.stop'; requestId: string }
  | { type: 'chat.retry'; messageId: string }
  | { type: 'composer.changed'; text: string; heightPx: number }
  | { type: 'accordion.toggled'; id: string; expanded: boolean }
  | { type: 'inspector.tabChanged'; tab: 'primary' | 'graph' | 'docs' | 'promptJson' | 'tokens' }
  | { type: 'inspector.rowSelected'; rowId: string }
  | { type: 'impact.groupToggled'; group: 'calls' | 'calledBy' | 'dependsOn' | 'docsCovering' | 'affects'; expanded: boolean }
  | { type: 'impact.openFile'; filePath: string; line?: number }
  | { type: 'dashboard.refresh' }
  | { type: 'link.openExternal'; href: string }
  | { type: 'feedback.submit'; messageId: string; rating: 'up' | 'down' };
```

### Extension host → webview messages

```ts
export type ExtensionToWebviewMessage =
  | { type: 'surface.init'; surface: 'chat' | 'inspector' | 'impact' | 'dashboard'; state: unknown }
  | { type: 'chat.requestStarted'; requestId: string; symbol?: string }
  | { type: 'chat.streamChunk'; requestId: string; chunk: string }
  | { type: 'chat.requestCompleted'; requestId: string; answer: string; context: PromptContextDto }
  | { type: 'chat.requestFailed'; requestId: string; error: string }
  | { type: 'chat.requestStopped'; requestId: string }
  | { type: 'chat.contextSummary'; summary: ContextSummaryDto }
  | { type: 'inspector.loaded'; payload: InspectorDto }
  | { type: 'impact.loaded'; payload: ImpactDto }
  | { type: 'dashboard.loaded'; payload: DashboardDto }
  | { type: 'workspace.updated'; workspaceId: string; activeFile: string | null; symbol: string | null; isDirty: boolean }
  | { type: 'backend.updated'; sidecarHealth: 'up' | 'down' | 'degraded'; cloudStatus: 'connected' | 'fallback-local' | 'offline' }
  | { type: 'toast.show'; level: 'info' | 'warning' | 'error'; message: string };
```

### DTO outlines

#### `PromptContextDto`

```ts
export interface PromptContextDto {
  primary_source: {
    symbol: string;
    file_path: string;
    is_dirty: boolean;
    code: string;
  };
  graph_context: Array<{
    symbol: string;
    file_path: string;
    relation: string;
    is_dirty: boolean;
    code: string;
    depth?: number;
    relevance_score?: number;
  }>;
  documentation: Array<{
    chunk_id: string;
    source_file: string;
    content: string;
    relevance_score?: number;
  }>;
  metadata?: {
    mode?: string;
    intent?: string;
    tiers_used?: string[];
    tier_tokens?: Record<string, number>;
  };
}
```

#### `ContextSummaryDto`

```ts
export interface ContextSummaryDto {
  primaryLabel: string;
  graphCount: number;
  docsCount: number;
  tokenText: string;
  chips: string[];
}
```

## Examples

### Example: auto-grow composer handler

```ts
const MIN_ROWS = 1;
const MAX_ROWS = 8;

export function measureComposerHeight(textarea: HTMLTextAreaElement): number {
  textarea.style.height = 'auto';
  const next = textarea.scrollHeight;
  const lineHeight = 24;
  const min = MIN_ROWS * lineHeight;
  const max = MAX_ROWS * lineHeight;
  return Math.max(min, Math.min(next, max));
}
```

### Example: streaming message reducer

```ts
export function appendStreamChunk(state: ChatState, requestId: string, chunk: string): ChatState {
  return {
    ...state,
    messages: state.messages.map((message) =>
      message.requestId === requestId
        ? { ...message, body: `${message.body}${chunk}`, status: 'streaming' }
        : message,
    ),
  };
}
```

### Example: accordion defaults

```ts
export const defaultAccordionState = {
  environment: false,
  contextSummary: false,
  advancedInfo: false,
};
```

## Accessibility

- The composer must support keyboard-only send.
- Accordions must expose `aria-expanded` and proper button semantics.
- Tables require visible focus styles and row selection states.
- Charts need text summaries for screen readers.
- Icon-only buttons require labels.

## Theme Rules

- Use VS Code CSS variables instead of hard-coded colors where possible.
- Keep chip colors semantic but subtle.
- Do not use bright accent colors for large surfaces.
- Preserve readable contrast for code snippets and metadata tables.

## Limitations (current)

- Webview state restoration is per-surface and may drift if the extension host changes schema without migration.
- Large context payloads may make inspector tabs expensive to render if rows are not virtualized.
- Dashboard charts need a non-chart fallback for low-power or accessibility modes.
- Impact groups may grow large enough to require lazy expansion and row virtualization.

## Planned Extensions

- Add row virtualization for graph and docs tables.
- Add diff-aware message cards when the selected symbol changed between asks.
- Add compare mode for two ask results.
- Add saved prompt presets for common workflows such as explain, refactor, and risk review.
