// ============================================================
// VAVOO.TO IPTV PROXY â€” /play/<id> resolver + HLS rewriter
// Optimized for Televizo and similar IPTV players
// ============================================================

const CACHE_TTL = 600;
const CHANNELS_CACHE_KEY = 'vavoo_channels';
const LANGUAGE = 'tr';
const REGION = 'TR';
const GROUPS = ['Turkey', 'Germany'];

const BASE_SITES = ['https://vavoo.to', 'https://kool.to'];
const PING_URL = 'https://www.vavoo.tv/api/app/ping';
const RESOLVE_PATH = '/mediahubmx-resolve.json';
const CATALOG_PATH = '/mediahubmx-catalog.json';

const ALLOWED_EXTENSIONS = new Set([
  '.m3u8', '.ts', '.aac', '.mp3', '.m4s', '.mp4', '.m4a', '.key', '.vtt', '.webvtt'
]);

// Standard media player User-Agent â€” not blocked by CDNs
const STREAM_USER_AGENT =
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36';

const FALLBACK_INDEX_URL =
  'https://raw.githubusercontent.com/coder199807/ttgtttteewwqq/main/fallbacks.json';
const FALLBACK_CACHE_KEY = 'fallbacks';
const FALLBACK_CACHE_TTL = 86400;
const VAVOO_HOSTS = new Set(['vavoo.to', 'kool.to', 'www.vavoo.tv', 'vavoo.tv']);

function hostOf(urlString) {
  try {
    return new URL(urlString).hostname.toLowerCase();
  } catch {
    return '';
  }
}

// Origin/Referer must match the upstream or foreign CDNs reject the request.
// Sending the vavoo origin to a fallback CDN was silently killing every fallback.
function refererFor(urlString) {
  const host = hostOf(urlString);
  if (!host) return {};
  if (VAVOO_HOSTS.has(host)) {
    return { 'Origin': 'https://vavoo.to', 'Referer': 'https://vavoo.to/' };
  }
  return { 'Referer': `https://${host}/` };
}

function pathExtension(urlString) {
  try {
    const p = new URL(urlString).pathname.toLowerCase();
    const dot = p.lastIndexOf('.');
    return dot === -1 ? '' : p.slice(dot);
  } catch {
    return '';
  }
}

// ============================================================
// HEADER BUILDERS
// ============================================================

function getStreamHeaders(upstreamUrl) {
  return {
    'User-Agent': STREAM_USER_AGENT,
    'Accept': '*/*',
    'Accept-Language': LANGUAGE,
    'Connection': 'keep-alive',
    ...refererFor(upstreamUrl),
  };
}

function getPlaylistHeaders(upstreamUrl) {
  return {
    'User-Agent': STREAM_USER_AGENT,
    'Accept': 'application/vnd.apple.mpegurl, application/x-mpegURL, */*',
    'Accept-Language': LANGUAGE,
    'Connection': 'keep-alive',
    ...refererFor(upstreamUrl),
  };
}

// ============================================================
// HLS DETECTION & REWRITING
// ============================================================

function isM3u8Url(url) {
  try {
    return new URL(url).pathname.toLowerCase().endsWith('.m3u8');
  } catch {
    return false;
  }
}

function isM3u8Response(url, contentType) {
  const ct = String(contentType || '').toLowerCase();
  return ct.includes('mpegurl') ||
    ct.includes('mpegURL') ||
    ct.includes('application/vnd.apple') ||
    isM3u8Url(url);
}

function describeUrl(url) {
  try {
    const u = new URL(url);
    return `${u.hostname}${u.pathname}`;
  } catch {
    return url;
  }
}

function getProxiedUrl(baseUrl, upstreamUrl) {
  return `${baseUrl}/hls-proxy?url=${encodeURIComponent(upstreamUrl)}`;
}

function shouldRewriteUri(uri) {
  const trimmed = String(uri || '').trim();
  if (!trimmed) return false;
  return !/^(data|urn|skd):/i.test(trimmed);
}

