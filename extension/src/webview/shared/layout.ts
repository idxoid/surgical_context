import { escapeHtml } from './html';
import { ChatMessage } from './protocol';

export { escapeHtml };

export function renderMessageCard(message: ChatMessage, selectedRequestId?: string | null): string {
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
      <article class="${baseClass}${statusClass}" data-message-id="${escapeHtml(message.id)}"${requestAttrs}${selectionAttrs} title="${escapeHtml(message.content)}">
        <div class="message-content">${escapeHtml(message.content)}</div>
        ${renderMessageFooter(message)}
      </article>
    `;
  }

  let content = `
    <article class="${baseClass}${statusClass}" data-message-id="${escapeHtml(message.id)}"${requestAttrs}>
      <div class="message-content">${escapeHtml(message.content)}</div>
  `;

  if (message.error) {
    content += `<div class="message-error">Error: ${escapeHtml(message.error)}</div>`;
  }

  content += renderMessageFooter(message);

  content += '</article>';
  return content;
}

function renderMessageFooter(message: ChatMessage): string {
  const time = formatMessageTime(message.timestamp);
  const route = formatModelRoute(message);
  const assistantFeedback = message.type === 'assistant' && message.status === 'done'
    ? `
        <button class="message-action-button" data-action="feedback" data-rating="up" title="Helpful" aria-label="Helpful">+</button>
        <button class="message-action-button" data-action="feedback" data-rating="down" title="Not helpful" aria-label="Not helpful">-</button>
      `
    : '';

  return `
    <div class="message-footer">
      <time class="message-time" datetime="${escapeHtml(time.iso)}" title="${escapeHtml(time.title)}">${escapeHtml(time.label)}</time>
      ${route ? `<span class="message-route ${route.fallback ? 'fallback' : ''}" title="${escapeHtml(route.title)}">${escapeHtml(route.label)}</span>` : ''}
      <div class="message-actions">
        ${assistantFeedback}
        <button class="message-action-button" data-action="copy" title="Copy message" aria-label="Copy message">
          <svg class="message-action-icon" viewBox="0 0 16 16" aria-hidden="true" focusable="false">
            <rect x="5" y="3" width="8" height="10" rx="1.5"></rect>
            <path d="M3 6.5V12a2 2 0 0 0 2 2h5.5"></path>
          </svg>
        </button>
      </div>
    </div>
  `;
}

function formatModelRoute(message: ChatMessage): { label: string; title: string; fallback: boolean } | null {
  if (message.type !== 'assistant') {
    return null;
  }

  const route = message.context?.metadata?.assembly?.model_route;
  if (!route) {
    return null;
  }

  const provider = routeText(route.provider) || 'unknown';
  const model = routeText(route.model);
  const preference = routeText(route.preference);
  const reason = routeText(route.reason);
  const degraded = Boolean(route.degraded);
  const fallback = degraded || reason.includes('fallback') || reason.includes('unavailable');
  const reasonText = routeReasonLabel(reason);
  const labelParts = [provider, model].filter(Boolean);
  const label = `${labelParts.join(' / ') || provider}${fallback ? ' · fallback' : ''}`;
  const titleParts = [
    `Answered by ${labelParts.join(' / ') || provider}`,
    preference ? `Preference: ${preference}` : '',
    reasonText,
    degraded ? 'Response was degraded.' : '',
  ].filter(Boolean);

  return {
    label,
    title: titleParts.join(' | '),
    fallback,
  };
}

function routeText(value: unknown): string {
  return typeof value === 'string' ? value.trim() : '';
}

function routeReasonLabel(reason: string): string {
  switch (reason) {
    case 'claude_unavailable_fallback':
      return 'Auto wanted Claude, but Anthropic credentials/client were unavailable; Ollama answered.';
    case 'claude_error_fallback':
      return 'Claude failed during the request; Ollama answered.';
    case 'router_selected_claude':
      return 'Router selected Claude.';
    case 'router_selected_ollama':
      return 'Router selected Ollama.';
    case 'llm_unreachable_context_only':
      return 'LLM was unreachable; context-only degraded response.';
    default:
      return reason ? `Route reason: ${reason}` : '';
  }
}

function formatMessageTime(timestamp: number): { label: string; title: string; iso: string } {
  const date = new Date(timestamp);
  if (Number.isNaN(date.getTime())) {
    return { label: '', title: '', iso: '' };
  }

  return {
    label: date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
    title: date.toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' }),
    iso: date.toISOString(),
  };
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
      ${summary.chips.map(renderContextChip).join('')}
    </div>
  `;
  return renderAccordion('contextSummary', 'Context Summary', content, expanded);
}

function renderContextChip(chip: string): string {
  const className = chip.startsWith('warning:') ? 'chip warning' : 'chip';
  const label = chip.startsWith('warning:') ? chip.slice('warning:'.length) : chip;
  return `<span class="${className}">${escapeHtml(label)}</span>`;
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

export function renderComposerDock(isStreaming = false): string {
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
      <button id="composer-send" class="composer-send-btn" title="Send (Enter)" aria-label="Send message" ${isStreaming ? 'hidden' : ''}>
        <span class="composer-send-icon" aria-hidden="true">➤</span>
      </button>
      <button
        id="composer-stop"
        class="composer-stop-btn"
        data-action="stopStreaming"
        title="Stop response"
        aria-label="Stop response generation"
        ${isStreaming ? '' : 'hidden'}
      >
        <span class="composer-stop-icon" aria-hidden="true"></span>
      </button>
      <div id="composer-help" class="sr-only">
        Press Enter to send. Press Shift+Enter for a new line. Press Cmd+L to focus composer. While a response is streaming, use Stop to cancel it.
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
