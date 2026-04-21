import * as vscode from 'vscode';

export class SurgicalContextCodeLensProvider implements vscode.CodeLensProvider {
  public codeLensProviders: vscode.CodeLens[] = [];

  provideCodeLenses(document: vscode.TextDocument): vscode.CodeLens[] {
    const lenses: vscode.CodeLens[] = [];

    const text = document.getText();
    const lines = text.split('\n');

    // Find symbol definitions (function, class, method declarations)
    // This is a simplified regex - a real implementation would use language-specific parsing
    const symbolRegex = /^(async\s+)?((function|class|const|let|var|interface|type|enum|export)\s+)?([a-zA-Z_$][a-zA-Z0-9_$]*)/;

    lines.forEach((line, lineIndex) => {
      const match = line.match(symbolRegex);
      if (match) {
        const symbolName = match[4];
        const range = new vscode.Range(
          new vscode.Position(lineIndex, 0),
          new vscode.Position(lineIndex, line.length)
        );

        // Ask about this symbol
        const askLens = new vscode.CodeLens(range, {
          title: '💬 Ask',
          command: 'surgicalContext.askCurrentSymbol',
          arguments: [document.fileName, symbolName],
        });

        // Show impact
        const impactLens = new vscode.CodeLens(range, {
          title: '📊 Impact',
          command: 'surgicalContext.showImpact',
          arguments: [symbolName],
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
