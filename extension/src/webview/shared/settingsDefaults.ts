import type { SettingsData } from './protocol';

export const DEFAULT_SETTINGS: SettingsData = {
  backendUrl: 'http://localhost:8000',
  workspaceId: '',
  modelPreference: 'auto',
  authToken: '',
  tokenBudget: 6000,
  lancedbPath: './data/lancedb',
  historyPath: './data/history/surgical_context.sqlite3',
  neo4jUri: 'bolt://localhost:7687',
  indexProfile: 'axis_python_v1',
  overlaySync: true,
  autoOpenInspector: false,
};

export type SettingsFormValues = Pick<
  SettingsData,
  | 'backendUrl'
  | 'workspaceId'
  | 'modelPreference'
  | 'authToken'
  | 'tokenBudget'
  | 'lancedbPath'
  | 'historyPath'
  | 'neo4jUri'
  | 'indexProfile'
  | 'overlaySync'
  | 'autoOpenInspector'
>;
