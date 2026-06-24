import { PromptContextPayload } from '../context_engineClient';

export interface LastRequest {
  requestId?: string;
  symbol?: string;
  question?: string;
  timestamp: number;
  context?: PromptContextPayload;
  answer: string;
}

export interface ExtensionState {
  selectedSymbol: string | undefined;
  activeFile: string | undefined;
  isDirty: boolean;
  lastContext: PromptContextPayload | undefined;
  lastRequest: LastRequest | undefined;
  context_engineHealth: 'up' | 'down' | 'degraded';
  cloudStatus: 'connected' | 'fallback-local' | 'local' | 'offline';
  workspaceId: string;
  authState: 'ready' | 'missing-token' | 'expired';
}

export const defaultState: ExtensionState = {
  selectedSymbol: undefined,
  activeFile: undefined,
  isDirty: false,
  lastContext: undefined,
  lastRequest: undefined,
  context_engineHealth: 'degraded',
  cloudStatus: 'offline',
  workspaceId: '',
  authState: 'ready',
};

/**
 * Global extension state holder. All UI surfaces read from and listen to updates.
 */
class StateManager {
  private state: ExtensionState = { ...defaultState };
  private listeners: Set<(state: ExtensionState) => void> = new Set();

  getState(): ExtensionState {
    return { ...this.state };
  }

  setState(updates: Partial<ExtensionState>): void {
    this.state = { ...this.state, ...updates };
    this.notifyListeners();
  }

  subscribe(listener: (state: ExtensionState) => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  /**
   * Clear lastRequest if more than 15 minutes have passed.
   * Called when editor changes symbol or on periodic checks.
   */
  clearLastRequestIfStale(): void {
    const state = this.state;
    if (!state.lastRequest) return;

    const now = Date.now();
    const ttlMs = 15 * 60 * 1000;
    const elapsed = now - state.lastRequest.timestamp;

    if (elapsed > ttlMs) {
      this.setState({ lastRequest: undefined });
    }
  }

  private notifyListeners(): void {
    const snapshot = this.getState();
    this.listeners.forEach(listener => listener(snapshot));
  }
}

export const stateManager = new StateManager();
