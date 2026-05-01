# Week 1 Implementation Guide

> **Status:** Day 1 — Documentation updates complete. Days 2–3 focus on Ask/Inspect/Impact sync + metadata visibility.

---

## Completed ✅

### Documentation Updates (Monday)
- ✅ Updated `architectura.md` § 2.4 with Phase 9.1/9.3/9.4 status
- ✅ Updated `architectura.md` § 4.1 with mechanism routing + Phase 9 integration
- ✅ Updated `road_map.md` Phase 9 section with completion status
- ✅ Created `WEEK_1_SYNCHRONIZATION.md` as primary planning doc

---

## Remaining: Ask/Inspect/Impact Synchronization

### Architecture Change

**Current (broken):**
```
User clicks "Ask" 
  → POST /ask 
  → stores lastContext in stateManager
  → webview renders answer

User clicks "Inspector"
  → pushInspectorContext() calls fetchContext again
  → webview shows new context (may differ from Ask)
  → Inspector state diverged from Ask state ❌

User clicks "Impact"
  → loadImpact() calls POST /impact with new symbol
  → shows AFFECTS edges for that symbol
  → unrelated to Ask context ❌
```

**Target (fixed):**
```
User clicks "Ask"
  → POST /ask
  → stores full response in stateManager.lastRequest = {
      symbol, question, timestamp, context, answer
    }
  → webview renders answer

User clicks "Inspector"
  → reads stateManager.lastRequest.context
  → webview shows same context as Ask ✅

User clicks "Impact"
  → uses stateManager.lastRequest.symbol
  → shows AFFECTS for the symbol that was asked about ✅
  → all three surfaces share request state
```

---

## Implementation (Days 2–3)

### Step 1: Extend ExtensionState

**File:** `extension/src/state/ExtensionState.ts`

```typescript
// Add after lastContext
export interface LastRequest {
  symbol?: string;
  question?: string;
  timestamp: number;
  context?: PromptContextPayload;
  answer: string;
}

export interface ExtensionState {
  selectedSymbol: string | undefined;
  activeFile: string | undefined;
  isDirty: boolean;
  lastContext: PromptContextPayload | undefined;  // Keep for backward compat
  lastRequest: LastRequest | undefined;           // NEW: full ask response
  sidecarHealth: 'up' | 'down' | 'degraded';
  cloudStatus: 'connected' | 'fallback-local' | 'local' | 'offline';
  workspaceId: string;
  authState: 'ready' | 'missing-token' | 'expired';
}

export const defaultState: ExtensionState = {
  // ... existing
  lastRequest: undefined,  // NEW
};

class StateManager {
  // ... existing
  
  /**
   * Clear lastRequest if user selected a new symbol or 15+ minutes passed.
   * Called when editor changes symbol or on TTL timeout.
   */
  clearLastRequestIfStale(): void {
    const state = this.state;
    const now = Date.now();
    const ttlMs = 15 * 60 * 1000;
    
    if (state.lastRequest && (now - state.lastRequest.timestamp) > ttlMs) {
      this.setState({ lastRequest: undefined });
    }
  }
}
```

### Step 2: Update SurgicalContextViewProvider.handleAsk()

**File:** `extension/src/providers/SurgicalContextViewProvider.ts`

Change the `onDone` callback:

```typescript
// OLD (line ~272):
onDone: (traceId: string) => {
  const context = latestContext || stateManager.getState().lastContext || null;
  if (context) {
    this.postMessage({
      type: 'chat.requestCompleted',
      requestId,
      answer: '',
      context,
    });
  }
  void this.persistAskHistory({...});
};

// NEW:
onDone: (traceId: string) => {
  const answer = answerParts.join('');
  const context = latestContext || stateManager.getState().lastContext || null;
  
  // Store full request in shared state
  if (context) {
    stateManager.setState({
      lastRequest: {
        symbol: targetSymbol,
        question: prompt,
        timestamp: Date.now(),
        context,
        answer,
      },
    });
  }
  
  this.postMessage({
    type: 'chat.requestCompleted',
    requestId,
    answer: '',
    context,
  });
  
  void this.persistAskHistory({...});
};
```

### Step 3: Update showInspector() to Use Shared State

**File:** `extension/src/providers/SurgicalContextViewProvider.ts`

Replace `pushInspectorContext()` call:

```typescript
// OLD (line ~72):
public showInspector(): void {
  this.postMessage({ type: 'surface.showInspector' });
  this.pushInspectorContext();  // Fetches new context ❌
}

// NEW:
public showInspector(): void {
  this.postMessage({ type: 'surface.showInspector' });
  
  const state = stateManager.getState();
  if (state.lastRequest?.context) {
    // Inspector reads from shared state
    this.postMessage({
      type: 'inspector.context',
      context: state.lastRequest.context,
      symbol: state.lastRequest.symbol,
      question: state.lastRequest.question,
    });
  } else {
    this.postMessage({
      type: 'inspector.notAvailable',
      message: 'No context available. Ask about a symbol first.',
    });
  }
}
```

**Remove or simplify `pushInspectorContext()` method** — it's no longer needed for the default flow.

### Step 4: Update showImpact() to Use Shared Symbol

**File:** `extension/src/providers/SurgicalContextViewProvider.ts`