function rewritePlaylistUri(baseUrl, playlistBase, uri) {
  if (!shouldRewriteUri(uri)) return uri;
  // Already proxied â€” skip
  if (uri.includes('/hls-proxy?')) return uri;
  try {
    const absolute = new URL(uri, playlistBase).toString();
    return getProxiedUrl(baseUrl, absolute);
  } catch {
    return uri;
  }
}

function rewritePlaylist(baseUrl, upstreamUrl, playlist) {
  return String(playlist)
    .split(/\r?\n/)
    .map(line => {
      const trimmed = line.trim();
      if (!trimmed) return line;

      if (trimmed.startsWith('#')) {
        // Rewrite URI="..." in any HLS tag (KEY, MAP, MEDIA, STREAM-INF, etc.)
        return line.replace(/URI="([^"]+)"/g, (match, uri) => {
          return `URI="${rewritePlaylistUri(baseUrl, upstreamUrl, uri)}"`;
        });
      }

      // Segment URI line â€” rewrite
      return rewritePlaylistUri(baseUrl, upstreamUrl, trimmed);
    })
    .join('\n');
}

// ============================================================
// FETCH WITH RETRY
// ============================================================

async function fetchJson(url, options = {}) {
  const maxRetries = options.retries ?? 1;
  let lastErr;

  for (let attempt = 0; attempt <= maxRetries; attempt++) {
    try {
      const response = await fetch(url, {
        method: options.method || 'GET',
        headers: options.headers || {},
        body: options.body ? JSON.stringify(options.body) : undefined,
        signal: AbortSignal.timeout(options.timeout || 30000),
      });

      if (!response.ok) {
        const error = new Error(`HTTP ${response.status} for ${url}`);
        error.status = response.status;
        throw error;
      }

      return response.json();
    } catch (err) {
      lastErr = err;
      if (attempt < maxRetries) {
        await new Promise(r => setTimeout(r, 200 * (attempt + 1)));
      }
    }
  }
  throw lastErr;
}

async function fetchWithRetry(url, options = {}) {
  const maxRetries = options.retries ?? 1;
  let lastErr;

  for (let attempt = 0; attempt <= maxRetries; attempt++) {
    try {
      const response = await fetch(url, {
        method: options.method || 'GET',
        headers: options.headers || {},
        signal: AbortSignal.timeout(options.timeout || 15000),
      });
      return response;
    } catch (err) {
      lastErr = err;
      if (attempt < maxRetries) {
        await new Promise(r => setTimeout(r, 150 * (attempt + 1)));
      }
    }
  }
  throw lastErr;
}

// ============================================================
// VAVOO API
// ============================================================

function getCatalogHeaders(signature) {
  return {
    'Content-Type': 'application/json; charset=utf-8',
    'mediahubmx-signature': signature,
    'User-Agent': 'MediaHubMX/2',
    'Accept': '*/*',
    'Accept-Language': LANGUAGE,
    'Accept-Encoding': 'gzip, deflate',
  };
}

async function getAddonSignature() {
  const cached = await VAVOO_KV?.get('signature');
  if (cached) return cached;

  const payload = {
    reason: 'app-focus',
    locale: LANGUAGE,
    theme: 'dark',
    metadata: {
      device: { type: 'desktop', uniqueId: `cf-${Date.now()}` },
      os: { name: 'linux', version: 'Linux', abis: ['x64'], host: 'cloudflare' },
      app: { platform: 'electron' }
    },
    appFocusTime: 0,
    playerActive: false,
    playDuration: 0,
    devMode: false,
    hasAddon: true,
    castConnected: false,
    package: 'tv.vavoo.app',
    version: '3.1.8',
    process: 'app',
    firstAppStart: Date.now(),
    lastAppStart: Date.now(),
    ipLocation: null,
    adblockEnabled: true,
    proxy: { supported: ['ss'], engine: 'Mu', enabled: false, autoServer: true },
    iap: { supported: false }
  };

  try {
    const body = await fetchJson(PING_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: payload
    });

    const signature = body?.addonSig;
    if (signature) {
      await VAVOO_KV?.put('signature', signature, { expirationTtl: CACHE_TTL });
      return signature;
    }
  } catch (error) {
    console.log(`[vavoo] addonSig failed: ${error.message}`);
  }

  throw new Error('Addon signature could not be obtained');
}

