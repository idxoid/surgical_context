# VS Code Manifest Surface — Spec

`package.json`, `src/extension.ts`, `src/commands/*` — defines the extension manifest, contributed views, commands, menus, configuration, and activation rules for the Surgical Context VS Code extension. This spec describes how the extension should appear to VS Code and how the workbench entry points map to the approved UI surfaces: Chat Panel, Context Inspector, Impact Explorer, and Dashboard.

## Overview

The Surgical Context extension exposes four user-facing UI surfaces through native VS Code contribution points:

1. **Sidebar Chat Panel** — default entry point for asking questions about the current symbol.
2. **Context Inspector** — webview panel for inspecting prompt context and evidence.
3. **Impact Explorer** — tree or webview-based impact analysis surface.
4. **Dashboard** — operational overview for indexing, health, and token savings.

The manifest should favor native VS Code affordances where possible: activity bar containers, views, commands, menus, keybindings, and configuration. The extension should not depend on hidden commands or custom title bars for core workflows.

## Design

### Why the manifest is opinionated

The backend already exposes stable endpoint categories for ask, search, overlay, impact, auth, cloud status, audit, and health. The manifest should make those capabilities discoverable through a small number of obvious workbench entry points instead of many scattered commands.

### Main trade-offs

- **Gain:** native VS Code contributions reduce user learning cost.
- **Gain:** clear command surface makes code review and QA easier.
- **Gain:** workbench menus support keyboard-first users.
- **Cost:** more manifest declarations increase maintenance overhead.
- **Cost:** webview-heavy surfaces require explicit state hydration.

## Manifest Structure

### Activity bar container

Create a single custom container:

- **ID:** `surgicalContext`
- **Title:** `Surgical Context`
- **Icon:** extension icon asset

This container owns the default sidebar experience.

### Primary views inside the container

| View ID | Type | Title | Purpose |
|---|---|---|---|
| `surgicalContext.chat` | `webviewView` | `Chat` | Chat-first ask surface |
| `surgicalContext.impact` | `treeView` or `webviewView` | `Impact` | Calls, reverse deps, docs, affects |
| `surgicalContext.status` | `treeView` | `Status` | Compact health, workspace, cloud status |

### Secondary panels

| Surface | Suggested implementation | Opened by |
|---|---|---|
| Context Inspector | `WebviewPanel` | command or chat action |
| Dashboard | `WebviewPanel` or editor webview tab | command |
| Search results | optional `WebviewPanel` | command |

## API / Interface

### Recommended `package.json` sections

- `activationEvents`
- `contributes.viewsContainers`
- `contributes.views`
- `contributes.commands`
- `contributes.menus`
- `contributes.keybindings`
- `contributes.configuration`
- `contributes.configurationDefaults` (optional)

### Activation events

Use narrow activation where practical.

```json
{
  "activationEvents": [
    "onView:surgicalContext.chat",
    "onCommand:surgicalContext.askCurrentSymbol",
    "onCommand:surgicalContext.openInspector",
    "onCommand:surgicalContext.showImpact",
    "onCommand:surgicalContext.openDashboard",
    "workspaceContains:**/.git"
  ]
}
```

`onStartupFinished` is acceptable only if background overlay sync or health polling must start immediately. Otherwise prefer lazy activation.

### Commands

#### Required commands

| Command ID | Title | When to use |
|---|---|---|
| `surgicalContext.askCurrentSymbol` | `Ask About Current Symbol` | Main ask workflow |
| `surgicalContext.askSelection` | `Ask About Selection` | Explicit selection-based ask |
| `surgicalContext.openInspector` | `Open Context Inspector` | Inspect last prompt context |
| `surgicalContext.showImpact` | `Show Impact` | Open impact surface for active symbol |
| `surgicalContext.findDocs` | `Find Related Docs` | Retrieve linked or semantic docs |
| `surgicalContext.openDashboard` | `Open Dashboard` | Open operational dashboard |
| `surgicalContext.reindexCurrentFile` | `Reindex Current File` | Force single-file update |
| `surgicalContext.toggleOverlaySync` | `Toggle Overlay Sync` | Debug or testing workflow |
| `surgicalContext.searchWorkspace` | `Search Surgical Context` | Semantic/code-aware search |

#### Optional commands

| Command ID | Title | Notes |
|---|---|---|
| `surgicalContext.retryLastAsk` | `Retry Last Ask` | Helpful for transient failures |
| `surgicalContext.createRefactorPlan` | `Create Refactor Plan` | Best opened from impact surface |
| `surgicalContext.copyContextSummary` | `Copy Context Summary` | Debugging and support |

### Menus

#### Editor title / inline actions

Surface a small number of context-sensitive actions when a supported file is open.

```json
{
  "command": "surgicalContext.askCurrentSymbol",
  "when": "editorTextFocus && resourceExtname =~ /\.(ts|tsx|py)$/",
  "group": "navigation@10"
}
```

