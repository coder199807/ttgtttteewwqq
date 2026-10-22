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
// The index is rebuilt by the GitHub Action; an hour is enough to pick up a
// new custom link without serving a stale one for a whole day.
const FALLBACK_CACHE_TTL = 3600;
const VAVOO_HOSTS = new Set(['vavoo.to', 'kool.to', 'www.vavoo.tv', 'vavoo.tv']);

function hostOf(urlString) {
  try {
    return new URL(urlString).hostname.toLowerCase();
  } catch {
    return '';
  }
}

// Origin/Referer must match where the URL came from, not what the host is called.
// A resolved vavoo stream lives on a third-party CDN that REQUIRES the vavoo
// Origin; only curated fallbacks must not receive it.
function refererFor(urlString, via) {
  const host = hostOf(urlString);
  if (!host) return {};
  // The matchora cdn rejects every request that carries its own host as
  // referer and answers 403 "watch on matchora.to".
  if (via === 'matchora') return { 'Referer': 'https://matchora.to/' };
  // Curated fallbacks must not receive the vavoo Origin.
  if (via === 'fallback') return { 'Referer': `https://${host}/` };
  // Everything the vavoo resolver handed us keeps the vavoo Origin, whatever
  // CDN it lives on.
  return { 'Origin': 'https://vavoo.to', 'Referer': 'https://vavoo.to/' };
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

function getStreamHeaders(upstreamUrl, via) {
  return {
    'User-Agent': STREAM_USER_AGENT,
    'Accept': '*/*',
    'Accept-Language': LANGUAGE,
    'Connection': 'keep-alive',
    ...refererFor(upstreamUrl, via),
  };
}

function getPlaylistHeaders(upstreamUrl, via) {
  return {
    'User-Agent': STREAM_USER_AGENT,
    'Accept': 'application/vnd.apple.mpegurl, application/x-mpegURL, */*',
    'Accept-Language': LANGUAGE,
    'Connection': 'keep-alive',
    ...refererFor(upstreamUrl, via),
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

// ffmpeg (and every other strict HLS demuxer) refuses a segment whose URL
// path has no allowed file extension: "URL ... is not in
// allowed_segment_extensions" -> the whole stream fails to open. The proxy
// path therefore has to carry the upstream extension.
function proxyExtension(upstreamUrl, isVariant) {
  const ext = pathExtension(upstreamUrl);
  if (/^\.[a-z0-9]{1,5}$/.test(ext)) return ext;
  // No extension to copy: a variant is always a playlist, a segment never is.
  return isVariant ? '.m3u8' : '.ts';
}

// channelId rides along so an expired segment can be re-resolved at request time.
// via rides along so a segment keeps the Origin its own source requires.
function getProxiedUrl(baseUrl, upstreamUrl, channelId, via, isVariant) {
  const id = channelId ? `&id=${encodeURIComponent(channelId)}` : '';
  const src = via && via !== 'vavoo' ? `&v=${encodeURIComponent(via)}` : '';
  return `${baseUrl}/hls-proxy${proxyExtension(upstreamUrl, isVariant)}?url=${encodeURIComponent(upstreamUrl)}${id}${src}`;
}

function shouldRewriteUri(uri) {
  const trimmed = String(uri || '').trim();
  if (!trimmed) return false;
  return !/^(data|urn|skd):/i.test(trimmed);
}

function rewritePlaylistUri(baseUrl, playlistBase, uri, channelId, via, isVariant) {
  if (!shouldRewriteUri(uri)) return uri;
  // Already proxied - skip
  if (uri.includes('/hls-proxy')) return uri;
  try {
    const absolute = new URL(uri, playlistBase).toString();
    return getProxiedUrl(baseUrl, absolute, channelId, via, isVariant);
  } catch {
    return uri;
  }
}

function rewritePlaylist(baseUrl, upstreamUrl, playlist, channelId, via) {
  const lines = String(playlist).split(/\r?\n/);
  // A variant line belongs to the #EXT-X-STREAM-INF above it and is always a
  // playlist, a plain URI line is a segment.
  let variant = false;

  return lines
    .map(line => {
      const trimmed = line.trim();
      if (!trimmed) return line;

      if (trimmed.startsWith('#')) {
        if (trimmed.startsWith('#EXT-X-STREAM-INF')) variant = true;
        // Rewrite URI="..." in any HLS tag (KEY, MAP, MEDIA, STREAM-INF, etc.)
        return line.replace(/URI="([^"]+)"/g, (match, uri) => {
          return `URI="${rewritePlaylistUri(baseUrl, upstreamUrl, uri, channelId, via, false)}"`;
        });
      }

      // Segment or variant URI line — rewrite
      const rewritten = rewritePlaylistUri(baseUrl, upstreamUrl, trimmed, channelId, via, variant);
      variant = false;
      return rewritten;
    })
    .join('\n');
}

