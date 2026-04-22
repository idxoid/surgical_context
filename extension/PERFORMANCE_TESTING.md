# Performance Testing Guide

Comprehensive performance benchmarking for Surgical Context extension.

## Running Tests

```bash
npm run perf-test
```

## Test Categories

### 1. 🚀 Extension Startup (Target: <2000ms)
- **Extension activation** - Time to load and activate extension
- **State initialization** - Initialize extension state

### 2. 🔌 Sidecar API Latency
- **Health check** (`/health`) - Target: <500ms
- **Cloud status** (`/cloud/status`) - Target: <800ms
- **Metrics fetch** (`/metrics`) - Target: <1500ms
- **Audit actions** (`/audit/actions`) - Target: <1000ms

### 3. 💬 Chat Response Performance
- **Time to first chunk** - Target: <2000ms (TTFB)
- **Total streaming duration** - Target: <10000ms
- **Message render time** - Target: <100ms per message

### 4. 💾 Memory Usage
- **Heap used** - Target: <500 MB
- **External memory** - Target: <100 MB

### 5. 🎨 UI Performance
- **Tab switch animation** - Target: <50ms (60fps)
- **Accordion expand** - Target: <50ms
- **Message list render** (50 items) - Target: <200ms

### 6. 🔍 Symbol Operations
- **Symbol detection** at cursor - Target: <50ms
- **Symbol list update** - Target: <200ms

## Interpreting Results

### ✅ Green (Passed)
```
✅ Health check: 250.00ms (threshold: 500ms)
```
Performance is within acceptable threshold.

### ❌ Red (Failed)
```
❌ Extension activation: 2500.00ms (threshold: 2000ms)
```
Performance exceeded threshold by 500ms - investigate and optimize.

## Performance Optimization Tips

### Extension Activation
- Lazy-load heavy modules
- Defer non-critical initialization
- Use `onCommand` activation events sparingly

### Chat Streaming
- Ensure SSE parsing is efficient
- Batch DOM updates in webview
- Use virtual scrolling for long conversations

### Memory
- Clean up disposables properly
- Limit conversation history size
- Use WeakMap for caches where appropriate

### UI Rendering
- Avoid force reflows/repaints
- Use CSS containment
- Debounce rapid updates

## Continuous Performance Monitoring

Add to CI/CD pipeline:
```bash
npm run perf-test || exit 1
```

Track results over time to catch regressions.

## Profiling Tools

### VS Code Extension Debugger
1. Run extension in debug mode
2. Open DevTools (Ctrl+Shift+I in webview)
3. Use Performance tab to profile

### Node.js Profiling
```bash
node --prof run-perf-tests.js
node --prof-process isolate-*.log > results.txt
```

### Chrome DevTools (Webview)
1. VS Code → Help → Toggle Developer Tools
2. Performance tab → Record
3. Interact with extension
4. Stop recording and analyze

## Baseline Thresholds

These are recommended starting points. Adjust based on your hardware:

| Metric | Threshold | Notes |
|--------|-----------|-------|
| Extension Activation | 2000ms | One-time cost |
| API Latency | 500-1500ms | Network-dependent |
| Chat First Chunk | 2000ms | Includes sidecar response |
| Message Render | 100ms | Per message |
| Tab Switch | 50ms | 60fps target |
| Memory (Heap) | 500MB | Max usage |

## Performance Regression Detection

Monitor these key metrics in CI:
- Extension activation time
- First chunk time for chat
- Memory usage trends
- API response times

Set alerts if:
- Any metric exceeds threshold by >20%
- Multiple metrics regress simultaneously
- Memory leaks detected (heap grows without reset)

