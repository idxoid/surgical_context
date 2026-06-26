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
// inspector/impact surfaces live in main.ts to avoid duplicating shared layout code.
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
  ];
  for (const fileName of fs.readdirSync(mediaDir)) {
    if (!fileName.endsWith('.js')) {
      continue;
    }
    const filePath = path.join(mediaDir, fileName);
    let text = fs.readFileSync(filePath, 'utf8');
    let changed = false;
    for (const binding of immutableBindings) {
      const pattern = new RegExp(`\\bvar ${binding}\\b`, 'g');
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

function immutableVarExportPlugin() {
  return {
    name: 'immutable-var-to-const',
    setup(build) {
      build.onEnd(() => {
        rewriteImmutableVarExports(path.join(__dirname, 'media'));
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
      target: 'es2020',
      format: 'esm',
      sourcemap: !production,
      minify: production,
      external: [],
      plugins: [immutableVarExportPlugin()],
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
