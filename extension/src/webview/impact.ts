declare function acquireVsCodeApi(): any;
const vscode = acquireVsCodeApi();

import {
  WebviewToHostMessage,
  HostToWebviewMessage,
  ImpactResponse,
} from './shared/protocol';
import {
  escapeHtml,
  renderImpactWorkspace,
} from './shared/impactLayout';

class ImpactPanel {
  private currentSymbol: string | null = null;
  private currentImpact: ImpactResponse | null = null;
  private currentDepth = 3;
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
          this.currentDepth = this.clampDepth(message.impact?.max_depth || this.currentDepth);
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
            maxDepth: this.currentDepth,
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
        maxDepth: this.currentDepth,
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

    root.innerHTML = `
      <div class="impact-container">
        ${renderImpactWorkspace(this.currentImpact, this.currentSymbol, 'live graph', {
          depth: this.currentDepth,
        })}
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

    document.querySelectorAll('.impact-group-header').forEach(header => {
      header.addEventListener('click', (e: Event) => {
        const target = e.currentTarget as HTMLElement;
        const group = target.closest('.impact-group');
        const content = group?.querySelector('.group-content');
        if (!group || !content) return;

        const expanded = target.getAttribute('aria-expanded') === 'true';
        target.setAttribute('aria-expanded', String(!expanded));
        group.classList.toggle('expanded', !expanded);
        content.toggleAttribute('hidden', expanded);
      });
    });

    document.querySelectorAll('[data-action="showMoreImpact"]').forEach(button => {
      button.addEventListener('click', (e: Event) => {
        const target = e.currentTarget as HTMLElement;
        const group = target.closest('.impact-group');
        const overflow = group?.querySelector('.impact-overflow');
        if (!overflow) return;
        overflow.removeAttribute('hidden');
        target.remove();
      });
    });

    document.querySelectorAll('[data-impact-depth]').forEach(slider => {
      slider.addEventListener('input', (e: Event) => {
        const target = e.currentTarget as HTMLInputElement;
        const depth = this.clampDepth(Number(target.value));
        const output = target.closest('.impact-depth-control')?.querySelector('output');
        if (output) output.textContent = `d${depth}`;
      });
      slider.addEventListener('change', (e: Event) => {
        const target = e.currentTarget as HTMLInputElement;
        this.currentDepth = this.clampDepth(Number(target.value));
        if (this.currentSymbol) {
          vscode.postMessage({
            type: 'action.showImpact',
            symbol: this.currentSymbol,
            maxDepth: this.currentDepth,
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

    const openFilesBtn = document.querySelector('[data-action="open-related-files"]');
    if (openFilesBtn && this.currentImpact?.affected_files?.length) {
      openFilesBtn.addEventListener('click', () => {
        vscode.postMessage({
          type: 'impact.openFiles',
          filePaths: this.currentImpact?.affected_files || [],
        });
      });
    }
  }

  private clampDepth(depth: number): number {
    if (!Number.isFinite(depth)) return 3;
    return Math.max(1, Math.min(4, Math.round(depth)));
  }
}

// Initialize on DOM ready
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', () => new ImpactPanel());
} else {
  new ImpactPanel();
}
