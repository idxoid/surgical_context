import type { SettingsData } from './protocol';
import { DEFAULT_SETTINGS, SETTINGS_FORM_FIELD_KEYS, SettingsFormValues } from './settingsDefaults';
import { escapeHtml } from './html';

export { escapeHtml };
export { DEFAULT_SETTINGS } from './settingsDefaults';

export interface SettingsFormData extends SettingsFormValues {
  graphStatusLabel?: string;
  graphStatusDetail?: string;
  graphStatusHealthy?: boolean;
}

export function settingsFormDataFromSettings(data: SettingsData): SettingsFormData {
  const values = Object.fromEntries(
    SETTINGS_FORM_FIELD_KEYS.map((key) => [key, data[key]]),
  ) as SettingsFormValues;
  return {
    ...values,
    graphStatusLabel: data.graphStatus?.label,
    graphStatusDetail: data.graphStatus?.detail,
    graphStatusHealthy: data.graphStatus?.healthy,
  };
}

interface TextInputSpec {
  id: string;
  label: string;
  value: string;
  hint: string;
  placeholder?: string;
  type?: 'text' | 'password' | 'number';
  min?: number;
  max?: number;
  step?: number;
  testAction?: { action: string; label: string };
  showStatus?: boolean;
}

interface SelectSpec {
  id: string;
  label: string;
  value: string;
  hint: string;
  options: Array<{ value: string; label: string }>;
}

interface CheckboxSpec {
  id: string;
  caption: string;
  checked: boolean;
  hint: string;
}

function renderSettingField(
  id: string,
  label: string,
  hint: string,
  controlHtml: string,
  statusHtml = '',
): string {
  return `
    <div class="setting-field">
      <label for="${id}">${escapeHtml(label)}</label>
      ${controlHtml}
      <p class="field-hint" id="${id}-hint">${hint}</p>
      ${statusHtml}
    </div>
  `;
}

function renderTextInput(spec: TextInputSpec): string {
  const type = spec.type || 'text';
  const inputAttrs = [
    `type="${type}"`,
    `id="${spec.id}"`,
    'class="setting-input"',
    `value="${escapeHtml(spec.value)}"`,
    spec.placeholder ? `placeholder="${escapeHtml(spec.placeholder)}"` : '',
    `aria-label="${escapeHtml(spec.label)}"`,
    `aria-describedby="${spec.id}-hint"`,
    spec.min === undefined ? '' : `min="${spec.min}"`,
    spec.max === undefined ? '' : `max="${spec.max}"`,
    spec.step === undefined ? '' : `step="${spec.step}"`,
  ].filter(Boolean).join(' ');

  const inputHtml = spec.testAction
    ? `<div class="field-group"><input ${inputAttrs} /><button class="field-action-btn" data-action="${spec.testAction.action}" aria-label="Test connection">${spec.testAction.label}</button></div>`
    : `<input ${inputAttrs} />`;

  const statusHtml = spec.showStatus
    ? `<div class="field-status" id="${spec.id}-status"></div>`
    : '';

  return renderSettingField(spec.id, spec.label, spec.hint, inputHtml, statusHtml);
}

function renderSelect(spec: SelectSpec): string {
  const options = spec.options.map(option => (
    `<option value="${escapeHtml(option.value)}" ${spec.value === option.value ? 'selected' : ''}>${escapeHtml(option.label)}</option>`
  )).join('');

  const selectHtml = `<select id="${spec.id}" class="setting-input" aria-label="${escapeHtml(spec.label)}" aria-describedby="${spec.id}-hint">
        ${options}
      </select>`;

  return renderSettingField(spec.id, spec.label, spec.hint, selectHtml);
}

function renderCheckbox(spec: CheckboxSpec): string {
  return `
    <div class="setting-field checkbox-field">
      <label for="${spec.id}">
        <input type="checkbox" id="${spec.id}" class="setting-checkbox" ${spec.checked ? 'checked' : ''} aria-describedby="${spec.id}-hint" />
        <span>${escapeHtml(spec.caption)}</span>
      </label>
      <p class="field-hint" id="${spec.id}-hint">${spec.hint}</p>
    </div>
  `;
}

function renderGraphStatus(data: SettingsFormData): string {
  const statusClass = data.graphStatusHealthy ? 'success' : 'warning';
  return `
    <div class="setting-field">
      <label>Graph provider status</label>
      <div class="field-status ${statusClass}" style="display:block">
        ${escapeHtml(data.graphStatusLabel || 'Unknown')}
      </div>
      <p class="field-hint">${escapeHtml(data.graphStatusDetail || 'Open Settings to refresh status from /status/cloud.')}</p>
    </div>
  `;
}

