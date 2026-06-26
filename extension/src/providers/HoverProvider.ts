import * as vscode from 'vscode';
import { OverlayManager } from '../overlayManager';

export class SurgicalContextHoverProvider implements vscode.HoverProvider {
  constructor(private readonly overlayManager: OverlayManager) {}

  provideHover(
    document: vscode.TextDocument,
    position: vscode.Position
  ): vscode.ProviderResult<vscode.Hover> {
    // Get the symbol at cursor position
    const editor = vscode.window.activeTextEditor;
    if (!editor || editor.document !== document) {
      return undefined;
    }

    const symbol = this.overlayManager.getSymbolAtCursor(editor);
    if (!symbol) {
      return undefined;
    }

    // Return a hover with quick action hints
    const markdown = new vscode.MarkdownString(
      `**${symbol}**\n\n` +
      `- **Ask** (Ctrl+Alt+A / Cmd+Alt+A): Get analysis about this symbol\n` +
      `- **Impact** (Ctrl+Alt+I / Cmd+Alt+I): See what depends on this symbol\n` +
      `- **Find Docs**: Search for related documentation`
    );

    return new vscode.Hover(markdown);
  }
}
