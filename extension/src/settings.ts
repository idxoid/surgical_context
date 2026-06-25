import * as vscode from 'vscode';
import { CloudStatusResponse, GraphStatusInfo, SettingsData } from './webview/shared/protocol';
import { DEFAULT_SETTINGS } from './webview/shared/settingsDefaults';
import { workspaceIdDisplayValue } from './workspaceIdentity';

export { DEFAULT_SETTINGS };

export function graphStatusFromCloud(cloud: CloudStatusResponse | null): GraphStatusInfo {
  if (!cloud) {
    return {
      mode: 'offline',
      label: 'Unreachable',
      detail: 'Sidecar /status/cloud did not respond. Start the context_engine and check Sidecar URL.',
      healthy: false,
    };
  }

  const healthy = cloud.health?.status === 'healthy';
  if (cloud.using_aura) {
    return {
      mode: 'aura',
      label: 'Neo4j Aura',
      detail: 'Cloud graph provider is connected.',
      healthy,
    };
  }
  if (cloud.using_fallback) {
    return {
      mode: 'fallback-local',
      label: healthy ? 'Local Neo4j' : 'Local Neo4j (fallback)',
      detail: healthy
        ? 'Local graph is healthy. Aura is configured but unreachable — set NEO4J_LOCAL_ONLY=1 in repo .env to skip Aura.'
        : 'Aura is unavailable; context_engine fell back to local Docker Neo4j (NEO4J_URI in repo .env).',
      healthy,
    };
  }
  return {
    mode: 'local',
    label: 'Local Neo4j',
    detail: healthy
      ? 'Local graph provider is healthy.'
      : cloud.health?.error || 'Graph health check reported unhealthy.',
    healthy,
  };
}

export function readSettings(): SettingsData {
  const config = vscode.workspace.getConfiguration('surgicalContext');
  return {
    backendUrl: config.get('backendUrl') ?? DEFAULT_SETTINGS.backendUrl,
    workspaceId: workspaceIdDisplayValue(),
    modelPreference: config.get('modelPreference') ?? DEFAULT_SETTINGS.modelPreference,
    authToken: config.get('authToken') ?? DEFAULT_SETTINGS.authToken,
    tokenBudget: config.get('tokenBudget') ?? DEFAULT_SETTINGS.tokenBudget,
    lancedbPath: config.get('storage.lancedbPath') ?? DEFAULT_SETTINGS.lancedbPath,
    historyPath: config.get('storage.historyPath') ?? DEFAULT_SETTINGS.historyPath,
    neo4jUri: config.get('graph.neo4jUri') ?? DEFAULT_SETTINGS.neo4jUri,
    indexProfile: config.get('graph.indexProfile') ?? DEFAULT_SETTINGS.indexProfile,
    overlaySync: config.get('overlaySync') ?? DEFAULT_SETTINGS.overlaySync,
    autoOpenInspector: config.get('chat.autoOpenInspector') ?? DEFAULT_SETTINGS.autoOpenInspector,
  };
}

export async function saveSettings(settings: SettingsData): Promise<void> {
  await Promise.all([
    updateSetting('backendUrl', settings.backendUrl),
    updateSetting('workspaceId', settings.workspaceId),
    updateSetting('modelPreference', settings.modelPreference),
    updateSetting('authToken', settings.authToken),
    updateSetting('tokenBudget', settings.tokenBudget),
    updateSetting('storage.lancedbPath', settings.lancedbPath),
    updateSetting('storage.historyPath', settings.historyPath),
    updateSetting('graph.neo4jUri', settings.neo4jUri),
    updateSetting('graph.indexProfile', settings.indexProfile),
    updateSetting('overlaySync', settings.overlaySync),
    updateSetting('chat.autoOpenInspector', settings.autoOpenInspector),
  ]);
}

export function normalizeSettingKey(key: string): string {
  return key.startsWith('surgicalContext.') ? key.slice('surgicalContext.'.length) : key;
}

export function updateSetting(key: string, value: unknown): Thenable<void> {
  const config = vscode.workspace.getConfiguration('surgicalContext');
  return config.update(normalizeSettingKey(key), value, vscode.ConfigurationTarget.Workspace);
}
