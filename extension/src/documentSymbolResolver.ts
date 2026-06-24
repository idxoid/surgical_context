import * as vscode from 'vscode';
import { isIgnoredSymbol, resolveSymbolNameFromLine } from './symbolResolution';

const CALLABLE_SYMBOL_KINDS = new Set([
  vscode.SymbolKind.Class,
  vscode.SymbolKind.Interface,
  vscode.SymbolKind.Enum,
  vscode.SymbolKind.Struct,
  vscode.SymbolKind.Function,
  vscode.SymbolKind.Method,
  vscode.SymbolKind.Constructor,
  vscode.SymbolKind.Namespace,
]);

function containsPosition(symbol: vscode.DocumentSymbol, position: vscode.Position): boolean {
  return symbol.range.contains(position);
}

/** Innermost function/class/module scope that contains `position`. */
function findInnermostCallable(
  symbols: vscode.DocumentSymbol[],
  position: vscode.Position
): vscode.DocumentSymbol | null {
  for (const symbol of symbols) {
    if (!containsPosition(symbol, position)) {
      continue;
    }
    if (symbol.children?.length) {
      const child = findInnermostCallable(symbol.children, position);
      if (child) {
        return child;
      }
    }
    if (CALLABLE_SYMBOL_KINDS.has(symbol.kind) && !isIgnoredSymbol(symbol.name)) {
      return symbol;
    }
  }
  return null;
}

export async function resolveDocumentSymbolAtPosition(
  document: vscode.TextDocument,
  position: vscode.Position
): Promise<string | null> {
  const symbols = await vscode.commands.executeCommand<vscode.DocumentSymbol[]>(
    'vscode.executeDocumentSymbolProvider',
    document.uri
  );

  if (symbols?.length) {
    const callable = findInnermostCallable(symbols, position);
    if (callable?.name) {
      return callable.name;
    }
  }

  return resolveSymbolNameFromLine(document.lineAt(position.line).text, position.character);
}
