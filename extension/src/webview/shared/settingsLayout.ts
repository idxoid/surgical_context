import type { SettingsData } from './protocol';
import { escapeHtml } from './html';

export { escapeHtml };

export interface SettingsFormData {
  backendUrl: string;
  workspaceId: string;
  modelPreference: string;
  authToken: string;
  tokenBudget: number;
  lancedbPath: string;
  historyPath: string;
  neo4jUri: string;
  indexProfile: string;
  overlaySync: boolean;
  autoOpenInspector: boolean;
  graphStatusLabel?: string;
  graphStatusDetail?: string;
  graphStatusHealthy?: boolean;
}

export function settingsFormDataFromSettings(data: SettingsData): SettingsFormData {
  return {
    backendUrl: data.backendUrl,
    workspaceId: data.workspaceId,
    modelPreference: data.modelPreference,
    authToken: data.authToken,
    tokenBudget: data.tokenBudget,
    lancedbPath: data.lancedbPath,
    historyPath: data.historyPath,
    neo4jUri: data.neo4jUri,
    indexProfile: data.indexProfile,
    overlaySync: data.overlaySync,
    autoOpenInspector: data.autoOpenInspector,
    graphStatusLabel: data.graphStatus?.label,
    graphStatusDetail: data.graphStatus?.detail,
    graphStatusHealthy: data.graphStatus?.healthy,
  };
}

