// Unit tests for worker.mjs auth/IPC-free logic.
// Run: node jp-relay/test.mjs (Node >= 18; no deps).
import assert from 'node:assert';
import worker from './worker.mjs';

const ISS = 'https://token.actions.githubusercontent.com';
const JWKS_URL = ISS + '/.well-known/jwks';
const AUD = 'suki-jp-relay';
const ORIGIN = 'https://trainlocation-aonamiline.jp';

const kp = await crypto.subtle.generateKey(
  { name: 'RSASSA-PKCS1-v1_5', modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]), hash: 'SHA-256' },
  true, ['sign', 'verify'],
);
const pub = await crypto.subtle.exportKey('jwk', kp.publicKey);
const otherKp = await crypto.subtle.generateKey(
  { name: 'RSASSA-PKCS1-v1_5', modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]), hash: 'SHA-256' },
  true, ['sign', 'verify'],
);

const b64u = (buf) => Buffer.from(buf).toString('base64url');
const now = () => Math.floor(Date.now() / 1000);

async function jwt(payload, { kid = 'test-kid', key = kp.privateKey, header = {} } = {}) {
  const h = { alg: 'RS256', typ: 'JWT', kid, ...header };
  const p = b64u(JSON.stringify(h)) + '.' + b64u(JSON.stringify(payload));
  const sig = await crypto.subtle.sign('RSASSA-PKCS1-v1_5', key, new TextEncoder().encode(p));
  return p + '.' + b64u(sig);
}

const baseClaims = () => ({
  iss: ISS, aud: AUD, repository: 'ooep/delay', ref: 'refs/heads/main',
  exp: now() + 300, nbf: now() - 10, iat: now() - 10,
});

let jwksHits = 0;
let originHits = 0;
let lastOriginReq = null;

globalThis.fetch = async (url, init = {}) => {
  const u = String(url);
  if (u === JWKS_URL) {
    jwksHits++;
    return Response.json({ keys: [{ kty: 'RSA', kid: 'test-kid', use: 'sig', alg: 'RS256', n: pub.n, e: pub.e }] });
  }
  if (u.startsWith(ORIGIN) || u.startsWith('https://trainlocation.hapi-line.co.jp')) {
    originHits++;
    lastOriginReq = { url: u, headers: init.headers, method: init.method };
    if (u.includes('redirect-test')) {
      const r = Response.json({ ok: 1 });
      Object.defineProperty(r, 'url', { value: 'https://evil.example/x.json' });
      return r;
    }
    return Response.json({ UP: [{ dt: '2026/10/30 13:00:00' }], TS: [] });
  }
  throw new Error('unexpected fetch ' + u);
};

const req = (path, token) => new Request('https://relay.example' + path, {
  headers: token ? { authorization: 'Bearer ' + token } : {},
});

let pass = 0;
const t = async (name, fn) => { await fn(); pass++; console.log('ok  ' + name); };

await t('no token -> 401 (no JWKS hit)', async () => {
  const before = jwksHits;
  const r = await worker.fetch(req('/jp/aonami/data/traffic_info.json?1'));
  assert.equal(r.status, 401);
  assert.equal(jwksHits, before);
});

await t('garbage token -> 401', async () => {
  const r = await worker.fetch(req('/jp/aonami/data/traffic_info.json?1', 'a.b.c'));
  assert.equal(r.status, 401);
});

await t('unknown path -> 404', async () => {
  const r = await worker.fetch(req('/jp/unknown/data/x.json', await jwt(baseClaims())));
  assert.equal(r.status, 404);
});

await t('valid token -> 200 proxied', async () => {
  const tok = await jwt(baseClaims());
  const r = await worker.fetch(req('/jp/aonami/data/traffic_info.json?123', tok));
  assert.equal(r.status, 200);
  const body = await r.json();
  assert.ok(Array.isArray(body.TS));
  assert.equal(r.headers.get('cache-control'), 'no-store');
  assert.ok(lastOriginReq.url.endsWith('/data/traffic_info.json?123'));
  assert.ok(String(lastOriginReq.headers['User-Agent']).startsWith('suki-jp-relay'));
});

