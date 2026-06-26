import { randomBytes } from 'node:crypto';
import * as vscode from 'vscode';

const NONCE_ALPHABET = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';

export function getNonce(): string {
  const bytes = randomBytes(32);
  return Array.from(bytes, (byte) => NONCE_ALPHABET[byte % NONCE_ALPHABET.length]).join('');
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
  <script nonce="${nonce}" type="module" src="${scriptUri}"><\/script>
</body>
</html>`;
}

export interface SSEEvent {
  event: string;
  data: unknown;
}

export function parseSSELine(line: string): SSEEvent | null {
  if (!line.startsWith('event:') || !line.startsWith('data:')) {
    return null;
  }

  const event = line.slice('event:'.length).trim();
  const dataRaw = line.slice('data:'.length).trimStart();
  let data: unknown;

  try {
    data = JSON.parse(dataRaw);
  } catch {
    data = dataRaw;
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

function dispatchSseTraceEvent(data: unknown, callbacks: SSECallbacks): void {
  if (typeof data === 'object' && data !== null && 'trace_id' in data) {
    callbacks.onTrace?.((data as { trace_id: string }).trace_id);
  }
}

function dispatchSseChunkEvent(data: unknown, callbacks: SSECallbacks): void {
  if (typeof data === 'object' && data !== null && 'content' in data) {
    callbacks.onChunk?.((data as { content: string }).content);
  }
}

function dispatchSseContextEvent(data: unknown, callbacks: SSECallbacks): void {
  if (typeof data === 'object' && data !== null && 'context' in data) {
    callbacks.onContext?.((data as { context: unknown }).context);
  }
}

function dispatchSseDoneEvent(data: unknown, callbacks: SSECallbacks): void {
  if (typeof data === 'object' && data !== null && 'trace_id' in data) {
    callbacks.onDone?.((data as { trace_id: string }).trace_id);
  }
}

function dispatchSseErrorEvent(data: unknown, callbacks: SSECallbacks): void {
  if (typeof data === 'object' && data !== null && 'error' in data) {
    callbacks.onError?.((data as { error: string }).error);
  }
}

const SSE_EVENT_HANDLERS: Record<string, (data: unknown, callbacks: SSECallbacks) => void> = {
  trace: dispatchSseTraceEvent,
  chunk: dispatchSseChunkEvent,
  context: dispatchSseContextEvent,
  done: dispatchSseDoneEvent,
  error: dispatchSseErrorEvent,
};

function dispatchNamedSseEvent(
  eventName: string,
  data: unknown,
  callbacks: SSECallbacks
): void {
  SSE_EVENT_HANDLERS[eventName]?.(data, callbacks);
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

    dispatchNamedSseEvent(eventName, data, callbacks);

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
