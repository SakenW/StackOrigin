// Execute the pinned official CLI. The guard only restricts dispatch and keeps
// the original envelope; command construction/auth/API parsing belong to cf.
import fs from 'node:fs';
import {pathToFileURL} from 'node:url';

const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const expected = new URL(input.url);
if (expected.origin !== 'https://api.cloudflare.com' || expected.username || expected.password)
  throw new Error('invalid fixed origin');
const originalFetch = globalThis.fetch;
let dispatched = false;
let envelope;
function query(url) {
  return JSON.stringify([...url.searchParams].sort((a, b) =>
    a[0].localeCompare(b[0]) || a[1].localeCompare(b[1])));
}
globalThis.fetch = async (resource, options = {}) => {
  const actual = new URL(typeof resource === 'string' || resource instanceof URL
    ? resource : resource.url);
  const method = options.method ?? resource.method ?? 'GET';
  const headers = new Headers(options.headers ?? resource.headers);
  if (dispatched || actual.origin !== expected.origin || actual.pathname !== expected.pathname ||
      query(actual) !== query(expected) || method !== input.method ||
      headers.get('authorization') !== `Bearer ${input.token}`)
    throw new Error('dispatch outside reviewed scope');
  if (input.body === null ? options.body != null :
      JSON.stringify(JSON.parse(options.body)) !== JSON.stringify(input.body))
    throw new Error('body outside reviewed scope');
  dispatched = true; // Never retry even if cf changes its retry policy.
  const response = await originalFetch(resource, {...options, redirect: 'error',
    signal: AbortSignal.timeout(15000)});
  const reader = response.body?.getReader();
  const chunks = [];
  let length = 0;
  if (reader) {
    try {
      for (;;) {
        const {done, value} = await reader.read();
        if (done) break;
        length += value.byteLength;
        if (length > 2097152) throw new Error('response too large');
        chunks.push(Buffer.from(value));
      }
    } finally { await reader.cancel(); }
  }
  const bytes = Buffer.concat(chunks);
  envelope = JSON.parse(bytes.toString('utf8'));
  return new Response(bytes, {status: response.status, statusText: response.statusText,
    headers: response.headers});
};
process.env.CLOUDFLARE_API_TOKEN = input.token;
process.env.CLOUDFLARE_ACCOUNT_ID = input.account_id;
process.env.CF_SEND_TELEMETRY = 'false';
process.env.WRANGLER_SEND_METRICS = 'false';
process.argv = [process.execPath, input.entry, ...input.argv];
console.log = () => {}; // cf strips result_info in display output; keep wire envelope.
await import(pathToFileURL(input.entry).href);
if (!dispatched || !envelope) throw new Error('no API result');
process.stdout.write(JSON.stringify(envelope));
