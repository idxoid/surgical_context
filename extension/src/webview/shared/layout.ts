import { ChatMessage } from './protocol';

export function escapeHtml(text: string): string {
  const map: Record<string, string> = {
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#039;',
  };
  return text.replace(/[&<>"']/g, char => map[char]);
}

export function renderMessageCard(message: ChatMessage, selectedRequestId?: string | null): string {
  const timestamp = new Date(message.timestamp).toLocaleTimeString();
  const isSelected = Boolean(message.requestId && selectedRequestId === message.requestId);
  const isSelectablePrompt = message.type === 'user' && Boolean(message.requestId);
  const baseClass = `message-card ${message.type}${isSelected ? ' selected' : ''}${isSelectablePrompt ? ' selectable' : ''}`;
  const statusClass = message.status ? ` status-${message.status}` : '';
  const requestAttrs = message.requestId
    ? ` data-request-id="${escapeHtml(message.requestId)}"`
    : '';
  const selectionAttrs = isSelectablePrompt
    ? ` data-action="selectPrompt" role="button" tabindex="0" aria-pressed="${isSelected}"`
    : '';

  if (message.type === 'user') {
    return `
      <article class="${baseClass}${statusClass}" data-message-id="${escapeHtml(message.id)}"${requestAttrs}${selectionAttrs}>
        <div class="message-header">
          <span class="message-role">You</span>
          <span class="message-time">${timestamp}</span>
        </div>
        <div class="message-content">${escapeHtml(message.content)}</div>
      </article>
    `;
  }

  // Assistant message
  let content = `
    <article class="${baseClass}${statusClass}" data-message-id="${escapeHtml(message.id)}"${requestAttrs}>
      <div class="message-header">
        <span class="message-role">
          <span class="message-icon" aria-hidden="true">✦</span>
          Surgical Context
          ${message.status === 'streaming' ? '<span class="live-dot"></span><span class="message-muted">Streaming answer...</span>' : ''}
        </span>
        <span class="message-time">${timestamp}</span>
      </div>
      <div class="message-content">${escapeHtml(message.content)}</div>
  `;

  if (message.error) {
    content += `<div class="message-error">Error: ${escapeHtml(message.error)}</div>`;
  }

  if (message.status === 'done') {
    content += `
      <div class="message-actions">
        <button class="icon-button" data-action="feedback" data-rating="up" title="Helpful" aria-label="Helpful">+</button>
        <button class="icon-button" data-action="feedback" data-rating="down" title="Not helpful" aria-label="Not helpful">-</button>
        <button class="icon-button" data-action="copy" title="Copy response" aria-label="Copy response">Copy</button>
      </div>
    `;
  }

  content += '</article>';
  return content;
}

export function renderStreamingCursor(): string {
  return `<div class="streaming-cursor">▌</div>`;
}

export function renderAccordion(id: string, title: string, content: string, expanded = false): string {
  return `
    <div class="accordion" data-accordion="${id}">
      <button id="${id}-header" class="accordion-header" aria-expanded="${expanded}" aria-controls="${id}-content" role="button">
        <span class="accordion-chevron" aria-hidden="true">›</span>
        <span class="accordion-title">${escapeHtml(title)}</span>
      </button>
      <div id="${id}-content" class="accordion-content ${expanded ? 'expanded' : ''}" ${expanded ? '' : 'hidden'} role="region" aria-labelledby="${id}-header">
        ${content}
      </div>
    </div>
  `;
}

export function renderEnvironmentAccordion(state: {
  workspace: string;
  cloud: string;
  mode: string;
  symbol?: string;
}, expanded = false): string {
  const content = `
    <div class="accordion-row">
      <div class="accordion-label">Workspace</div>
      <div class="accordion-value">${escapeHtml(state.workspace)}</div>
    </div>
    <div class="accordion-row">
      <div class="accordion-label">Cloud</div>
      <div class="accordion-value">${escapeHtml(state.cloud)}</div>
    </div>
    <div class="accordion-row">
      <div class="accordion-label">Mode</div>
      <div class="accordion-value">${escapeHtml(state.mode)}</div>
    </div>
    ${
      state.symbol
        ? `<div class="accordion-row">
      <div class="accordion-label">Symbol</div>
      <div class="accordion-value">${escapeHtml(state.symbol)}</div>
    </div>`
        : ''
    }
  `;
  return renderAccordion('environment', 'Environment', content, expanded);
}

export function renderContextSummaryAccordion(summary?: {
  primaryLabel: string;
  graphCount: number;
  docsCount: number;
  tokenText: string;
  chips: string[];
}, expanded = false): string {
  if (!summary) {
    return renderAccordion('contextSummary', 'Context Summary', 'Run an ask to populate this section.', expanded);
  }

  const content = `
    <div class="accordion-row">
      <div class="accordion-label">Primary</div>
      <div class="accordion-value">${escapeHtml(summary.primaryLabel)}</div>
    </div>
    <div class="accordion-row">
      <div class="accordion-label">Graph Symbols</div>
      <div class="accordion-value">${summary.graphCount}</div>
    </div>
    <div class="accordion-row">
      <div class="accordion-label">Doc Chunks</div>
      <div class="accordion-value">${summary.docsCount}</div>
    </div>
    <div class="accordion-row">
      <div class="accordion-label">Tokens</div>
      <div class="accordion-value">${escapeHtml(summary.tokenText)}</div>
    </div>
    <div class="accordion-chips">
      ${summary.chips.map(chip => `<span class="chip">${escapeHtml(chip)}</span>`).join('')}
    </div>
  `;
  return renderAccordion('contextSummary', 'Context Summary', content, expanded);
}

export function renderAdvancedInfoAccordion(
  info?: { intent: string; tiersUsed: string[]; isDirty: boolean },
  expanded = false
): string {
  if (!info) {
    return renderAccordion('advancedInfo', 'Advanced Info', 'Run an ask to populate this section.', expanded);
  }

  const content = `
    <div class="accordion-row">
      <div class="accordion-label">Intent</div>
      <div class="accordion-value">${escapeHtml(info.intent)}</div>
    </div>
    <div class="accordion-row">
      <div class="accordion-label">Tiers Used</div>
      <div class="accordion-value">${info.tiersUsed.map(escapeHtml).join(', ')}</div>
    </div>
    <div class="accordion-row">
      <div class="accordion-label">Has Unsaved Changes</div>
      <div class="accordion-value">${info.isDirty ? 'Yes' : 'No'}</div>
    </div>
  `;
  return renderAccordion('advancedInfo', 'Advanced Info', content, expanded);
}

export function renderStatusChips(state: { isDirty: boolean; graphFirst: boolean; docLinked: boolean }): string {
  return `
    <div class="status-chip-row">
      <span class="status-chip dirty">${state.isDirty ? 'dirty-aware' : 'clean'}</span>
      ${state.graphFirst ? '<span class="status-chip graph">graph-first</span>' : ''}
      ${state.docLinked ? '<span class="status-chip docs">doc-linked</span>' : ''}
      <span class="status-spacer"></span>
      <button class="status-info" title="Context provenance and privacy state" aria-label="Context provenance and privacy state">i</button>
    </div>
  `;
}

export function renderActionBar(active: 'chat' | 'inspector' | 'impact' | 'settings' = 'chat'): string {
  return `
    <div class="action-bar">
      <button class="action-btn ${active === 'chat' ? 'primary' : ''}" data-action="openChat" title="Ask about current symbol">
        <span aria-hidden="true">✦</span> Ask
      </button>
      <button class="action-btn ${active === 'inspector' ? 'primary' : ''}" data-action="openInspector" title="Inspect context">
        <span aria-hidden="true">○</span> Inspect Context
      </button>
      <button class="action-btn ${active === 'impact' ? 'primary' : ''}" data-action="showImpact" title="Show impact">
        <span aria-hidden="true">⌘</span> Impact
      </button>
      <button class="action-btn" data-action="search" title="Search workspace">
        <span aria-hidden="true">⌕</span> Search
      </button>
    </div>
  `;
}

export function renderComposerDock(): string {
  return `
    <div class="composer-dock">
      <textarea
        id="composer-input"
        class="composer-textarea"
        placeholder="Ask about this symbol, its behavior, dependencies..."
        aria-label="Message composer"
        aria-describedby="composer-help"
        rows="1"
      ></textarea>
      <button id="composer-send" class="composer-send-btn" title="Send (Enter)" aria-label="Send message">
        <span class="composer-send-icon" aria-hidden="true">➤</span>
      </button>
      <div id="composer-help" class="sr-only">
        Press Enter to send. Press Shift+Enter for a new line. Press Cmd+L to focus composer.
      </div>
    </div>
  `;
}

export function resizeComposerToFit(textarea: HTMLTextAreaElement, maxHeightPx = 220): void {
  textarea.style.height = 'auto';
  const scrollHeight = textarea.scrollHeight;
  const newHeight = Math.min(scrollHeight, maxHeightPx);
  textarea.style.height = `${newHeight}px`;
  textarea.style.overflow = scrollHeight > maxHeightPx ? 'auto' : 'hidden';
}
