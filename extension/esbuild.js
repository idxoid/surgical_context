const esbuild = require('esbuild');
const fs = require('node:fs');
const path = require('node:path');

const watch = process.argv.includes('--watch');
const production = process.argv.includes('--production');

/** Extension host bundle (Node.js) */
const hostOptions = {
  entryPoints: ['src/extension.ts'],
  bundle: true,
  outfile: 'dist/extension.js',
  external: ['vscode'],
  platform: 'node',
  target: 'node18',
  format: 'cjs',
  sourcemap: !production,
  minify: production,
};

// Standalone webview bundles still referenced by legacy panels. Settings/chat/
// inspector/impact surfaces live in main.ts; shared layout modules are pulled
// into the esbuild chunk via webviewCore/webviewShared (also imported by dashboard).
const webviewEntryPoints = {
  main: path.join('src/webview', 'main.ts'),
  dashboard: path.join('src/webview', 'dashboard.ts'),
};

/** esbuild ESM chunks emit `var` for exported bindings; Sonar expects `const`. */
function rewriteImmutableVarExports(mediaDir) {
  if (!fs.existsSync(mediaDir)) {
    return;
  }
  const immutableBindings = [
    'vscode',
    'VSCODE_WEBVIEW_ORIGIN_PREFIX',
    'DEFAULT_SETTINGS',
    'SETTINGS_FORM_FIELD_KEYS',
    'MainSurface',
    'DashboardPanel',
    'IMPACT_KIND_EXPLAINERS',
    'DEFAULT_CALLS_EDGE_LABEL',
    'MISSING_SYMBOL_PATTERN',
  ];
  for (const fileName of fs.readdirSync(mediaDir)) {
    if (!fileName.endsWith('.js')) {
      continue;
    }
    const filePath = path.join(mediaDir, fileName);
    let text = fs.readFileSync(filePath, 'utf8');
    let changed = false;
    for (const binding of immutableBindings) {
      const pattern = new RegExp(String.raw`\bvar ${binding}\b`, 'g');
      const next = text.replace(pattern, `const ${binding}`);
      if (next !== text) {
        text = next;
        changed = true;
      }
    }
    if (changed) {
      fs.writeFileSync(filePath, text);
    }
  }
}

function isStaticClassFieldValue(value) {
  const trimmed = value.trim();
  if (/^("([^"\\]|\\.)*"|'([^'\\]|\\.)*')$/.test(trimmed)) {
    return true;
  }
  if (/^(true|false|null)$/.test(trimmed)) {
    return true;
  }
  if (/^-?\d+$/.test(trimmed)) {
    return true;
  }
  if (trimmed === '[]') {
    return true;
  }
  return /^(\/\* @__PURE__ \*\/ )?new Map\(\)$/.test(trimmed);
}

/** esbuild lowers TS class fields into constructor assignments; hoist static literals for Sonar. */
function rewriteMainSurfaceClassFields(mediaDir) {
  const mainPath = path.join(mediaDir, 'main.js');
  if (!fs.existsSync(mainPath)) {
    return;
  }

  let text = fs.readFileSync(mainPath, 'utf8');
  const marker = 'const MainSurface = class {';
  const markerIdx = text.indexOf(marker);
  if (markerIdx === -1) {
    return;
  }

  const ctorNeedle = 'constructor() {';
  const ctorIdx = text.indexOf(ctorNeedle, markerIdx);
  if (ctorIdx === -1) {
    return;
  }

  const openBraceIdx = text.indexOf('{', ctorIdx);
  const closeBraceIdx = findMatchingBrace(text, openBraceIdx);
  if (closeBraceIdx === -1) {
    return;
  }

  const bodyStart = openBraceIdx + 1;
  const ctorBody = text.slice(bodyStart, closeBraceIdx);
  const fieldLines = [];
  const keptLines = [];

  for (const line of ctorBody.split('\n')) {
    const match = line.match(/^ {4}this\.(\w+) = (.+);$/);
    if (match && isStaticClassFieldValue(match[2])) {
      fieldLines.push(`  ${match[1]} = ${match[2]};`);
    } else {
      keptLines.push(line);
    }
  }

  if (fieldLines.length === 0) {
    return;
  }

  const insertPoint = markerIdx + marker.length;
  text =
    text.slice(0, insertPoint) +
    '\n' +
    fieldLines.join('\n') +
    text.slice(insertPoint, bodyStart) +
    keptLines.join('\n') +
    text.slice(closeBraceIdx);

  fs.writeFileSync(mainPath, text);
}

function findMatchingBrace(text, openBraceIdx) {
  let depth = 0;
  for (let i = openBraceIdx; i < text.length; i += 1) {
    const ch = text[i];
    if (ch === '{') {
      depth += 1;
    } else if (ch === '}') {
      depth -= 1;
      if (depth === 0) {
        return i;
      }
    }
  }
  return -1;
}

function cleanupOrphanChunks(mediaDir) {
  const entryFiles = ['main.js', 'dashboard.js'];
  const referenced = new Set();

  for (const entry of entryFiles) {
    const entryPath = path.join(mediaDir, entry);
    if (!fs.existsSync(entryPath)) {
      continue;
    }
    const text = fs.readFileSync(entryPath, 'utf8');
    for (const match of text.matchAll(/from\s+["'](\.\/chunk-[^"']+)["']/g)) {
      referenced.add(match[1]);
    }
  }

  for (const fileName of fs.readdirSync(mediaDir)) {
    if (!fileName.startsWith('chunk-') || !fileName.endsWith('.js')) {
      continue;
    }
    const importPath = `./${fileName}`;
    if (referenced.has(importPath)) {
      continue;
    }
    fs.unlinkSync(path.join(mediaDir, fileName));
    const mapPath = path.join(mediaDir, `${fileName}.map`);
    if (fs.existsSync(mapPath)) {
      fs.unlinkSync(mapPath);
    }
  }
}

function webviewPostProcessPlugin() {
  return {
    name: 'webview-post-process',
    setup(build) {
      build.onEnd(() => {
        const mediaDir = path.join(__dirname, 'media');
        rewriteImmutableVarExports(mediaDir);
        rewriteMainSurfaceClassFields(mediaDir);
        cleanupOrphanChunks(mediaDir);
      });
    },
  };
}

/** Webview bundles (Browser) — ESM + splitting shares runtime helpers across entries. */
const webviewOptions = Object.values(webviewEntryPoints).every(f => fs.existsSync(f))
  ? {
      entryPoints: webviewEntryPoints,
      bundle: true,
      splitting: true,
      outdir: 'media',
      platform: 'browser',
      target: 'es2022',
      format: 'esm',
      sourcemap: !production,
      minify: production,
      external: [],
      plugins: [webviewPostProcessPlugin()],
    }
  : null;

async function build() {
  try {
    await esbuild.build(hostOptions);
    console.log('✓ Host bundle built');

    if (webviewOptions) {
      await esbuild.build(webviewOptions);
      console.log('✓ Webview bundles built');
    }
  } catch (e) {
    console.error(e);
    process.exit(1);
  }
}

async function watchMode() {
  const hostCtx = await esbuild.context(hostOptions);
  console.log('✓ Watching host...');
  await hostCtx.watch();

  if (webviewOptions) {
    const webviewCtx = await esbuild.context(webviewOptions);
    console.log('✓ Watching webviews...');
    await webviewCtx.watch();
  }
}

if (watch) {
  watchMode().catch(() => process.exit(1));
} else {
  build();
}
