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

export function listenForHostMessages<T>(handler: (message: T) => void): void {
  const webviewOrigin = window.location.origin;

  window.addEventListener('message', (event: MessageEvent<T>) => {
    const origin = event.origin;
    if (origin === webviewOrigin) {
      handler(event.data);
      return;
    }
    if (origin === '') {
      handler(event.data);
      return;
    }
    if (origin.startsWith('vscode-webview://')) {
      handler(event.data);
    }
  });
}

export function bootWebview(init: () => void): void {
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
}
