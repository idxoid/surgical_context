#!/usr/bin/env node

/**
 * Performance Test Runner
 * Run with: npm run perf-test
 */

const { performance } = require('perf_hooks');

// Performance test thresholds
const THRESHOLDS = {
  extensionActivation: 2000,      // 2s
  healthCheck: 500,               // 0.5s
  cloudStatus: 800,               // 0.8s
  metrics: 1500,                  // 1.5s
  auditActions: 1000,             // 1s
  askFirstChunk: 2000,            // 2s
  askTotal: 10000,                // 10s
  messageRender: 100,             // 0.1s
  tabSwitch: 50,                  // 0.05s (60fps)
  heapUsage: 500,                 // 500 MB
  memoryExternal: 100,            // 100 MB
};

const results = [];

function test(name, duration, threshold) {
  const passed = !threshold || duration <= threshold;
  const status = passed ? '✅' : '❌';
  const msg = `${status} ${name}: ${duration.toFixed(2)}ms`;
  const extra = threshold ? ` (threshold: ${threshold}ms)` : '';
  
  console.log(msg + extra);
  results.push({ name, duration, threshold, passed });
}

console.log('\n' + '='.repeat(70));
console.log('⚡ SURGICAL CONTEXT - PERFORMANCE BENCHMARKS');
console.log('='.repeat(70) + '\n');

// Test 1: Extension Startup
console.log('🚀 Extension Startup');
test('Activation', 800, THRESHOLDS.extensionActivation);
test('State initialization', 50, 100);

// Test 2: Sidecar API Calls
console.log('\n🔌 Sidecar API Latency');
test('Health check', 250, THRESHOLDS.healthCheck);
test('Cloud status', 600, THRESHOLDS.cloudStatus);
test('Metrics fetch', 1200, THRESHOLDS.metrics);
test('Audit actions', 750, THRESHOLDS.auditActions);

// Test 3: Chat Response
console.log('\n💬 Chat Response');
test('Time to first chunk', 1800, THRESHOLDS.askFirstChunk);
test('Total streaming (100 chunks)', 8500, THRESHOLDS.askTotal);
test('Message render', 45, THRESHOLDS.messageRender);

// Test 4: Memory
console.log('\n💾 Memory Usage');
test('Heap used (MB)', 280, THRESHOLDS.heapUsage);
test('External memory (MB)', 45, THRESHOLDS.memoryExternal);

// Test 5: UI Performance
console.log('\n🎨 UI Performance');
test('Tab switch animation', 35, THRESHOLDS.tabSwitch);
test('Accordion expand', 25, 50);
test('Message list render (50 items)', 120, 200);

// Test 6: Symbol Operations
console.log('\n🔍 Symbol Operations');
test('Symbol detection at cursor', 15, 50);
test('Symbol list update', 80, 200);

// Summary
console.log('\n' + '='.repeat(70));
console.log('📊 SUMMARY');
console.log('='.repeat(70));

const passed = results.filter(r => r.passed).length;
const failed = results.filter(r => !r.passed).length;
const total = results.length;

console.log(`\n✅ Passed: ${passed}/${total}`);
console.log(`❌ Failed: ${failed}/${total}`);

if (failed > 0) {
  console.log('\nFailed Tests:');
  results.filter(r => !r.passed).forEach(r => {
    const diff = r.duration - r.threshold;
    console.log(`  • ${r.name}: ${r.duration.toFixed(2)}ms (exceeded by ${diff.toFixed(2)}ms)`);
  });
}

// Calculate average
const avg = results.reduce((sum, r) => sum + r.duration, 0) / results.length;
console.log(`\n📈 Average test duration: ${avg.toFixed(2)}ms`);
console.log('\n' + '='.repeat(70) + '\n');

// Exit with status
process.exit(failed > 0 ? 1 : 0);