async function loadCatalog(baseUrl, signature) {
  const catalogUrl = `${baseUrl.replace(/\/$/, '')}${CATALOG_PATH}`;
  const headers = getCatalogHeaders(signature);
  const channels = [];

  for (const group of GROUPS) {
    let cursor = null;

    while (true) {
      try {
        const body = await fetchJson(catalogUrl, {
          method: 'POST',
          headers,
          body: {
            language: LANGUAGE,
            region: REGION,
            catalogId: 'iptv',
            id: 'iptv',
            adult: false,
            search: '',
            sort: '',
            filter: { group },
            cursor,
            clientVersion: '3.0.2'
          }
        });

        const items = Array.isArray(body?.items) ? body.items : [];
        for (const item of items) {
          const vavooId = item?.ids?.id || item?.id;
          if (item?.type === 'iptv' && item?.url && vavooId) {
            channels.push({
              url: item.url,
              name: item.name || 'Unknown',
              logo: item.logo || '',
              vavooId
            });
          }
        }

        if (!body?.nextCursor) break;
        cursor = body.nextCursor;
      } catch (error) {
        console.log(`[vavoo] Catalog load failed (${group}): ${error.message}`);
        break;
      }
    }
  }

  return channels;
}

async function getChannels() {
  const cached = await VAVOO_KV?.get(CHANNELS_CACHE_KEY, 'json');
  if (cached && Array.isArray(cached)) return cached;

  const signature = await getAddonSignature();

  for (const baseUrl of BASE_SITES) {
    try {
      const channels = await loadCatalog(baseUrl, signature);
      if (channels.length > 0) {
        await VAVOO_KV?.put(CHANNELS_CACHE_KEY, JSON.stringify(channels), { expirationTtl: CACHE_TTL });
        return channels;
      }
    } catch (error) {
      console.log(`[vavoo] Catalog failed (${baseUrl}): ${error.message}`);
    }
  }

  throw new Error('Channel list could not be obtained');
}

async function findChannel(id) {
  const channels = await getChannels();
  return channels.find(c => String(c.vavooId) === String(id));
}

async function resolveStream(channel) {
  const signature = await getAddonSignature();

  for (const baseUrl of BASE_SITES) {
    const resolveUrl = `${baseUrl.replace(/\/$/, '')}${RESOLVE_PATH}`;

    try {
      const body = await fetchJson(resolveUrl, {
        method: 'POST',
        headers: getCatalogHeaders(signature),
        body: {
          language: LANGUAGE,
          region: REGION,
          url: channel.url,
          clientVersion: '3.0.2'
        },
        retries: 1,
      });

      if (Array.isArray(body) && body[0]?.url) return body[0].url;
      if (body?.url) return body.url;
      if (body?.streamUrl) return body.streamUrl;
    } catch (error) {
      console.log(`[vavoo] Resolve failed (${baseUrl}): ${error.message}`);
    }
  }

  throw new Error(`Stream could not be resolved: ${channel.name}`);
}

