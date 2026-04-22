import { PromptContextPayload } from '../sidecarClient';

export interface ExtensionState {
  selectedSymbol: string | undefined;
  activeFile: string | undefined;
  isDirty: boolean;
  lastContext: PromptContextPayload | undefined;
  sidecarHealth: 'up' | 'down' | 'degraded';
  cloudStatus: 'connected' | 'fallback-local' | 'local' | 'offline';
  workspaceId: string;
  authState: 'ready' | 'missing-token' | 'expired';
}

export const defaultState: ExtensionState = {
  selectedSymbol: undefined,
  activeFile: undefined,
  isDirty: false,
  lastContext: undefined,
  sidecarHealth: 'degraded',
  cloudStatus: 'offline',
  workspaceId: 'local/default@main',
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

  private notifyListeners(): void {
    const snapshot = this.getState();
    this.listeners.forEach(listener => listener(snapshot));
  }
}

export const stateManager = new StateManager();