export function renderSettingsForm(data: SettingsFormData): string {
  const connectionFields = [
    renderTextInput({
      id: 'backendUrl',
      label: 'Sidecar URL',
      value: data.backendUrl,
      placeholder: 'http://localhost:8000',
      hint: 'Base URL where the Surgical Context context_engine is running',
      testAction: { action: 'testUrl', label: 'Test' },
      showStatus: true,
    }),
    renderTextInput({
      id: 'authToken',
      label: 'Auth Token (Optional)',
      value: data.authToken,
      type: 'password',
      placeholder: 'Leave blank if no authentication required',
      hint: 'Token for authenticating with the context_engine if required',
    }),
  ].join('');

  const workspaceFields = [
    renderTextInput({
      id: 'workspaceId',
      label: 'Workspace ID',
      value: data.workspaceId,
      placeholder: 'derived from workspace and Git branch',
      hint: 'Optional override. Leave blank to derive from the open workspace and Git branch.',
    }),
    renderSelect({
      id: 'modelPreference',
      label: 'Model Preference',
      value: data.modelPreference,
      hint: 'Preferred context_engine model route for local asks',
      options: [
        { value: 'auto', label: 'Auto' },
        { value: 'claude', label: 'Claude' },
        { value: 'ollama', label: 'Ollama' },
      ],
    }),
    renderTextInput({
      id: 'tokenBudget',
      label: 'Token Budget',
      value: String(data.tokenBudget),
      type: 'number',
      min: 1000,
      max: 32000,
      step: 500,
      hint: 'Default context budget used for ask and streaming ask requests',
      showStatus: true,
    }),
  ].join('');

  const graphFields = [
    renderTextInput({
      id: 'neo4jUri',
      label: 'Neo4j URI',
      value: data.neo4jUri,
      placeholder: 'bolt://localhost:7687',
      hint: 'Sidecar reads NEO4J_URI from the repo <code>.env</code>. Match this for documentation; start graph with <code>docker compose up -d neo4j</code>.',
    }),
    renderSelect({
      id: 'indexProfile',
      label: 'Index profile',
      value: data.indexProfile,
      hint: 'Set INDEX_PROFILE in context_engine <code>.env</code> to the same value, then restart context_engine and reindex.',
      options: [
        { value: 'axis_python_v1', label: 'axis_python_v1' },
        { value: 'legacy', label: 'legacy' },
      ],
    }),
    renderGraphStatus(data),
  ].join('');

  const storageFields = `
    <div class="setting-grid">
      ${renderTextInput({
        id: 'lancedbPath',
        label: 'LanceDB Path',
        value: data.lancedbPath,
        placeholder: './data/lancedb',
        hint: 'Local vector index path used by the context_engine environment',
      })}
      ${renderTextInput({
        id: 'historyPath',
        label: 'History DB Path',
        value: data.historyPath,
        placeholder: './data/history/surgical_context.sqlite3',
        hint: 'Planned local SQLite history path for dialogs and snapshots',
      })}
    </div>
  `;

  const behaviorFields = [
    renderCheckbox({
      id: 'overlaySync',
      caption: 'Send unsaved content to context_engine',
      checked: data.overlaySync,
      hint: 'When enabled, unsaved editor changes are sent with asks so answers reflect in-memory code',
    }),
    renderCheckbox({
      id: 'autoOpenInspector',
      caption: 'Auto-open Context Inspector',
      checked: data.autoOpenInspector,
      hint: 'Automatically open the Inspector tab after a completed ask',
    }),
  ].join('');

  return `
    <div class="settings-form">
      <div class="settings-header">
        <h2>Surgical Context Settings</h2>
        <p class="settings-description">Configure your Surgical Context environment and preferences.</p>
      </div>

      <div class="settings-section">
        <h3>Connection</h3>
        ${connectionFields}
      </div>

      <div class="settings-section">
        <h3>Workspace</h3>
        ${workspaceFields}
      </div>

      <div class="settings-section">
        <h3>Graph (Neo4j)</h3>
        ${graphFields}
      </div>

      <div class="settings-section">
        <h3>Local Storage</h3>
        ${storageFields}
      </div>

      <div class="settings-section">
        <h3>Behavior</h3>
        ${behaviorFields}
      </div>

      <div class="settings-section">
        <h3>Keyboard Shortcuts</h3>
        <p class="settings-description">VS Code keyboard shortcuts for Surgical Context commands:</p>
        <div class="shortcuts-list">
          <div class="shortcut-item">
            <code class="shortcut-key">Ctrl+Alt+A</code>
            <span class="shortcut-desc">Ask about current symbol</span>
          </div>
          <div class="shortcut-item">
            <code class="shortcut-key">Ctrl+Alt+I</code>
            <span class="shortcut-desc">Show impact</span>
          </div>
          <div class="shortcut-item">
            <code class="shortcut-key">Cmd+L</code>
            <span class="shortcut-desc">Focus chat composer</span>
          </div>
        </div>
        <button class="secondary-btn" data-action="openKeybindings" aria-label="Open VS Code keyboard shortcuts settings">
          Customize Shortcuts
        </button>
      </div>

      <div class="settings-actions">
        <button class="primary-btn" data-action="save" aria-label="Save all settings">
          Save Settings
        </button>
        <button class="secondary-btn" data-action="reset" aria-label="Reset to default values">
          Reset to Defaults
        </button>
      </div>

      <div class="settings-feedback" id="settings-feedback"></div>
    </div>
  `;
}

