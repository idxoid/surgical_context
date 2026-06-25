import { escapeHtml } from './html';

export function renderImpactSurfaceShell(
  chrome: string,
  subtitle: string,
  body: string,
): string {
  return `
    <section class="surface surface-impact" aria-label="Impact analysis">
      ${chrome}
      <div class="surface-title">Impact Analysis</div>
      <div class="surface-subtitle">${escapeHtml(subtitle)}</div>
      ${body}
    </section>
  `;
}
