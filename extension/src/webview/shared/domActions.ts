export function bindClickAction(
  root: ParentNode,
  action: string,
  handler: () => void,
): void {
  const button = root.querySelector(`[data-action="${action}"]`) as HTMLButtonElement | null;
  if (button) {
    button.addEventListener('click', handler);
  }
}

export function bindDataActions(root: ParentNode, handler: (event: Event) => void): void {
  root.querySelectorAll('[data-action]').forEach(element => {
    element.addEventListener('click', handler);
  });
}
