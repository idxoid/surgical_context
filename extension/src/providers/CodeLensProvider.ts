import * as vscode from 'vscode';

export class SurgicalContextCodeLensProvider implements vscode.CodeLensProvider {
  public codeLensProviders: vscode.CodeLens[] = [];

  provideCodeLenses(document: vscode.TextDocument): vscode.CodeLens[] {
    const lenses: vscode.CodeLens[] = [];

    const text = document.getText();
    const lines = text.split('\n');

    lines.forEach((line, lineIndex) => {
      const symbol = symbolFromDefinitionLine(line);
      if (symbol) {
        const position = new vscode.Position(lineIndex, symbol.character);
        const range = new vscode.Range(
          position,
          new vscode.Position(lineIndex, symbol.character + symbol.name.length)
        );
        const target = {
          filePath: document.fileName,
          symbol: symbol.name,
          line: lineIndex,
          character: symbol.character,
        };

        const askLens = new vscode.CodeLens(range, {
          title: '💬 Ask',
          command: 'surgicalContext.askCurrentSymbol',
          arguments: [target],
        });

        const impactLens = new vscode.CodeLens(range, {
          title: '📊 Impact',
          command: 'surgicalContext.showImpact',
          arguments: [target],
        });

        lenses.push(askLens, impactLens);
      }
    });

    return lenses;
  }

  resolveCodeLens(codeLens: vscode.CodeLens): vscode.CodeLens {
    return codeLens;
  }
}

function symbolFromDefinitionLine(line: string): { name: string; character: number } | null {
  const patterns = [
    /^\s*(?:async\s+)?(?:def|class)\s+([A-Za-z_][A-Za-z0-9_]*)/,
    /^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:function|class|interface|type|enum)\s+([A-Za-z_$][A-Za-z0-9_$]*)/,
    /^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*[=:]/,
    /^\s*(?:public\s+|private\s+|protected\s+|static\s+|async\s+|readonly\s+)*([A-Za-z_$][A-Za-z0-9_$]*)\s*\(/,
  ];
  const ignored = new Set(['if', 'for', 'while', 'switch', 'catch', 'return', 'function']);

  for (const pattern of patterns) {
    const match = line.match(pattern);
    const name = match?.[1];
    if (name && !ignored.has(name)) {
      return {
        name,
        character: match.indexOf(name),
      };
    }
  }

  return null;
}
