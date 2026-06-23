declare function acquireVsCodeApi(): any;
const vscode = acquireVsCodeApi();

import {
  WebviewToHostMessage,
  HostToWebviewMessage,
  SettingsData,
} from './shared/protocol';
import {
  renderSettingsForm,
  settingsFormDataFromSettings,
  showFieldStatus,
  showFeedback,
} from './shared/settingsLayout';

class SettingsPanel {
  private settings: SettingsData | null = null;
  private listenersAttached = false;

  constructor() {
    this.initializeMessageListener();
    this.initializeUI();
    this.loadSettings();
  }

  private initializeMessageListener(): void {
    window.addEventListener('message', (event: MessageEvent<HostToWebviewMessage>) => {
      const message = event.data;

      switch (message.type) {
        case 'settings.loaded':
          this.settings = message.settings;
          this.render();
          break;

        case 'settings.saved':
          showFeedback(message.message, 'success');
          break;

        case 'settings.saveFailed':
          showFeedback(message.error, 'error');
          break;

        case 'settings.testUrlComplete':
          showFieldStatus('backendUrl', message.success, message.message);
          break;
      }
    });
  }

  private initializeUI(): void {
    this.setupFormListeners();
  }

  private setupFormListeners(): void {
    if (this.listenersAttached) return;
    this.listenersAttached = true;

    document.addEventListener('click', (e: Event) => {
      const btn = (e.target as HTMLElement).closest('[data-action]');
      if (!btn) return;

      const action = btn.getAttribute('data-action');
      switch (action) {
        case 'save':
          this.saveSettings();
          break;
        case 'reset':
          this.resetSettings();
          break;
        case 'testUrl':
          this.testUrl();
          break;
        case 'openKeybindings':
          this.postMessage({ type: 'settings.openKeybindings' });
          break;
      }
    });
  }

  private loadSettings(): void {
    this.postMessage({ type: 'settings.loaded' });
  }

  private saveSettings(): void {
    if (!this.settings) return;

    const backendUrl = (document.getElementById('backendUrl') as HTMLInputElement)?.value || '';
    const workspaceId = (document.getElementById('workspaceId') as HTMLInputElement)?.value || '';
    const modelPreference = (document.getElementById('modelPreference') as HTMLSelectElement)?.value || 'auto';
    const authToken = (document.getElementById('authToken') as HTMLInputElement)?.value || '';
    const tokenBudget = Number((document.getElementById('tokenBudget') as HTMLInputElement)?.value || '6000');
    const lancedbPath = (document.getElementById('lancedbPath') as HTMLInputElement)?.value || '';
    const historyPath = (document.getElementById('historyPath') as HTMLInputElement)?.value || '';
    const neo4jUri = (document.getElementById('neo4jUri') as HTMLInputElement)?.value || '';
    const indexProfile = (document.getElementById('indexProfile') as HTMLSelectElement)?.value || 'axis_python_v1';
    const overlaySync = (document.getElementById('overlaySync') as HTMLInputElement)?.checked || false;
    const autoOpenInspector = (document.getElementById('autoOpenInspector') as HTMLInputElement)?.checked || false;

    if (backendUrl && !backendUrl.startsWith('http://') && !backendUrl.startsWith('https://')) {
      showFieldStatus('backendUrl', false, 'URL must start with http:// or https://');
      return;
    }

    if (!Number.isFinite(tokenBudget) || tokenBudget < 1000 || tokenBudget > 32000) {
      showFieldStatus('tokenBudget', false, 'Use a value from 1000 to 32000');
      return;
    }

    this.postMessage({
      type: 'settings.save',
      settings: {
        backendUrl,
        workspaceId,
        modelPreference,
        authToken,
        tokenBudget,
        lancedbPath,
        historyPath,
        neo4jUri,
        indexProfile,
        overlaySync,
        autoOpenInspector,
      },
    });
  }

  private resetSettings(): void {
    if (!this.settings) return;

    // Reset to defaults
    const defaults: SettingsData = {
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

    const backendUrl = document.getElementById('backendUrl') as HTMLInputElement | null;
    const workspaceId = document.getElementById('workspaceId') as HTMLInputElement | null;
    const modelPreference = document.getElementById('modelPreference') as HTMLSelectElement | null;
    const authToken = document.getElementById('authToken') as HTMLInputElement | null;
    const tokenBudget = document.getElementById('tokenBudget') as HTMLInputElement | null;
    const lancedbPath = document.getElementById('lancedbPath') as HTMLInputElement | null;
    const historyPath = document.getElementById('historyPath') as HTMLInputElement | null;
    const neo4jUri = document.getElementById('neo4jUri') as HTMLInputElement | null;
    const indexProfile = document.getElementById('indexProfile') as HTMLSelectElement | null;
    const overlaySync = document.getElementById('overlaySync') as HTMLInputElement | null;
    const autoOpenInspector = document.getElementById('autoOpenInspector') as HTMLInputElement | null;

    if (backendUrl) backendUrl.value = defaults.backendUrl;
    if (workspaceId) workspaceId.value = defaults.workspaceId;
    if (modelPreference) modelPreference.value = defaults.modelPreference;
    if (authToken) authToken.value = defaults.authToken;
    if (tokenBudget) tokenBudget.value = String(defaults.tokenBudget);
    if (lancedbPath) lancedbPath.value = defaults.lancedbPath;
    if (historyPath) historyPath.value = defaults.historyPath;
    if (neo4jUri) neo4jUri.value = defaults.neo4jUri;
    if (indexProfile) indexProfile.value = defaults.indexProfile;
    if (overlaySync) overlaySync.checked = defaults.overlaySync;
    if (autoOpenInspector) autoOpenInspector.checked = defaults.autoOpenInspector;

    showFeedback('Reset to default settings', 'info');
  }

  private testUrl(): void {
    const url = (document.getElementById('backendUrl') as HTMLInputElement)?.value || '';
    if (!url) {
      showFieldStatus('backendUrl', false, 'Please enter a URL');
      return;
    }
    const authToken = (document.getElementById('authToken') as HTMLInputElement | null)?.value || '';
    this.postMessage({ type: 'settings.testUrl', url, authToken });
  }

  private render(): void {
    const root = document.getElementById('root');
    if (!root || !this.settings) return;

    root.innerHTML = renderSettingsForm(settingsFormDataFromSettings(this.settings));
    this.setupFormListeners();
  }

  private postMessage(message: WebviewToHostMessage): void {
    vscode.postMessage(message);
  }
}

// Initialize on DOM ready
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', () => new SettingsPanel());
} else {
  new SettingsPanel();
}
