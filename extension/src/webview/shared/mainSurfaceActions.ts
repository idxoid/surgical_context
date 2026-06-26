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

type VoidActionMethod = {
  [K in keyof MainSurfaceActionHost]: MainSurfaceActionHost[K] extends (
    ...args: infer Args
  ) => infer Return
    ? Args extends []
      ? Return extends void
        ? K
        : never
      : never
    : never;
}[keyof MainSurfaceActionHost];

type TargetActionMethod = {
  [K in keyof MainSurfaceActionHost]: MainSurfaceActionHost[K] extends (
    ...args: infer Args
  ) => infer Return
    ? Args extends [HTMLElement]
      ? Return extends void
        ? K
        : never
      : never
    : never;
}[keyof MainSurfaceActionHost];

function invokeVoidAction(method: VoidActionMethod): DomActionHandler {
  return (host) => {
    (host[method] as () => void)();
  };
}

function invokeTargetAction(method: TargetActionMethod): DomActionHandler {
  return (host, target) => {
    (host[method] as (target: HTMLElement) => void)(target);
  };
}

const MAIN_SURFACE_DOM_ACTION_HANDLERS: Record<string, DomActionHandler> = {
  switchSurface: (h, t) => h.switchSurface(t.dataset.surface as Surface),
  switchInspectorTab: (h, t) => h.switchInspectorTab(t.dataset.inspectorTab as InspectorTab),
  selectPrompt: (h, t) => h.selectPrompt(t.dataset.requestId ?? null),
  toggleHistory: invokeVoidAction('toggleHistory'),
  newDialog: invokeVoidAction('startNewDialog'),
  restoreDialog: (h, t) => h.restoreDialog(t.dataset.dialogId ?? null),
  openDashboard: invokeVoidAction('postOpenDashboard'),
  ask: invokeVoidAction('focusComposer'),
  'ask-followup': (h) => h.prefillImpactAsk(IMPACT_CHANGE_CHECK_PROMPT(h.getActiveImpactSymbol())),
  'open-related-files': invokeVoidAction('openRelatedImpactFiles'),
  openFile: invokeTargetAction('openFileFromImpact'),
  showMoreImpact: invokeTargetAction('showMoreImpactRows'),
  explainImpact: invokeTargetAction('toggleImpactExplanation'),
  'create-refactor-plan': (h) => h.prefillImpactAsk(IMPACT_REFACTOR_PLAN_PROMPT(h.getActiveImpactSymbol())),
  save: invokeVoidAction('saveSettings'),
  reset: invokeVoidAction('resetSettings'),
  testUrl: invokeVoidAction('testSettingsUrl'),
  openKeybindings: invokeVoidAction('postOpenKeybindings'),
  search: invokeVoidAction('showSearchComingSoon'),
  noop: invokeTargetAction('toggleImpactGroup'),
  feedback: invokeTargetAction('submitFeedback'),
  copy: invokeTargetAction('copyMessage'),
  'copy-json': invokeTargetAction('copyInspectorJson'),
  'copy-api-json': invokeTargetAction('copyInspectorJson'),
  stopStreaming: invokeVoidAction('stopStreaming'),
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
