// Prints, as hex, the two plain frames the client sends that the server's bridge reads
// (the request to reach a device on the ID socket, the request to be paired on the
// relay socket), encoded by the client's own code. The bridge's tests use these so
// they check what the real client sends, not a second encoder written to match.
//
//   node scripts/print-frames.mjs
import { build } from 'esbuild';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve, dirname } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const dir = mkdtempSync(join(tmpdir(), 'rd-frames-'));
const entry = join(dir, 'entry.ts');
writeFileSync(
  entry,
  `import { buildPunchHoleRequest } from ${JSON.stringify(join(root, 'src/core/signaling.ts'))};
import { buildRequestRelay } from ${JSON.stringify(join(root, 'src/core/relay.ts'))};
const hex = (b: Uint8Array) => Array.from(b, (x) => x.toString(16).padStart(2, '0')).join('');
console.log(JSON.stringify({
  punch_hole_request: hex(buildPunchHoleRequest({ peerId: '123456789', licenceKey: 'PLACEHOLDER-KEY', version: '1.4.0' })),
  request_relay: hex(buildRequestRelay({ licenceKey: 'PLACEHOLDER-KEY', peerId: '123456789', uuid: '00000000-0000-4000-8000-000000000000' })),
}, null, 2));
`,
);
const out = join(dir, 'entry.mjs');
await build({ entryPoints: [entry], bundle: true, format: 'esm', platform: 'node', outfile: out, logLevel: 'error' });
await import(pathToFileURL(out).href);
