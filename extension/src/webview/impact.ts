declare function acquireVsCodeApi(): any;
const vscode = acquireVsCodeApi();

import {
  WebviewToHostMessage,
  HostToWebviewMessage,
  ImpactResponse,
} from './shared/protocol';
import {
  renderSymbolSummaryCard,
  renderAffectsGroup,
  renderPlaceholderGroup,
  renderActionButtonRow,
  escapeHtml,
} from './shared/impactLayout';

class ImpactPanel {
  private currentSymbol: string | null = null;
  private currentImpact: ImpactResponse | null = null;
  private isLoading: boolean = false;

  constructor() {
    this.initializeMessageListener();
    this.initializeUI();
  }

  private initializeMessageListener(): void {
    window.addEventListener('message', (event: MessageEvent<HostToWebviewMessage>) => {
      const message = event.data;

      switch (message.type) {
        case 'impact.loading':
          this.onLoading();
          break;

        case 'impact.loaded':
          this.currentSymbol = message.symbol || null;
          this.currentImpact = message.impact || null;
          this.render();
          break;

        case 'impact.loadFailed':
          this.onError(message.error);
          break;

        case 'workspace.updated':
          this.onWorkspaceUpdated(message.symbol);
          break;
      }
    });
  }

  private initializeUI(): void {
    const askBtn = document.querySelector('[data-action="ask-impact"]') as HTMLButtonElement | null;
    if (askBtn) {
      askBtn.addEventListener('click', () => {
        if (this.currentSymbol) {
          vscode.postMessage({
            type: 'action.showImpact',
            symbol: this.currentSymbol,
          });
        }
      });
    }
  }

  private onLoading(): void {
    this.isLoading = true;
    this.render();
  }

  private onError(error: string): void {
    this.isLoading = false;
    const root = document.getElementById('root');
    if (root) {
      root.innerHTML = `
        <div class="impact-error">
          <p>Failed to load impact: ${escapeHtml(error)}</p>
        </div>
      `;
    }
  }

  private onWorkspaceUpdated(symbol: string | null): void {
    if (symbol && symbol !== this.currentSymbol) {
      this.currentSymbol = symbol;
      vscode.postMessage({
        type: 'action.showImpact',
        symbol,
      });
    }
  }

  private render(): void {
    const root = document.getElementById('root');
    if (!root) return;

    if (this.isLoading) {
      root.innerHTML = `
        <div class="impact-loading">
          <p>Loading impact analysis...</p>
        </div>
      `;
      return;
    }

    if (!this.currentSymbol || !this.currentImpact) {
      root.innerHTML = `
        <div class="impact-empty">
          <p>Select a symbol to see its impact.</p>
        </div>
      `;
      return;
    }

    const summaryCard = renderSymbolSummaryCard({
      symbol: this.currentSymbol,
      filePath: this.currentImpact.file_path || 'unknown',
      uid: this.currentImpact.symbol_uid || this.currentSymbol,
    });

    const affectsGroup = renderAffectsGroup(this.currentImpact.affected_symbols || []);
    const callsGroup = renderPlaceholderGroup('Calls', 'Calls are coming in Phase 6');
    const calledByGroup = renderPlaceholderGroup('Called By', 'Called By information is coming in Phase 6');
    const dependsOnGroup = renderPlaceholderGroup('Depends On', 'Dependency analysis is coming in Phase 6');
    const docsCoveringGroup = renderPlaceholderGroup('Docs Covering', 'Documentation linking is coming in Phase 6');
    const actionButtons = renderActionButtonRow();

    root.innerHTML = `
      <div class="impact-container">
        ${summaryCard}
        <div class="impact-groups">
          ${affectsGroup}
          ${callsGroup}
          ${calledByGroup}
          ${dependsOnGroup}
          ${docsCoveringGroup}
        </div>
        ${actionButtons}
      </div>
    `;

    this.attachEventListeners();
  }

  private attachEventListeners(): void {
    // Attach row click handlers for opening files
    document.querySelectorAll('[data-file-path]').forEach(row => {
      row.addEventListener('click', (e: Event) => {
        const filePath = (e.currentTarget as HTMLElement).getAttribute('data-file-path');
        if (filePath) {
          vscode.postMessage({
            type: 'link.openFile',
            filePath,
            line: 1,
          });
        }
      });
    });

    // Attach ask-follow-up button
    const askFollowUpBtn = document.querySelector('[data-action="ask-followup"]');
    if (askFollowUpBtn && this.currentSymbol) {
      askFollowUpBtn.addEventListener('click', () => {
        vscode.postMessage({
          type: 'action.openChat',
          prefillSymbol: this.currentSymbol,
        });
      });
    }
  }
}

// Initialize on DOM ready
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', () => new ImpactPanel());
} else {
  new ImpactPanel();
}