// ============================================================
// NESTED PLAYLISTS
// ============================================================

// Some Turkish CDNs put a playlist where a segment belongs. A player cannot
// read a playlist in a segment slot, so the inner playlist has to be handed
// out instead of the outer one.
function firstSegmentUri(upstreamUrl, playlist) {
  // Only a media playlist has segment slots; a master playlist holds variants.
  if (!/#EXTINF/.test(playlist)) return null;
  for (const line of playlist.split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith('#')) continue;
    try {
      return new URL(trimmed, upstreamUrl).toString();
    } catch {
      return null;
    }
  }
  return null;
}

async function probeContentType(url, via) {
  try {
    const response = await fetchWithRetry(url, {
      method: 'HEAD',
      headers: getStreamHeaders(url, via),
      timeout: 6000,
      retries: 0,
    });
    if (response.ok) return String(response.headers.get('content-type') || '').toLowerCase();
  } catch { }
  return '';
}

// All variant uris of a master playlist, resolved against its base url.
function variantUris(upstreamUrl, playlist) {
  const uris = [];
  let variant = false;
  for (const line of playlist.split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    if (trimmed.startsWith('#')) {
      if (trimmed.startsWith('#EXT-X-STREAM-INF')) variant = true;
      continue;
    }
    if (!variant) continue;
    try {
      uris.push(new URL(trimmed, upstreamUrl).toString());
    } catch { }
  }
  return uris;
}

