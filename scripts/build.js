"use strict";

const fs = require("node:fs/promises");
const path = require("node:path");

const CATALOG_URL = "https://vavoo.to/mediahubmx-catalog.json";
const GROUPS = ["Turkey", "Germany"];

const M3U_FILE = path.join(__dirname, "..", "iptv.m3u");
const CACHE_FILE = path.join(__dirname, "..", "link_cache.json");
const FALLBACKS_FILE = path.join(__dirname, "..", "fallbacks.json");
const CUSTOM_LINKS_FILE = path.join(__dirname, "..", "custom_links.json");

const FALLBACK_URLS_PER_CHANNEL = 3;

const FAMELACK_DATA_URL =
  "https://raw.githubusercontent.com/famelack/famelack-data/main/tv/raw/countries";

const LIVETV_M3U_URL =
  process.env.LIVETV_M3U_URL ||
  "https://raw.githubusercontent.com/kadirsener1/livetv/be5ade66482707a4eb9a5934836275562250fc57/tv247tr.m3u";

const IPTVORG_CHANNELS_URL =
  process.env.IPTVORG_CHANNELS_URL || "https://iptv-org.github.io/api/channels.json";
const IPTVORG_LOGOS_URL =
  process.env.IPTVORG_LOGOS_URL || "https://iptv-org.github.io/api/logos.json";
const IPTVORG_STREAMS_URL =
  process.env.IPTVORG_STREAMS_URL || "https://iptv-org.github.io/api/streams.json";

// -- Configuration --
const FETCH_TIMEOUT_MS = 20000;
const CHECK_ENABLED = process.env.CHECK_ENABLED !== "false";
const CHECK_CONCURRENCY = parseInt(process.env.CHECK_CONCURRENCY || "32", 10);
const CHECK_TIMEOUT_MS = parseInt(process.env.CHECK_TIMEOUT_MS || "3000", 10);
const CACHE_TTL_MS = parseInt(
  process.env.CACHE_TTL_MS || String(3 * 24 * 60 * 60 * 1000),
  10
);

const PROXY_BASE = (process.env.PROXY_BASE || "").replace(/\/+$/, "");

const STREAM_USER_AGENT =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36";

const HEADERS = {
  "content-type": "application/json; charset=utf-8",
  accept: "*/*",
  "accept-language": "en-US,en;q=0.9,tr;q=0.8",
  "cache-control": "no-cache",
  pragma: "no-cache",
  origin: "https://vavoo.to",
  referer: "https://vavoo.to/live",
  dnt: "1",
  "user-agent": STREAM_USER_AGENT,
};

// Channels the user does not want in the playlist
const BLOCKED_CHANNELS =
  /alanya|izmir\s+tv|antalya\s+tv|bursa\s+tv|ankara\s+tv|istanbul\s+tv|eskisehir\s+tv|konya\s+tv|trabzon\s+tv|izmir\s+haber/i;

function isBlockedChannel(name) {
  return BLOCKED_CHANNELS.test(String(name || ""));
}

// ==================================================================
// LINK CHECKER
// ==================================================================

function splitPipeParams(url) {
  if (!url || !url.includes("|")) return { url, params: {} };
  const [base, paramStr] = url.split("|", 2);
  const params = {};
  for (const part of paramStr.split("&")) {
    const [k, v] = part.split("=", 2);
    if (k) params[k] = v || "";
  }
  return { url: base.trim(), params };
}

async function checkLink(rawUrl, timeout = CHECK_TIMEOUT_MS) {
  if (!rawUrl || typeof rawUrl !== "string") return false;

  const { url, params } = splitPipeParams(rawUrl);
  if (!url) return false;

  const headers = {
    "User-Agent": params["User-Agent"] || STREAM_USER_AGENT,
    Accept: "*/*",
    Origin: params["Origin"] || "https://vavoo.to",
    Referer: params["Referer"] || "https://vavoo.to/",
    Connection: "keep-alive",
  };

  try {
    const res = await fetch(url, {
      method: "GET",
      headers,
      signal: AbortSignal.timeout(timeout),
      redirect: "follow",
    });

    if (res.ok || [301, 302, 307, 308, 405].includes(res.status)) {
      res.body?.cancel().catch(() => {});
      return true;
    }
    return false;
  } catch {
    return false;
  }
}