Recommended editor/context menu commands:

- `Ask About Current Symbol`
- `Show Impact`
- `Find Related Docs`
- `Open Context Inspector`

#### View title actions

Add compact title bar commands to `surgicalContext.chat`:

- `Ask About Current Symbol`
- `Open Context Inspector`
- `Show Impact`
- `Open Dashboard`

#### Command palette

All required commands must be visible in the command palette with consistent `Surgical Context:` prefixes.

### Keybindings

Do not overload common VS Code defaults. Prefer opt-in bindings.

```json
[
  {
    "command": "surgicalContext.askCurrentSymbol",
    "key": "ctrl+alt+a",
    "mac": "cmd+alt+a",
    "when": "editorTextFocus"
  },
  {
    "command": "surgicalContext.showImpact",
    "key": "ctrl+alt+i",
    "mac": "cmd+alt+i",
    "when": "editorTextFocus"
  }
]
```

### Configuration

#### Required settings

| Setting | Type | Default | Purpose |
|---|---|---|---|
| `surgicalContext.backendUrl` | `string` | `http://localhost:8000` | Sidecar base URL |
| `surgicalContext.workspaceId` | `string` | empty | Optional workspace scope override; blank derives from VS Code workspace + Git branch |
| `surgicalContext.overlaySync` | `boolean` | `true` | Enable dirty-state sync |
| `surgicalContext.chat.autoOpenInspector` | `boolean` | `false` | Open inspector after completed ask |
| `surgicalContext.dashboard.autoRefreshSeconds` | `number` | `30` | Dashboard polling interval |
| `surgicalContext.experimental.searchPanel` | `boolean` | `false` | Enable search webview |

#### Suggested setting descriptions

Write settings descriptions as task-oriented sentences.

Example:

```json
{
  "surgicalContext.overlaySync": {
    "type": "boolean",
    "default": true,
    "description": "Send unsaved editor content to the sidecar so asks use the latest in-memory code."
  }
}
```

## Example Manifest Fragments

### Views container

```json
{
  "contributes": {
    "viewsContainers": {
      "activitybar": [
        {
          "id": "surgicalContext",
          "title": "Surgical Context",
          "icon": "media/surgical-context.svg"
        }
      ]
    }
  }
}
```

### Views

```json
{
  "contributes": {
    "views": {
      "surgicalContext": [
        {
          "id": "surgicalContext.chat",
          "name": "Chat",
          "type": "webview"
        },
        {
          "id": "surgicalContext.impact",
          "name": "Impact"
        },
        {
          "id": "surgicalContext.status",
          "name": "Status"
        }
      ]
    }
  }
}
```

### Commands

```json
{
  "contributes": {
    "commands": [
      {
        "command": "surgicalContext.askCurrentSymbol",
        "title": "Surgical Context: Ask About Current Symbol",
        "category": "Surgical Context"
      },
      {
        "command": "surgicalContext.openInspector",
        "title": "Surgical Context: Open Context Inspector",
        "category": "Surgical Context"
      },
      {
        "command": "surgicalContext.openDashboard",
        "title": "Surgical Context: Open Dashboard",
        "category": "Surgical Context"
      }
    ]
  }
}
```

## Workflows

### Workflow: Ask from editor

1. User places the cursor inside a symbol.
2. User runs `Surgical Context: Ask About Current Symbol`.
3. Extension resolves the active symbol.
4. Extension posts the ask request to the chat webview or opens the chat view if needed.
5. Extension host sends `/overlay` first if the editor is dirty and overlay sync is enabled.
6. Extension host sends `/ask/stream`.
7. Chat view streams response chunks into the conversation area.
8. User opens the Context Inspector from the action row or message footer.

### Workflow: Show impact

1. User runs `Surgical Context: Show Impact` from the editor title or command palette.
2. Extension resolves the active symbol.
3. Extension host requests `/impact`.
4. Impact surface renders `Calls`, `Called By`, `Depends On`, `Docs Covering`, and `Affects`.
5. User opens related files or asks a follow-up.

### Workflow: Open dashboard

1. User runs `Surgical Context: Open Dashboard`.
2. Extension host requests health, cloud status, and operational summaries.
3. Dashboard webview renders cards and recent activity.
4. Dashboard refreshes on the configured interval while visible.

## Limitations (current)

- VS Code manifest cannot express all runtime state; webviews still need explicit hydration from the extension host.
- `webviewView` surfaces are narrower than full editor tabs and require compact layouts.
- Tree view actions are easier to keep native, but rich evidence tables fit better in webviews.
- Keyboard shortcuts may conflict with local user preferences.

## Planned Extensions

- Add `Walkthrough` contribution for onboarding.
- Add inline CodeLens contributions for `Ask`, `Impact`, `Find Docs`, and `Inspect Context`.
- Add `notebook` integration if the project later targets data-science workflows.
- Add profile-specific configuration presets for local-only vs. cloud-enabled usage.
