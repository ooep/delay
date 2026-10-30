// suki-jp-relay — authenticated egress relay for Japan-only JSON APIs.
//
// あおなみ線 / ハピラインNAVI accept TCP only from Japanese IPs, and
// api.tokyoglance.com 403s the GitHub runner, which sits in the US.
// placement.region pins this fetch handler near Tokyo so the upstream
// request leaves from Japan.
//
// Two locks: a shared gate key (Worker secret RELAY_KEY + GitHub Actions
// secret relay_key) is checked first and answered with a static 404 when
// missing, so scans cost neither CPU nor subrequests; then a GitHub Actions
// OIDC RS256 JWT (aud "suki-jp-relay", repository ooep/delay, ref main) is
// verified against GitHub's JWKS. Only that workflow can reach the origin.
const ORIGINS = {
  aonami: { origin: 'https://trainlocation-aonamiline.jp', path: /^\/(data|config)\// },
  hapi: { origin: 'https://trainlocation.hapi-line.co.jp', path: /^\/(data|config)\// },
  tg: { origin: 'https://api.tokyoglance.com', path: /^\/v[12]\/transit\// },
};
const AUD = 'suki-jp-relay';
const REPO = 'ooep/delay';
const REF = 'refs/heads/main';
const ISS = 'https://token.actions.githubusercontent.com';
const JWKS_URL = ISS + '/.well-known/jwks';
const SKEW = 60;

// Gate parameter, checked before anything else: requests without the exact
// header get a fixed response and never reach verification or the upstream.
// Kept in a header (not ?key=) so it is neither logged in URLs nor forwarded.
const GATE_HEADER = 'x-suki-relay-key';

let jwksCache = { keys: null, at: 0 };
const keyCache = new Map();

function staticNotFound() {
  return new Response('Not Found', {
    status: 404,
    headers: { 'content-type': 'text/plain; charset=utf-8', 'cache-control': 'no-store' },
  });
}

async function sha256(text) {
  return new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text)));
}

async function gatePasses(request, env) {
  const expected = String(env.RELAY_KEY || '');
  if (!expected) return false;  // fail closed: an unset secret disables the relay
  const got = request.headers.get(GATE_HEADER) || '';
  if (!got) return false;
  const a = await sha256(got);
  const b = await sha256(expected);
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a[i] ^ b[i];
  return diff === 0;
}

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
    if (!(await gatePasses(request, env))) return staticNotFound();
    const url = new URL(request.url);
    const m = url.pathname.match(/^\/jp\/([a-z0-9-]+)(\/.*)$/);
    const src = m ? ORIGINS[m[1]] : null;
    if (!src) return new Response('not found', { status: 404 });

    const auth = request.headers.get('authorization') || '';
    const claims = await verifyToken(auth.replace(/^Bearer\s+/i, ''));
    if (!claims) {
      return new Response('unauthorized', { status: 401, headers: { 'cache-control': 'no-store' } });
    }
    if (request.method !== 'GET' && request.method !== 'HEAD') {
      return new Response('method not allowed', { status: 405 });
    }
    // closed proxy: per-source allowlist of the JSON endpoints the baker reads
    if (!src.path.test(m[2])) {
      return new Response('path not allowed', { status: 403 });
    }
    const target = src.origin + m[2] + url.search;
    try {
      const upstream = await fetch(target, {
        method: request.method,
        headers: { 'User-Agent': 'suki-jp-relay/1.0', accept: 'application/json,*/*' },
        redirect: 'follow',
        signal: AbortSignal.timeout(20000),
      });
      let finalHost = '';
      try { finalHost = new URL(upstream.url || '').host; } catch {}
      if (finalHost && finalHost !== new URL(src.origin).host) {
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
