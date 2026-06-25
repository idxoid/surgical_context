import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import {
  extractFunctionName,
  measureAsync,
  measureSync,
  metricPassed,
  serializeChatChunks,
  summarizeMetrics,
  type PerformanceMetric,
} from '../performance/metrics.ts';

describe('performance metrics', () => {
  it('metricPassed accepts durations within threshold', () => {
    assert.equal(metricPassed({ name: 'ok', duration: 10, timestamp: '', threshold: 50 }), true);
    assert.equal(metricPassed({ name: 'slow', duration: 51, timestamp: '', threshold: 50 }), false);
    assert.equal(metricPassed({ name: 'no-threshold', duration: 999, timestamp: '' }), true);
  });

  it('summarizeMetrics splits passed and failed entries', () => {
    const metrics: PerformanceMetric[] = [
      { name: 'fast', duration: 5, timestamp: '', threshold: 10 },
      { name: 'slow', duration: 20, timestamp: '', threshold: 10 },
      { name: 'unbounded', duration: 100, timestamp: '' },
    ];

    const summary = summarizeMetrics(metrics);
    assert.equal(summary.passed.length, 2);
    assert.equal(summary.failed.length, 1);
    assert.equal(summary.failed[0]?.name, 'slow');
    assert.equal(summary.average, (5 + 20 + 100) / 3);
    assert.equal(summary.max, 100);
    assert.equal(summary.min, 5);
  });

  it('measureSync records timing and returns the function result', () => {
    const metrics: PerformanceMetric[] = [];
    const value = measureSync(metrics, 'add', () => 2 + 2, 100);

    assert.equal(value, 4);
    assert.equal(metrics.length, 1);
    assert.equal(metrics[0]?.name, 'add');
    assert.ok((metrics[0]?.duration ?? -1) >= 0);
  });

  it('measureAsync records timing for async work', async () => {
    const metrics: PerformanceMetric[] = [];
    const value = await measureAsync(metrics, 'delay', async () => {
      await new Promise(resolve => setTimeout(resolve, 1));
      return 'done';
    }, 50);

    assert.equal(value, 'done');
    assert.equal(metrics.length, 1);
    assert.ok((metrics[0]?.duration ?? -1) >= 1);
  });
});

describe('webview micro-benchmarks', () => {
  it('serializes many chat chunks within a generous budget', () => {
    const duration = serializeChatChunks(1000);
    assert.ok(duration < 2000, `expected <2000ms, got ${duration.toFixed(2)}ms`);
  });

  it('extracts a function name from source text quickly', () => {
    const source = 'function myFunction(param1: string): void { return; }';
    assert.equal(extractFunctionName(source), 'myFunction');
  });
});