async function resolveDirect(id) {
  const signature = await getAddonSignature();
  const directUrl = `https://vavoo.to/watch?live=${id}`;

  for (const baseUrl of BASE_SITES) {
    const resolveUrl = `${baseUrl.replace(/\/$/, '')}${RESOLVE_PATH}`;
    try {
      const body = await fetchJson(resolveUrl, {
        method: 'POST',
        headers: getCatalogHeaders(signature),
        body: {
          language: LANGUAGE,
          region: REGION,
          url: directUrl,
          clientVersion: '3.0.2'
        },
        retries: 1,
      });

      if (Array.isArray(body) && body[0]?.url) return body[0].url;
      if (body?.url) return body.url;
      if (body?.streamUrl) return body.streamUrl;
    } catch (error) {
      console.log(`[vavoo] Direct resolve failed (${baseUrl}): ${error.message}`);
    }
  }

  return null;
}

// ============================================================
// FALLBACK INDEX  (shipped by scripts/build.js as fallbacks.json)
// ============================================================

// Kept in sync with normalizeChannelName() in scripts/build.js â€” both sides
// must fold diacritics the same way or every lookup misses.
function normalizeChannelName(name) {
  return String(name || '')
    .toLowerCase()
    .replace(/^\s*(?:4k\s*tr|4k|tr|de|at|ch)\s*:\s*/, '')
    .replace(/\s*\.(?:b|c|s)\b/g, '')
    .replace(/\[[^\]]*\]/g, '')
    .replace(/\([^)]*\)/g, '')
    .replace(/\b(hd|fhd|uhd|4k|sd|hevc|h265|h264|raw)\b/g, '')
    .replace(/\u00fc/g, 'u')
    .replace(/\u011f/g, 'g')
    .replace(/\u015f/g, 's')
    .replace(/\u0131/g, 'i')
    .replace(/\u00f6/g, 'o')
    .replace(/\u00e7/g, 'c')
    .replace(/\s+/g, ' ')
    .trim();
}

// Guards against ATV picking up ATV Alanya.
function isSimilarName(a, b) {
  if (a === b) return true;
  if (!a || !b) return false;
  if (!a.includes(b) && !b.includes(a)) return false;
  const longer = Math.max(a.length, b.length);
  const shorter = Math.min(a.length, b.length);
  return shorter / longer >= 0.7;
}

function lookupFallbackUrls(index, channelName) {
  if (!index || typeof index !== 'object') return [];
  const key = normalizeChannelName(channelName);
  if (!key) return [];

  if (Array.isArray(index[key])) return index[key];

  const urls = [];
  for (const [candidate, list] of Object.entries(index)) {
    if (Array.isArray(list) && isSimilarName(key, candidate)) urls.push(...list);
  }
  return urls.slice(0, 3);
}

async function getFallbackIndex() {
  const cached = await VAVOO_KV?.get(FALLBACK_CACHE_KEY, 'json');
  if (cached && typeof cached === 'object' && Object.keys(cached).length) return cached;

  const response = await fetchWithRetry(FALLBACK_INDEX_URL, { timeout: 20000, retries: 1 });
  if (!response.ok) throw new Error(`fallback index HTTP ${response.status}`);

  const index = await response.json();
  if (!index || typeof index !== 'object') throw new Error('fallback index malformed');

  await VAVOO_KV?.put(FALLBACK_CACHE_KEY, JSON.stringify(index), {
    expirationTtl: FALLBACK_CACHE_TTL
  });
  console.log(`[vavoo] fallback index cached: ${Object.keys(index).length} channels`);
  return index;
}

// ============================================================
// CORS HEADERS
// ============================================================

function corsHeaders() {
  return {
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type, Range, User-Agent, Accept, Origin, Referer',
    'Access-Control-Expose-Headers': 'Content-Length, Content-Range, Content-Type, Accept-Ranges',
  };
}

// ============================================================
// WORKER MAIN HANDLER
// ============================================================

export { normalizeChannelName, isSimilarName, lookupFallbackUrls, refererFor };

