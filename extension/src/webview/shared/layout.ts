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

export function renderMessageCard(message: ChatMessage): string {
  const timestamp = new Date(message.timestamp).toLocaleTimeString();
  const baseClass = `message-card message-${message.type}`;
  const statusClass = message.status ? ` status-${message.status}` : '';

  if (message.type === 'user') {
    return `
      <div class="${baseClass}${statusClass}">
        <div class="message-header">
          <span class="message-role">You</span>
          <span class="message-time">${timestamp}</span>
        </div>
        <div class="message-content">${escapeHtml(message.content)}</div>
      </div>
    `;
  }

  // Assistant message
  let content = `
    <div class="${baseClass}${statusClass}">
      <div class="message-header">
        <span class="message-role">Surgical Context</span>
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
        <button class="action-btn" data-action="feedback" data-rating="up" title="Helpful">👍</button>
        <button class="action-btn" data-action="feedback" data-rating="down" title="Not helpful">👎</button>
        <button class="action-btn" data-action="copy" title="Copy response">📋</button>
      </div>
    `;
  }

  content += '</div>';
  return content;
}

export function renderStreamingCursor(): string {
  return `<div class="streaming-cursor">▌</div>`;
}

export function renderAccordion(id: string, title: string, content: string, expanded = false): string {
  return `
    <div class="accordion-group" data-accordion="${id}">
      <button class="accordion-header" aria-expanded="${expanded}" aria-controls="${id}-content">
        <span class="accordion-title">${title}</span>
        <span class="accordion-icon">▼</span>
      </button>
      <div id="${id}-content" class="accordion-content ${expanded ? 'expanded' : ''}" hidden="${!expanded}">
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
}): string {
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
  return renderAccordion('environment', 'Environment', content, false);
}

export function renderContextSummaryAccordion(summary?: {
  primaryLabel: string;
  graphCount: number;
  docsCount: number;
  tokenText: string;
  chips: string[];
}): string {
  if (!summary) {
    return renderAccordion('contextSummary', 'Context Summary', 'Run an ask to populate this section.', false);
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
  return renderAccordion('contextSummary', 'Context Summary', content, false);
}

export function renderAdvancedInfoAccordion(info?: { intent: string; tiersUsed: string[]; isDirty: boolean }): string {
  if (!info) {
    return renderAccordion('advancedInfo', 'Advanced Info', 'Run an ask to populate this section.', false);
  }

  const content = `
    <div class="accordion-row">
      <div class="accordion-label">Intent</div>
      <div class="accordion-value">${escapeHtml(info.intent)}</div>
    </div>
    <div class="accordion-row">
      <div class="accordion-label">Tiers Used</div>
      <div class="accordion-value">${info.tiersUsed.join(', ')}</div>
    </div>
    <div class="accordion-row">
      <div class="accordion-label">Has Unsaved Changes</div>
      <div class="accordion-value">${info.isDirty ? 'Yes' : 'No'}</div>
    </div>
  `;
  return renderAccordion('advancedInfo', 'Advanced Info', content, false);
}

export function renderStatusChips(state: { isDirty: boolean; graphFirst: boolean; docLinked: boolean }): string {
  return `
    <div class="status-chip-row">
      ${state.isDirty ? '<span class="status-chip dirty">Unsaved Changes</span>' : ''}
      ${state.graphFirst ? '<span class="status-chip graph">Graph-First</span>' : ''}
      ${state.docLinked ? '<span class="status-chip docs">Doc-Linked</span>' : ''}
    </div>
  `;
}

export function renderActionBar(): string {
  return `
    <div class="action-bar">
      <button class="action-main-btn" data-action="ask" title="Ask about current symbol">Ask</button>
      <button class="action-sec-btn" data-action="openInspector" title="Inspect context">Context</button>
      <button class="action-sec-btn" data-action="showImpact" title="Show impact">Impact</button>
      <button class="action-sec-btn" data-action="search" title="Search workspace">Search</button>
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
        rows="1"
      ></textarea>
      <button id="composer-send" class="composer-send-btn" title="Send (Enter)">Send</button>
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
