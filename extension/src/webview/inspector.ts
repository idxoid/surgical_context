// @ts-ignore vscode API injected at runtime
declare const vscode: any;

import {
  HostToWebviewMessage,
  PromptContextPayload,
} from './shared/protocol';
import {
  renderPrimarySourceTab,
  renderGraphContextTab,
  renderDocumentationTab,
  renderPromptJsonTab,
  renderTokenBreakdownTab,
  escapeHtml,
} from './shared/inspectorLayout';

interface TabState {
  activeTab: 'primary' | 'graph' | 'docs' | 'json' | 'tokens';
}

class InspectorPanel {
  private context: PromptContextPayload | null = null;
  private tabState: TabState = { activeTab: 'primary' };

  constructor() {
    this.initializeMessageListener();
    this.restoreTabState();
  }

  private initializeMessageListener(): void {
    window.addEventListener('message', (event: MessageEvent<HostToWebviewMessage>) => {
      const message = event.data;

      switch (message.type) {
        case 'inspector.loaded':
          this.context = message.context || null;
          this.render();
          break;
      }
    });
  }

  private render(): void {
    const root = document.getElementById('root');
    if (!root) return;

    if (!this.context) {
      root.innerHTML = `
        <div class="inspector-empty">
          <p>No context available. Ask about a symbol to populate the inspector.</p>
        </div>
      `;
      return;
    }

    const tabButtons = `
      <div class="inspector-tab-bar">
        <button class="tab-button ${this.tabState.activeTab === 'primary' ? 'active' : ''}" data-tab="primary">
          Primary Source
        </button>
        <button class="tab-button ${this.tabState.activeTab === 'graph' ? 'active' : ''}" data-tab="graph">
          Graph Context
        </button>
        <button class="tab-button ${this.tabState.activeTab === 'docs' ? 'active' : ''}" data-tab="docs">
          Documentation
        </button>
        <button class="tab-button ${this.tabState.activeTab === 'json' ? 'active' : ''}" data-tab="json">
          Prompt JSON
        </button>
        <button class="tab-button ${this.tabState.activeTab === 'tokens' ? 'active' : ''}" data-tab="tokens">
          Token Breakdown
        </button>
      </div>
    `;

    let tabContent = '';
    switch (this.tabState.activeTab) {
      case 'primary':
        tabContent = renderPrimarySourceTab(this.context);
        break;
      case 'graph':
        tabContent = renderGraphContextTab(this.context);
        break;
      case 'docs':
        tabContent = renderDocumentationTab(this.context);
        break;
      case 'json':
        tabContent = renderPromptJsonTab(this.context);
        break;
      case 'tokens':
        tabContent = renderTokenBreakdownTab(this.context);
        break;
    }

    root.innerHTML = `
      <div class="inspector-header">
        <h2>Context Inspector</h2>
      </div>
      ${tabButtons}
      <div class="inspector-content">
        ${tabContent}
      </div>
    `;

    this.attachTabListeners();
  }

  private attachTabListeners(): void {
    document.querySelectorAll('.tab-button').forEach(btn => {
      btn.addEventListener('click', (e: Event) => {
        const tab = (e.currentTarget as HTMLElement).getAttribute('data-tab') as TabState['activeTab'];
        if (tab) {
          this.tabState.activeTab = tab;
          this.persistTabState();
          this.render();
        }
      });
    });

    // Attach row click handlers
    document.querySelectorAll('[data-file-path]').forEach(row => {
      row.addEventListener('click', (e: Event) => {
        const filePath = (e.currentTarget as HTMLElement).getAttribute('data-file-path');
        const lineStr = (e.currentTarget as HTMLElement).getAttribute('data-line');
        if (filePath) {
          vscode.postMessage({
            type: 'link.openFile',
            filePath,
            line: lineStr ? parseInt(lineStr, 10) : undefined,
          });
        }
      });
    });

    // Attach copy button
    const copyBtn = document.querySelector('[data-action="copy-json"]');
    if (copyBtn) {
      copyBtn.addEventListener('click', () => {
        const jsonContent = JSON.stringify(this.context, null, 2);
        navigator.clipboard.writeText(jsonContent).then(() => {
          const btn = copyBtn as HTMLElement;
          const original = btn.textContent;
          btn.textContent = 'Copied!';
          setTimeout(() => {
            btn.textContent = original;
          }, 2000);
        });
      });
    }
  }

  private persistTabState(): void {
    vscode.setState(this.tabState);
  }

  private restoreTabState(): void {
    const saved = vscode.getState();
    if (saved?.activeTab) {
      this.tabState.activeTab = saved.activeTab;
    }
  }
}

// Initialize on DOM ready
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', () => new InspectorPanel());
} else {
  new InspectorPanel();
}
