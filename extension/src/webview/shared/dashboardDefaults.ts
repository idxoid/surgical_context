import { DashboardMetrics } from './protocol';

export function emptyDashboardMetrics(): DashboardMetrics {
  return {
    indexedFiles: null,
    indexedSymbols: null,
    docChunks: null,
    avgLatencyMs: null,
    tokenSavingsPercent: null,
    fallbackRatePercent: null,
    contextQualityPercent: null,
    symbolsWithDocs: null,
    storageGb: null,
    requestsTotal: null,
    tokensTotal: null,
    costUsdTotal: null,
    queuePending: null,
    queueProcessing: null,
    queueProcessed: null,
    queueFailedBatches: null,
    lastIndexJobStatus: null,
  };
}