export function renderSettingsForm(data: SettingsFormData): string {
  return `
    <div class="settings-form">
      <div class="settings-header">
        <h2>Surgical Context Settings</h2>
        <p class="settings-description">Configure your Surgical Context environment and preferences.</p>
      </div>

      <div class="settings-section">
        <h3>Connection</h3>

        <div class="setting-field">
          <label for="backendUrl">Sidecar URL</label>
          <div class="field-group">
            <input
              type="text"
              id="backendUrl"
              class="setting-input"
              value="${escapeHtml(data.backendUrl)}"
              placeholder="http://localhost:8000"
              aria-label="Sidecar backend URL"
              aria-describedby="backendUrl-hint"
            />
            <button class="field-action-btn" data-action="testUrl" aria-label="Test connection">
              Test
            </button>
          </div>
          <p class="field-hint" id="backendUrl-hint">Base URL where the Surgical Context context_engine is running</p>
          <div class="field-status" id="backendUrl-status"></div>
        </div>

        <div class="setting-field">
          <label for="authToken">Auth Token (Optional)</label>
          <input
            type="password"
            id="authToken"
            class="setting-input"
            value="${escapeHtml(data.authToken)}"
            placeholder="Leave blank if no authentication required"
            aria-label="Authentication token for context_engine"
            aria-describedby="authToken-hint"
          />
          <p class="field-hint" id="authToken-hint">Token for authenticating with the context_engine if required</p>
        </div>
      </div>

      <div class="settings-section">
        <h3>Workspace</h3>

        <div class="setting-field">
          <label for="workspaceId">Workspace ID</label>
          <input
            type="text"
            id="workspaceId"
            class="setting-input"
            value="${escapeHtml(data.workspaceId)}"
            placeholder="derived from workspace and Git branch"
            aria-label="Workspace scope identifier"
            aria-describedby="workspaceId-hint"
          />
          <p class="field-hint" id="workspaceId-hint">Optional override. Leave blank to derive from the open workspace and Git branch.</p>
        </div>

        <div class="setting-field">
          <label for="modelPreference">Model Preference</label>
          <select
            id="modelPreference"
            class="setting-input"
            aria-label="LLM model to use"
            aria-describedby="modelPreference-hint"
          >
            <option value="auto" ${data.modelPreference === 'auto' ? 'selected' : ''}>Auto</option>
            <option value="claude" ${data.modelPreference === 'claude' ? 'selected' : ''}>Claude</option>
            <option value="ollama" ${data.modelPreference === 'ollama' ? 'selected' : ''}>Ollama</option>
          </select>
          <p class="field-hint" id="modelPreference-hint">Preferred context_engine model route for local asks</p>
        </div>

        <div class="setting-field">
          <label for="tokenBudget">Token Budget</label>
          <input
            type="number"
            id="tokenBudget"
            class="setting-input"
            value="${escapeHtml(String(data.tokenBudget))}"
            min="1000"
            max="32000"
            step="500"
            aria-label="Default token budget"
            aria-describedby="tokenBudget-hint"
          />
          <p class="field-hint" id="tokenBudget-hint">Default context budget used for ask and streaming ask requests</p>
          <div class="field-status" id="tokenBudget-status"></div>
        </div>
      </div>

      <div class="settings-section">
        <h3>Graph (Neo4j)</h3>

        <div class="setting-field">
          <label for="neo4jUri">Neo4j URI</label>
          <input
            type="text"
            id="neo4jUri"
            class="setting-input"
            value="${escapeHtml(data.neo4jUri)}"
            placeholder="bolt://localhost:7687"
            aria-label="Neo4j Bolt URI"
            aria-describedby="neo4jUri-hint"
          />
          <p class="field-hint" id="neo4jUri-hint">Sidecar reads NEO4J_URI from the repo <code>.env</code>. Match this for documentation; start graph with <code>docker compose up -d neo4j</code>.</p>
        </div>

        <div class="setting-field">
          <label for="indexProfile">Index profile</label>
          <select
            id="indexProfile"
            class="setting-input"
            aria-label="Sidecar index profile"
            aria-describedby="indexProfile-hint"
          >
            <option value="axis_python_v1" ${data.indexProfile === 'axis_python_v1' ? 'selected' : ''}>axis_python_v1</option>
            <option value="legacy" ${data.indexProfile === 'legacy' ? 'selected' : ''}>legacy</option>
          </select>
          <p class="field-hint" id="indexProfile-hint">Set INDEX_PROFILE in context_engine <code>.env</code> to the same value, then restart context_engine and reindex.</p>
        </div>

        <div class="setting-field">
          <label>Graph provider status</label>
          <div class="field-status ${data.graphStatusHealthy ? 'success' : 'warning'}" style="display:block">
            ${escapeHtml(data.graphStatusLabel || 'Unknown')}
          </div>
          <p class="field-hint">${escapeHtml(data.graphStatusDetail || 'Open Settings to refresh status from /status/cloud.')}</p>
        </div>
      </div>

      <div class="settings-section">
        <h3>Local Storage</h3>

        <div class="setting-grid">
          <div class="setting-field">
            <label for="lancedbPath">LanceDB Path</label>
            <input
              type="text"
              id="lancedbPath"
              class="setting-input"
              value="${escapeHtml(data.lancedbPath)}"
              placeholder="./data/lancedb"
              aria-label="LanceDB path"
              aria-describedby="lancedbPath-hint"
            />
            <p class="field-hint" id="lancedbPath-hint">Local vector index path used by the context_engine environment</p>
          </div>

          <div class="setting-field">
            <label for="historyPath">History DB Path</label>
            <input
              type="text"
              id="historyPath"
              class="setting-input"
              value="${escapeHtml(data.historyPath)}"
              placeholder="./data/history/surgical_context.sqlite3"
              aria-label="SQLite history path"
              aria-describedby="historyPath-hint"
            />
            <p class="field-hint" id="historyPath-hint">Planned local SQLite history path for dialogs and snapshots</p>
          </div>
        </div>
      </div>

      <div class="settings-section">
        <h3>Behavior</h3>

        <div class="setting-field checkbox-field">
          <label for="overlaySync">
            <input
              type="checkbox"
              id="overlaySync"
              class="setting-checkbox"
              ${data.overlaySync ? 'checked' : ''}
              aria-describedby="overlaySync-hint"
            />
            <span>Send unsaved content to context_engine</span>
          </label>
          <p class="field-hint" id="overlaySync-hint">When enabled, unsaved editor changes are sent with asks so answers reflect in-memory code</p>
        </div>

        <div class="setting-field checkbox-field">
          <label for="autoOpenInspector">
            <input
              type="checkbox"
              id="autoOpenInspector"
              class="setting-checkbox"
              ${data.autoOpenInspector ? 'checked' : ''}
              aria-describedby="autoOpenInspector-hint"
            />
            <span>Auto-open Context Inspector</span>
          </label>
          <p class="field-hint" id="autoOpenInspector-hint">Automatically open the Inspector tab after a completed ask</p>
        </div>
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

export function showFieldStatus(fieldId: string, success: boolean, message: string): void {
  const status = document.getElementById(`${fieldId}-status`);
  if (!status) return;

  status.className = `field-status ${success ? 'success' : 'error'}`;
  status.textContent = message;
  status.style.display = 'block';

  if (success) {
    setTimeout(() => {
      status.style.display = 'none';
    }, 3000);
  }
}

export function showFeedback(message: string, level: 'info' | 'success' | 'warning' | 'error'): void {
  const feedback = document.getElementById('settings-feedback');
  if (!feedback) return;

  feedback.className = `settings-feedback settings-feedback-${level}`;
  feedback.textContent = message;
  feedback.style.display = 'block';

  if (level === 'success') {
    setTimeout(() => {
      feedback.style.display = 'none';
    }, 3000);
  }
}
