import * as vscode from 'vscode';
import { SidecarClient } from './sidecarClient';

const DEBOUNCE_MS = 300;

export class OverlayManager {
  private timers = new Map<string, NodeJS.Timeout>();

  onDocumentChanged(event: vscode.TextDocumentChangeEvent): void {
    if (event.contentChanges.length === 0) return;
    const key = event.document.uri.fsPath;

    const existing = this.timers.get(key);
    if (existing) clearTimeout(existing);

    const handle = setTimeout(async () => {
      this.timers.delete(key);
      try {
        await SidecarClient.overlay(key, event.document.getText());
      } catch {
        // silent — sidecar may be temporarily down; no UI noise on keypress
      }
    }, DEBOUNCE_MS);

    this.timers.set(key, handle);
  }

  onDocumentSaved(doc: vscode.TextDocument): void {
    const key = doc.uri.fsPath;
    const t = this.timers.get(key);
    if (t) {
      clearTimeout(t);
      this.timers.delete(key);
    }
    SidecarClient.deleteOverlay(key).catch(() => {});
  }

  onDocumentClosed(doc: vscode.TextDocument): void {
    const key = doc.uri.fsPath;
    const t = this.timers.get(key);
    if (t) {
      clearTimeout(t);
      this.timers.delete(key);
    }
    SidecarClient.deleteOverlay(key).catch(() => {});
  }

  getSymbolAtCursor(editor: vscode.TextEditor): string | null {
    const pos = editor.selection.active;
    const range = editor.document.getWordRangeAtPosition(pos);
    if (!range) return null;
    return editor.document.getText(range);
  }
}
