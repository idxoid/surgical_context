import * as vscode from 'vscode';
import { symbolFromDefinitionLine } from '../symbolResolution';

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
