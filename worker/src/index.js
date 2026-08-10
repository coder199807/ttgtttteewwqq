// HLS-aware proxy for Vavoo IPTV links.
// - GET /play/<id>  -> POSTs mediahubmx-resolve.json, follows the returned signed HLS URL,
//                      rewrites URLs inside the manifest so the client never talks to Vavoo
//                      or the rotating CDN directly.
// - GET /p?u=<url>  -> proxies an HLS-adjacent asset (m3u8/ts/key/aac/m4s/mp4/vtt/...).
//                      Extension whitelisted to prevent open-proxy abuse.
// - GET /health     -> "vavoo-iptv proxy: OK"

const UA =
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36";

// Chrome fingerprint headers Vavoo's Cloudflare in front of api.vavoo.to sometimes checks.
const CHROME_FP_HEADERS = {
  "sec-ch-ua":
    '"Not=A?Brand";v="99", "Google Chrome";v="151", "Chromium";v="151"',
  "sec-ch-ua-mobile": "?0",
  "sec-ch-ua-platform": '"macOS"',
  "sec-fetch-dest": "empty",
  "sec-fetch-mode": "cors",
  "sec-fetch-site": "same-origin",
};

const UPSTREAM_HEADERS = {
  "user-agent": UA,
  accept: "*/*",
  "accept-language": "en-US,en;q=0.9,tr;q=0.8",
  origin: "https://vavoo.to",
  referer: "https://vavoo.to/",
};

const RESOLVE_URL = "https://vavoo.to/mediahubmx-resolve.json";
const RESOLVE_HEADERS = {
  "content-type": "application/json; charset=utf-8",
  "user-agent": UA,
  accept: "*/*",
  "accept-language": "en-US,en;q=0.9,tr;q=0.8",
  origin: "https://vavoo.to",
  referer: "https://vavoo.to/live",
  ...CHROME_FP_HEADERS,
};

// Headers we must NOT forward from upstream to the client.
const HOP_BY_HOP = new Set([
  "connection",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "te",
  "trailers",
  "transfer-encoding",
  "upgrade",
  "content-encoding", // Workers already decoded gzip/br
  "content-length",
]);

// Only these path extensions can be fetched via /p?u=... — locks the endpoint to HLS traffic.
const ALLOWED_EXTENSIONS = new Set([
  ".m3u8",
  ".ts",
  ".aac",
  ".mp3",
  ".m4s",
  ".mp4",
  ".m4a",
  ".key",
  ".vtt",
  ".webvtt",
  ".jpg",
  ".png",
]);

const REQUEST_TIMEOUT_MS = 15000;
const RETRY_ATTEMPTS = 3;

// -- helpers ---------------------------------------------------------------

function corsHeaders() {
  return {
    "access-control-allow-origin": "*",
    "access-control-allow-methods": "GET, HEAD, OPTIONS",
    "access-control-allow-headers": "*",
    "access-control-expose-headers": "*",
  };
}

function pathExtension(urlString) {
  try {
    const p = new URL(urlString).pathname.toLowerCase();
    const dot = p.lastIndexOf(".");
    return dot === -1 ? "" : p.slice(dot);
  } catch {
    return "";
  }
}

function isM3U8(contentType, urlPath) {
  const ct = (contentType || "").toLowerCase();
  return (
    ct.includes("mpegurl") ||
    ct.includes("m3u8") ||
    urlPath.toLowerCase().endsWith(".m3u8")
  );
}

function proxifyUrl(absoluteUrl, workerOrigin) {
  return `${workerOrigin}/p?u=${encodeURIComponent(absoluteUrl)}`;
}

function rewriteHLS(text, baseUrl, workerOrigin) {
  const out = [];
  const lines = text.split(/\r?\n/);
  for (const raw of lines) {
    if (!raw) {
      out.push(raw);
      continue;
    }
    if (raw.startsWith("#")) {
      // Rewrite URI="..." on EXT-X-KEY, EXT-X-MAP, EXT-X-MEDIA, EXT-X-SESSION-KEY, ...
      const rewritten = raw.replace(/URI="([^"]+)"/g, (_m, u) => {
        try {
          const abs = new URL(u, baseUrl).toString();
          return `URI="${proxifyUrl(abs, workerOrigin)}"`;
        } catch {
          return _m;
        }
      });
      out.push(rewritten);
    } else {
      // Bare URL line: a variant playlist or a media segment
      try {
        const abs = new URL(raw.trim(), baseUrl).toString();
        out.push(proxifyUrl(abs, workerOrigin));
      } catch {
        out.push(raw);
      }
    }
  }
  return out.join("\n");
}

async function fetchWithTimeoutRetry(url, init, attempts = RETRY_ATTEMPTS) {
  let lastErr;
  for (let i = 1; i <= attempts; i++) {
    try {
      const signal = AbortSignal.timeout(REQUEST_TIMEOUT_MS);
      const res = await fetch(url, { ...init, signal });
      // Retry only on 5xx; 4xx propagates (bad id, gone, etc.)
      if (res.status >= 500 && i < attempts) {
        throw new Error(`upstream ${res.status}`);
      }
      return res;
    } catch (err) {
      lastErr = err;
      if (i >= attempts) break;
      await new Promise((r) => setTimeout(r, 250 * i));
    }
  }
  throw lastErr;
}

