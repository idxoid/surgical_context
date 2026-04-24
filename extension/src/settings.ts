import * as vscode from 'vscode';
import { SettingsData } from './webview/shared/protocol';

export const DEFAULT_SETTINGS: SettingsData = {
  backendUrl: 'http://localhost:8000',
  workspaceId: 'local/default@main',
  modelPreference: 'auto',
  authToken: '',
  tokenBudget: 40000,
  lancedbPath: './data/lancedb',
  historyPath: './data/history/surgical_context.sqlite3',
  overlaySync: true,
  autoOpenInspector: false,
};

export function readSettings(): SettingsData {
  const config = vscode.workspace.getConfiguration('surgicalContext');
  return {
    backendUrl: config.get('backendUrl') ?? DEFAULT_SETTINGS.backendUrl,
    workspaceId: config.get('workspaceId') ?? DEFAULT_SETTINGS.workspaceId,
    modelPreference: config.get('modelPreference') ?? DEFAULT_SETTINGS.modelPreference,
    authToken: config.get('authToken') ?? DEFAULT_SETTINGS.authToken,
    tokenBudget: config.get('tokenBudget') ?? DEFAULT_SETTINGS.tokenBudget,
    lancedbPath: config.get('storage.lancedbPath') ?? DEFAULT_SETTINGS.lancedbPath,
    historyPath: config.get('storage.historyPath') ?? DEFAULT_SETTINGS.historyPath,
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