export default {
  async fetch(request, env) {
    globalThis.VAVOO_KV = env?.VAVOO_KV;

    if (request.method === 'OPTIONS') {
      return new Response(null, { headers: corsHeaders() });
    }

    const url = new URL(request.url);
    const baseUrl = `${url.protocol}//${url.host}`;
    const path = url.pathname;

    // ============================================================
    // PLAY â€” resolves /play/<vavooId> to actual stream
    // ============================================================
    if (path.startsWith('/play/')) {
      const channelId = path.split('/')[2]?.split('|')[0];
      if (!channelId) {
        return new Response('Channel ID missing', { status: 400, headers: corsHeaders() });
      }

      // Candidate order: vavoo resolve -> curated fallback index -> direct watch url.
      // Every candidate is actually fetched, because the upstream can return 403
      // even when the resolve call succeeded â€” that is the case the old code lost.
      const candidates = [];
      let channelName = '';

      try {
        const channel = await findChannel(channelId);
        if (channel) {
          channelName = channel.name || '';
          const resolved = await resolveStream(channel).catch(err => {
            console.log(`[vavoo] resolve failed: ${err.message}`);
            return null;
          });
          if (resolved) candidates.push({ url: resolved, via: 'vavoo' });
        }
      } catch (error) {
        console.log(`[vavoo] catalog lookup failed: ${error.message}`);
      }

      try {
        const index = await getFallbackIndex();
        for (const url of lookupFallbackUrls(index, channelName)) {
          candidates.push({ url, via: 'fallback' });
        }
      } catch (error) {
        console.log(`[vavoo] fallback index unavailable: ${error.message}`);
      }

      if (candidates.length === 0) {
        const direct = await resolveDirect(channelId).catch(() => null);
        if (direct) candidates.push({ url: direct, via: 'direct' });
      }

      let lastStatus = 0;
      for (const candidate of candidates) {
        let response;
        try {
          response = await proxyStream(baseUrl, candidate.url, request);
        } catch (error) {
          console.log(`[vavoo] ${candidate.via} threw: ${error.message}`);
          lastStatus = 502;
          continue;
        }

        if (response.status < 400) {
          if (candidate.via !== 'vavoo') {
            console.log(`[vavoo] "${channelName || channelId}" recovered via ${candidate.via}: ${describeUrl(candidate.url)}`);
          }
          return response;
        }

        lastStatus = response.status;
        console.log(`[vavoo] ${candidate.via} ${describeUrl(candidate.url)} -> ${response.status}, trying next`);
      }

      console.log(`[vavoo] all sources failed for "${channelName || channelId}" (last ${lastStatus})`);
      return new Response(
        `No working source for ${channelName || channelId} (last upstream ${lastStatus || 'none'})`,
        { status: 502, headers: corsHeaders() }
      );
    }

    // ============================================================
    // HLS PROXY â€” proxies .m3u8 and .ts segments
    // ============================================================
    if (path === '/hls-proxy') {
      const upstreamUrl = url.searchParams.get('url');
      if (!upstreamUrl) {
        return new Response('URL parameter missing', { status: 400, headers: corsHeaders() });
      }

      try {
        const parsed = new URL(upstreamUrl);
        if (!['http:', 'https:'].includes(parsed.protocol)) {
          return new Response('Unsupported protocol', { status: 400, headers: corsHeaders() });
        }
        const ext = pathExtension(upstreamUrl);
        if (ext && !ALLOWED_EXTENSIONS.has(ext)) {
          return new Response('Unsupported file type', { status: 403, headers: corsHeaders() });
        }

        // Build upstream headers â€” forward Range for .ts segments
const upstreamHeaders = { ...getStreamHeaders(upstreamUrl) };
        const rangeHeader = request.headers.get('Range') || request.headers.get('range');
        if (rangeHeader && (ext === '.ts' || ext === '.aac' || ext === '.mp4' || ext === '.m4s')) {
          upstreamHeaders['Range'] = rangeHeader;
        }

        let response = await fetchWithRetry(upstreamUrl, {
          headers: upstreamHeaders,
          timeout: 15000,
          retries: 1,
        });

        if (response.status === 403 || response.status === 401) {
          response = await fetchWithRetry(upstreamUrl, {
            headers: { ...getPlaylistHeaders(upstreamUrl), ...(rangeHeader ? { Range: rangeHeader } : {}) },
            timeout: 15000,
            retries: 1,
          });
        }

        console.log(`[vavoo] hls-proxy ${describeUrl(upstreamUrl)} -> ${response.status}`);

        if (!response.ok) {
          return new Response(`Upstream error: ${response.status}`, { status: response.status, headers: corsHeaders() });
        }

        const contentType = response.headers.get('content-type') || '';

        if (isM3u8Response(upstreamUrl, contentType)) {
          const playlist = await response.text();
          const rewritten = rewritePlaylist(baseUrl, upstreamUrl, playlist);
          return new Response(rewritten, {
            headers: {
              'Content-Type': 'application/vnd.apple.mpegurl; charset=utf-8',
              'Cache-Control': 'no-cache, no-store, must-revalidate',
              'Pragma': 'no-cache',
              ...corsHeaders()
            }
          });
        }

        // Segment / binary response
        const respHeaders = {
          'Content-Type': contentType || 'video/mp2t',
          'Content-Length': response.headers.get('content-length') || '',
          'Accept-Ranges': 'bytes',
          'Cache-Control': 'public, max-age=86400',
          ...corsHeaders()
        };
        const cr = response.headers.get('content-range');
        if (cr) respHeaders['Content-Range'] = cr;

        return new Response(response.body, {
          status: response.status,
          headers: respHeaders,
        });

      } catch (error) {
        console.log(`[vavoo] Proxy error: ${error.message}`);
        return new Response(`Proxy error: ${error.message}`, { status: 500, headers: corsHeaders() });
      }
    }

    // ============================================================
    // FALLBACK
    // ============================================================
    return new Response('Usage: /play/<id>', {
      status: 404,
      headers: corsHeaders()
    });
  }
};

