/** Resolve the symbol name at an editor position (definition-aware, keyword-safe). */

const IGNORED_SYMBOLS = new Set([
  'if',
  'for',
  'while',
  'switch',
  'catch',
  'return',
  'function',
  'class',
  'def',
  'async',
  'await',
  'import',
  'from',
  'export',
  'default',
  'const',
  'let',
  'var',
  'new',
  'try',
  'else',
  'elif',
  'pass',
  'break',
  'continue',
  'raise',
  'yield',
  'with',
  'public',
  'private',
  'protected',
  'static',
  'readonly',
  'interface',
  'type',
  'enum',
  'config',
  'options',
  'result',
  'response',
  'error',
  'err',
  'data',
  'body',
  'value',
]);

const DEFINITION_PATTERNS = [
  /^\s*(?:async\s+)?(?:def|class)\s+([A-Za-z_][A-Za-z0-9_]*)/,
  /^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:function|class|interface|type|enum)\s+([A-Za-z_$][A-Za-z0-9_$]*)/,
  /^(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*[=:]/,
  /^\s*(?:public\s+|private\s+|protected\s+|static\s+|async\s+|readonly\s+)*([A-Za-z_$][A-Za-z0-9_$]*)\s*\(/,
];

const LOCAL_BINDING_PATTERN = /^\s+(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*[=:]/;

export function symbolFromDefinitionLine(line: string): { name: string; character: number } | null {
  for (const pattern of DEFINITION_PATTERNS) {
    const match = line.match(pattern);
    const name = match?.[1];
    if (name && !IGNORED_SYMBOLS.has(name)) {
      return {
        name,
        character: match!.index! + match![0].indexOf(name),
      };
    }
  }
  return null;
}

export function isIgnoredSymbol(name: string): boolean {
  return IGNORED_SYMBOLS.has(name);
}

/** True when a document provider returned its file container as the symbol. */
export function isFileNameSymbol(name: string, filePath?: string): boolean {
  if (!name || !filePath) return false;
  const fileName = filePath.replace(/\\/g, '/').split('/').pop();
  return fileName === name;
}

export function isLocalBindingLine(line: string, name: string): boolean {
  const match = line.match(LOCAL_BINDING_PATTERN);
  return match?.[1] === name;
}

export function resolveSymbolNameFromLine(line: string, character: number): string | null {
  const wordPattern = /[$A-Za-z_][$\w]*/g;
  let match: RegExpExecArray | null;
  while ((match = wordPattern.exec(line)) !== null) {
    const start = match.index;
    const end = start + match[0].length;
    if (character >= start && character <= end) {
      const name = match[0];
      if (!isIgnoredSymbol(name) && !isLocalBindingLine(line, name)) {
        return name;
      }
      break;
    }
  }

  return symbolFromDefinitionLine(line)?.name ?? null;
}
