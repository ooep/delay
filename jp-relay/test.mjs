// Unit tests for worker.mjs auth/IPC-free logic.
// Run: node jp-relay/test.mjs (Node >= 18; no deps).
import assert from 'node:assert';
import worker from './worker.mjs';

const ISS = 'https://token.actions.githubusercontent.com';
const JWKS_URL = ISS + '/.well-known/jwks';
const AUD = 'suki-jp-relay';
const AONAMI = 'https://trainlocation-aonamiline.jp';
const HAPI = 'https://trainlocation.hapi-line.co.jp';
const TG = 'https://api.tokyoglance.com';

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
  if (u.startsWith(AONAMI) || u.startsWith(HAPI) || u.startsWith(TG)) {
    originHits++;
    lastOriginReq = { url: u, headers: init.headers || {}, method: init.method };
    if (u.includes('redirect-test')) {
      const r = Response.json({ ok: 1 });
      Object.defineProperty(r, 'url', { value: 'https://evil.example/x.json' });
      return r;
    }
    return Response.json({ UP: [{ dt: '2026/10/30 13:00:00' }], TS: [] });
  }
  throw new Error('unexpected fetch ' + u);
};

const KEY = 'test-key';
const ENV = { RELAY_KEY: KEY };
const req = (path, token, { key = KEY, method = 'GET' } = {}) => new Request('https://relay.example' + path, {
  method,
  headers: {
    ...(key ? { 'x-suki-relay-key': key } : {}),
    ...(token ? { authorization: 'Bearer ' + token } : {}),
  },
});
const call = (path, token, opts) => worker.fetch(req(path, token, opts), ENV);

const headerNames = (h) => Object.keys(h || {}).map(s => s.toLowerCase());
const hasGateHeader = (h) => headerNames(h).includes('x-suki-relay-key');

let pass = 0;
const t = async (name, fn) => { await fn(); pass++; console.log('ok  ' + name); };

// ---- gate key: anything but the exact header gets a static 404, zero egress

await t('no gate key -> static 404, no JWKS, no origin', async () => {
  const j0 = jwksHits, o0 = originHits;
  const r = await call('/jp/aonami/data/traffic_info.json?1', await jwt(baseClaims()), { key: '' });
  assert.equal(r.status, 404);
  assert.equal(await r.text(), 'Not Found');
  assert.equal(r.headers.get('cache-control'), 'no-store');
  assert.equal(jwksHits, j0);
  assert.equal(originHits, o0);
});

await t('wrong gate key -> static 404, no JWKS, no origin', async () => {
  const j0 = jwksHits, o0 = originHits;
  const r = await call('/jp/tg/v2/transit/objects/train', await jwt(baseClaims()), { key: KEY + 'x' });
  assert.equal(r.status, 404);
  assert.equal(jwksHits, j0);
  assert.equal(originHits, o0);
});

await t('gate 404 looks identical for random paths (no route oracle)', async () => {
  const a = await call('/jp/aonami/data/traffic_info.json', null, { key: '' });
  const b = await call('/.env', null, { key: '' });
  const c = await call('/jp/tg/v2/transit/objects/train', null, { key: '' });
  for (const r of [a, b, c]) {
    assert.equal(r.status, 404);
    assert.equal(await r.clone().text(), 'Not Found');
  }
});

await t('RELAY_KEY unset -> fail closed 404 even with a key header', async () => {
  const r = await worker.fetch(req('/jp/aonami/data/traffic_info.json', await jwt(baseClaims())), {});
  assert.equal(r.status, 404);
});

await t('gate ok, no token -> 401 (no JWKS hit)', async () => {
  const before = jwksHits;
  const r = await call('/jp/aonami/data/traffic_info.json?1');
  assert.equal(r.status, 401);
  assert.equal(jwksHits, before);
});

await t('gate ok, garbage token -> 401, no origin hit', async () => {
  const o0 = originHits;
  const r = await call('/jp/aonami/data/traffic_info.json?1', 'a.b.c');
  assert.equal(r.status, 401);
  assert.equal(originHits, o0);
});

await t('gate header is never forwarded upstream', async () => {
  const r = await call('/jp/aonami/data/traffic_info.json?1', await jwt(baseClaims()));
  assert.equal(r.status, 200);
  assert.equal(hasGateHeader(lastOriginReq.headers), false);
  assert.equal(headerNames(lastOriginReq.headers).includes('authorization'), false);
});

// ---- OIDC token claims

await t('unknown path -> 404', async () => {
  const r = await call('/jp/unknown/data/x.json', await jwt(baseClaims()));
  assert.equal(r.status, 404);
});