// ============================================================
// STREAM PROXY â€” fetches upstream and rewrites HLS playlists
// ============================================================

async function proxyStream(baseUrl, streamUrl, clientRequest) {
  // Forward Range header from client for seeking support
  const upstreamHeaders = { ...getStreamHeaders(streamUrl) };
  const rangeHeader = clientRequest?.headers?.get('Range') || clientRequest?.headers?.get('range');
  if (rangeHeader) {
    upstreamHeaders['Range'] = rangeHeader;
  }

  const response = await fetchWithRetry(streamUrl, {
    headers: upstreamHeaders,
    timeout: 15000,
    retries: 1,
  });

  console.log(`[vavoo] play ${describeUrl(streamUrl)} -> ${response.status} (${response.headers.get('content-type') || 'no ct'})`);

  if (!response.ok) {
    return new Response(`Stream error: ${response.status}`, { status: response.status, headers: corsHeaders() });
  }

  const contentType = response.headers.get('content-type') || '';

  if (isM3u8Response(streamUrl, contentType)) {
    const playlist = await response.text();
    const rewritten = rewritePlaylist(baseUrl, streamUrl, playlist);
    return new Response(rewritten, {
      headers: {
        'Content-Type': 'application/vnd.apple.mpegurl; charset=utf-8',
        'Cache-Control': 'no-cache, no-store, must-revalidate',
        'Pragma': 'no-cache',
        ...corsHeaders()
      }
    });
  }

  // Binary segment â€” return with proper headers
  const respHeaders = {
    'Content-Type': contentType || 'video/mp2t',
    'Content-Length': response.headers.get('content-length') || '',
    'Accept-Ranges': 'bytes',
    'Cache-Control': 'public, max-age=86400',
    ...corsHeaders()
  };
  const cr = response.headers.get('content-range');
  if (cr) respHeaders['Content-Range'] = cr;

  return new Response(response.body, {
    status: response.status,
    headers: respHeaders,
  });
}
