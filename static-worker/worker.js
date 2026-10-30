// jr-delay runs as a static-assets Worker; /jp/* is the only path that runs
// this script. あおなみ線 and ハピラインNAVI only accept TCP from Japanese
// IPs while GitHub runners sit in the US, so the baker reads them through
// this proxy. Smart Placement moves the fetch handler to a Japanese colo so
// the subrequest leaves from Japan; everything else is served from assets
// (run_worker_first in wrangler.toml keeps this script off those paths).
const ORIGINS = {
  aonami: 'https://trainlocation-aonamiline.jp',
  hapi: 'https://trainlocation.hapi-line.co.jp',
};

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const m = url.pathname.match(/^\/jp\/([a-z0-9-]+)(\/.*)$/);
    const origin = m ? ORIGINS[m[1]] : null;
    if (!origin) return env.ASSETS.fetch(request);
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
        headers: { 'User-Agent': 'jr-delay-bake/1.0', 'Accept': 'application/json,*/*' },
        redirect: 'follow',
        signal: AbortSignal.timeout(20000),
      });
      const headers = new Headers();
      headers.set('content-type', upstream.headers.get('content-type') || 'application/json; charset=utf-8');
      headers.set('access-control-allow-origin', '*');
      headers.set('cache-control', 'no-store');
      headers.set('x-jp-proxy-upstream', String(upstream.status));
      // identifies the edge that executed this handler; with Smart Placement
      // a JP colo (NRT/KIX/ITM) means the upstream request left from Japan
      headers.set('x-jp-proxy-colo', (request.cf && request.cf.colo) || 'unknown');
      return new Response(upstream.body, { status: upstream.status, headers });
    } catch (err) {
      return new Response(`upstream fetch failed: ${err}`, {
        status: 502,
        headers: { 'cache-control': 'no-store' },
      });
    }
  },
};
