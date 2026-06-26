import type { HostToWebviewMessage } from './protocol';
import { showFeedback, showFieldStatus } from './settingsLayout';
import { SURFACE_FROM_HOST_MESSAGE, Surface } from './surfaceChrome';

export type MainSurfaceHostMessage = Extract<
  HostToWebviewMessage,
  {
    type:
      | 'surface.init'
      | 'chat.requestStarted'
      | 'chat.streamChunk'
      | 'chat.requestCompleted'
      | 'chat.requestFailed'
      | 'chat.requestStopped'
      | 'chat.contextSummary'
      | 'workspace.updated'
      | 'backend.updated'
      | 'impact.loading'
      | 'impact.loaded'
      | 'impact.loadFailed'
      | 'inspector.loaded'
      | 'inspector.intentLoaded'
      | 'settings.loaded'
      | 'settings.saved'
      | 'settings.saveFailed'
      | 'settings.testUrlComplete'
      | 'toast.show';
  }
>;

export interface MainSurfaceHostDelegate {
  showSurface(surface: Surface, beforeRender?: () => void): void;
  onSurfaceInit(message: Extract<HostToWebviewMessage, { type: 'surface.init' }>): void;
  setSurface(surface: Surface): void;
  onRequestStarted(requestId: string, symbol?: string): void;
  onStreamChunk(requestId: string, chunk: string): void;
  onRequestCompleted(requestId: string, answer: string, context: unknown): void;
  onRequestFailed(requestId: string, error: string): void;
  onRequestStopped(requestId: string): void;
  setContextSummary(summary: Extract<HostToWebviewMessage, { type: 'chat.contextSummary' }>['summary']): void;
  onWorkspaceUpdated(message: Extract<HostToWebviewMessage, { type: 'workspace.updated' }>): void;
  onBackendUpdated(message: Extract<HostToWebviewMessage, { type: 'backend.updated' }>): void;
  onImpactLoading(): void;
  onImpactLoaded(message: Extract<HostToWebviewMessage, { type: 'impact.loaded' }>): void;
  onImpactLoadFailed(message: Extract<HostToWebviewMessage, { type: 'impact.loadFailed' }>): void;
  onInspectorLoaded(message: Extract<HostToWebviewMessage, { type: 'inspector.loaded' }>): void;
  onInspectorIntentLoaded(message: Extract<HostToWebviewMessage, { type: 'inspector.intentLoaded' }>): void;
  onSettingsLoaded(message: Extract<HostToWebviewMessage, { type: 'settings.loaded' }>): void;
  requestSettings(): void;
  showToast(message: string, level: 'info' | 'success' | 'warning' | 'error'): void;
  refreshAccordions(): void;
}

type HostHandler = (
  delegate: MainSurfaceHostDelegate,
  message: Extract<HostToWebviewMessage, { type: MainSurfaceHostMessage['type'] }>,
) => void;

function hostHandler<M extends MainSurfaceHostMessage['type']>(
  handler: (
    delegate: MainSurfaceHostDelegate,
    message: Extract<HostToWebviewMessage, { type: M }>,
  ) => void,
): HostHandler {
  return handler as HostHandler;
}

const MAIN_SURFACE_HOST_HANDLERS: Record<MainSurfaceHostMessage['type'], HostHandler> = {
  'surface.init': hostHandler((d, m) => d.onSurfaceInit(m)),
  'chat.requestStarted': hostHandler((d, m) => {
    d.setSurface('chat');
    d.onRequestStarted(m.requestId, m.symbol);
  }),
  'chat.streamChunk': hostHandler((d, m) => d.onStreamChunk(m.requestId, m.chunk)),
  'chat.requestCompleted': hostHandler((d, m) => d.onRequestCompleted(m.requestId, m.answer, m.context)),
  'chat.requestFailed': hostHandler((d, m) => d.onRequestFailed(m.requestId, m.error)),
  'chat.requestStopped': hostHandler((d, m) => d.onRequestStopped(m.requestId)),
  'chat.contextSummary': hostHandler((d, m) => {
    d.setContextSummary(m.summary);
    d.refreshAccordions();
  }),
  'workspace.updated': hostHandler((d, m) => d.onWorkspaceUpdated(m)),
  'backend.updated': hostHandler((d, m) => d.onBackendUpdated(m)),
  'impact.loading': hostHandler((d) => d.onImpactLoading()),
  'impact.loaded': hostHandler((d, m) => d.onImpactLoaded(m)),
  'impact.loadFailed': hostHandler((d, m) => d.onImpactLoadFailed(m)),
  'inspector.loaded': hostHandler((d, m) => d.onInspectorLoaded(m)),
  'inspector.intentLoaded': hostHandler((d, m) => d.onInspectorIntentLoaded(m)),
  'settings.loaded': hostHandler((d, m) => d.onSettingsLoaded(m)),
  'settings.saved': hostHandler((_d, m) => showFeedback(m.message, 'success')),
  'settings.saveFailed': hostHandler((_d, m) => showFeedback(m.error, 'error')),
  'settings.testUrlComplete': hostHandler((_d, m) => showFieldStatus('backendUrl', m.success, m.message)),
  'toast.show': hostHandler((d, m) => d.showToast(m.message, m.level)),
};

export function dispatchMainHostMessage(
  delegate: MainSurfaceHostDelegate,
  message: HostToWebviewMessage,
): void {
  const hostSurface = SURFACE_FROM_HOST_MESSAGE[message.type];
  if (hostSurface) {
    delegate.showSurface(
      hostSurface,
      hostSurface === 'settings' ? () => delegate.requestSettings() : undefined,
    );
    return;
  }

  const handler = MAIN_SURFACE_HOST_HANDLERS[message.type as MainSurfaceHostMessage['type']];
  if (handler) {
    handler(delegate, message as Extract<HostToWebviewMessage, { type: MainSurfaceHostMessage['type'] }>);
  }
}
