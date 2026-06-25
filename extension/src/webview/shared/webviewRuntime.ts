declare function acquireVsCodeApi(): { postMessage(message: unknown): void };

export const vscode = acquireVsCodeApi();

export function bootWebview(init: () => void): void {
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
}
