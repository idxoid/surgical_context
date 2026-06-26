import { HostToWebviewMessage } from './shared/protocol';
import { renderDashboardView } from './shared/dashboardLayout';
import {
  applyDashboardHostMessage,
  bindDashboardActions,
  createInitialDashboardState,
  DashboardPanelState,
} from './shared/dashboardState';
import { mountLayoutHtml } from './shared/domRender';
import { bootWebview, listenForHostMessages, vscode } from './shared/webviewCore';

class DashboardPanel {
  private state: DashboardPanelState = createInitialDashboardState();

  constructor() {
    this.initializeMessageListener();
    this.bindActions(document);
  }

  private initializeMessageListener(): void {
    listenForHostMessages<HostToWebviewMessage>((message) => {
      const nextState = applyDashboardHostMessage(this.state, message);
      if (nextState === null) {
        return;
      }
      this.state = nextState;
      this.render();
    });
  }

  private bindActions(root: ParentNode): void {
    bindDashboardActions(root, (message) => vscode.postMessage(message));
  }

  private render(): void {
    const root = document.getElementById('root');
    if (!root) return;

    mountLayoutHtml(root, renderDashboardView(this.state));
    this.bindActions(root);
  }
}

bootWebview(() => new DashboardPanel());
