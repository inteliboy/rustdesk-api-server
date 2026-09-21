// Builds the web client into the Python package's static folder. The output is
// committed (like the precompiled Tailwind CSS), so running or installing the
// server never needs Node.js.
//
//   npm run build            one-shot production bundle
//   npm run build -- --watch rebuild on change (dev only)
//
// Two independent entry points:
//   src/ui/app.ts             -> app.js             (main thread)
//   src/worker/session.worker -> session.worker.js  (worker)
//
// WASM (libsodium-wrappers ships base64-inlined already; zstddec ships a .wasm)
// is inlined as base64 data URIs via the `binary` loader so the output stays a
// self-contained static asset tree with no extra fetches.

import { build, context } from 'esbuild';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const __dirname = dirname(fileURLToPath(import.meta.url));
const outdir = resolve(__dirname, '../src/rustdesk_api/web/static/webclient');
const watch = process.argv.includes('--watch');
const dev = watch || process.argv.includes('--dev');

/** @type {import('esbuild').BuildOptions} */
const shared = {
  bundle: true,
  format: 'esm',
  target: ['chrome110', 'edge110'],
  platform: 'browser',
  // Source maps only for development builds; the committed bundle stays small.
  sourcemap: dev,
  minify: !dev,
  legalComments: 'none',
  // Says what this is and where the source is (AGPL-3.0: users of the running service can get it).
  banner: {
    js: '/* RustDesk API Server web client, AGPL-3.0-only. Source: https://github.com/inteliboy/rustdesk-api-server/tree/main/webclient (see NOTICE there) */',
    css: '/* RustDesk API Server web client, AGPL-3.0-only. Source: https://github.com/inteliboy/rustdesk-api-server/tree/main/webclient */',
  },
  logLevel: 'info',
  loader: {
    '.wasm': 'binary',
    '.css': 'css',
  },
  alias: {
    // libsodium-wrappers' ESM build imports './libsodium.mjs' from the sibling
    // `libsodium` package — unresolvable. Bundle the CJS build instead (same
    // alias as vitest.config.ts).
    'libsodium-wrappers': require.resolve('libsodium-wrappers'),
    // jMuxer imports node's `stream` for its optional createStream() wrapper,
    // which this client never calls. Stub it rather than bundling a polyfill
    // for an unreachable path.
    stream: resolve(__dirname, 'src/shims/node-stream.ts'),
  },
  define: {
    'process.env.NODE_ENV': dev ? '"development"' : '"production"',
  },
};

const entryPoints = {
  app: resolve(__dirname, 'src/ui/app.ts'),
  'session.worker': resolve(__dirname, 'src/worker/session.worker.ts'),
};

if (watch) {
  const ctx = await context({ ...shared, entryPoints, outdir });
  await ctx.watch();
  console.log(`[esbuild] watching -> ${outdir}`);
} else {
  await build({ ...shared, entryPoints, outdir });
  console.log(`[esbuild] built -> ${outdir}`);
}
