# Test Plan for Steps 1–3

## Quick Manual Verification

### Setup
1. Open VS Code with the extension loaded
2. Open `tests/fixtures/sample_project/`
3. Open the sidecar in another terminal: `python scripts/local_dev.py sidecar --reload`

### Test 1: Ask → Inspector (Should show same context)

**Steps:**
1. Click on the `process_payment` function in `src/core.py`
2. Type a question: "What does this function do?"
3. Click "Ask"
4. Wait for the answer to complete
5. Click the "Inspector" button in the response or sidebar

**Expected:**
- Inspector panel opens
- Shows the **exact same context** as what was sent to the model
- Title or subtitle shows the symbol: `process_payment`
- Shows the question: "What does this function do?"

**Verify:**
- No HTTP request made to sidecar (check network tab in DevTools)
- Inspector context matches Ask context (compare token count, symbols, docs)

---

### Test 2: Ask → Edit → Inspector (Should NOT refresh)

**Steps:**
1. Complete Test 1
2. Edit the `process_payment` function (change a variable name)
3. Click Inspector again

**Expected:**
- Inspector shows **the same context as before** (not refreshed)
- Context timestamp does not change
- The edit is not reflected in the context

**Verify:**
- Editing code does NOT trigger a new context fetch
- lastRequest is stable until 15 minutes or symbol change

---

### Test 3: Inspector Without Ask (Should show message)

**Steps:**
1. Restart VS Code extension
2. Immediately click Inspector (without asking about anything)

**Expected:**
- Inspector opens
- Shows message: "No context available. Ask about a symbol first."
- No error in console

**Verify:**
- Graceful fallback when lastRequest is undefined

---

### Test 4: State Persistence (Ask → Switch File → Inspector)

**Steps:**
1. Ask about `process_payment` in `src/core.py`
2. Close the file
3. Open a different file (`src/utils.py`)
4. Click Inspector

**Expected:**
- Inspector still shows context from `process_payment` Ask
- Symbol field shows: `process_payment`
- Context is not cleared by file switch

**Verify:**
- lastRequest persists across file changes
- TTL cleanup is not triggered by file switch (only triggered after 15 minutes)

---

### Test 5: Multiple Asks (Each overwrites previous)

**Steps:**
1. Ask about `process_payment`
2. Ask about `validate_amount`
3. Click Inspector

**Expected:**
- Inspector shows context for `validate_amount` (the latest ask)
- Symbol field shows: `validate_amount`
- Previous ask context is replaced

**Verify:**
- lastRequest is overwritten by each new Ask
- Inspector always shows the most recent Ask

---

## DevTools Console Tests

Open DevTools (F12) and check:

```javascript
// In extension console, after an Ask:
// You can't directly access stateManager from console, but you can:

// 1. Check webview message logs
// Look for: "chat.requestCompleted"
// Should have: requestId, answer, context

// 2. Check for "inspector.loaded" message
// When you click Inspector, look for this message in console
// Should have: context, symbol, question (not null)
```

---

## Code Review Checklist

- [ ] `ExtensionState.ts` compiles without errors
- [ ] `LastRequest` interface is properly exported
- [ ] `clearLastRequestIfStale()` logic is correct (15 min = 900000ms)
- [ ] `SurgicalContextViewProvider.ts` compiles without errors
- [ ] `handleAsk()` onDone callback stores lastRequest
- [ ] `showInspector()` reads from shared state (no HTTP call)
- [ ] `pushInspectorContext()` maintains backward compat
- [ ] All message types match webview protocol

---

## Expected Behavior Summary

| Action | Before | After |
|---|---|---|
| Ask about symbol | Stores in lastContext | Stores in lastRequest + lastContext |
| Click Inspector | HTTP call to fetch context | Read from lastRequest (instant) |
| Edit code, click Inspector | Shows updated context (diverges) | Shows old Ask context (consistent) |
| No Ask, click Inspector | Shows empty context | Shows "not available" message |
| Ask A, then Ask B, Inspector | Shows A (stale) | Shows B (latest) |

---

## Next Steps (If Tests Pass)

1. Commit changes: `git commit -m "Implement Ask/Inspect/Impact synchronization (steps 1–3)"`
2. Proceed to Step 4: Refactor showImpact() to use shared symbol
3. Proceed to Step 5: Wire TTL cleanup on editor changes

---

## Troubleshooting

### Inspector shows "not available" after Ask
- **Problem:** lastRequest not being stored
- **Check:** `handleAsk()` onDone callback is executing
- **Fix:** Verify `stateManager.setState({ lastRequest: {...} })` is called

### Inspector takes a long time to show (HTTP call happening)
- **Problem:** showInspector() is fetching new context
- **Check:** Is `pushInspectorContext()` being called instead of new showInspector()?
- **Fix:** Verify showInspector() does NOT call pushInspectorContext()

### TypeScript compilation error
- **Problem:** LastRequest type not exported or imported
- **Check:** Ensure export is in ExtensionState.ts
- **Fix:** Add to SurgicalContextViewProvider.ts imports if needed

### lastRequest cleared too early
- **Problem:** Context disappears after file switch
- **Check:** Is clearLastRequestIfStale() being called on file change?
- **Fix:** Should only clear if > 15 min elapsed, not on every change
