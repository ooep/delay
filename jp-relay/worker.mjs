// suki-jp-relay — authenticated egress relay for two Japan-only JSON APIs.
//
// あおなみ線 / ハピラインNAVI accept TCP only from Japanese IPs, while the
// GitHub runner sits in the US. placement.region pins this fetch handler
// near Tokyo so the upstream request leaves from Japan.
//
// Auth is GitHub Actions OIDC: the bake workflow mints a short-lived RS256
// JWT for audience "suki-jp-relay"; this Worker verifies it against GitHub's
// JWKS and requires repository=ooep/delay, ref=refs/heads/main. Nothing
// secret is stored here, and only that workflow can call the origin.
const ORIGINS = {
  aonami: 'https://trainlocation-aonamiline.jp',
  hapi: 'https://trainlocation.hapi-line.co.jp',
};
const AUD = 'suki-jp-relay';
const REPO = 'ooep/delay';
const REF = 'refs/heads/main';
const ISS = 'https://token.actions.githubusercontent.com';
const JWKS_URL = ISS + '/.well-known/jwks';
const SKEW = 60;

let jwksCache = { keys: null, at: 0 };
const keyCache = new Map();

async function jwksKeys() {
  const now = Date.now();
  if (jwksCache.keys && now - jwksCache.at < 10 * 60 * 1000) return jwksCache.keys;
  const r = await fetch(JWKS_URL, { headers: { accept: 'application/json' }, signal: AbortSignal.timeout(10000) });
  if (!r.ok) throw new Error('jwks ' + r.status);
  const d = await r.json();
  jwksCache = { keys: d.keys || [], at: now };
  return jwksCache.keys;
}

function b64url(s) {
  s = String(s).replace(/-/g, '+').replace(/_/g, '/');
  s += '='.repeat((4 - (s.length % 4)) % 4);
  const bin = atob(s);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

async function verifyToken(token) {
  const parts = String(token || '').split('.');
  if (parts.length !== 3) return null;
  let head, claims;
  try {
    head = JSON.parse(new TextDecoder().decode(b64url(parts[0])));
    claims = JSON.parse(new TextDecoder().decode(b64url(parts[1])));
  } catch {
    return null;
  }
  if (head.alg !== 'RS256' || !head.kid) return null;
  try {
    let key = keyCache.get(head.kid);
    if (!key) {
      const jwk = (await jwksKeys()).find(k => k.kid === head.kid);
      if (!jwk) return null;
      key = await crypto.subtle.importKey(
        'jwk',
        { kty: jwk.kty, n: jwk.n, e: jwk.e, alg: 'RS256', use: 'sig', ext: true },
        { name: 'RSASSA-PKCS1-v1_5', hash: 'SHA-256' },
        false,
        ['verify'],
      );
      keyCache.set(head.kid, key);
    }
    const good = await crypto.subtle.verify(
      'RSASSA-PKCS1-v1_5', key, b64url(parts[2]),
      new TextEncoder().encode(parts[0] + '.' + parts[1]),
    );
    if (!good) return null;
  } catch {
    return null;
  }
  const now = Math.floor(Date.now() / 1000);
  if (claims.iss !== ISS) return null;
  if (claims.repository !== REPO) return null;
  if (claims.ref !== REF) return null;
  const aud = Array.isArray(claims.aud) ? claims.aud : [claims.aud];
  if (!aud.includes(AUD)) return null;
  if (!claims.exp || claims.exp < now - SKEW) return null;
  if (claims.nbf && claims.nbf > now + SKEW) return null;
  return claims;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const m = url.pathname.match(/^\/jp\/([a-z0-9-]+)(\/.*)$/);
    const origin = m ? ORIGINS[m[1]] : null;
    if (!origin) return new Response('not found', { status: 404 });

    const auth = request.headers.get('authorization') || '';
    const claims = await verifyToken(auth.replace(/^Bearer\s+/i, ''));
    if (!claims) {
      return new Response('unauthorized', { status: 401, headers: { 'cache-control': 'no-store' } });
    }
    if (request.method !== 'GET' && request.method !== 'HEAD') {
      return new Response('method not allowed', { status: 405 });
    }
    // closed proxy: only the JSON endpoints the baker actually reads
    if (!/^\/(data|config)\//.test(m[2])) {
      return new Response('path not allowed', { status: 403 });
    }
    const target = origin + m[2] + url.search;
    try {
      const upstream = await fetch(target, {
        method: request.method,
        headers: { 'User-Agent': 'suki-jp-relay/1.0', accept: 'application/json,*/*' },
        redirect: 'follow',
        signal: AbortSignal.timeout(20000),
      });
      let finalHost = '';
      try { finalHost = new URL(upstream.url || '').host; } catch {}
      if (finalHost && finalHost !== new URL(origin).host) {
        return new Response('upstream redirected off-origin', { status: 502, headers: { 'cache-control': 'no-store' } });
      }
      const headers = new Headers();
      headers.set('content-type', upstream.headers.get('content-type') || 'application/json; charset=utf-8');
      headers.set('access-control-allow-origin', '*');
      headers.set('cache-control', 'no-store');
      headers.set('x-jp-relay-upstream', String(upstream.status));
      headers.set('x-jp-relay-ingress-colo', (request.cf && request.cf.colo) || 'unknown');
      return new Response(upstream.body, { status: upstream.status, headers });
    } catch (err) {
      return new Response(`upstream fetch failed: ${err}`, { status: 502, headers: { 'cache-control': 'no-store' } });
    }
  },
};
