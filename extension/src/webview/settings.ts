declare function acquireVsCodeApi(): any;
const vscode = acquireVsCodeApi();

import {
  WebviewToHostMessage,
  HostToWebviewMessage,
  SettingsData,
} from './shared/protocol';
import {
  renderSettingsForm,
  showFieldStatus,
  showFeedback,
  SettingsFormData,
} from './shared/settingsLayout';

class SettingsPanel {
  private settings: SettingsData | null = null;

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
    document.addEventListener('click', (e: Event) => {
      const target = e.currentTarget as HTMLElement;
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
    const overlaySync = (document.getElementById('overlaySync') as HTMLInputElement)?.checked || false;
    const autoOpenInspector = (document.getElementById('autoOpenInspector') as HTMLInputElement)?.checked || false;

    // Validate backendUrl format
    if (backendUrl && !backendUrl.startsWith('http://') && !backendUrl.startsWith('https://')) {
      showFieldStatus('backendUrl', false, 'URL must start with http:// or https://');
      return;
    }

    // Send updates
    this.postMessage({ type: 'settings.update', key: 'surgicalContext.backendUrl', value: backendUrl });
    this.postMessage({ type: 'settings.update', key: 'surgicalContext.workspaceId', value: workspaceId });
    this.postMessage({ type: 'settings.update', key: 'surgicalContext.modelPreference', value: modelPreference });
    this.postMessage({ type: 'settings.update', key: 'surgicalContext.authToken', value: authToken });
    this.postMessage({ type: 'settings.update', key: 'surgicalContext.overlaySync', value: overlaySync });
    this.postMessage({ type: 'settings.update', key: 'surgicalContext.chat.autoOpenInspector', value: autoOpenInspector });

    showFeedback('Settings saved successfully', 'success');
  }

  private resetSettings(): void {
    if (!this.settings) return;

    // Reset to defaults
    const defaults: SettingsData = {
      backendUrl: 'http://localhost:8000',
      workspaceId: 'local/default@main',
      modelPreference: 'auto',
      authToken: '',
      overlaySync: true,
      autoOpenInspector: false,
    };

    (document.getElementById('backendUrl') as HTMLInputElement).value = defaults.backendUrl;
    (document.getElementById('workspaceId') as HTMLInputElement).value = defaults.workspaceId;
    (document.getElementById('modelPreference') as HTMLSelectElement).value = defaults.modelPreference;
    (document.getElementById('authToken') as HTMLInputElement).value = defaults.authToken;
    (document.getElementById('overlaySync') as HTMLInputElement).checked = defaults.overlaySync;
    (document.getElementById('autoOpenInspector') as HTMLInputElement).checked = defaults.autoOpenInspector;

    showFeedback('Reset to default settings', 'info');
  }

  private testUrl(): void {
    const url = (document.getElementById('backendUrl') as HTMLInputElement)?.value || '';
    if (!url) {
      showFieldStatus('backendUrl', false, 'Please enter a URL');
      return;
    }
    this.postMessage({ type: 'settings.testUrl', url });
  }

  private render(): void {
    const root = document.getElementById('root');
    if (!root || !this.settings) return;

    const formData: SettingsFormData = {
      backendUrl: this.settings.backendUrl,
      workspaceId: this.settings.workspaceId,
      modelPreference: this.settings.modelPreference,
      authToken: this.settings.authToken,
      overlaySync: this.settings.overlaySync,
      autoOpenInspector: this.settings.autoOpenInspector,
    };

    root.innerHTML = renderSettingsForm(formData);
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
