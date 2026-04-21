import * as vscode from 'vscode';
import { SidecarClient } from './sidecarClient';

const DEBOUNCE_MS = 300;
const SAVE_BATCH_DEBOUNCE_MS = 500;
const MAX_SAVE_BATCH_SIZE = 100;

export class OverlayManager {
  private timers = new Map<string, NodeJS.Timeout>();
  private savedFiles = new Set<string>();
  private saveBatchTimer: NodeJS.Timeout | undefined;

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
    this.scheduleSavedFile(key);
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

  private scheduleSavedFile(filePath: string): void {
    this.savedFiles.add(filePath);
    if (this.saveBatchTimer) clearTimeout(this.saveBatchTimer);

    this.saveBatchTimer = setTimeout(() => {
      this.saveBatchTimer = undefined;
      void this.flushSavedFiles();
    }, SAVE_BATCH_DEBOUNCE_MS);
  }

  private async flushSavedFiles(): Promise<void> {
    const batch = Array.from(this.savedFiles).slice(0, MAX_SAVE_BATCH_SIZE);
    for (const filePath of batch) this.savedFiles.delete(filePath);
    if (!batch.length) return;

    try {
      await SidecarClient.indexFiles(batch);
    } catch {
      // silent — indexing queue may be temporarily down; overlay cleanup already happened
    }

    if (this.savedFiles.size > 0 && !this.saveBatchTimer) {
      this.saveBatchTimer = setTimeout(() => {
        this.saveBatchTimer = undefined;
        void this.flushSavedFiles();
      }, SAVE_BATCH_DEBOUNCE_MS);
    }
  }
}