function inputValue(id: string): string {
  return (document.getElementById(id) as HTMLInputElement | null)?.value || '';
}

function selectValue(id: string, fallback: string): string {
  return (document.getElementById(id) as HTMLSelectElement | null)?.value || fallback;
}

function checkboxValue(id: string): boolean {
  return (document.getElementById(id) as HTMLInputElement | null)?.checked || false;
}

export function readSettingsFormFromDom(): SettingsFormValues {
  return {
    backendUrl: inputValue('backendUrl'),
    workspaceId: inputValue('workspaceId'),
    modelPreference: selectValue('modelPreference', DEFAULT_SETTINGS.modelPreference),
    authToken: inputValue('authToken'),
    tokenBudget: Number(inputValue('tokenBudget') || String(DEFAULT_SETTINGS.tokenBudget)),
    lancedbPath: inputValue('lancedbPath'),
    historyPath: inputValue('historyPath'),
    neo4jUri: inputValue('neo4jUri'),
    indexProfile: selectValue('indexProfile', DEFAULT_SETTINGS.indexProfile),
    overlaySync: checkboxValue('overlaySync'),
    autoOpenInspector: checkboxValue('autoOpenInspector'),
  };
}

export function applySettingsDefaultsToDom(defaults: SettingsFormValues = DEFAULT_SETTINGS): void {
  const setInput = (id: string, value: string) => {
    const element = document.getElementById(id) as HTMLInputElement | null;
    if (element) element.value = value;
  };
  const setSelect = (id: string, value: string) => {
    const element = document.getElementById(id) as HTMLSelectElement | null;
    if (element) element.value = value;
  };
  const setCheckbox = (id: string, checked: boolean) => {
    const element = document.getElementById(id) as HTMLInputElement | null;
    if (element) element.checked = checked;
  };

  setInput('backendUrl', defaults.backendUrl);
  setInput('workspaceId', defaults.workspaceId);
  setSelect('modelPreference', defaults.modelPreference);
  setInput('authToken', defaults.authToken);
  setInput('tokenBudget', String(defaults.tokenBudget));
  setInput('lancedbPath', defaults.lancedbPath);
  setInput('historyPath', defaults.historyPath);
  setInput('neo4jUri', defaults.neo4jUri);
  setSelect('indexProfile', defaults.indexProfile);
  setCheckbox('overlaySync', defaults.overlaySync);
  setCheckbox('autoOpenInspector', defaults.autoOpenInspector);
}

export type SettingsValidationError = { fieldId: string; message: string };

export function validateSettingsForm(values: SettingsFormValues): SettingsValidationError | null {
  if (values.backendUrl && !values.backendUrl.startsWith('http://') && !values.backendUrl.startsWith('https://')) {
    return { fieldId: 'backendUrl', message: 'URL must start with http:// or https://' };
  }
  if (!Number.isFinite(values.tokenBudget) || values.tokenBudget < 1000 || values.tokenBudget > 32000) {
    return { fieldId: 'tokenBudget', message: 'Use a value from 1000 to 32000' };
  }
  return null;
}

function showTransientDomMessage(
  elementId: string,
  className: string,
  message: string,
  autoHide: boolean,
): void {
  const element = document.getElementById(elementId);
  if (!element) return;

  element.className = className;
  element.textContent = message;
  element.style.display = 'block';

  if (autoHide) {
    setTimeout(() => {
      element.style.display = 'none';
    }, 3000);
  }
}

export function showFieldStatus(fieldId: string, success: boolean, message: string): void {
  showTransientDomMessage(
    `${fieldId}-status`,
    `field-status ${success ? 'success' : 'error'}`,
    message,
    success,
  );
}

export function showFeedback(message: string, level: 'info' | 'success' | 'warning' | 'error'): void {
  showTransientDomMessage(
    'settings-feedback',
    `settings-feedback settings-feedback-${level}`,
    message,
    level === 'success',
  );
}