// ==================================================================
// FALLBACK INDEX  (famelack-data + custom_links.json)
// ==================================================================

let _fallbackIndex = null;

// Folds case, quality tags and Turkish diacritics so "CNN TÜRK" === "CNN Turk"
function normalizeChannelName(name) {
  return String(name || "")
    .toLowerCase()
    .replace(/^\s*(?:4k\s*tr|4k|tr|de|at|ch)\s*:\s*/, "")
    .replace(/\s*\.(?:b|c|s)\b/g, "")
    .replace(/\[[^\]]*\]/g, "")
    .replace(/\([^)]*\)/g, "")
    .replace(/\b(hd|fhd|uhd|4k|sd|hevc|h265|h264|raw)\b/g, "")
    .replace(/\u00fc/g, "u")
    .replace(/\u011f/g, "g")
    .replace(/\u015f/g, "s")
    .replace(/\u0131/g, "i")
    .replace(/\u00f6/g, "o")
    .replace(/\u00e7/g, "c")
    .replace(/\s-\s[a-z]{2,5}$/, "")
    .replace(/[-_/]+/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

// Two names only match if they are near-identical in length,
// otherwise "ATV" would pick up the "ATV Alanya" stream.
function isSimilarName(a, b) {
  if (a === b) return true;
  if (!a.includes(b) && !b.includes(a)) return false;
  const longer = Math.max(a.length, b.length);
  const shorter = Math.min(a.length, b.length);
  return shorter / longer >= 0.7;
}

async function loadFallbackIndex() {
  if (_fallbackIndex) return _fallbackIndex;

  console.log("  Loading fallback sources...");
  _fallbackIndex = new Map();

  for (const country of ["tr", "de"]) {
    try {
      const res = await fetch(`${FAMELACK_DATA_URL}/${country}.json`, {
        signal: AbortSignal.timeout(30000),
      });
      if (!res.ok) {
        console.warn(`    ${country.toUpperCase()}: HTTP ${res.status}`);
        continue;
      }
      const channels = await res.json();
      if (!Array.isArray(channels)) continue;

      let added = 0;
      for (const ch of channels) {
        const streams = ch?.sources?.streams;
        if (!ch?.name || !Array.isArray(streams) || streams.length === 0) continue;
        if (ch.isGeoBlocked || isBlockedChannel(ch.name)) continue;

        const key = normalizeChannelName(ch.name);
        if (key && !_fallbackIndex.has(key)) {
          _fallbackIndex.set(key, { name: ch.name, streams });
          added++;
        }
      }
      console.log(`    famelack-data ${country.toUpperCase()}: ${added} channels`);
    } catch (err) {
      console.warn(`    famelack-data ${country.toUpperCase()} failed: ${err.message}`);
    }
  }

  // Static community list - lowest priority, only fills what the curated sources miss
  try {
    const res = await fetch(LIVETV_M3U_URL, { signal: AbortSignal.timeout(60000) });
    if (res.ok) {
      const lines = (await res.text()).split(/\r?\n/);
      let pending = null;
      let added = 0;

      for (const line of lines) {
        const t = line.trim();
        if (!t) continue;
        if (t.startsWith("#EXTINF")) {
          pending = t.split(",").pop().trim();
          continue;
        }
        if (!pending || t.startsWith("#")) continue;

        if (!isBlockedChannel(pending) && /^https?:\/\//i.test(t)) {
          const key = normalizeChannelName(pending);
          if (key && !_fallbackIndex.has(key)) {
            _fallbackIndex.set(key, { name: pending, streams: [t] });
            added++;
          }
        }
        pending = null;
      }
      console.log(`    livetv m3u: ${added} channels`);
    } else {
      console.warn(`    livetv m3u: HTTP ${res.status}`);
    }
  } catch (err) {
    console.warn(`    livetv m3u failed: ${err.message}`);
  }

  // iptv-org is the only source carrying real direct URLs for German channels,
  // which the Turkish curated lists never covered. streams.json holds the urls,
  // channels.json the country and closed flags - they join on channel id.
  // Fills gaps only, curated entries keep priority.
  try {
    const [channels, streams] = await Promise.all([
      fetchJson(IPTVORG_CHANNELS_URL),
      fetchJson(IPTVORG_STREAMS_URL),
    ]);
    const meta = new Map();
    for (const c of channels) {
      if (c && c.id) meta.set(c.id, c);
    }

    let added = 0;
    for (const s of streams) {
      if (!s?.channel || s.closed) continue;
      if (!/^https?:\/\//i.test(s.url || "")) continue;
      const c = meta.get(s.channel);
      if (!c || c.closed) continue;
      if (c.country !== "DE" && c.country !== "TR") continue;
      if (isBlockedChannel(c.name)) continue;

      const key = normalizeChannelName(c.name);
      if (!key) continue;
      const existing = _fallbackIndex.get(key);
      if (!existing) {
        _fallbackIndex.set(key, { name: c.name, streams: [s.url] });
        added++;
      } else if (existing.streams.length < FALLBACK_URLS_PER_CHANNEL) {
        existing.streams.push(s.url);
      }
    }
    console.log(`    iptv-org TR/DE: ${added} channels added, gaps filled`);
  } catch (err) {
    console.warn(`    iptv-org failed: ${err.message}`);
  }

  // Hand-curated overrides win over upstream data
  try {
    const raw = await fs.readFile(CUSTOM_LINKS_FILE, "utf8");
    const data = JSON.parse(raw);
    let added = 0;
    for (const [name, urls] of Object.entries(data)) {
      if (!Array.isArray(urls) || urls.length === 0) continue;
      if (isBlockedChannel(name)) continue;
      const key = normalizeChannelName(name);
      if (key) {
        _fallbackIndex.set(key, { name, streams: urls });
        added++;
      }
    }
    console.log(`    custom_links.json: ${added} channels`);
  } catch (err) {
    console.warn(`    custom_links.json unavailable: ${err.message}`);
  }

  console.log(`  Fallback index ready: ${_fallbackIndex.size} channels`);
  return _fallbackIndex;
}

// Famelack streams are objects, livetv entries are plain strings — normalise both.
function pickFallbackUrls(entry) {
  return (entry?.streams || [])
    .map((u) => (typeof u === "string" ? u : u?.url))
    .filter((u) => typeof u === "string" && /^https?:\/\//i.test(u))
    .slice(0, FALLBACK_URLS_PER_CHANNEL);
}

// Opt-in: whether a url answers depends on the network probing it. GitHub Actions
// and Cloudflare Workers are both datacenter egress and get blocked by some CDNs
// that work fine from a home connection, so pruning by default threw away sources
// that are perfectly usable from elsewhere. A dead fallback only costs one fetch.
const PRUNE_ENABLED = process.env.PRUNE_FALLBACKS === "1";
const PRUNE_CONCURRENCY = 24;
const PRUNE_TIMEOUT_MS = 6000;
const PROBE_UA =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36";

// A fallback the Worker cannot reach is worse than none: it burns a fetch and
// still returns 403. 58% of the index was a single dead host and for most
// channels it was the only entry, so probe once per build and ship what answers.
async function probeFallbackUrl(url) {
  try {
    const res = await fetch(url, {
      redirect: "follow",
      signal: AbortSignal.timeout(PRUNE_TIMEOUT_MS),
      headers: { "User-Agent": PROBE_UA, Referer: new URL(url).origin + "/" },
    });
    if (!res.ok) return false;
    const ct = res.headers.get("content-type") || "";
    return /mpegurl|m3u/i.test(ct) || /\.m3u8(\?|$)/i.test(url);
  } catch {
    return false;
  }
}

async function runPool(items, limit, workerFn) {
  const results = new Array(items.length);
  let next = 0;
  const runners = Array.from({ length: Math.min(limit, items.length) }, async () => {
    while (next < items.length) {
      const i = next++;
      results[i] = await workerFn(items[i], i);
    }
  });
  await Promise.all(runners);
  return results;
}

async function pruneIndex(index) {
  const urls = [...new Set(index.values().flatMap((e) => pickFallbackUrls(e)))];
  console.log(`  probing ${urls.length} unique fallback urls...`);
  const started = Date.now();

  const verdicts = await runPool(urls, PRUNE_CONCURRENCY, probeFallbackUrl);
  const alive = new Set(urls.filter((_, i) => verdicts[i]));

  const pruned = new Map();
  let kept = 0;
  for (const [key, entry] of index) {
    const good = pickFallbackUrls(entry).filter((u) => alive.has(u));
    if (good.length) {
      pruned.set(key, { name: entry.name, streams: good });
      kept += good.length;
    }
  }
  const hosts = {};
  for (const u of alive) {
    try {
      const h = new URL(u).hostname;
      hosts[h] = (hosts[h] || 0) + 1;
    } catch {}
  }
  const top = Object.entries(hosts).sort((a, b) => b[1] - a[1])[0];
  console.log(
    `  alive: ${alive.size}/${urls.length} urls in ${((Date.now() - started) / 1000).toFixed(0)}s` +
      (top ? `, top host ${top[0]} ${((top[1] / alive.size) * 100).toFixed(0)}%` : "")
  );
  return pruned;
}

// The Worker owns the runtime chain (vavoo -> this index -> original).
// Build just has to ship the index; it must not try to resolve channels itself.
async function writeFallbackIndex() {
  const index = await loadFallbackIndex();
  const finalIndex = PRUNE_ENABLED ? await pruneIndex(index) : index;
  const out = {};
  for (const [key, entry] of finalIndex) {
    const urls = pickFallbackUrls(entry);
    if (urls.length) out[key] = urls;
  }
  await fs.writeFile(FALLBACKS_FILE, JSON.stringify(out), "utf8");
  const bytes = (await fs.stat(FALLBACKS_FILE)).size;
  console.log(`  fallbacks.json: ${Object.keys(out).length} channels, ${bytes} bytes`);
}

// ==================================================================
// REPAIR
// ==================================================================

async function repairLink(item) {
  const originalUrl = item.url;
  const vavooId = item?.ids?.id;

  // Every channel goes through the Worker, which holds the fallback chain.
  if (vavooId && PROXY_BASE) {
    return { url: `${PROXY_BASE}/play/${vavooId}`, status: "proxy" };
  }

  // No proxy configured: keep the direct link only if it answers now.
  if (await checkLink(originalUrl)) {
    return { url: originalUrl, status: "ok" };
  }

  return { url: originalUrl, status: "dead" };
}

async function loadLinkCache() {
  try {
    const data = JSON.parse(await fs.readFile(CACHE_FILE, "utf8"));
    return data && typeof data === "object" ? data : {};
  } catch {
    return {};
  }
}

async function saveLinkCache(cache) {
  try {
    await fs.writeFile(CACHE_FILE, JSON.stringify(cache, null, 2), "utf8");
  } catch (err) {
    console.warn(`Cache save failed: ${err.message}`);
  }
}

async function repairAll(items) {
  console.log("\nLINK CHECKER");
  console.log(`${items.length} channels | concurrency ${CHECK_CONCURRENCY} | timeout ${CHECK_TIMEOUT_MS}ms`);

  const cache = await loadLinkCache();
  const now = Date.now();
  const results = new Array(items.length);
  const counts = { proxy: 0, ok: 0, fallback: 0, dead: 0, cached: 0 };
  let processed = 0;

  for (let i = 0; i < items.length; i += CHECK_CONCURRENCY) {
    const batch = [];
    for (let j = i; j < Math.min(i + CHECK_CONCURRENCY, items.length); j++) {
      batch.push({ index: j, item: items[j] });
    }

    const done = await Promise.all(
      batch.map(async ({ index, item }) => {
        const key = item.url;
        const hit = cache[key];

        // Only healthy links are cached; fallbacks are re-verified every run
        if (hit && (hit.status === "ok" || hit.status === "proxy")) {
          if (now - hit.timestamp < CACHE_TTL_MS) {
            return { index, result: { url: hit.url, status: hit.status }, cached: true };
          }
        }

        const result = await repairLink(item);
        cache[key] = { url: result.url, status: result.status, timestamp: now };
        return { index, result, cached: false };
      })
    );

    for (const { index, result, cached } of done) {
      results[index] = result;
      if (cached) counts.cached++;
      else counts[result.status]++;
    }

    processed += batch.length;
    if (processed % (CHECK_CONCURRENCY * 10) === 0 || processed === items.length) {
      console.log(
        `  [${Math.round((processed / items.length) * 100)}%] ${processed}/${items.length} | ` +
          `proxy:${counts.proxy} ok:${counts.ok} fallback:${counts.fallback} dead:${counts.dead} cached:${counts.cached}`
      );
    }
  }

  await saveLinkCache(cache);

  items.forEach((item, i) => {
    if (!results[i]) return;
    item.url = results[i].url;
    item._repairStatus = results[i].status;
    item._repaired = results[i].status === "proxy" || results[i].status === "fallback";
  });

  console.log(
    `\n  proxy:${counts.proxy} ok:${counts.ok} fallback:${counts.fallback} ` +
      `dead:${counts.dead} from-cache:${counts.cached}\n`
  );
  return items;
}

// ==================================================================
// VAVOO API
// ==================================================================

function buildBody(group, cursor) {
  return JSON.stringify({
    language: "de",
    region: "DE",
    catalogId: "iptv",
    id: "",
    adult: false,
    search: "",
    sort: "name",
    filter: { group },
    cursor,
  });
}

async function fetchPage(group, cursor) {
  const body = buildBody(group, cursor);
  let lastErr;

  for (let attempt = 1; attempt <= 5; attempt++) {
    try {
      const res = await fetch(CATALOG_URL, {
        method: "POST",
        headers: HEADERS,
        body,
        signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      if (data?.error) throw new Error(`Vavoo: ${data.error}`);
      return data;
    } catch (err) {
      lastErr = err;
      const wait = 1000 * attempt;
      console.warn(`[${group}] attempt ${attempt} failed (${err.message}), retry in ${wait}ms`);
      await new Promise((r) => setTimeout(r, wait));
    }
  }
  throw lastErr;
}

async function fetchAllForGroup(group) {
  const items = [];
  let cursor = null;
  let page = 0;

  do {
    page++;
    const data = await fetchPage(group, cursor);
    for (const item of data.items || []) {
      if (isBlockedChannel(item.name)) continue;
      items.push(item);
    }
    console.log(`  ${group} page ${page}: ${items.length} total`);
    cursor = data.nextCursor ?? null;
  } while (cursor != null && page < 200);

  return items;
}

async function fetchAll() {
  const all = [];
  const seen = new Set();

  for (const group of GROUPS) {
    for (const item of await fetchAllForGroup(group)) {
      const id = item?.ids?.id;
      if (id && !seen.has(id)) {
        seen.add(id);
        all.push(item);
      }
    }
  }
  return all;
}

// ==================================================================
// CATEGORIES
// ==================================================================

function categorize(name) {
  const s = String(name || "")
    .replace(/^\s*(?:4K TR:|DE:|AT:|CH:)\s*/i, "")
    .replace(/\s+(?:UHD|FHD|HD\+|HD|SD|HEVC|RAW|H265|H\.265|FEED)(?=\s|$)/gi, " ")
    .replace(/\s*\.(?:b|c|s)\b/gi, "")
    .replace(/\s+/g, " ")
    .trim();

  const rules = [
    [
      "Almanya Sport",
      /\b(DAZN|SKY SPORT|SKY BULI|SKY BUNDESLIGA|SKY PREMIER|BUNDESLIGA|PREMIER LEAGUE|MAGENTA SPORT|MAGENTASPORT|MAGENTA FUSSBALL|SPORT1|EUROSPORT|FUSSBALL|SPORTS TV|SPORTDIGITAL|RED BULL TV|BLUE SPORT|SKY SPORT MIX|SKY SPORT NEWS|SKY F1|SKY FORMEL 1|SPORTDEUTSCHLAND|LAOLA1)\b/i,
    ],
    [
      "Almanya TV",
      /\b(ARD|ZDF|ZDF NEO|RTL|RTL PLUS|RTL\+|RTL PASSION|RTL NITRO|PRO SIEBEN|PRO7|SAT\.1|SAT1|VOX|KABEL 1|KABEL 1 DOKU|SUPER RTL|NICKELODEON DE|NICK DE|NDR|WDR|MDR|BR|SWR|HR|RBB|SR|PHOENIX|TAGESSCHAU24|WELT|N24|N-TV|TELE 5|SIXX|DISNEY CHANNEL DE|TOGGO|KIKA|DEUTSCH|GERMAN|DEUTSCHLAND|DAS ERSTE|ONE|ARTE|3SAT|ZDF INFO|ZDF KULTUR|BR ALPHA|ARD ALPHA|RTL ZWEI|RTL2)\b/i,
    ],
    ["Radio", /\b(RADYO|RADIO|FM)\b/i],
    [
      "Yerel",
      /\b(YEREL|REGIONAL|YÖRESEL|BELEDİYE|Muğla|Antalya|İzmir|Ankara|İstanbul|Bursa|Konya|Gaziantep|Kayseri|Mersin|Diyarbakır|Trabzon|Samsun|Eskişehir|Denizli|Malatya|Erzurum|Van|Batman|Şanlıurfa|Hatay|Manisa|Aydın|Tekirdağ|Edirne|Çanakkale|Balıkesir|Bolu|Sakarya|Düzce|Karabük|Bartın|Isparta|Burdur|Afyon|Uşak|Kütahya|Bilecik|Yalova|Kocaeli|Kırklareli|Kırşehir|Kırıkkale|Aksaray|Niğde|Nevşehir|Yozgat|Sivas|Tokat|Amasya|Çorum|Kastamonu|Sinop|Ordu|Giresun|Artvin|Rize|Gümüşhane|Bayburt|Erzincan|Tunceli|Elazığ|Adıyaman|Şırnak|Siirt|Bitlis|Muş|Ağrı|Iğdır|Kars|Ardahan)\b/i,
    ],
    [
      "Ulusal",
      /\b(TRT|MECLİS|TABII|SHOW ?TV|STAR ?TV|ATV(?!\s+ALANYA)|KANAL D|NOW TV|EXXEN|TV ?8|TEVE 2|BEYAZ ?TV|360|SKY 360|A2 TV|EURO D|KANAL 7|DMAX TURKIYE|BENG[UÜ]T[UÜ]RK|ULUSAL|KANAL FIRAT|KANAL V|KANAL 23|KANAL 26|KANAL 33|KANAL 34|KANAL 58|KANAL 12|KANAL 15|KANAL 19|KANAL AVRUPA)\b/i,
    ],
    [
      "Haber",
      /\b(HABER|NEWS|CNN|NTV|A HABER|BLOOMBERG|HALK TV|SÖZCÜ|LIDER|FLASH|GLOBAL|TV 100|TGRT HABER|ÜLKE|DHA|KANAL B|KANAL 24|TV NET|AKIT|ANADOLU|HABERTÜRK|HABER GLOBAL|TÜRK HABER)\b/i,
    ],
    [
      "Belgesel",
      /\b(BELGESEL|DOKU|DOCU|DISCOVERY|NATIONAL GEOGRAPHIC|NAT GEO|HISTORY|ANIMAL PLANET|BBC EARTH|TLC|TRT BELGESEL|TGRT BELGESEL|VIASAT|DA VINCI|DOCUBOX|FASHION|BEIN IZ|GURME|DOCUMENTARY|WILD)\b/i,
    ],
    [
      "Spor",
      /\b(SPOR|SPORT|A SPOR|TRT SPOR|S SPORT|TIVIBU|TABII SPOR|BEIN SPORTS|NBA|EXXEN SPORTS|FB TV|GS TV|SPOR SMART|BASKETBOL|VOLEYBOL|TENIS|FUTBOL|IDMAN|EXXEN SPO)\b/i,
    ],
    [
      "Çocuk",
      /\b(ÇOCUK|COCUK|KINDER|KIDS|CARTOON|DISNEY|NICK|BABY|MINIKA|TOGGO|TRT ÇOCUK|BABY TV|NICK JR|NICKELODEON)\b/i,
    ],
    [
      "Film",
      /\b(SINEMA|CINEMA|MOVIE|FILM|YESILCAM|BOX OFFICE|SHOWMAX|KINGBOX|ARENA BOX|BEIN MOVIES|MOVIEMAX|MOVIESMART|SINEVIZYON|SINEMAX)\b/i,
    ],
    [
      "Dini",
      /\b(DİYANET|MEHTAP|HİLAL|KUDUS|SEMERKAND|MERCAN|VUSLAT|KARDELEN|DOST TV|YOL TV|TVNET|DINI|İSLAM|KURAN)\b/i,
    ],
  ];

  for (const [name, re] of rules) if (re.test(s)) return name;
  return "Sonstige";
}

// ==================================================================
// LOGOS
// ==================================================================

function normalizeForMatch(name) {
  return String(name || "")
    .toUpperCase()
    .replace(/^\s*(?:4K\s*TR:|4K:|TR:|DE:|AT:|CH:)\s*/i, "")
    .replace(/\s*\.(?:B|C|S)\b/gi, "")
    .replace(/[İIÜÖÇŞĞ]/g, (c) => ({ İ: "I", I: "I", Ü: "U", Ö: "O", Ç: "C", Ş: "S", Ğ: "G" })[c])
    .replace(/[^A-Z0-9\s]/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

const stripQuality = (s) =>
  s.replace(/\b(UHD|FHD|HD\+|HD|SD|HEVC|RAW|H265|4K|8K|LIVE|BACKUP)\b/g, "").replace(/\s+/g, " ").trim();

async function fetchJson(url) {
  const res = await fetch(url, { signal: AbortSignal.timeout(60000) });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

async function buildLogoIndex() {
  const [channels, logos] = await Promise.all([
    fetchJson(IPTVORG_CHANNELS_URL),
    fetchJson(IPTVORG_LOGOS_URL),
  ]);

  const relevant = channels.filter((c) => c && (c.country === "TR" || c.country === "DE"));
  const ids = new Set(relevant.map((c) => c.id));

  const best = new Map();
  for (const l of logos) {
    if (!l?.url || !ids.has(l.channel)) continue;
    const cur = best.get(l.channel);
    if (!cur || (l.in_use && !cur.in_use)) best.set(l.channel, l);
  }

  const idx = new Map();
  for (const c of relevant) {
    const logo = best.get(c.id);
    if (!logo) continue;
    for (const n of [c.name, ...(c.alt_names || [])]) {
      if (!n) continue;
      const k = normalizeForMatch(n);
      if (k && !idx.has(k)) idx.set(k, logo.url);
      const sq = stripQuality(k);
      if (sq && !idx.has(sq)) idx.set(sq, logo.url);
    }
  }
  return idx;
}

function makeLogoResolver(idx) {
  if (!idx?.size) return () => "";
  return (name) => {
    const k = normalizeForMatch(name);
    return idx.get(k) || idx.get(stripQuality(k)) || "";
  };
}

// ==================================================================
// M3U OUTPUT
// ==================================================================

const escapeAttr = (v) =>
  String(v ?? "").replace(/\r?\n/g, " ").replace(/"/g, "'");

function toM3U(items, resolveLogo) {
  const lines = ["#EXTM3U"];

  for (const it of items) {
    const name = String(it?.name ?? "").replace(/\r?\n/g, " ").trim();
    if (!name || !it.url) continue;

    const group = categorize(name);
    const logo = escapeAttr(resolveLogo(name) || it.logo || "");
    const repair = it._repaired ? ` repair="${it._repairStatus}"` : "";

    lines.push(
      `#EXTINF:-1 tvg-name="${escapeAttr(name)}" tvg-logo="${logo}" group-title="${escapeAttr(group)}"${repair},${name}`
    );
    lines.push(`#EXTGRP:${group}`);
    lines.push(`#EXTVLCOPT:network-caching=1000`);
    lines.push(`#EXTVLCOPT:live-caching=1000`);
    lines.push(it.url);
  }

  lines.push("");
  return lines.join("\n");
}

// ==================================================================
// MAIN
// ==================================================================

async function main() {
  const started = Date.now();

  console.log(`Fetching ${JSON.stringify(GROUPS)} from vavoo.to`);
  if (PROXY_BASE) console.log(`PROXY_BASE=${PROXY_BASE}`);
  else console.warn("WARNING: PROXY_BASE empty - raw vavoo URLs will be written");

  const items = await fetchAll();
  console.log(`Total: ${items.length} channels`);

  try {
    await writeFallbackIndex();
  } catch (err) {
    console.warn(`Fallback index not written: ${err.message}`);
  }

  items.sort((a, b) =>
    String(a.name ?? "").localeCompare(String(b.name ?? ""), "tr-TR")
  );

  let resolveLogo = () => "";
  try {
    resolveLogo = makeLogoResolver(await buildLogoIndex());
  } catch (err) {
    console.warn(`Logos unavailable (${err.message})`);
  }

  let final = items;
  if (CHECK_ENABLED) {
    try {
      final = await repairAll(items);
    } catch (err) {
      console.warn(`Link checker failed (${err.message}); keeping original links`);
    }
  } else {
    console.log("Link checker disabled");
  }

  const m3u = toM3U(final, resolveLogo);
  await fs.writeFile(M3U_FILE, m3u, "utf8");
  console.log(`Wrote ${M3U_FILE} (${m3u.length} bytes)`);

  const dist = new Map();
  for (const it of final) {
    const g = categorize(String(it?.name ?? ""));
    dist.set(g, (dist.get(g) || 0) + 1);
  }
  console.log("\nCategories:");
  for (const [c, n] of [...dist].sort((a, b) => b[1] - a[1])) {
    console.log(`  ${c.padEnd(16)} ${n}`);
  }

  console.log(`\nDone in ${((Date.now() - started) / 1000).toFixed(1)}s`);
}

// ponytail: self-check for name matching, the part that silently mislabels channels
if (process.env.SELFCHECK) {
  const assert = require("node:assert");
  assert.strictEqual(normalizeChannelName("CNN TÜRK"), "cnn turk");
  assert.strictEqual(normalizeChannelName("ATV HD"), "atv");
  assert.strictEqual(normalizeChannelName("ATV Alanya"), "atv alanya");
  assert.strictEqual(normalizeChannelName("4K TR: Show TV"), "show tv");
  assert.ok(!isSimilarName("atv", "atv alanya"), "ATV must not match ATV Alanya");
  assert.ok(!isSimilarName("trt 1", "trt 2"), "TRT 1 must not match TRT 2");
  assert.ok(!isSimilarName("show tv", "show max"), "SHOW TV must not match Show Max");
  assert.ok(
    isSimilarName(normalizeChannelName("cnn türk"), normalizeChannelName("CNN TÜRK")),
    "diacritics fold both ways"
  );
  assert.ok(isSimilarName("halk tv", "halk tv hd"), "quality suffix matches");
  assert.ok(isBlockedChannel("ATV Alanya"), "ATV Alanya blocked");
  assert.ok(!isBlockedChannel("ATV"), "ATV kept");
  // fallbacks.json shape: object streams from famelack, plain strings from livetv
  assert.deepStrictEqual(
    pickFallbackUrls({ streams: [{ url: "https://a/1.m3u8" }, { url: "not-a-url" }, "https://b/2.m3u8", "ftp://x/3.m3u8", "https://c/4.m3u8", "https://d/5.m3u8"] }),
    ["https://a/1.m3u8", "https://b/2.m3u8", "https://c/4.m3u8"],
    "only http(s), capped at FALLBACK_URLS_PER_CHANNEL"
  );
  assert.deepStrictEqual(pickFallbackUrls({ streams: [] }), [], "empty entry yields nothing");
  assert.deepStrictEqual(pickFallbackUrls(null), [], "null entry yields nothing");
  console.log("selfcheck OK");
} else {
  main().catch((err) => {
    console.error(err);
    process.exit(1);
  });
}
