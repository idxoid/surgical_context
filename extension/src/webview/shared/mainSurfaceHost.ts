import type { HostToWebviewMessage } from './protocol';
import { showFeedback, showFieldStatus } from './settingsLayout';
import { SURFACE_FROM_HOST_MESSAGE, Surface } from './surfaceChrome';
import type { InspectorTab } from './inspectorLayout';

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
  getSurface(): Surface;
  getInspectorTab(): InspectorTab;
  refreshAccordions(): void;
}

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

  switch (message.type) {
    case 'surface.init':
      delegate.onSurfaceInit(message);
      break;
    case 'chat.requestStarted':
      delegate.setSurface('chat');
      delegate.onRequestStarted(message.requestId, message.symbol);
      break;
    case 'chat.streamChunk':
      delegate.onStreamChunk(message.requestId, message.chunk);
      break;
    case 'chat.requestCompleted':
      delegate.onRequestCompleted(message.requestId, message.answer, message.context);
      break;
    case 'chat.requestFailed':
      delegate.onRequestFailed(message.requestId, message.error);
      break;
    case 'chat.requestStopped':
      delegate.onRequestStopped(message.requestId);
      break;
    case 'chat.contextSummary':
      delegate.setContextSummary(message.summary);
      delegate.refreshAccordions();
      break;
    case 'workspace.updated':
      delegate.onWorkspaceUpdated(message);
      break;
    case 'backend.updated':
      delegate.onBackendUpdated(message);
      break;
    case 'impact.loading':
      delegate.onImpactLoading();
      break;
    case 'impact.loaded':
      delegate.onImpactLoaded(message);
      break;
    case 'impact.loadFailed':
      delegate.onImpactLoadFailed(message);
      break;
    case 'inspector.loaded':
      delegate.onInspectorLoaded(message);
      break;
    case 'inspector.intentLoaded':
      delegate.onInspectorIntentLoaded(message);
      break;
    case 'settings.loaded':
      delegate.onSettingsLoaded(message);
      break;
    case 'settings.saved':
      showFeedback(message.message, 'success');
      break;
    case 'settings.saveFailed':
      showFeedback(message.error, 'error');
      break;
    case 'settings.testUrlComplete':
      showFieldStatus('backendUrl', message.success, message.message);
      break;
    case 'toast.show':
      delegate.showToast(message.message, message.level);
      break;
  }
}
