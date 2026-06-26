import { performance } from 'node:perf_hooks';

export interface PerformanceMetric {
  name: string;
  duration: number;
  timestamp: string;
  threshold?: number;
}

export interface PerformanceSummary {
  passed: PerformanceMetric[];
  failed: PerformanceMetric[];
  average: number;
  max: number;
  min: number;
}

export function metricPassed(metric: PerformanceMetric): boolean {
  return metric.threshold === undefined || metric.duration <= metric.threshold;
}

export function summarizeMetrics(metrics: PerformanceMetric[]): PerformanceSummary {
  const passed = metrics.filter(metricPassed);
  const failed = metrics.filter(metric => !metricPassed(metric));
  const durations = metrics.map(metric => metric.duration);

  if (durations.length === 0) {
    return { passed, failed, average: 0, max: 0, min: 0 };
  }

  const total = durations.reduce((sum, value) => sum + value, 0);
  return {
    passed,
    failed,
    average: total / durations.length,
    max: Math.max(...durations),
    min: Math.min(...durations),
  };
}

export function measureSync<T>(
  metrics: PerformanceMetric[],
  name: string,
  fn: () => T,
  threshold?: number,
): T {
  const start = performance.now();
  const result = fn();
  metrics.push({
    name,
    duration: performance.now() - start,
    timestamp: new Date().toISOString(),
    threshold,
  });
  return result;
}

export async function measureAsync<T>(
  metrics: PerformanceMetric[],
  name: string,
  fn: () => Promise<T>,
  threshold?: number,
): Promise<T> {
  const start = performance.now();
  const result = await fn();
  metrics.push({
    name,
    duration: performance.now() - start,
    timestamp: new Date().toISOString(),
    threshold,
  });
  return result;
}

export function serializeChatChunks(count: number): number {
  const start = performance.now();
  for (let index = 0; index < count; index += 1) {
    JSON.stringify({
      type: 'chat.streamChunk',
      requestId: `req-${index}`,
      chunk: `content-${index}`,
    });
  }
  return performance.now() - start;
}

export function extractFunctionName(source: string): string | null {
  const match = source.match(/function\s+(\w+)/);
  return match ? match[1] : null;
}
