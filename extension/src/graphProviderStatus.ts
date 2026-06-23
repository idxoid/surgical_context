import { CloudStatusResponse } from './webview/shared/protocol';

export function graphProviderIsHealthy(cloud: CloudStatusResponse | null): boolean {
  return cloud?.health?.status === 'healthy';
}

export function graphProviderHealthStatus(
  cloud: CloudStatusResponse | null
): 'ok' | 'warning' | 'error' {
  if (!cloud) return 'error';
  return graphProviderIsHealthy(cloud) ? 'ok' : 'warning';
}

export function graphProviderValue(cloud: CloudStatusResponse | null): string {
  if (!cloud) return 'offline';
  if (cloud.using_aura) return 'aura';
  return 'local';
}

export function graphProviderDetail(cloud: CloudStatusResponse | null): string {
  if (!cloud) return 'Could not read graph provider status.';
  if (!graphProviderIsHealthy(cloud)) {
    return cloud.health?.error || cloud.health?.hint || 'Graph health check failed.';
  }
  if (cloud.using_aura) return 'Connected to Neo4j Aura.';
  if (cloud.using_fallback) {
    return 'Local Neo4j is healthy. Aura is configured but unreachable — set NEO4J_LOCAL_ONLY=1 in repo .env to skip Aura.';
  }
  return 'Local Neo4j (NEO4J_URI in repo .env).';
}

export function resolveCloudStatus(
  cloudStatus: CloudStatusResponse | null
): 'connected' | 'fallback-local' | 'local' | 'offline' {
  if (!cloudStatus) return 'offline';
  if (cloudStatus.using_aura) return 'connected';
  return 'local';
}