async function resolveStream(id) {
  const body = JSON.stringify({
    language: "de",
    region: "DE",
    catalogId: "iptv",
    id,
    url: `https://vavoo.to/vavoo-iptv/play/${id}`,
  });
  const res = await fetchWithTimeoutRetry(RESOLVE_URL, {
    method: "POST",
    headers: RESOLVE_HEADERS,
    body,
  });
  if (!res.ok) return null;
  let data;
  try {
    data = await res.json();
  } catch {
    return null;
  }
  const arr = Array.isArray(data)
    ? data
    : Array.isArray(data?.items)
      ? data.items
      : null;
  const first = arr && arr[0];
  return first && typeof first.url === "string" ? first.url : null;
}

// Forward client's Range / conditional headers so segment seeks and byte-range playlists work.
function buildProxyHeaders(request) {
  const headers = { ...UPSTREAM_HEADERS };
  const forwardable = ["range", "if-none-match", "if-modified-since"];
  for (const name of forwardable) {
    const v = request.headers.get(name);
    if (v) headers[name] = v;
  }
  return headers;
}

async function proxy(request, targetUrl, workerOrigin) {
  const upstream = await fetchWithTimeoutRetry(targetUrl, {
    method: request.method === "HEAD" ? "HEAD" : "GET",
    headers: buildProxyHeaders(request),
    redirect: "follow",
  });

  const finalUrl = upstream.url || targetUrl;
  const ct = upstream.headers.get("content-type") || "";
  const finalPath = (() => {
    try {
      return new URL(finalUrl).pathname;
    } catch {
      return "";
    }
  })();

  const respHeaders = new Headers();
  for (const [k, v] of upstream.headers.entries()) {
    if (!HOP_BY_HOP.has(k.toLowerCase())) respHeaders.set(k, v);
  }
  for (const [k, v] of Object.entries(corsHeaders())) respHeaders.set(k, v);

  if (isM3U8(ct, finalPath)) {
    const text = await upstream.text();
    const rewritten = rewriteHLS(text, finalUrl, workerOrigin);
    respHeaders.set("content-type", "application/vnd.apple.mpegurl");
    respHeaders.set("cache-control", "no-store");
    return new Response(rewritten, {
      status: upstream.status,
      headers: respHeaders,
    });
  }

  return new Response(upstream.body, {
    status: upstream.status,
    headers: respHeaders,
  });
}

async function handle(request) {
  const url = new URL(request.url);
  const workerOrigin = `${url.protocol}//${url.host}`;

  if (request.method === "OPTIONS") {
    return new Response(null, { status: 204, headers: corsHeaders() });
  }
  if (request.method !== "GET" && request.method !== "HEAD") {
    return new Response("method not allowed", {
      status: 405,
      headers: corsHeaders(),
    });
  }

  if (url.pathname === "/" || url.pathname === "/health") {
    return new Response("vavoo-iptv proxy: OK", {
      status: 200,
      headers: { "content-type": "text/plain", ...corsHeaders() },
    });
  }

  if (url.pathname.startsWith("/play/")) {
    const id = url.pathname.slice("/play/".length);
    if (!/^[a-f0-9]{8,64}$/i.test(id)) {
      return new Response("bad id", { status: 400, headers: corsHeaders() });
    }
    const resolved = await resolveStream(id);
    if (!resolved) {
      return new Response("resolve failed", {
        status: 502,
        headers: corsHeaders(),
      });
    }
    return proxy(request, resolved, workerOrigin);
  }

  if (url.pathname === "/p") {
    const raw = url.searchParams.get("u");
    if (!raw) {
      return new Response("missing u", { status: 400, headers: corsHeaders() });
    }
    let target;
    try {
      target = new URL(raw);
    } catch {
      return new Response("bad u", { status: 400, headers: corsHeaders() });
    }
    if (target.protocol !== "https:" && target.protocol !== "http:") {
      return new Response("bad scheme", {
        status: 400,
        headers: corsHeaders(),
      });
    }
    // Lock the endpoint to HLS-adjacent assets.
    const ext = pathExtension(target.toString());
    if (!ALLOWED_EXTENSIONS.has(ext)) {
      return new Response("disallowed asset", {
        status: 403,
        headers: corsHeaders(),
      });
    }
    return proxy(request, target.toString(), workerOrigin);
  }

  return new Response("not found", { status: 404, headers: corsHeaders() });
}

export default {
  async fetch(request) {
    try {
      return await handle(request);
    } catch (err) {
      const msg = err && err.message ? err.message : "unknown";
      const status = err && err.name === "TimeoutError" ? 504 : 502;
      return new Response(`proxy error: ${msg}`, {
        status,
        headers: corsHeaders(),
      });
    }
  },
};
