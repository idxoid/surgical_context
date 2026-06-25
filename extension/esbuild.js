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

// Standalone webview bundles still referenced by legacy panels. Settings/chat/
// inspector/impact surfaces live in main.ts to avoid duplicating shared layout code.
const webviewEntryPoints = {
  main: path.join('src/webview', 'main.ts'),
  dashboard: path.join('src/webview', 'dashboard.ts'),
};

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
    }
  : null;

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
