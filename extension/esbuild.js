const esbuild = require('esbuild');
const fs = require('fs');
const path = require('path');

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

// Find all webview entry points
const webviewDir = 'src/webview';
let webviewEntryPoints = [];
if (fs.existsSync(webviewDir)) {
  webviewEntryPoints = fs
    .readdirSync(webviewDir)
    .filter(f => f.endsWith('.ts') && !f.startsWith('shared'))
    .map(f => path.join(webviewDir, f));
}

/** Webview bundles (Browser) */
const webviewOptions = webviewEntryPoints.length > 0 ? {
  entryPoints: webviewEntryPoints,
  bundle: true,
  outdir: 'media',
  platform: 'browser',
  target: 'es2020',
  format: 'iife',
  sourcemap: !production,
  minify: production,
  external: [],
} : null;

async function build() {
  try {
    // Build host
    await esbuild.build(hostOptions);
    console.log('✓ Host bundle built');

    // Build webviews (if any exist)
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