```typescript
// OLD (line ~77):
public async showImpact(symbol?: string): Promise<void> {
  const targetSymbol = symbol || this.currentEditorSymbol();
  if (!targetSymbol) {
    this.postMessage({
      type: 'impact.loadFailed',
      error: 'No symbol selected.',
    });
    return;
  }
  await this.loadImpact(targetSymbol);
}

// NEW:
public async showImpact(symbol?: string): Promise<void> {
  // Priority: explicit symbol > lastRequest.symbol > editor cursor > fail
  let targetSymbol = symbol;
  
  if (!targetSymbol) {
    const lastRequest = stateManager.getState().lastRequest;
    if (lastRequest?.symbol) {
      targetSymbol = lastRequest.symbol;  // Use shared Ask symbol
    } else {
      targetSymbol = this.currentEditorSymbol();
    }
  }
  
  if (!targetSymbol) {
    this.postMessage({
      type: 'impact.loadFailed',
      error: 'No symbol selected. Position cursor on a symbol or ask about it first.',
    });
    return;
  }
  
  await this.loadImpact(targetSymbol);
}
```

### Step 5: Wire State TTL Cleanup

**File:** `extension/src/extension.ts`

In the `activate()` function, add TTL check when editor changes:

```typescript
// In activate(), after registering document lifecycle subscriptions:
context.subscriptions.push(
  vscode.window.onDidChangeActiveTextEditor(() => {
    // Clear stale lastRequest when user switches files
    stateManager.clearLastRequestIfStale();
  })
);
```

### Step 6: Update Webview Message Protocol

**File:** `extension/src/webview/shared/protocol.ts`

Add new message types:

```typescript
// Add to HostToWebviewMessage union:
| {
    type: 'inspector.context';
    context: PromptContextPayload;
    symbol?: string;
    question?: string;
  }
| {
    type: 'inspector.notAvailable';
    message: string;
  };
```

### Step 7: Update Webview Handler

**File:** `extension/src/webview/main.ts` (or wherever webview message handling occurs)

Add handler for new message types:

```typescript
case 'inspector.context':
  // Update inspector panel with shared context
  inspector.setContext(message.context);
  inspector.setQuestion(message.question);
  showPanel('inspector');
  break;

case 'inspector.notAvailable':
  showNotification(message.message, 'info');
  break;
```

---

## Testing Checklist

### Unit Tests
- [ ] `ExtensionState` — verify `lastRequest` storage and TTL clearing
- [ ] `showInspector()` — verify reads from `stateManager.lastRequest`
- [ ] `showImpact()` — verify uses `stateManager.lastRequest.symbol` as fallback

### Integration Tests
- [ ] Ask → Inspector: Inspector shows same context as Ask
- [ ] Ask → Edit file → Inspector: lastRequest TTL expires, Inspector shows "not available"
- [ ] Ask → Impact: Impact shows AFFECTS for the asked-about symbol
- [ ] Impact with explicit symbol: Impact uses passed symbol, ignores lastRequest

### Manual E2E
1. Open a sample repo
2. Click on a symbol, type a question, Ask
3. Verify answer renders
4. Click Inspector → verify context matches the Ask
5. Edit the file slightly
6. Click Inspector again → verify context is still from the Ask (not refreshed)
7. Wait 15 minutes (or mock the TTL)
8. Click Inspector → verify "context not available" message
9. Ask about a different symbol
10. Click Inspector → verify context from new Ask
11. Click Impact → verify AFFECTS for the new symbol

---

## Optional: Metadata Visibility (Day 3, if time permits)

### Add RankingDetails Component

If time allows, add a collapsible section in Inspector webview:

```typescript
// File: extension/src/webview/components/RankingDetails.tsx (new file)
interface Props {
  context: PromptContextPayload;
  collapsed?: boolean;
}

export function RankingDetails({ context, collapsed = true }: Props) {
  const [isOpen, setIsOpen] = useState(!collapsed);
  
  return (
    <div className="ranking-details">
      <button onClick={() => setIsOpen(!isOpen)}>
        {isOpen ? '▼' : '▶'} Ranking Scores
      </button>
      
      {isOpen && (
        <div className="ranking-content">
          {/* Intent */}
          <div>Intent: {context.intent}</div>
          <div>Mode: {context.mode}</div>
          
          {/* Per-symbol scores */}
          <h4>Retrieved Symbols</h4>
          {context.symbols?.map(s => (
            <div key={s.uid}>
              [{s.relevance_score?.toFixed(2)}] {s.name}
              <details>
                <summary>Scores</summary>
                <pre>{JSON.stringify(s.scores, null, 2)}</pre>
              </details>
            </div>
          ))}
          
          {/* Cache & assembly */}
          <h4>Assembly</h4>
          {context.metadata?.assembly && (
            <pre>{JSON.stringify(context.metadata.assembly, null, 2)}</pre>
          )}
        </div>
      )}
    </div>
  );
}
```

Then integrate into Inspector panel. **Defer if complex.**

---

## Success Criteria

### Code
- [ ] All 7 steps implemented
- [ ] TypeScript compiles without errors
- [ ] ExtensionState changes backward-compatible (lastContext still available)
- [ ] stateManager.clearLastRequestIfStale() is called on editor changes

### Behavior
- [ ] Ask stores full response in lastRequest
- [ ] Inspector reads from lastRequest (no new fetch)
- [ ] Impact uses lastRequest.symbol if available
- [ ] TTL clears stale context after 15 minutes or symbol change

### Testing
- [ ] Unit tests pass for state management
- [ ] E2E test passes: Ask → Inspector → Impact flow
- [ ] Metadata visibility added (optional, defer if needed)

---

## Notes

**Risk Areas:**
- If overlay changes between Ask and Inspector, they'll show different state (expected, but could confuse). Consider noting this in UI.
- If lastRequest.symbol is wrong (e.g., workspace-level ask), Impact will use it anyway. Test with filePath fallback.

**Deferred:**
- Full RankingDetails component (optional enhancement)
- Keyboard shortcut for Ask → Inspector → Impact pipeline
- Settings UI for disabling shared state

**Handoff to Week 2:**
- Once sync is stable, ready for real-repo benchmark runs
- Week 2 can surface ranking metadata if needed
