export function escapeHtml(text: string): string {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

export interface SettingsFormData {
  backendUrl: string;
  workspaceId: string;
  modelPreference: string;
  authToken: string;
  tokenBudget: number;
  lancedbPath: string;
  historyPath: string;
  overlaySync: boolean;
  autoOpenInspector: boolean;
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
          <p class="field-hint" id="backendUrl-hint">Base URL where the Surgical Context sidecar is running</p>
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
            aria-label="Authentication token for sidecar"
            aria-describedby="authToken-hint"
          />
          <p class="field-hint" id="authToken-hint">Token for authenticating with the sidecar if required</p>
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
            placeholder="local/default@main"
            aria-label="Workspace scope identifier"
            aria-describedby="workspaceId-hint"
          />
          <p class="field-hint" id="workspaceId-hint">Scope identifier for multi-workspace support</p>
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
          <p class="field-hint" id="modelPreference-hint">Preferred sidecar model route for local asks</p>
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
            <p class="field-hint" id="lancedbPath-hint">Local vector index path used by the sidecar environment</p>
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
            <span>Send unsaved content to sidecar</span>
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
