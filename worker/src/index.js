// ============================================================
// VAVOO.TO IPTV PROXY — /play/<id> resolver + HLS rewriter
// Optimized for Televizo and similar IPTV players
// ============================================================

const CACHE_TTL = 600;
const CHANNELS_CACHE_KEY = 'vavoo_channels';
const LANGUAGE = 'tr';
const REGION = 'TR';
const GROUP = 'Turkey';

const BASE_SITES = ['https://vavoo.to', 'https://kool.to'];
const PING_URL = 'https://www.vavoo.tv/api/app/ping';
const RESOLVE_PATH = '/mediahubmx-resolve.json';
const CATALOG_PATH = '/mediahubmx-catalog.json';

const ALLOWED_EXTENSIONS = new Set([
  '.m3u8', '.ts', '.aac', '.mp3', '.m4s', '.mp4', '.m4a', '.key', '.vtt', '.webvtt'
]);

// Standard media player User-Agent — not blocked by CDNs
const STREAM_USER_AGENT =
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36';

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

function getStreamHeaders() {
  return {
    'User-Agent': STREAM_USER_AGENT,
    'Accept': '*/*',
    'Accept-Language': LANGUAGE,
    'Origin': 'https://vavoo.to',
    'Referer': 'https://vavoo.to/',
    'Connection': 'keep-alive',
  };
}

function getPlaylistHeaders() {
  return {
    'User-Agent': STREAM_USER_AGENT,
    'Accept': 'application/vnd.apple.mpegurl, application/x-mpegURL, */*',
    'Accept-Language': LANGUAGE,
    'Origin': 'https://vavoo.to',
    'Referer': 'https://vavoo.to/',
    'Connection': 'keep-alive',
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
  // Already proxied — skip
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

      // Segment URI line — rewrite
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
          filter: { group: GROUP },
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
      console.log(`[vavoo] Catalog load failed: ${error.message}`);
      break;
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
    // PLAY — resolves /play/<vavooId> to actual stream
    // ============================================================
    if (path.startsWith('/play/')) {
      const channelId = path.split('/')[2]?.split('|')[0];
      if (!channelId) {
        return new Response('Channel ID missing', { status: 400, headers: corsHeaders() });
      }

      try {
        const channel = await findChannel(channelId);
        const streamUrl = channel
          ? await resolveStream(channel)
          : await resolveDirect(channelId);

        if (!streamUrl) {
          return new Response(`Stream not found: ${channelId}`, { status: 404, headers: corsHeaders() });
        }

        if (channel) {
          console.log(`[vavoo] "${channel.name}" resolved: ${describeUrl(streamUrl)}`);
        }

        return await proxyStream(baseUrl, streamUrl, request);

      } catch (error) {
        console.log(`[vavoo] Play error: ${error.message}`);
        return new Response(`Stream error: ${error.message}`, { status: 500, headers: corsHeaders() });
      }
    }

    // ============================================================
    // HLS PROXY — proxies .m3u8 and .ts segments
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

        // Build upstream headers — forward Range for .ts segments
        const upstreamHeaders = { ...getStreamHeaders() };
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
            headers: { ...getPlaylistHeaders(), ...(rangeHeader ? { Range: rangeHeader } : {}) },
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
// STREAM PROXY — fetches upstream and rewrites HLS playlists
// ============================================================

async function proxyStream(baseUrl, streamUrl, clientRequest) {
  // Forward Range header from client for seeking support
  const upstreamHeaders = { ...getStreamHeaders() };
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

  // Binary segment — return with proper headers
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
