declare function acquireVsCodeApi(): any;
const vscode = acquireVsCodeApi();

import {
  HostToWebviewMessage,
  PromptContextPayload,
} from './shared/protocol';
import {
  renderPrimarySourceTab,
  renderGraphContextTab,
  renderDocumentationTab,
  renderPromptJsonTab,
  renderApiPayloadTab,
  renderTokenBreakdownTab,
  escapeHtml,
} from './shared/inspectorLayout';

interface TabState {
  activeTab: 'primary' | 'graph' | 'docs' | 'json' | 'api' | 'tokens';
}

class InspectorPanel {
  private context: PromptContextPayload | null = null;
  private tabState: TabState = { activeTab: 'primary' };

  constructor() {
    console.log('InspectorPanel constructor called');
    this.initializeMessageListener();
    this.restoreTabState();
  }

  private initializeMessageListener(): void {
    window.addEventListener('message', (event: MessageEvent<HostToWebviewMessage>) => {
      const message = event.data;
      console.log('InspectorPanel received message:', message.type);

      switch (message.type) {
        case 'inspector.loaded':
          console.log('inspector.loaded message received, context:', message.context);
          this.context = message.context || null;
          this.render();
          break;
      }
    });
  }

  private render(): void {
    const root = document.getElementById('root');
    if (!root) return;

    console.log('InspectorPanel.render() called, context:', this.context, 'tabState:', this.tabState);

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
        <button class="tab-button ${this.tabState.activeTab === 'api' ? 'active' : ''}" data-tab="api">
          API Payload
        </button>
        <button class="tab-button ${this.tabState.activeTab === 'tokens' ? 'active' : ''}" data-tab="tokens">
          Token Breakdown
        </button>
      </div>
    `;

    console.log('tabButtons HTML generated, about to render tabContent for:', this.tabState.activeTab);

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
      case 'api':
        tabContent = renderApiPayloadTab(this.context);
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

    // Attach copy button (Prompt JSON)
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

    // Attach copy button (API Payload JSON)
    const copyApiBtn = document.querySelector('[data-action="copy-api-json"]');
    if (copyApiBtn) {
      copyApiBtn.addEventListener('click', () => {
        const primary = this.context?.primary_source;
        const graphItems = this.context?.graph_context || [];
        const docs = this.context?.documentation || [];

        const systemPrompt = this._buildSystemPromptForCopy();
        const apiPayload = {
          api_request: {
            model: 'claude-opus-4-7',
            max_tokens: 8096,
            system: systemPrompt,
            messages: [
              {
                role: 'user',
                content: '(User query would appear here)',
              },
            ],
          },
          context_metadata: {
            mode: this.context?.mode,
            intent: this.context?.intent,
            assembly_metadata: this.context?.metadata?.assembly,
            tier_tokens: this.context?.metadata?.tier_tokens,
            budget_info: this.context?.budget,
          },
        };

        const jsonContent = JSON.stringify(apiPayload, null, 2);
        navigator.clipboard.writeText(jsonContent).then(() => {
          const btn = copyApiBtn as HTMLElement;
          const original = btn.textContent;
          btn.textContent = 'Copied!';
          setTimeout(() => {
            btn.textContent = original;
          }, 2000);
        });
      });
    }
  }

  private _buildSystemPromptForCopy(): string {
    const primary = this.context?.primary_source;
    const graphItems = this.context?.graph_context || [];
    const docs = this.context?.documentation || [];

    const blocks: string[] = [
      `--- TARGET SYMBOL: ${primary?.symbol || 'unknown'} ---`,
    ];

    if (primary?.code) {
      blocks.push(primary.code);
    }

    if (graphItems.length > 0) {
      blocks.push('\n--- DEPENDENCIES ---');
      for (const dep of graphItems) {
        blocks.push(`\n# From ${dep.symbol} [${dep.relation}]:`);
        if (dep.code) {
          blocks.push(dep.code);
        }
      }
    }

    if (docs.length > 0) {
      blocks.push('\n--- DOCUMENTATION ---');
      for (const doc of docs) {
        blocks.push(`[${doc.source_file}]\n${doc.content}`);
      }
    }

    return blocks.join('\n');
  }

  private persistTabState(): void {
    vscode.setState(this.tabState);
  }

  private restoreTabState(): void {
    const saved = vscode.getState();
    const validTabs = ['primary', 'graph', 'docs', 'json', 'api', 'tokens'];
    if (saved?.activeTab && validTabs.includes(saved.activeTab)) {
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
