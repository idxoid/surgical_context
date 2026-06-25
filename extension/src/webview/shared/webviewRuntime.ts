declare function acquireVsCodeApi(): { postMessage(message: unknown): void };

export const vscode = acquireVsCodeApi();

/** Accept only messages posted by the VS Code extension host into this webview. */
export function isTrustedHostWebviewMessage(event: MessageEvent): boolean {
  const origin = event.origin;
  if (origin === window.location.origin) {
    return true;
  }
  // Extension host posts with an empty origin in some embedded webview contexts.
  if (origin === '') {
    return true;
  }
  return origin.startsWith('vscode-webview://');
}

export function bootWebview(init: () => void): void {
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
}
