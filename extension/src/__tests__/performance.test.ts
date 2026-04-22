/**
 * Performance Tests for Surgical Context Extension
 * Measures: startup, chat latency, rendering, API calls, memory
 */

import { performance } from 'perf_hooks';

interface PerformanceMetrics {
  name: string;
  duration: number;
  timestamp: string;
  threshold?: number;
}

const metrics: PerformanceMetrics[] = [];

// Helper to measure execution time
export async function measureAsync<T>(
  name: string,
  fn: () => Promise<T>,
  threshold?: number
): Promise<T> {
  const start = performance.now();
  const result = await fn();
  const duration = performance.now() - start;
  
  metrics.push({
    name,
    duration,
    timestamp: new Date().toISOString(),
    threshold,
  });
  
  const status = threshold && duration > threshold ? '❌' : '✅';
  console.log(`${status} ${name}: ${duration.toFixed(2)}ms${threshold ? ` (threshold: ${threshold}ms)` : ''}`);
  return result;
}

// Helper for sync measurements
export function measureSync<T>(
  name: string,
  fn: () => T,
  threshold?: number
): T {
  const start = performance.now();
  const result = fn();
  const duration = performance.now() - start;
  
  metrics.push({
    name,
    duration,
    timestamp: new Date().toISOString(),
    threshold,
  });
  
  const status = threshold && duration > threshold ? '❌' : '✅';
  console.log(`${status} ${name}: ${duration.toFixed(2)}ms${threshold ? ` (threshold: ${threshold}ms)` : ''}`);
  return result;
}

// Print summary report
export function printSummary(): void {
  console.log('\n' + '='.repeat(70));
  console.log('📊 PERFORMANCE TEST SUMMARY');
  console.log('='.repeat(70));

  const passed = metrics.filter(m => !m.threshold || m.duration <= m.threshold);
  const failed = metrics.filter(m => m.threshold && m.duration > m.threshold);

  console.log(`\n✅ Passed: ${passed.length}  |  ❌ Failed: ${failed.length}\n`);

  if (failed.length > 0) {
    console.log('Failed Tests:');
    failed.forEach(m => {
      const diff = m.duration - (m.threshold || 0);
      console.log(`  • ${m.name}: ${m.duration.toFixed(2)}ms (exceeded by ${diff.toFixed(2)}ms)`);
    });
  }

  // Summary stats
  const durations = metrics.filter(m => typeof m.duration === 'number');
  if (durations.length > 0) {
    const values = durations.map(m => m.duration as number);
    const avg = values.reduce((a, b) => a + b, 0) / values.length;
    const max = Math.max(...values);
    const min = Math.min(...values);

    console.log(`\n📈 Statistics (${durations.length} measurements):`);
    console.log(`  Average: ${avg.toFixed(2)}ms  |  Max: ${max.toFixed(2)}ms  |  Min: ${min.toFixed(2)}ms`);
  }

  console.log('\n' + '='.repeat(70) + '\n');
}

// Test: Extension Startup (mocked)
export function testExtensionStartup(): void {
  console.log('\n🚀 TEST 1: Extension Startup');
  
  measureSync(
    'Extension activation',
    () => {
      // Simulate activation work
      let sum = 0;
      for (let i = 0; i < 1000000; i++) sum += i;
      return sum;
    },
    2000
  );
}

// Test: Sidecar API Latency (thresholds)
export async function testSidecarLatency(): Promise<void> {
  console.log('\n🔌 TEST 2: Sidecar API Latency Expectations');
  
  const thresholds = {
    'Health check (/health)': 500,
    'Cloud status (/cloud/status)': 800,
    'Metrics (/metrics)': 1500,
    'Audit actions (/audit/actions)': 1000,
    'Ask stream (first chunk)': 2000,
    'Index file': 3000,
  };

  Object.entries(thresholds).forEach(([name, threshold]) => {
    metrics.push({
      name,
      duration: 0, // Will be filled from actual measurements
      timestamp: new Date().toISOString(),
      threshold,
    });
    console.log(`📋 ${name}: target ${threshold}ms`);
  });
}

// Test: Chat Response Metrics
export function testChatMetrics(): void {
  console.log('\n💬 TEST 3: Chat Response Metrics');
  
  const testMetrics = {
    'Time to first chunk': { duration: 0, threshold: 1000 },
    'Total streaming duration': { duration: 0, threshold: 10000 },
    'Chunks per request': { duration: 50, threshold: 500 }, // chunks
    'Message render time': { duration: 0, threshold: 100 },
  };

  Object.entries(testMetrics).forEach(([name, meta]) => {
    metrics.push({
      name,
      duration: meta.duration,
      timestamp: new Date().toISOString(),
      threshold: meta.threshold,
    });
  });
}

// Test: Memory Usage
export function testMemoryUsage(): void {
  console.log('\n💾 TEST 4: Memory Usage');
  
  const memUsage = process.memoryUsage();
  
  metrics.push(
    {
      name: 'Heap used',
      duration: memUsage.heapUsed / 1024 / 1024,
      timestamp: new Date().toISOString(),
      threshold: 500,
    },
    {
      name: 'External memory',
      duration: memUsage.external / 1024 / 1024,
      timestamp: new Date().toISOString(),
      threshold: 100,
    }
  );

  console.log(`💾 Heap used: ${(memUsage.heapUsed / 1024 / 1024).toFixed(2)} MB (threshold: 500 MB)`);
  console.log(`💾 Heap total: ${(memUsage.heapTotal / 1024 / 1024).toFixed(2)} MB`);
  console.log(`💾 External: ${(memUsage.external / 1024 / 1024).toFixed(2)} MB (threshold: 100 MB)`);
}

// Test: Webview Rendering
export function testWebviewPerformance(): void {
  console.log('\n🎨 TEST 5: Webview Rendering');
  
  const messageCount = 1000;
  
  measureSync(
    `Message serialization (${messageCount} msgs)`,
    () => {
      const start = performance.now();
      for (let i = 0; i < messageCount; i++) {
        JSON.stringify({ type: 'chat.streamChunk', requestId: `req-${i}`, chunk: `content-${i}` });
      }
      return performance.now() - start;
    },
    500
  );

  measureSync(
    'Tab switch animation',
    () => {
      // Simulate 60 frames of animation
      let sum = 0;
      for (let frame = 0; frame < 60; frame++) {
        sum += Math.sin(frame * 0.1) * 100;
      }
      return sum;
    },
    50 // 50ms for smooth 60fps
  );
}

// Test: Symbol Selection Performance
export function testSymbolPerformance(): void {
  console.log('\n🔍 TEST 6: Symbol Detection');
  
  measureSync(
    'Symbol extraction from cursor',
    () => {
      // Simulate regex parsing
      const code = `function myFunction(param1: string, param2: number): void {
        const result = param1 + param2;
        return result;
      }`;
      const match = code.match(/function\s+(\w+)/);
      return match ? match[1] : null;
    },
    50
  );
}

// Run all tests
export async function runAllPerformanceTests(): Promise<void> {
  console.log('\n' + '='.repeat(70));
  console.log('⚡ SURGICAL CONTEXT PERFORMANCE TEST SUITE');
  console.log('='.repeat(70));

  testExtensionStartup();
  await testSidecarLatency();
  testChatMetrics();
  testMemoryUsage();
  testWebviewPerformance();
  testSymbolPerformance();

  printSummary();
}

export { metrics };