await t('valid token but /config path ok', async () => {
  const r = await worker.fetch(req('/jp/hapi/config/station.json', await jwt(baseClaims())));
  assert.equal(r.status, 200);
});

await t('wrong audience -> 401', async () => {
  const c = baseClaims(); c.aud = 'something-else';
  assert.equal((await worker.fetch(req('/jp/aonami/data/traffic_info.json', await jwt(c)))).status, 401);
});

await t('wrong repository -> 401', async () => {
  const c = baseClaims(); c.repository = 'attacker/fork';
  assert.equal((await worker.fetch(req('/jp/aonami/data/traffic_info.json', await jwt(c)))).status, 401);
});

await t('wrong ref (PR head) -> 401', async () => {
  const c = baseClaims(); c.ref = 'refs/pull/7/merge';
  assert.equal((await worker.fetch(req('/jp/aonami/data/traffic_info.json', await jwt(c)))).status, 401);
});

await t('wrong issuer -> 401', async () => {
  const c = baseClaims(); c.iss = 'https://example.com';
  assert.equal((await worker.fetch(req('/jp/aonami/data/traffic_info.json', await jwt(c)))).status, 401);
});

await t('expired -> 401', async () => {
  const c = baseClaims(); c.exp = now() - 600;
  assert.equal((await worker.fetch(req('/jp/aonami/data/traffic_info.json', await jwt(c)))).status, 401);
});

await t('nbf in future -> 401', async () => {
  const c = baseClaims(); c.nbf = now() + 600;
  assert.equal((await worker.fetch(req('/jp/aonami/data/traffic_info.json', await jwt(c)))).status, 401);
});

await t('bad signature (foreign key) -> 401', async () => {
  const tok = await jwt(baseClaims(), { key: otherKp.privateKey });
  assert.equal((await worker.fetch(req('/jp/aonami/data/traffic_info.json', tok))).status, 401);
});

await t('HS256 alg -> 401', async () => {
  const tok = await jwt(baseClaims(), { header: { alg: 'HS256' } });
  assert.equal((await worker.fetch(req('/jp/aonami/data/traffic_info.json', tok))).status, 401);
});

await t('unknown kid -> 401', async () => {
  const tok = await jwt(baseClaims(), { kid: 'nope-kid' });
  assert.equal((await worker.fetch(req('/jp/aonami/data/traffic_info.json', tok))).status, 401);
});

await t('path outside /data|/config -> 403', async () => {
  const r = await worker.fetch(req('/jp/aonami/', await jwt(baseClaims())));
  assert.equal(r.status, 403);
  const r2 = await worker.fetch(req('/jp/aonami/admin/secret', await jwt(baseClaims())));
  assert.equal(r2.status, 403);
});

await t('POST -> 405', async () => {
  const tok = await jwt(baseClaims());
  const r = await worker.fetch(new Request('https://relay.example/jp/aonami/data/x.json', {
    method: 'POST', headers: { authorization: 'Bearer ' + tok },
  }));
  assert.equal(r.status, 405);
});

await t('off-origin redirect -> 502', async () => {
  const tok = await jwt(baseClaims());
  const r = await worker.fetch(req('/jp/aonami/data/redirect-test.json', tok));
  assert.equal(r.status, 502);
});

await t('second valid call does not refetch jwks (cache)', async () => {
  const before = jwksHits;
  const r = await worker.fetch(req('/jp/aonami/data/traffic_info.json', await jwt(baseClaims())));
  assert.equal(r.status, 200);
  assert.equal(jwksHits, before);
});

console.log(`\nALL ${pass} TESTS PASSED (jwksHits=${jwksHits}, originHits=${originHits})`);
