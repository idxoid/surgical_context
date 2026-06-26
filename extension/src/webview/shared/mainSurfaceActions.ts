import type { InspectorTab } from './inspectorLayout';
import { SURFACE_FROM_DOM_ACTION, Surface } from './surfaceChrome';

const COPY_ACTIONS = new Set(['copy', 'copy-json', 'copy-api-json', 'feedback']);

const IMPACT_CHANGE_CHECK_PROMPT = (symbol: string | null) =>
  `What should I check before changing ${symbol || 'this symbol'}?`;

const IMPACT_REFACTOR_PLAN_PROMPT = (symbol: string | null) =>
  `Create a refactor plan for ${symbol || 'this symbol'}.`;

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

type DomActionHandler = (host: MainSurfaceActionHost, target: HTMLElement) => void;

function domAction(handler: DomActionHandler): DomActionHandler {
  return handler;
}

const MAIN_SURFACE_DOM_ACTION_HANDLERS: Record<string, DomActionHandler> = {
  switchSurface: domAction((h, t) => h.switchSurface(t.dataset.surface as Surface)),
  switchInspectorTab: domAction((h, t) => h.switchInspectorTab(t.dataset.inspectorTab as InspectorTab)),
  selectPrompt: domAction((h, t) => h.selectPrompt(t.dataset.requestId ?? null)),
  toggleHistory: domAction((h) => h.toggleHistory()),
  newDialog: domAction((h) => h.startNewDialog()),
  restoreDialog: domAction((h, t) => h.restoreDialog(t.dataset.dialogId ?? null)),
  openDashboard: domAction((h) => h.postOpenDashboard()),
  ask: domAction((h) => h.focusComposer()),
  'ask-followup': domAction((h) => h.prefillImpactAsk(IMPACT_CHANGE_CHECK_PROMPT(h.getActiveImpactSymbol()))),
  'open-related-files': domAction((h) => h.openRelatedImpactFiles()),
  openFile: domAction((h, t) => h.openFileFromImpact(t)),
  showMoreImpact: domAction((h, t) => h.showMoreImpactRows(t)),
  explainImpact: domAction((h, t) => h.toggleImpactExplanation(t)),
  'create-refactor-plan': domAction((h) => h.prefillImpactAsk(IMPACT_REFACTOR_PLAN_PROMPT(h.getActiveImpactSymbol()))),
  save: domAction((h) => h.saveSettings()),
  reset: domAction((h) => h.resetSettings()),
  testUrl: domAction((h) => h.testSettingsUrl()),
  openKeybindings: domAction((h) => h.postOpenKeybindings()),
  search: domAction((h) => h.showSearchComingSoon()),
  noop: domAction((h, t) => h.toggleImpactGroup(t)),
  feedback: domAction((h, t) => h.submitFeedback(t)),
  copy: domAction((h, t) => h.copyMessage(t)),
  'copy-json': domAction((h, t) => h.copyInspectorJson(t)),
  'copy-api-json': domAction((h, t) => h.copyInspectorJson(t)),
  stopStreaming: domAction((h) => h.stopStreaming()),
};

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

  const handler = MAIN_SURFACE_DOM_ACTION_HANDLERS[action];
  if (handler) {
    handler(host, target);
  }
}