await t('valid token -> 200 proxied', async () => {
  const tok = await jwt(baseClaims());
  const r = await call('/jp/aonami/data/traffic_info.json?123', tok);
  assert.equal(r.status, 200);
  const body = await r.json();
  assert.ok(Array.isArray(body.TS));
  assert.equal(r.headers.get('cache-control'), 'no-store');
  assert.ok(lastOriginReq.url.endsWith('/data/traffic_info.json?123'));
  assert.ok(String(lastOriginReq.headers['User-Agent']).startsWith('suki-jp-relay'));
});

await t('valid token but /config path ok', async () => {
  const r = await call('/jp/hapi/config/station.json', await jwt(baseClaims()));
  assert.equal(r.status, 200);
});

await t('wrong audience -> 401', async () => {
  const c = baseClaims(); c.aud = 'something-else';
  assert.equal((await call('/jp/aonami/data/traffic_info.json', await jwt(c))).status, 401);
});

await t('wrong repository -> 401', async () => {
  const c = baseClaims(); c.repository = 'attacker/fork';
  assert.equal((await call('/jp/aonami/data/traffic_info.json', await jwt(c))).status, 401);
});

await t('wrong ref (PR head) -> 401', async () => {
  const c = baseClaims(); c.ref = 'refs/pull/7/merge';
  assert.equal((await call('/jp/aonami/data/traffic_info.json', await jwt(c))).status, 401);
});

await t('wrong issuer -> 401', async () => {
  const c = baseClaims(); c.iss = 'https://example.com';
  assert.equal((await call('/jp/aonami/data/traffic_info.json', await jwt(c))).status, 401);
});

await t('expired -> 401', async () => {
  const c = baseClaims(); c.exp = now() - 600;
  assert.equal((await call('/jp/aonami/data/traffic_info.json', await jwt(c))).status, 401);
});

await t('nbf in future -> 401', async () => {
  const c = baseClaims(); c.nbf = now() + 600;
  assert.equal((await call('/jp/aonami/data/traffic_info.json', await jwt(c))).status, 401);
});

await t('bad signature (foreign key) -> 401', async () => {
  const tok = await jwt(baseClaims(), { key: otherKp.privateKey });
  assert.equal((await call('/jp/aonami/data/traffic_info.json', tok)).status, 401);
});

await t('HS256 alg -> 401', async () => {
  const tok = await jwt(baseClaims(), { header: { alg: 'HS256' } });
  assert.equal((await call('/jp/aonami/data/traffic_info.json', tok)).status, 401);
});

await t('unknown kid -> 401', async () => {
  const tok = await jwt(baseClaims(), { kid: 'nope-kid' });
  assert.equal((await call('/jp/aonami/data/traffic_info.json', tok)).status, 401);
});

// ---- routes

await t('path outside /data|/config -> 403', async () => {
  const r = await call('/jp/aonami/', await jwt(baseClaims()));
  assert.equal(r.status, 403);
  const r2 = await call('/jp/aonami/admin/secret', await jwt(baseClaims()));
  assert.equal(r2.status, 403);
});

await t('tg objects -> 200 proxied to tokyoglance', async () => {
  const tok = await jwt(baseClaims());
  const r = await call('/jp/tg/v2/transit/objects/train?169', tok);
  assert.equal(r.status, 200);
  assert.equal(typeof (await r.json()), 'object');
  assert.ok(lastOriginReq.url.startsWith('https://api.tokyoglance.com/v2/transit/objects/train?169'));
});

await t('tg delays v1 -> 200', async () => {
  const r = await call('/jp/tg/v1/transit/delays', await jwt(baseClaims()));
  assert.equal(r.status, 200);
});

await t('path allowlist is per source (cross-source -> 403)', async () => {
  assert.equal((await call('/jp/tg/data/traffic_info.json', await jwt(baseClaims()))).status, 403);
  assert.equal((await call('/jp/aonami/v2/transit/objects/train', await jwt(baseClaims()))).status, 403);
});

await t('POST -> 405', async () => {
  const tok = await jwt(baseClaims());
  const r = await call('/jp/aonami/data/x.json', tok, { method: 'POST' });
  assert.equal(r.status, 405);
});

await t('off-origin redirect -> 502', async () => {
  const tok = await jwt(baseClaims());
  const r = await call('/jp/aonami/data/redirect-test.json', tok);
  assert.equal(r.status, 502);
});

await t('second valid call does not refetch jwks (cache)', async () => {
  const before = jwksHits;
  const r = await call('/jp/aonami/data/traffic_info.json', await jwt(baseClaims()));
  assert.equal(r.status, 200);
  assert.equal(jwksHits, before);
});

console.log(`\nALL ${pass} TESTS PASSED (jwksHits=${jwksHits}, originHits=${originHits})`);
