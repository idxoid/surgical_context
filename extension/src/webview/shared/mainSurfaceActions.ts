import type { InspectorTab } from './inspectorLayout';
import { SURFACE_FROM_DOM_ACTION, Surface } from './surfaceChrome';

const COPY_ACTIONS = new Set(['copy', 'copy-json', 'copy-api-json', 'feedback']);

export interface MainSurfaceActionHost {
  switchSurface(surface: Surface | null): void;
  switchInspectorTab(tab: InspectorTab | null): void;
  selectPrompt(requestId: string | null): void;
  toggleHistory(): void;
  startNewDialog(): void;
  restoreDialog(dialogId: string | null): void;
  postOpenDashboard(): void;
  focusComposer(): void;
  focusComposerDeferred(): void;
  prefillImpactAsk(text: string): void;
  openRelatedImpactFiles(): void;
  openFileFromImpact(target: HTMLElement): void;
  showMoreImpactRows(target: HTMLElement): void;
  toggleImpactExplanation(target: HTMLElement): void;
  saveSettings(): void;
  resetSettings(): void;
  testSettingsUrl(): void;
  postOpenKeybindings(): void;
  showSearchComingSoon(): void;
  toggleImpactGroup(target: HTMLElement): void;
  submitFeedback(target: HTMLElement): void;
  copyMessage(target: HTMLElement): void;
  copyInspectorJson(target: HTMLElement): void;
  stopStreaming(): void;
  handleSurfaceDomAction(surface: Surface, action: string, target: HTMLElement): void;
  getActiveImpactSymbol(): string | null;
}

export function handleMainSurfaceAction(host: MainSurfaceActionHost, event: Event): void {
  const target = event.currentTarget as HTMLElement;
  const action = target.dataset.action;
  if (!action) return;

  if (COPY_ACTIONS.has(action)) {
    event.preventDefault();
    event.stopPropagation();
  }

  const surfaceAction = SURFACE_FROM_DOM_ACTION[action];
  if (surfaceAction) {
    host.handleSurfaceDomAction(surfaceAction, action, target);
    return;
  }

  switch (action) {
    case 'switchSurface':
      host.switchSurface(target.dataset.surface as Surface);
      break;
    case 'switchInspectorTab':
      host.switchInspectorTab(target.dataset.inspectorTab as InspectorTab);
      break;
    case 'selectPrompt':
      host.selectPrompt(target.dataset.requestId ?? null);
      break;
    case 'toggleHistory':
      host.toggleHistory();
      break;
    case 'newDialog':
      host.startNewDialog();
      break;
    case 'restoreDialog':
      host.restoreDialog(target.dataset.dialogId ?? null);
      break;
    case 'openDashboard':
      host.postOpenDashboard();
      break;
    case 'ask':
      host.focusComposer();
      break;
    case 'ask-followup':
      host.prefillImpactAsk(
        `What should I check before changing ${host.getActiveImpactSymbol() || 'this symbol'}?`,
      );
      break;
    case 'open-related-files':
      host.openRelatedImpactFiles();
      break;
    case 'openFile':
      host.openFileFromImpact(target);
      break;
    case 'showMoreImpact':
      host.showMoreImpactRows(target);
      break;
    case 'explainImpact':
      host.toggleImpactExplanation(target);
      break;
    case 'create-refactor-plan':
      host.prefillImpactAsk(
        `Create a refactor plan for ${host.getActiveImpactSymbol() || 'this symbol'}.`,
      );
      break;
    case 'save':
      host.saveSettings();
      break;
    case 'reset':
      host.resetSettings();
      break;
    case 'testUrl':
      host.testSettingsUrl();
      break;
    case 'openKeybindings':
      host.postOpenKeybindings();
      break;
    case 'search':
      host.showSearchComingSoon();
      break;
    case 'noop':
      host.toggleImpactGroup(target);
      break;
    case 'feedback':
      host.submitFeedback(target);
      break;
    case 'copy':
      host.copyMessage(target);
      break;
    case 'copy-json':
    case 'copy-api-json':
      host.copyInspectorJson(target);
      break;
    case 'stopStreaming':
      host.stopStreaming();
      break;
  }
}
