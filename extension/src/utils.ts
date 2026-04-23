import * as vscode from 'vscode';

export function getNonce(): string {
  let text = '';
  const possible = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';
  for (let i = 0; i < 32; i++) {
    text += possible.charAt(Math.floor(Math.random() * possible.length));
  }
  return text;
}

export function getWebviewContent(
  webview: vscode.Webview,
  extensionUri: vscode.Uri,
  scriptFile: string,
  cssFile?: string
): string {
  const scriptUri = webview.asWebviewUri(vscode.Uri.joinPath(extensionUri, 'media', scriptFile));
  const cssUri = cssFile
    ? webview.asWebviewUri(vscode.Uri.joinPath(extensionUri, 'media', cssFile))
    : undefined;
  const nonce = getNonce();

  return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta http-equiv="Content-Security-Policy"
    content="default-src 'none'; script-src 'nonce-${nonce}'; style-src 'unsafe-inline' 'nonce-${nonce}';">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Surgical Context</title>
  ${
    cssUri
      ? `<link rel="stylesheet" nonce="${nonce}" href="${cssUri}">`
      : '<style nonce="' + nonce + '">body { font-family: var(--vscode-font-family); color: var(--vscode-foreground); background: var(--vscode-editor-background); }</style>'
  }
</head>
<body>
  <div id="root"></div>
  <script nonce="${nonce}" src="${scriptUri}"><\/script>
</body>
</html>`;
}

export interface SSEEvent {
  event: string;
  data: unknown;
}

export function parseSSELine(line: string): SSEEvent | null {
  const eventMatch = line.match(/^event:\s*(.+)$/);
  const dataMatch = line.match(/^data:\s*(.*)$/);

  if (!eventMatch || !dataMatch) {
    return null;
  }

  const event = eventMatch[1].trim();
  let data: unknown;

  try {
    data = JSON.parse(dataMatch[1]);
  } catch {
    data = dataMatch[1];
  }

  return { event, data };
}

export interface SSECallbacks {
  onTrace?: (traceId: string) => void;
  onChunk?: (chunk: string) => void;
  onContext?: (context: unknown) => void;
  onDone?: (traceId: string) => void;
  onError?: (error: string) => void;
}

export async function parseSSEStream(
  response: Response,
  callbacks: SSECallbacks
): Promise<void> {
  const reader = response.body?.getReader();
  if (!reader) {
    throw new Error('Response body is not readable');
  }

  const decoder = new TextDecoder();
  let buffer = '';
  let eventName = '';
  let dataLines: string[] = [];

  const dispatchEvent = () => {
    if (!eventName || dataLines.length === 0) {
      eventName = '';
      dataLines = [];
      return;
    }

    let data: unknown = dataLines.join('\n');
    try {
      data = JSON.parse(data as string);
    } catch {
      // Leave malformed event data as plain text for the error path.
    }

    switch (eventName) {
      case 'trace':
        if (typeof data === 'object' && data !== null && 'trace_id' in data) {
          callbacks.onTrace?.((data as any).trace_id);
        }
        break;
      case 'chunk':
        if (typeof data === 'object' && data !== null && 'content' in data) {
          callbacks.onChunk?.((data as any).content);
        }
        break;
      case 'context':
        if (typeof data === 'object' && data !== null && 'context' in data) {
          callbacks.onContext?.((data as any).context);
        }
        break;
      case 'done':
        if (typeof data === 'object' && data !== null && 'trace_id' in data) {
          callbacks.onDone?.((data as any).trace_id);
        }
        break;
      case 'error':
        if (typeof data === 'object' && data !== null && 'error' in data) {
          callbacks.onError?.((data as any).error);
        }
        break;
    }

    eventName = '';
    dataLines = [];
  };

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');

      // Keep the last incomplete line in the buffer
      buffer = lines.pop() || '';

      for (const line of lines) {
        const trimmed = line.trimEnd();
        if (!trimmed) {
          dispatchEvent();
        } else if (trimmed.startsWith('event:')) {
          eventName = trimmed.slice('event:'.length).trim();
        } else if (trimmed.startsWith('data:')) {
          dataLines.push(trimmed.slice('data:'.length).trimStart());
        }
      }
    }

    if (buffer.trim()) {
      const trimmed = buffer.trimEnd();
      if (trimmed.startsWith('event:')) {
        eventName = trimmed.slice('event:'.length).trim();
      } else if (trimmed.startsWith('data:')) {
        dataLines.push(trimmed.slice('data:'.length).trimStart());
      }
    }
    dispatchEvent();
  } finally {
    reader.releaseLock();
  }
}
