import { escapeHtml } from './html';

export type Surface = 'chat' | 'inspector' | 'impact' | 'settings';

export const SURFACE_FROM_HOST_MESSAGE: Record<string, Surface> = {
  'surface.showChat': 'chat',
  'surface.showInspector': 'inspector',
  'surface.showImpact': 'impact',
  'surface.showSettings': 'settings',
};

export const SURFACE_FROM_DOM_ACTION: Record<string, Surface> = {
  openChat: 'chat',
  openInspector: 'inspector',
  openSettings: 'settings',
  showImpact: 'impact',
};

const MAIN_SURFACE_TABS: Array<{ id: Surface; label: string; icon: string }> = [
  { id: 'chat', label: 'Chat', icon: '◌' },
  { id: 'inspector', label: 'Inspector', icon: '◎' },
  { id: 'impact', label: 'Impact', icon: '⌁' },
];

export function renderSurfaceNavTab(options: {
  label: string;
  icon: string;
  active?: boolean;
  action: string;
  surface?: Surface;
}): string {
  const surfaceAttr = options.surface ? ` data-surface="${options.surface}"` : '';
  return `
    <button
      class="surface-tab ${options.active ? 'active' : ''}"
      data-action="${options.action}"${surfaceAttr}
      aria-current="${options.active ? 'page' : 'false'}"
      title="${options.label}"
      aria-label="${options.label}"
    >
      <span aria-hidden="true">${options.icon}</span>
    </button>
  `;
}

export function renderMainSurfaceTabBar(
  activeSurface: Surface,
  chatSessionActionsHtml: string,
): string {
  return `
    <nav class="surface-tab-bar" aria-label="Surgical Context sections">
      <div class="surface-tab-group">
        ${MAIN_SURFACE_TABS.map(tab => renderSurfaceNavTab({
          label: tab.label,
          icon: tab.icon,
          active: activeSurface === tab.id,
          action: 'switchSurface',
          surface: tab.id,
        })).join('')}
        ${renderSurfaceNavTab({
          label: 'Dashboard',
          icon: '▦',
          action: 'openDashboard',
        })}
      </div>
      <div class="surface-tab-actions">
        ${activeSurface === 'chat' ? chatSessionActionsHtml : ''}
        ${renderSurfaceNavTab({
          label: 'Settings',
          icon: '⚙',
          active: activeSurface === 'settings',
          action: 'switchSurface',
          surface: 'settings',
        })}
      </div>
    </nav>
  `;
}

export function renderSurfaceShell(
  surfaceClass: string,
  ariaLabel: string,
  chrome: string,
  body: string,
): string {
  return `
    <section class="surface ${surfaceClass}" aria-label="${escapeHtml(ariaLabel)}">
      ${chrome}
      ${body}
    </section>
  `;
}

export function renderImpactSurfaceShell(
  chrome: string,
  subtitle: string,
  body: string,
): string {
  return renderSurfaceShell(
    'surface-impact',
    'Impact analysis',
    chrome,
    `
      <div class="surface-title">Impact Analysis</div>
      <div class="surface-subtitle">${escapeHtml(subtitle)}</div>
      ${body}
    `,
  );
}

export function renderInspectorSurfaceShell(chrome: string, body: string): string {
  return renderSurfaceShell('surface-inspector', 'Context inspector', chrome, body);
}