// Some CDNs answer a variant request with another master playlist (the
// artidijital sources do, with a rotating session id) and some put a playlist
// where a segment belongs. Both break the player, so the playlist is unwrapped
// until a media playlist with real segments shows up.
async function unwrapPlaylist(upstreamUrl, playlist, via, depth = 0) {
  if (depth >= 3) return { url: upstreamUrl, text: playlist, playable: false };

  if (/#EXT-X-STREAM-INF/.test(playlist)) {
    for (const variant of variantUris(upstreamUrl, playlist).slice(0, 3)) {
      const response = await fetchWithRetry(variant, {
        headers: getPlaylistHeaders(variant, via),
        timeout: 8000,
        retries: 0,
      }).catch(() => null);

      if (!response || !response.ok) continue;
      const text = await response.text();
      if (!text.trimStart().startsWith('#EXTM3U')) continue;

      const inner = await unwrapPlaylist(variant, text, via, depth + 1);
      if (inner.playable) {
        console.log(`[vavoo] master playlist resolved to media: ${describeUrl(inner.url)}`);
        return inner;
      }
    }
    return { url: upstreamUrl, text: playlist, playable: false };
  }

  const segment = firstSegmentUri(upstreamUrl, playlist);
  if (segment) {
    const contentType = await probeContentType(segment, via);
    if (contentType.includes('mpegurl')) {
      const inner = await fetchWithRetry(segment, {
        headers: getPlaylistHeaders(segment, via),
        timeout: 8000,
        retries: 0,
      }).catch(() => null);

      if (inner && inner.ok) {
        const text = await inner.text();
        if (text.trimStart().startsWith('#EXTM3U')) {
          console.log(`[vavoo] unwrapped nested playlist ${describeUrl(segment)}`);
          const deeper = await unwrapPlaylist(segment, text, via, depth + 1);
          if (deeper.playable) return deeper;
        }
      }
    }
  }

  return { url: upstreamUrl, text: playlist, playable: true };
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
      // The Turkish CDNs answer 502/503/504 out of the blue. Those are cheap to
      // retry (they come back in ms) and a dead channel is the result if we do
      // not try again.
      if (response.status >= 500 && attempt < maxRetries) {
        await response.body?.cancel();
        await new Promise(r => setTimeout(r, 250 * (attempt + 1)));
        continue;
      }

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

// Folds the channel name into a comparable form. The worker normalises both
// sides (its own channel name and every key of the fallback index), so the
// index stays readable even when the build wrote it with its own rules.
function normalizeChannelName(name) {
  return String(name || '')
    .toLowerCase()
    .replace(/^\s*(?:4k\s*tr|4k|tr|de|at|ch)\s*:\s*/, '')
    .replace(/\s*\.(?:b|c|s)\b/g, '')
    .replace(/[\[\]]+/g, ' ').replace(/\[[^\]]*\]/g, '')
    .replace(/\([^)]*\)/g, '')
    // "E X X E N" is one word, not five
    .replace(/\b(?:[a-z0-9]\s+){2,}[a-z0-9]\b/g, (m) => m.replace(/\s+/g, ''))
    .replace(/\b(hd|fhd|uhd|4k|sd|hevc|h265|h264|raw|mahsun)\b/g, '')
    .replace(/\bsports?\b/g, 'spor')
    .replace(/\u00fc/g, 'u')
    .replace(/\u011f/g, 'g')
    .replace(/\u015f/g, 's')
    .replace(/\u0131/g, 'i')
    .replace(/\u00f6/g, 'o')
    .replace(/\u00e7/g, 'c')
    .replace(/[^a-z0-9]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

// The user does not want radio. A radio entry in the index must never be used
// as a fallback for a tv channel, and a radio stream is not playable as HLS.
const RADIO_TOKENS = new Set(['radyo', 'radio']);

function isRadioName(name) {
  return String(name || '').split(' ').some(token => RADIO_TOKENS.has(token));
}

// Ranking of two normalized channel names. 0 means "different channel".
// A trailing number marks a sibling channel ("TRT SPOR 2" is not "TRT SPOR"),
// a trailing word marks a family member ("EXXEN TV SPOR 1" belongs to "EXXEN TV").
// The overlap decides the order, the class only breaks ties: exact > one word
// dropped > same words plus extras.
function similarityScore(a, b) {
  if (!a || !b) return 0;
  if (isRadioName(a) !== isRadioName(b)) return 0;

  const tokensA = new Set(a.split(' ').filter(Boolean));
  const tokensB = new Set(b.split(' ').filter(Boolean));
  if (!tokensA.size || !tokensB.size) return 0;
  const shared = [...tokensA].filter(token => tokensB.has(token)).length;
  const union = new Set([...tokensA, ...tokensB]).size;
  const overlap = shared / union;

  if (a === b) return 1200 + overlap * 1000;

  const squashedA = a.replace(/\s+/g, '');
  const squashedB = b.replace(/\s+/g, '');
  if (squashedA === squashedB) return 1150 + overlap * 1000;

  const longer = squashedA.length >= squashedB.length ? squashedA : squashedB;
  const shorter = longer === squashedA ? squashedB : squashedA;
  if (longer.startsWith(shorter) && !/^\d+$/.test(longer.slice(shorter.length))) {
    if (overlap < 0.4) return 0;
    return 1100 + overlap * 1000;
  }

  // The shorter name must be made of the same words as the longer one, so
  // "EXXEN TV SPOR 1" never lands on "STC TV SPORTS 1".
  const [small, big] = tokensA.size <= tokensB.size ? [tokensA, tokensB] : [tokensB, tokensA];
  if (![...small].every(token => big.has(token))) return 0;
  const extra = [...big].filter(token => !small.has(token));
  // A trailing number marks a sibling channel - "TRT SPOR 2" is not "TRT SPOR".
  // The number 1 is the exception: "ATV 1" is just ATV.
  if (extra.length && extra.every(token => /^\d+$/.test(token)) && !extra.every(token => token === '1')) return 0;
  if (overlap < 0.4) return 0;
  return overlap * 1000;
}

function isSimilarName(a, b) {
  return similarityScore(a, b) > 0;
}

// The index has its own entries for generic words ("spor", "tv"). Those would
// send every sports channel to the same source, so they never match loosely.
const GENERIC_TOKENS = new Set([
  'tv', 'spor', 'sport', 'sports', 'radio', 'radyo', 'haber', 'news', 'live',
  'yayin', 'canli', 'hd', 'fhd', 'uhd', 'sd', '4k', 'hevc', 'muzik', 'music',
  'kanal', 'plus', 'raw', 'tr', 'de', 'at', 'ch', 'ilk', 'ortaokul'
]);

function isDistinctive(name) {
  return name.split(' ').some(token => token && !GENERIC_TOKENS.has(token));
}

// The index is scanned on every /play request and has ~5k keys, so the
// normalized form is memoized for a minute.
let normalizedIndexMemo = { at: 0, entries: [] };
const NORMALIZED_INDEX_TTL = 60000;

function normalizedIndexEntries(index) {
  const now = Date.now();
  if (normalizedIndexMemo.entries.length && now - normalizedIndexMemo.at < NORMALIZED_INDEX_TTL) {
    return normalizedIndexMemo.entries;
  }
  const entries = [];
  for (const [candidate, list] of Object.entries(index)) {
    if (!Array.isArray(list) || !list.length) continue;
    const name = normalizeChannelName(candidate);
    if (!isDistinctive(name)) continue;
    entries.push({ name, list });
  }
  normalizedIndexMemo = { at: now, entries };
  return entries;
}

function lookupFallbackUrls(index, channelName) {
  if (!index || typeof index !== 'object') return [];
  const key = normalizeChannelName(channelName);
  if (!key) return [];

  const entries = normalizedIndexEntries(index);
  const scored = [];
  for (const entry of entries) {
    const score = similarityScore(key, entry.name);
    if (score > 0) scored.push({ score, list: entry.list });
  }
  scored.sort((a, b) => b.score - a.score);

  const urls = [];
  for (const entry of scored) {
    for (const url of entry.list) {
      if (!urls.includes(url)) urls.push(url);
      if (urls.length >= 3) return urls;
    }
  }
  return urls;
}

// The matchora links in the index are player pages, not playlists. Their api
// hands out the real hls url, which is valid for ten minutes.
const MATCHORA_EMBED = /^https?:\/\/matchora\.to\/embed\/channel\/([\w-]+)/i;

async function resolveFallbackUrl(url) {
  const match = MATCHORA_EMBED.exec(url);
  if (!match) return { url, via: 'fallback' };

  const channelId = match[1];
  for (let round = 0; round < 2; round++) {
    try {
      const response = await fetchWithRetry(`https://matchora.to/api/play/${channelId}`, {
        headers: {
          'User-Agent': STREAM_USER_AGENT,
          'Accept': 'application/json',
          'Referer': `https://matchora.to/embed/channel/${channelId}`
        },
        timeout: 8000,
        retries: 0
      });
      if (!response.ok) {
        console.log(`[vavoo] matchora ${channelId} -> HTTP ${response.status}`);
        return null;
      }

      const data = await response.json();
      if (!data || typeof data.url !== 'string' || !/^https?:\/\//.test(data.url)) {
        console.log(`[vavoo] matchora ${channelId} -> no stream url`);
        return null;
      }

      // A stream that is still warming up has no segments yet. Asking again
      // after a moment is cheaper than handing out a dead playlist.
      if (data.ready === false && data.reason === 'warming' && round === 0) {
        await new Promise(resolve => setTimeout(resolve, 2000));
        continue;
      }

      return { url: data.url, via: 'matchora' };
    } catch (error) {
      console.log(`[vavoo] matchora ${channelId} failed: ${error.message}`);
      return null;
    }
  }
  return null;
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

// Single place that turns an upstream segment response into a client response.
// Both /play/ and /hls-proxy must use it — a header dropped here aborts the
// stream mid-playback, and it used to be duplicated so fixes landed in one path only.
function segmentResponse(response) {
  const headers = {
    'Content-Type': response.headers.get('content-type') || 'video/mp2t',
    'Accept-Ranges': 'bytes',
    'Cache-Control': 'public, max-age=86400',
    ...corsHeaders()
  };

  // The runtime already decodes gzip/br, so re-sending Content-Encoding would
  // make the client decode plain bytes and Content-Length would then describe
  // the compressed size -> truncated responses ("Malformed HTTP status line").
  const encoding = response.headers.get('content-encoding');
  const length = response.headers.get('content-length');
  if (!encoding && length) headers['Content-Length'] = length;

  const range = response.headers.get('content-range');
  if (range) headers['Content-Range'] = range;

  return new Response(response.body, { status: response.status, headers });
}

// ==================================================================
// HLS PLAYLIST RESPONSE
// ==================================================================

function playlistResponse(playlist) {
  return new Response(playlist, {
    headers: {
      'Content-Type': 'application/vnd.apple.mpegurl; charset=utf-8',
      'Cache-Control': 'no-cache, no-store, must-revalidate',
      'Pragma': 'no-cache',
      ...corsHeaders()
    }
  });
}

async function streamUrlForChannel(channelId) {
  const channel = await findChannel(channelId);
  if (!channel) return null;
  return await resolveStream(channel);
}

// Vavoo stream URLs carry an expiry. When a segment starts 403ing the token is
// gone, and no amount of retrying that URL helps — only a fresh resolve does.
// One redirect per channel per window, otherwise a playlist that keeps listing
// the same dead segment loops forever.
const REDIRECT_COOLDOWN_MS = 60000;

async function claimRedirectBudget(channelId) {
  const key = `redir:${channelId}`;
  try {
    const last = Number(await VAVOO_KV?.get(key));
    if (last && Date.now() - last < REDIRECT_COOLDOWN_MS) return false;
    await VAVOO_KV?.put(key, String(Date.now()), { expirationTtl: 120 });
    return true;
  } catch {
    return false;
  }
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
      // even when the resolve call succeeded — that is the case the old code lost.
      let channelName = '';
      let channel = null;

      try {
        channel = await findChannel(channelId);
        if (channel) channelName = channel.name || '';
      } catch (error) {
        console.log(`[vavoo] catalog lookup failed: ${error.message}`);
      }

      const fallbackUrls = [];
      try {
        const index = await getFallbackIndex();
        fallbackUrls.push(...lookupFallbackUrls(index, channelName));
      } catch (error) {
        console.log(`[vavoo] fallback index unavailable: ${error.message}`);
      }

      let lastStatus = 0;
      let passthrough = null;

      // Fetch a candidate and report what came back. The edge cannot reach
      // every source: Cloudflare answers 403 for the vavoo sunshine servers
      // and 530 for dead hostnames. The player's own IP usually gets through,
      // so such a source is handed over instead of failing the channel.
      const attempt = async (url, via, validate) => {
        const response = await proxyStream(baseUrl, url, request, channelId, via, {
          validateSegments: validate,
          // A source that does not answer must not eat the whole budget.
          timeout: via === 'vavoo' ? 4000 : 10000,
          retries: 0,
        }).catch(error => {
          console.log(`[vavoo] ${via} threw: ${error.message}`);
          return null;
        });

        if (!response) {
          lastStatus = 502;
          return { status: 502 };
        }

        if (response.status < 400) {
          const contentType = String(response.headers.get('content-type') || '').toLowerCase();
          // A radio stream or an html page answers 200 but cannot be played as
          // HLS, so it must not count as a recovered channel.
          if (!isM3u8Response(url, contentType)) {
            console.log(`[vavoo] ${via} ${describeUrl(url)} -> ${response.status} ${contentType || 'no ct'}, not a playlist`);
            lastStatus = 502;
            return { status: 502 };
          }
          return { status: response.status, response };
        }

        lastStatus = response.status;
        // The edge is blocked for the vavoo sources, the player's own IP is
        // not. Handing over the original url is the fastest way to a stream,
        // so it wins over the slower curated fallbacks.
        if (via === 'vavoo' && (response.status === 403 || response.status === 530)) {
          passthrough = url;
        }
        console.log(`[vavoo] ${via} ${describeUrl(url)} -> ${response.status}, trying next`);
        return { status: response.status };
      };

      // Vavoo first. The resolver also hands out dead sunshine servers, and a
      // dead one answers 403/530 — that means "resolve again", not "give up on
      // vavoo and fall back to the curated index".
      if (channel) {
        // The player waits for the answer, so the vavoo phase is bounded.
        // When the edge is blocked (403/530) the passthrough is already set,
        // so we go straight to the curated index instead of re-resolving
        // three times (each resolve gives the same sunshine host anyway).
        const vavooDeadline = Date.now() + 4000;
        for (let round = 0; round < 2; round++) {
          if (Date.now() > vavooDeadline) break;
          const resolved = await resolveStream(channel).catch(err => {
            console.log(`[vavoo] resolve failed: ${err.message}`);
            return null;
          });
          if (!resolved) break;

          const result = await attempt(resolved, 'vavoo');
          if (result.response) return result.response;
          if (result.status !== 403 && result.status !== 530) break;
          // Edge-blocked: passthrough is set, no point re-resolving.
          if (result.status === 403 || result.status === 530) {
            console.log(`[vavoo] vavoo source ${result.status}, going to fallbacks`);
            break;
          }
        }
      }

      // The vavoo source is gone for good, so the curated index is the only
      // chance left. A source that answers with a dead playlist is skipped,
      // so every candidate is probed before it is handed out.
      // The candidates are checked in parallel: the first one that really
      // works wins, and a slow dead source no longer eats the whole budget.
      // When the player can fetch the vavoo stream itself, the curated index
      // only gets a short budget - the player waits for the answer.
      const fallbackDeadline = Date.now() + (passthrough ? 5000 : 8000);
      const fallbackResults = await Promise.all(fallbackUrls.map(async url => {
        if (Date.now() > fallbackDeadline) return null;
        // Most index entries are player pages that hide the stream behind an
        // api call. They are turned into the real playlist url first.
        const candidate = await resolveFallbackUrl(url);
        if (!candidate) return null;
        const result = await attempt(candidate.url, candidate.via, true);
        if (!result.response) return null;
        return { candidate, response: result.response };
      }));

      for (const recovered of fallbackResults) {
        if (!recovered) continue;
        console.log(`[vavoo] "${channelName || channelId}" recovered via fallback: ${describeUrl(recovered.candidate.url)}`);
        return recovered.response;
      }

      // The vavoo source exists but the edge cannot reach it. The player can,
      // so hand it over instead of failing the channel.
      if (passthrough) {
        console.log("[vavoo] handing passthrough");
        // Return the passthrough url as a minimal but valid HLS playlist so the
        // client gets a parseable manifest instead of a 302 that Cloudflare blocks.
        const playlist = `#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:10\n#EXT-X-MEDIA-SEQUENCE:0\n#EXTINF:10.0,\n${passthrough}\n`;
        return new Response(playlist, {
          status: 200,
          headers: {
            'Content-Type': 'application/vnd.apple.mpegurl; charset=utf-8',
            'Cache-Control': 'no-store',
            ...corsHeaders()
          }
        });
      }

      if (!channel && fallbackUrls.length === 0) {
        const direct = await resolveDirect(channelId).catch(() => null);
        if (direct) {
          const result = await attempt(direct, 'direct');
          if (result.response) return result.response;
        }
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
if (path.startsWith('/hls-proxy')) {
      const upstreamUrl = url.searchParams.get('url');
      const channelId = url.searchParams.get('id');
      const via = url.searchParams.get('v') || 'vavoo';
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
        const upstreamHeaders = { ...getStreamHeaders(upstreamUrl, via) };
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
            headers: { ...getPlaylistHeaders(upstreamUrl, via), ...(rangeHeader ? { Range: rangeHeader } : {}) },
            timeout: 15000,
            retries: 1,
          });
        }

        console.log(`[vavoo] hls-proxy ${describeUrl(upstreamUrl)} -> ${response.status}`);

        // Still blocked after the header retry: the token is dead, not the request.
        // Re-resolve and send the player back for a playlist with fresh segment URLs.
        if (!response.ok && (response.status === 403 || response.status === 401) && channelId) {
          const fresh = await streamUrlForChannel(channelId).catch(() => null);
          if (fresh && await claimRedirectBudget(channelId)) {
            console.log(`[vavoo] token expired on ${describeUrl(upstreamUrl)}, re-resolved for ${channelId}`);
            // The nonce keeps the redirect target unique: a player that lands
            // back on the same playlist URL counts as a redirect loop and gives
            // up, and the fresh segment URLs only differ per re-resolve.
            return new Response(null, {
              status: 302,
              headers: {
                Location: `${baseUrl}/play/${encodeURIComponent(channelId)}?r=${Date.now()}`,
                'Cache-Control': 'no-store',
                ...corsHeaders()
              }
            });
          }
          console.log(`[vavoo] re-resolve gave nothing for ${channelId}, passing ${response.status} through`);
        }

        if (!response.ok) {
          return new Response(`Upstream error: ${response.status}`, { status: response.status, headers: corsHeaders() });
        }

        const contentType = response.headers.get('content-type') || '';

        // The proxy path carries the role that was assigned while rewriting:
        // .m3u8 is a playlist, .ts and friends are media segments. Some CDNs
        // answer a segment with a mpegurl content type, and serving that as a
        // playlist breaks the player.
        const role = pathExtension(path);
        const asPlaylist = role === '.m3u8' || (!role && isM3u8Response(upstreamUrl, contentType));

        if (asPlaylist) {
          return playlistResponse(await playlistBody(baseUrl, upstreamUrl, await response.text(), channelId, via));
        }

        return segmentResponse(response);

      } catch (error) {
        console.log(`[vavoo] Proxy error: ${error.message}`);
        return new Response(`Proxy error: ${error.message}`, { status: 500, headers: corsHeaders() });
      }
    }

    // ============================================================
    // STREAM PROXY — generic HTTPS proxy for any URL (bypass for mixed content)
    // ============================================================
    if (path.startsWith('/stream')) {
      const upstreamUrl = url.searchParams.get('url');
      const via = url.searchParams.get('v') || 'fallback';
      if (!upstreamUrl) {
        return new Response('URL parameter missing', { status: 400, headers: corsHeaders() });
      }

      try {
        const parsed = new URL(upstreamUrl);
        if (!['http:', 'https:'].includes(parsed.protocol)) {
          return new Response('Unsupported protocol', { status: 400, headers: corsHeaders() });
        }

        const response = await fetchWithRetry(upstreamUrl, {
          headers: getStreamHeaders(upstreamUrl, via),
          timeout: 15000,
          retries: 0,
        });

        if (!response.ok) {
          return new Response(`Upstream error: ${response.status}`, { status: response.status, headers: corsHeaders() });
        }

        const contentType = response.headers.get('content-type') || '';
        if (isM3u8Response(upstreamUrl, contentType)) {
          return playlistResponse(rewritePlaylist(baseUrl, upstreamUrl, await response.text(), null, via));
        }

        return segmentResponse(response);
      } catch (error) {
        console.log(`[vavoo] stream proxy error: ${error.message}`);
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

// ffmpeg only accepts these segment extensions in an HLS playlist. The
// turkish cdn "tracks-v1a1" sources serve jpeg segments, which no player can
// demux - such a source is skipped instead of being handed out.
const PLAYABLE_SEGMENT_EXTENSIONS = new Set([
  'ts', 'm4s', 'mp4', 'm4a', 'm4v', 'aac', 'vtt', 'webvtt', 'cmfv', 'cmfa', 'fmp4'
]);

function segmentExtensionIsPlayable(url) {
  const ext = pathExtension(url);
  if (!ext) return true;
  return PLAYABLE_SEGMENT_EXTENSIONS.has(ext.slice(1).toLowerCase());
}

// Checks that a segment really comes back. Curated sources often serve a
// playlist whose segments all time out, and the player would report that as
// "I/O error" without ever saying which source failed.
async function segmentReachable(url, via) {
  try {
    const response = await fetchWithRetry(url, {
      headers: { ...getStreamHeaders(url, via), Range: 'bytes=0-1023' },
      timeout: 5000,
      retries: 0,
    });
    return response.ok;
  } catch {
    return false;
  }
}

async function proxyStream(baseUrl, streamUrl, clientRequest, channelId, via, options = {}) {
  // Forward Range header from client for seeking support - but only for
  // segments. A playlist answered with 206 comes back truncated, and a
  // truncated master has a broken session id, which the variant then
  // rejects with 403.
  const upstreamHeaders = { ...getStreamHeaders(streamUrl, via) };
  const rangeHeader = clientRequest?.headers?.get('Range') || clientRequest?.headers?.get('range');
  if (rangeHeader && !isM3u8Url(streamUrl)) {
    upstreamHeaders['Range'] = rangeHeader;
  }

  const response = await fetchWithRetry(streamUrl, {
    headers: upstreamHeaders,
    timeout: options.timeout ?? 10000,
    retries: options.retries ?? 1,
  });

  console.log(`[vavoo] play ${describeUrl(streamUrl)} -> ${response.status} (${response.headers.get('content-type') || 'no ct'})`);

  if (!response.ok) {
    return new Response(`Stream error: ${response.status}`, { status: response.status, headers: corsHeaders() });
  }

  const contentType = response.headers.get('content-type') || '';

  if (isM3u8Response(streamUrl, contentType)) {
    const resolved = await unwrapPlaylist(streamUrl, await response.text(), via);

    if (!resolved.playable) {
      console.log(`[vavoo] ${via} ${describeUrl(streamUrl)}: no media playlist behind it`);
      return new Response('Stream error: no media playlist', { status: 502, headers: corsHeaders() });
    }

    const segment = firstSegmentUri(resolved.url, resolved.text);

    if (segment && !segmentExtensionIsPlayable(segment)) {
      console.log(`[vavoo] ${via} ${describeUrl(streamUrl)}: segment type ${pathExtension(segment)} is not playable, skipping source`);
      return new Response('Stream error: unusable segment type', { status: 502, headers: corsHeaders() });
    }

    if (options.validateSegments) {
      // Probe the segment of the playlist the player will actually use, not
      // of the outer one - the outer one may still be a master.
      if (segment && !(await segmentReachable(segment, via))) {
        console.log(`[vavoo] ${via} ${describeUrl(streamUrl)}: first segment unreachable, skipping source`);
        return new Response('Stream error: segments unreachable', { status: 502, headers: corsHeaders() });
      }
    }

    return playlistResponse(rewritePlaylist(baseUrl, resolved.url, resolved.text, channelId, via));
  }

  return segmentResponse(response);
}
