declare function acquireVsCodeApi(): { postMessage(message: unknown): void };

export const vscode = acquireVsCodeApi();

const VSCODE_WEBVIEW_ORIGIN_PREFIX = 'vscode-webview://';

function isAllowedHostMessageOrigin(origin: string, webviewOrigin: string): boolean {
  return (
    origin === webviewOrigin
    || origin === ''
    || origin.startsWith(VSCODE_WEBVIEW_ORIGIN_PREFIX)
  );
}

/** Accept only messages posted by the VS Code extension host into this webview. */
export function isTrustedHostWebviewMessage(event: MessageEvent): boolean {
  return isAllowedHostMessageOrigin(event.origin, globalThis.location.origin);
}

export function listenForHostMessages<T>(handler: (message: T) => void): void {
  const webviewOrigin = globalThis.location.origin;

  function receiveHostMessage(event: MessageEvent<T>): void {
    if (
      event.origin !== webviewOrigin
      && event.origin !== ''
      && !event.origin.startsWith(VSCODE_WEBVIEW_ORIGIN_PREFIX)
    ) {
      return;
    }
    handler(event.data);
  }

  window.addEventListener('message', receiveHostMessage);
}

export function bootWebview(init: () => void): void {
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
}
