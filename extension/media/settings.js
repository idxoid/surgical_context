"use strict";
(() => {
  // src/webview/shared/settingsLayout.ts
  function escapeHtml(text) {
    const div = document.createElement("div");
    div.textContent = text;
    return div.innerHTML;
  }
  function renderSettingsForm(data) {
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
            <option value="auto" ${data.modelPreference === "auto" ? "selected" : ""}>Auto (use backend default)</option>
            <option value="claude-opus" ${data.modelPreference === "claude-opus" ? "selected" : ""}>Claude Opus</option>
            <option value="claude-sonnet" ${data.modelPreference === "claude-sonnet" ? "selected" : ""}>Claude Sonnet</option>
            <option value="claude-haiku" ${data.modelPreference === "claude-haiku" ? "selected" : ""}>Claude Haiku</option>
          </select>
          <p class="field-hint" id="modelPreference-hint">Which Claude model to use for analysis</p>
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
              ${data.overlaySync ? "checked" : ""}
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
              ${data.autoOpenInspector ? "checked" : ""}
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
  function showFieldStatus(fieldId, success, message) {
    const status = document.getElementById(`${fieldId}-status`);
    if (!status) return;
    status.className = `field-status ${success ? "success" : "error"}`;
    status.textContent = message;
    status.style.display = "block";
    if (success) {
      setTimeout(() => {
        status.style.display = "none";
      }, 3e3);
    }
  }
  function showFeedback(message, level) {
    const feedback = document.getElementById("settings-feedback");
    if (!feedback) return;
    feedback.className = `settings-feedback settings-feedback-${level}`;
    feedback.textContent = message;
    feedback.style.display = "block";
    if (level === "success") {
      setTimeout(() => {
        feedback.style.display = "none";
      }, 3e3);
    }
  }

  // src/webview/settings.ts
  var SettingsPanel = class {
    constructor() {
      this.settings = null;
      this.initializeMessageListener();
      this.initializeUI();
      this.loadSettings();
    }
    initializeMessageListener() {
      window.addEventListener("message", (event) => {
        const message = event.data;
        switch (message.type) {
          case "settings.loaded":
            this.settings = message.settings;
            this.render();
            break;
          case "settings.saved":
            showFeedback(message.message, "success");
            break;
          case "settings.saveFailed":
            showFeedback(message.error, "error");
            break;
          case "settings.testUrlComplete":
            showFieldStatus("backendUrl", message.success, message.message);
            break;
        }
      });
    }
    initializeUI() {
      this.setupFormListeners();
    }
    setupFormListeners() {
      document.addEventListener("click", (e) => {
        const target = e.currentTarget;
        const btn = e.target.closest("[data-action]");
        if (!btn) return;
        const action = btn.getAttribute("data-action");
        switch (action) {
          case "save":
            this.saveSettings();
            break;
          case "reset":
            this.resetSettings();
            break;
          case "testUrl":
            this.testUrl();
            break;
          case "openKeybindings":
            this.postMessage({ type: "settings.openKeybindings" });
            break;
        }
      });
    }
    loadSettings() {
      this.postMessage({ type: "settings.loaded" });
    }
    saveSettings() {
      if (!this.settings) return;
      const backendUrl = document.getElementById("backendUrl")?.value || "";
      const workspaceId = document.getElementById("workspaceId")?.value || "";
      const modelPreference = document.getElementById("modelPreference")?.value || "auto";
      const authToken = document.getElementById("authToken")?.value || "";
      const overlaySync = document.getElementById("overlaySync")?.checked || false;
      const autoOpenInspector = document.getElementById("autoOpenInspector")?.checked || false;
      if (backendUrl && !backendUrl.startsWith("http://") && !backendUrl.startsWith("https://")) {
        showFieldStatus("backendUrl", false, "URL must start with http:// or https://");
        return;
      }
      this.postMessage({ type: "settings.update", key: "surgicalContext.backendUrl", value: backendUrl });
      this.postMessage({ type: "settings.update", key: "surgicalContext.workspaceId", value: workspaceId });
      this.postMessage({ type: "settings.update", key: "surgicalContext.modelPreference", value: modelPreference });
      this.postMessage({ type: "settings.update", key: "surgicalContext.authToken", value: authToken });
      this.postMessage({ type: "settings.update", key: "surgicalContext.overlaySync", value: overlaySync });
      this.postMessage({ type: "settings.update", key: "surgicalContext.chat.autoOpenInspector", value: autoOpenInspector });
      showFeedback("Settings saved successfully", "success");
    }
    resetSettings() {
      if (!this.settings) return;
      const defaults = {
        backendUrl: "http://localhost:8000",
        workspaceId: "local/default@main",
        modelPreference: "auto",
        authToken: "",
        overlaySync: true,
        autoOpenInspector: false
      };
      document.getElementById("backendUrl").value = defaults.backendUrl;
      document.getElementById("workspaceId").value = defaults.workspaceId;
      document.getElementById("modelPreference").value = defaults.modelPreference;
      document.getElementById("authToken").value = defaults.authToken;
      document.getElementById("overlaySync").checked = defaults.overlaySync;
      document.getElementById("autoOpenInspector").checked = defaults.autoOpenInspector;
      showFeedback("Reset to default settings", "info");
    }
    testUrl() {
      const url = document.getElementById("backendUrl")?.value || "";
      if (!url) {
        showFieldStatus("backendUrl", false, "Please enter a URL");
        return;
      }
      this.postMessage({ type: "settings.testUrl", url });
    }
    render() {
      const root = document.getElementById("root");
      if (!root || !this.settings) return;
      const formData = {
        backendUrl: this.settings.backendUrl,
        workspaceId: this.settings.workspaceId,
        modelPreference: this.settings.modelPreference,
        authToken: this.settings.authToken,
        overlaySync: this.settings.overlaySync,
        autoOpenInspector: this.settings.autoOpenInspector
      };
      root.innerHTML = renderSettingsForm(formData);
      this.setupFormListeners();
    }
    postMessage(message) {
      vscode.postMessage(message);
    }
  };
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => new SettingsPanel());
  } else {
    new SettingsPanel();
  }
})();
//# sourceMappingURL=settings.js.map
