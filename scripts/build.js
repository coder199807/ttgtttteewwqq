"use strict";

const fs = require("node:fs/promises");
const path = require("node:path");

const CATALOG_URL = "https://vavoo.to/mediahubmx-catalog.json";

const GROUPS = ["Turkey", "Germany"];

const M3U_FILE = path.join(__dirname, "..", "iptv.m3u");
const CACHE_FILE = path.join(__dirname, "..", "link_cache.json");

// ─── Configuration ──────────────────────────────────────────
const FETCH_TIMEOUT_MS = 20000;
const CHECK_ENABLED = process.env.CHECK_ENABLED !== "false";
const CHECK_CONCURRENCY = parseInt(process.env.CHECK_CONCURRENCY || "8", 10);
const CHECK_TIMEOUT_MS = parseInt(process.env.CHECK_TIMEOUT_MS || "6000", 10);
const CACHE_TTL_MS = parseInt(process.env.CACHE_TTL_MS || String(24 * 60 * 60 * 1000), 10);

const STREAM_USER_AGENT =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36";

// ═══════════════════════════════════════════════════════════════
// LINK CHECKER
// ═══════════════════════════════════════════════════════════════

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

function buildBrowserHeaders(extra = {}) {
  return {
    "User-Agent": STREAM_USER_AGENT,
    Accept: "*/*",
    "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    Origin: "https://vavoo.to",
    Referer: "https://vavoo.to/",
    "sec-ch-ua":
      '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-site",
    Connection: "keep-alive",
    ...extra,
  };
}

async function checkLink(rawUrl, timeout = CHECK_TIMEOUT_MS) {
  if (!rawUrl || typeof rawUrl !== "string") return false;

  const { url, params } = splitPipeParams(rawUrl);
  if (!url) return false;

  const headers = buildBrowserHeaders();
  if (params["User-Agent"]) headers["User-Agent"] = params["User-Agent"];
  if (params["Referer"]) headers["Referer"] = params["Referer"];
  if (params["Origin"]) headers["Origin"] = params["Origin"];

  const isM3U8 = /\.m3u8(\?|$)/i.test(url);
  if (!isM3U8) {
    headers["Range"] = "bytes=0-8192";
  }

  try {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeout);

    const res = await fetch(url, {
      method: "GET",
      headers,
      signal: controller.signal,
      redirect: "follow",
    });

    clearTimeout(timer);

    if (res.status === 200 || res.status === 206) {
      try {
        const reader = res.body?.getReader();
        if (reader) {
          const { value } = await reader.read();
          reader.cancel().catch(() => {});
          return !!value && value.length > 0;
        }
      } catch {
        return true;
      }
      return true;
    }

    if ([301, 302, 307, 308].includes(res.status)) return true;
    if (res.status === 405) return true;

    return false;
  } catch {
    return false;
  }
}

// Famelack CDN fallback
const FAMELACK_DOMAINS = ["rnttwmjcin.turknet.ercdn.net"];
const FAMELACK_PREFIXES = ["lcpmvefbyo"];
const FAMELACK_QUALITIES = ["1080p", "720p", "576p"];

const CUSTOM_LINKS_FILE = path.join(__dirname, "..", "custom_links.json");
const IPTVORG_CHANNELS_URL =
  process.env.IPTVORG_CHANNELS_URL ||
  "https://iptv-org.github.io/api/channels.json";
const IPTVORG_LOGOS_URL =
  process.env.IPTVORG_LOGOS_URL || "https://iptv-org.github.io/api/logos.json";

const PROXY_BASE = (process.env.PROXY_BASE || "").replace(/\/+$/, "");

const HEADERS = {
  "content-type": "application/json; charset=utf-8",
  accept: "*/*",
  "accept-language": "en-US,en;q=0.9,tr;q=0.8",
  "cache-control": "no-cache",
  pragma: "no-cache",
  origin: "https://vavoo.to",
  referer: "https://vavoo.to/live",
  dnt: "1",
  "sec-ch-ua":
    '"Not=A?Brand";v="99", "Google Chrome";v="151", "Chromium";v="151"',
  "sec-ch-ua-mobile": "?0",
  "sec-ch-ua-platform": '"macOS"',
  "sec-fetch-dest": "empty",
  "sec-fetch-mode": "cors",
  "sec-fetch-site": "same-origin",
  "user-agent": STREAM_USER_AGENT,
};

function famelackVariants(name) {
  const clean = String(name || "")
    .toLowerCase()
    .replace(/^\s*(?:4k\s*tr|4k|tr|de|at|ch)\s*:\s*/i, "")
    .replace(/\s*\.(?:b|c|s)\b/gi, "")
    .replace(/\[[^\]]*\]/g, "")
    .replace(/\([^)]*\)/g, "")
    .replace(/\b(hd|fhd|uhd|4k|sd|hevc|h265|h264|raw)\b/gi, "")
    .replace(/\s+/g, " ")
    .trim();

  const variants = new Set();
  variants.add(clean);
  variants.add(clean.replace(/\s+/g, ""));
  variants.add(clean.replace(/\s+/g, "-"));

  const trMap = { ü: "u", ğ: "g", ş: "s", ı: "i", ö: "o", ç: "c" };
  let normalized = clean;
  for (const [old, neu] of Object.entries(trMap)) {
    normalized = normalized.replace(new RegExp(old, "g"), neu);
  }
  if (normalized !== clean) {
    variants.add(normalized);
    variants.add(normalized.replace(/\s+/g, ""));
  }

  return Array.from(variants).filter(Boolean);
}

async function tryFamelack(channelName) {
  const variants = famelackVariants(channelName).slice(0, 3);

  for (const domain of FAMELACK_DOMAINS) {
    for (const prefix of FAMELACK_PREFIXES) {
      for (const variant of variants) {
        for (const quality of FAMELACK_QUALITIES) {
          const url = `https://${domain}/${prefix}/${variant}/${variant}_${quality}.m3u8`;
          const ok = await checkLink(url, 4000);
          if (ok) return url;
        }
      }
    }
  }
  return null;
}

// ─── Custom Links (direct m3u8 URLs from custom_links.json) ──
let _customLinksIndex = null;

async function loadCustomLinks() {
  if (_customLinksIndex) return _customLinksIndex;
  try {
    const raw = await fs.readFile(CUSTOM_LINKS_FILE, "utf8");
    const data = JSON.parse(raw);
    _customLinksIndex = new Map();
    for (const [name, urls] of Object.entries(data)) {
      if (Array.isArray(urls) && urls.length > 0) {
        _customLinksIndex.set(name.toLowerCase().trim(), urls);
      }
    }
    console.log(`  Custom links loaded: ${_customLinksIndex.size} channels`);
    return _customLinksIndex;
  } catch {
    _customLinksIndex = new Map();
    return _customLinksIndex;
  }
}

async function tryCustomLinks(channelName) {
  const index = await loadCustomLinks();
  const normalized = channelName.toLowerCase()
    .replace(/\bhd\b|\bfhd\b|\buhd\b|\b4k\b|\bsraw\b|\bsb\b|\bs\b|\bc\b/g, "")
    .replace(/\s+/g, " ")
    .trim();

  // Exact match
  if (index.has(normalized)) {
    const urls = index.get(normalized);
    for (const url of urls) {
      if (await checkLink(url, 4000)) return url;
    }
  }

  // Partial match — channel name contains key or vice versa
  for (const [key, urls] of index) {
    if (normalized.includes(key) || key.includes(normalized)) {
      for (const url of urls) {
        if (await checkLink(url, 4000)) return url;
      }
    }
  }

  return null;
}

// ─── Famelack Data (curated TV streams from GitHub) ──────────
const FAMELACK_DATA_URL =
  "https://raw.githubusercontent.com/famelack/famelack-data/main/tv/raw/countries";
let _famelackDataIndex = null;

function normalizeChannelName(name) {
  let s = String(name || "")
    .toLowerCase()
    .replace(/^\s*(?:4k\s*tr|4k|tr|de|at|ch)\s*:\s*/i, "")
    .replace(/\s*\.(?:b|c|s)\b/gi, "")
    .replace(/\[[^\]]*\]/g, "")
    .replace(/\([^)]*\)/g, "")
    .replace(/\b(hd|fhd|uhd|4k|sd|hevc|h265|h264|raw)\b/gi, "")
    .replace(/\s+/g, " ")
    .trim();

  const trMap = { ü: "u", ğ: "g", ş: "s", ı: "i", ö: "o", ç: "c" };
  for (const [old, neu] of Object.entries(trMap)) {
    s = s.replace(new RegExp(old, "g"), neu);
  }

  return s;
}

async function loadFamelackData() {
  if (_famelackDataIndex) return _famelackDataIndex;

  console.log("  Fetching famelack-data index...");
  const countries = ["tr", "de"];
  _famelackDataIndex = new Map();

  for (const country of countries) {
    try {
      const url = `${FAMELACK_DATA_URL}/${country}.json`;
      const res = await fetch(url, { signal: AbortSignal.timeout(30000) });
      if (!res.ok) {
        console.warn(`    ${country.toUpperCase()}: HTTP ${res.status}`);
        continue;
      }

      const channels = await res.json();
      if (!Array.isArray(channels)) continue;

      for (const ch of channels) {
        if (!ch?.name || !ch?.sources?.streams?.length) continue;
        if (ch.isGeoBlocked) continue;

        const key = normalizeChannelName(ch.name);
        if (!key) continue;

        // Store streams array, skip if already exists
        if (!_famelackDataIndex.has(key)) {
          _famelackDataIndex.set(key, {
            name: ch.name,
            streams: ch.sources.streams,
            country: ch.country,
          });
        }
      }
      console.log(`    ${country.toUpperCase()}: ${channels.length} channels indexed`);
    } catch (err) {
      console.warn(`    ${country.toUpperCase()} fetch failed: ${err.message || err}`);
    }
  }

  console.log(`  Famelack-data loaded: ${_famelackDataIndex.size} channels`);
  return _famelackDataIndex;
}

async function tryFamelackData(channelName) {
  const index = await loadFamelackData();
  if (index.size === 0) return null;

  const normalized = normalizeChannelName(channelName);

  // Exact match
  if (index.has(normalized)) {
    const entry = index.get(normalized);
    for (const url of entry.streams) {
      if (await checkLink(url, 4000)) return url;
    }
  }

  // Partial match
  for (const [key, entry] of index) {
    if (normalized.includes(key) || key.includes(normalized)) {
      for (const url of entry.streams) {
        if (await checkLink(url, 4000)) return url;
      }
    }
  }

  return null;
}

function getProxyUrl(item) {
  const id = item?.ids?.id;
  if (PROXY_BASE && id) return `${PROXY_BASE}/play/${id}`;
  return null;
}

async function repairLink(item) {
  const originalUrl = item.url;
  const name = item.name || "";
  const vavooId = item?.ids?.id;

  // 1. For vavoo items: test the proxy URL
  if (vavooId && PROXY_BASE) {
    const proxyUrl = `${PROXY_BASE}/play/${vavooId}`;
    if (await checkLink(proxyUrl)) {
      return { url: proxyUrl, status: "ok" };
    }
  }

  // 2. For direct URLs (or fallback): test original
  if (await checkLink(originalUrl)) {
    return { url: originalUrl, status: "ok" };
  }

  // 3. Try Famelack Data (curated TV streams from GitHub)
  const famelackDataUrl = await tryFamelackData(name);
  if (famelackDataUrl) {
    return { url: famelackDataUrl, status: "famelack-data" };
  }

  // 4. Try custom_links.json (direct m3u8 URLs)
  const customUrl = await tryCustomLinks(name);
  if (customUrl) {
    return { url: customUrl, status: "custom" };
  }

  // 5. Try Famelack CDN pattern
  const famelackUrl = await tryFamelack(name);
  if (famelackUrl) {
    return { url: famelackUrl, status: "famelack" };
  }

  // 6. Nothing works — keep original (Worker will try at runtime)
  return { url: originalUrl, status: "dead" };
}

async function loadLinkCache() {
  try {
    const raw = await fs.readFile(CACHE_FILE, "utf8");
    const data = JSON.parse(raw);
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
  console.log("\n═══════════════════════════════════════════════════════");
  console.log("LINK CHECKER STARTING");
  console.log("═══════════════════════════════════════════════════════");
  console.log(`${items.length} channels total`);
  console.log(`Vavoo channels: tested via proxy URL`);
  console.log(`Fallback sources: Famelack Data → Custom Links → Famelack CDN`);
  console.log(`Concurrency: ${CHECK_CONCURRENCY} | Timeout: ${CHECK_TIMEOUT_MS}ms`);

  const cache = await loadLinkCache();
  const now = Date.now();

  let okCount = 0;
  let proxyCount = 0;
  let famelackDataCount = 0;
  let customCount = 0;
  let famelackCount = 0;
  let deadCount = 0;
  let cacheHits = 0;
  let processed = 0;

  const results = new Array(items.length);

  for (let i = 0; i < items.length; i += CHECK_CONCURRENCY) {
    const batch = [];
    for (let j = i; j < Math.min(i + CHECK_CONCURRENCY, items.length); j++) {
      batch.push({ index: j, item: items[j] });
    }

    const batchResults = await Promise.all(
      batch.map(async ({ index, item }) => {
        const cacheKey = item.url;
        const cached = cache[cacheKey];

        if (
          cached &&
          cached.timestamp &&
          now - cached.timestamp < CACHE_TTL_MS
        ) {
          cacheHits++;
          return { index, result: { url: cached.url, status: cached.status } };
        }

        const result = await repairLink(item);

        cache[cacheKey] = {
          url: result.url,
          status: result.status,
          timestamp: now,
        };

        return { index, result };
      })
    );

    for (const { index, result } of batchResults) {
      results[index] = result;

      switch (result.status) {
        case "ok":
          okCount++;
          break;
        case "proxy":
          proxyCount++;
          break;
        case "famelack-data":
          famelackDataCount++;
          break;
        case "custom":
          customCount++;
          break;
        case "famelack":
          famelackCount++;
          break;
        case "dead":
          deadCount++;
          break;
      }
    }

    processed += batch.length;
    const pct = Math.round((processed / items.length) * 100);
    if (processed % (CHECK_CONCURRENCY * 5) === 0 || processed === items.length) {
      console.log(
        `  [${pct}%] ${processed}/${items.length} | ok:${okCount} proxy:${proxyCount} famelack-data:${famelackDataCount} custom:${customCount} famelack:${famelackCount} dead:${deadCount} cached:${cacheHits}`
      );
    }
  }

  await saveLinkCache(cache);

  for (let i = 0; i < items.length; i++) {
    const r = results[i];
    if (!r) continue;
    items[i].url = r.url;
    items[i]._repairStatus = r.status;
    items[i]._repaired = r.status === "proxy" || r.status === "famelack-data" || r.status === "famelack" || r.status === "custom";
  }

  console.log("\n═══════════════════════════════════════════════════════");
  console.log("LINK CHECKER RESULTS");
  console.log("═══════════════════════════════════════════════════════");
  console.log(`Original OK:        ${okCount}`);
  console.log(`Via Proxy:          ${proxyCount}`);
  console.log(`Via Famelack Data:  ${famelackDataCount}`);
  console.log(`Via Custom Links:   ${customCount}`);
  console.log(`Via Famelack CDN:   ${famelackCount}`);
  console.log(`Still dead:         ${deadCount}`);
  console.log(`From cache:         ${cacheHits}`);
  console.log(`Repaired:           ${proxyCount + famelackDataCount + customCount + famelackCount}`);
  console.log("═══════════════════════════════════════════════════════\n");

  return items;
}

// ═══════════════════════════════════════════════════════════════
// VAVOO API
// ═══════════════════════════════════════════════════════════════

function isAllowedGermanChannel(channelName) {
  return true;
}

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
      if (!res.ok) {
        throw new Error(`HTTP ${res.status} ${res.statusText}`);
      }
      const data = await res.json();
      if (data && data.error) {
        throw new Error(`Vavoo error: ${data.error}`);
      }
      return data;
    } catch (err) {
      lastErr = err;
      const wait = 1000 * attempt;
      console.warn(
        `[${group}] Attempt ${attempt} failed (${err.message}). Retrying in ${wait}ms...`
      );
      await new Promise((r) => setTimeout(r, wait));
    }
  }
  throw lastErr;
}

async function fetchAllForGroup(group) {
  const items = [];
  let cursor = null;
  let page = 0;
  const MAX_PAGES = 200;

  do {
    page++;
    const data = await fetchPage(group, cursor);
    if (Array.isArray(data.items)) {
      for (const item of data.items) {
        if (group === "Germany") {
          if (isAllowedGermanChannel(item.name)) {
            items.push(item);
          }
        } else {
          items.push(item);
        }
      }
    }
    console.log(
      `Group ${group} - Page ${page}: fetched ${data.items?.length ?? 0} items, added ${items.length} total.`
    );
    cursor = data.nextCursor ?? null;
    if (page >= MAX_PAGES) {
      console.warn(`[${group}] Reached MAX_PAGES (${MAX_PAGES}), stopping.`);
      break;
    }
  } while (cursor !== null && cursor !== undefined);

  return items;
}

async function fetchAll() {
  const allItems = [];
  const seenIds = new Set();

  for (const group of GROUPS) {
    console.log(`Fetching catalog for group="${group}"...`);
    const groupItems = await fetchAllForGroup(group);
    for (const item of groupItems) {
      const itemId = item?.ids?.id;
      if (itemId && !seenIds.has(itemId)) {
        seenIds.add(itemId);
        allItems.push(item);
      }
    }
  }

  return allItems;
}

// ═══════════════════════════════════════════════════════════════
// CATEGORIES
// ═══════════════════════════════════════════════════════════════

function normalizeForCategory(name) {
  let s = String(name || "")
    .replace(/^\s*(?:4K TR:|DE:|AT:|CH:)\s*/i, "")
    .replace(/\s+(?:UHD|FHD|HD\+|HD|SD|HEVC|RAW|H265|H\.265|FEED)(?=\s|$)/gi, " ")
    .replace(/\s*\.(?:b|c|s)\b/gi, "")
    .replace(/\s+/g, " ")
    .trim();

  s = s
    .replace(/\bT RK\b/g, "TURK")
    .replace(/\bT RKIYEM\b/g, "TURKIYEM")
    .replace(/\bBENG\b/g, "BENGU")
    .replace(/\bBENGT\b/g, "BENGUT")
    .replace(/\bAK T\b/g, "AKIT")
    .replace(/\bS NEMA\b/g, "SINEMA")
    .replace(/\bM N KA\b/g, "MINIKA")
    .replace(/\bOCUK\b/g, "COCUK")
    .replace(/\bM Z K\b/g, "MUZIK")
    .replace(/\bS ZC\b/g, "SOZCU")
    .replace(/\bSZC\b/g, "SOZCU")
    .replace(/\bLKE\b/g, "ULKE")
    .replace(/\bYE IL AM\b/g, "YESILCAM")
    .replace(/\bYE IL[ ]?CAM\b/g, "YESILCAM")
    .replace(/\bT[ÜU]RK\b/gi, "TURK");

  return s;
}

const CATEGORY_RULES = [
  {
    name: "Almanya Sport",
    re: /\b(DAZN|SKY SPORT|SKY BULI|SKY BUNDESLIGA|SKY PREMIER|BUNDESLIGA|PREMIER LEAGUE|MAGENTA SPORT|MAGENTASPORT|MAGENTA FUSSBALL|SPORT1|EUROSPORT|FUSSBALL|SPORTS TV|SPORTDIGITAL|RED BULL TV|BLUE SPORT|SKY SPORT MIX|SKY SPORT NEWS|SKY F1|SKY FORMEL 1|SPORTDEUTSCHLAND|LAOLA1)\b/i,
  },
  {
    name: "Almanya TV",
    re: /\b(ARD|ZDF|ZDF NEO|RTL|RTL PLUS|RTL\+|RTL PASSION|RTL NITRO|PRO SIEBEN|PRO7|SAT\.1|SAT1|VOX|KABEL 1|KABEL 1 DOKU|SUPER RTL|NICKELODEON DE|NICK DE|NDR|WDR|MDR|BR|SWR|HR|RBB|SR|PHOENIX|TAGESSCHAU24|WELT|N24|N-TV|TELE 5|SIXX|DISNEY CHANNEL DE|TOGGO|KIKA|DEUTSCH|GERMAN|DEUTSCHLAND|DAS ERSTE|ONE|ARTE|3SAT|ZDF INFO|ZDF KULTUR|WDR|BR ALPHA|ARD ALPHA|RTL ZWEI|RTL2)\b/i,
  },
  {
    name: "Ulusal",
    re: /\b(TRT|MECLİS|TABII|SHOW|STAR|ATV|KANAL D|NOW TV|EXXEN|TV ?8|TEVE 2|BEYAZ|360|SKY 360|A2 TV|EURO D|KANAL 7|DMAX TURKIYE|BENGUTÜRK|ULUSAL|KANAL|TÜRK|TURK)\b/i,
  },
  {
    name: "Haber",
    re: /\b(HABER|NEWS|CNN|NTV|A HABER|BLOOMBERG|HALK TV|SÖZCÜ|LIDER|FLASH|GLOBAL|TV 100|TGRT HABER|ÜLKE|DHA|KANAL B|KANAL 24|TV NET|AKIT|ANADOLU|HABERTÜRK|HABER GLOBAL|KANAL AVRUPA|BENGUTÜRK|TÜRK HABER)\b/i,
  },
  {
    name: "Belgesel",
    re: /\b(BELGESEL|DOKU|DOCU|DISCOVERY|NATIONAL GEOGRAPHIC|NAT GEO|HISTORY|ANIMAL PLANET|BBC EARTH|TLC|TRT BELGESEL|TGRT BELGESEL|VIASAT|DA VINCI|DOCUBOX|FASHION|BEIN IZ|GURME|DOCUMENTARY|WILD)\b/i,
  },
  {
    name: "Spor",
    re: /\b(SPOR|SPORT|A SPOR|TRT SPOR|S SPORT|TIVIBU|TABII SPOR|BEIN SPORTS|NBA|EXXEN SPORTS|FB TV|GS TV|SPOR SMART|BASKETBOL|VOLEYBOL|TENIS|FUTBOL|IDMAN|EXXEN SPO)\b/i,
  },
  {
    name: "Çocuk",
    re: /\b(ÇOCUK|COCUK|KINDER|KIDS|CARTOON|DISNEY|NICK|BABY|MINIKA|TOGGO|TRT ÇOCUK|BABY TV|NICK JR|NICKELODEON|DISNEY CHANNEL)\b/i,
  },
  {
    name: "Film",
    re: /\b(SINEMA|CINEMA|MOVIE|FILM|YESILCAM|BOX OFFICE|FX|SHOWMAX|KINGBOX|ARENA BOX|BEIN MOVIES|MOVIEMAX|MOVIESMART|SINEVIZYON|SINEMAX|PROTURK)\b/i,
  },
  {
    name: "Dini",
    re: /\b(DİYANET|AKIT|MEHTAP|HİLAL|KUDUS|SEMERKAND|MERCAN|VUSLAT|KARDELEN|DOST TV|YOL TV|TVNET|DINI|İSLAM|KURAN|KUR'AN)\b/i,
  }
];

function categorize(name) {
  const s = normalizeForCategory(name);
  for (const rule of CATEGORY_RULES) {
    if (rule.re.test(s)) return rule.name;
  }
  return "Sonstige";
}

// ═══════════════════════════════════════════════════════════════
// M3U GENERATION (optimized for Televizo)
// ═══════════════════════════════════════════════════════════════

function escapeAttr(value) {
  return String(value ?? "")
    .replace(/\r?\n/g, " ")
    .replace(/"/g, "'");
}

function sanitizeName(name) {
  return String(name ?? "")
    .replace(/\r?\n/g, " ")
    .trim();
}

function toStreamUrl(item) {
  // If repair already set a working URL (proxy or famelack), use it
  if (item._repaired && item.url) return item.url;

  // Vavoo items → proxy URL
  const id = item?.ids?.id;
  if (PROXY_BASE && id) return `${PROXY_BASE}/play/${id}`;

  // Direct URL fallback
  return item.url;
}

function toM3U(items, logoResolver) {
  const lines = ["#EXTM3U"];

  for (const it of items) {
    if (!it || !it.url) continue;
    const name = sanitizeName(it.name);
    if (!name) continue;

    const group = categorize(name);
    if (!group) continue;

    const logo = resolveLogo(name, it.logo, logoResolver);
    const streamUrl = toStreamUrl(it);
    const repairAttr = it._repaired ? ` repair="${it._repairStatus}"` : "";

    lines.push(
      `#EXTINF:-1 tvg-name="${escapeAttr(name)}" tvg-logo="${escapeAttr(logo)}" group-title="${escapeAttr(group)}"${repairAttr},${name}`
    );

    // Televizo / VLC optimization: set network caching for smoother playback
    lines.push(`#EXTVLCOPT:network-caching=1000`);
    lines.push(`#EXTVLCOPT:live-caching=1000`);

    lines.push(streamUrl);
  }

  lines.push("");
  return lines.join("\n");
}

function resolveLogo(name, vavooLogo, logoResolver) {
  if (logoResolver) {
    const l = logoResolver(name);
    if (l) return l;
  }
  return vavooLogo || "";
}

// ═══════════════════════════════════════════════════════════════
// IPTV-ORG LOGOS
// ═══════════════════════════════════════════════════════════════

function normalizeForMatch(name) {
  let s = String(name || "")
    .toUpperCase()
    .replace(/^\s*(?:4K\s*TR:|4K:|TR:|DE:|AT:|CH:)\s*/i, "")
    .replace(/\s*\.(?:B|C|S)\b/gi, "")
    .replace(/\[[^\]]*\]/g, " ")
    .replace(/\([^\)]*\)/g, " ")
    .replace(/\bT RK\b/g, "TURK")
    .replace(/\bAK T\b/g, "AKIT")
    .replace(/\bS NEMA\b/g, "SINEMA")
    .replace(/\bM N KA\b/g, "MINIKA")
    .replace(/\bOCUK\b/g, "COCUK")
    .replace(/\bM Z K\b/g, "MUZIK")
    .replace(/\bBENG\b/g, "BENGU");
  s = s
    .replace(/[İI]/g, "I")
    .replace(/Ü/g, "U")
    .replace(/Ö/g, "O")
    .replace(/Ç/g, "C")
    .replace(/Ş/g, "S")
    .replace(/Ğ/g, "G")
    .replace(/[^A-Z0-9\s]/g, " ")
    .replace(/\s+/g, " ")
    .trim();
  return s;
}

function normalizeStripQuality(s) {
  return s
    .replace(/\b(?:UHD|FHD|HD\+|HD|SD|HEVC|RAW|H265|4K|8K|FEED|LIVE|BACKUP)\b/g, "")
    .replace(/\s+/g, " ")
    .trim();
}

async function fetchJson(url) {
  const res = await fetch(url, { signal: AbortSignal.timeout(60000) });
  if (!res.ok) throw new Error(`${url} → HTTP ${res.status}`);
  return res.json();
}

async function buildLogoIndex() {
  const [channels, logos] = await Promise.all([
    fetchJson(IPTVORG_CHANNELS_URL),
    fetchJson(IPTVORG_LOGOS_URL),
  ]);
  const trChannels = channels.filter((c) => c && (c.country === "TR" || c.country === "DE"));
  const trIds = new Set(trChannels.map((c) => c.id));

  const chosen = new Map();
  for (const l of logos) {
    if (!l || !trIds.has(l.channel) || !l.url) continue;
    const current = chosen.get(l.channel);
    if (!current || (l.in_use && !current.in_use)) {
      chosen.set(l.channel, l);
    }
  }

  const idx = new Map();
  for (const c of trChannels) {
    const l = chosen.get(c.id);
    if (!l) continue;
    const names = [c.name, ...(Array.isArray(c.alt_names) ? c.alt_names : [])];
    for (const n of names) {
      if (!n) continue;
      const k1 = normalizeForMatch(n);
      const k2 = normalizeStripQuality(k1);
      if (k1 && !idx.has(k1)) idx.set(k1, l.url);
      if (k2 && !idx.has(k2)) idx.set(k2, l.url);
    }
  }
  return idx;
}

function makeLogoResolver(idx) {
  if (!idx || idx.size === 0) return null;
  return (vavooName) => {
    const k1 = normalizeForMatch(vavooName);
    if (idx.has(k1)) return idx.get(k1);
    const k2 = normalizeStripQuality(k1);
    if (idx.has(k2)) return idx.get(k2);
    return "";
  };
}

// ═══════════════════════════════════════════════════════════════
// MAIN
// ═══════════════════════════════════════════════════════════════

async function main() {
  const startTime = Date.now();

  console.log(`Fetching groups=${JSON.stringify(GROUPS)} from ${CATALOG_URL} ...`);
  if (PROXY_BASE) {
    console.log(`Using PROXY_BASE=${PROXY_BASE}`);
  } else {
    console.warn(
      "WARNING: PROXY_BASE is empty. Raw vavoo.to URLs will be written; players without VPN may fail."
    );
  }

  const items = await fetchAll();
  console.log(`Total fetched items combined: ${items.length}`);

  items.sort((a, b) => {
    const an = String(a.name ?? "").toLocaleLowerCase("tr-TR");
    const bn = String(b.name ?? "").toLocaleLowerCase("tr-TR");
    if (an < bn) return -1;
    if (an > bn) return 1;
    const ai = a.ids?.id ?? "";
    const bi = b.ids?.id ?? "";
    return ai < bi ? -1 : ai > bi ? 1 : 0;
  });

  let logoIdx = new Map();
  try {
    logoIdx = await buildLogoIndex();
  } catch (err) {
    console.warn(`Logo index unavailable (${err.message}); logos will be empty.`);
  }
  const logoResolver = makeLogoResolver(logoIdx);

  let finalItems = items;
  if (CHECK_ENABLED) {
    try {
      // Pre-load famelack-data index before starting concurrent repairs
      await loadFamelackData();
      finalItems = await repairAll(items);
    } catch (err) {
      console.warn(`Link-Checker failed: ${err.message}`);
      console.warn("   Using original links.");
      finalItems = items;
    }
  } else {
    console.log("Link-Checker disabled (CHECK_ENABLED=false)");
  }

  const m3u = toM3U(finalItems, logoResolver);
  await fs.writeFile(M3U_FILE, m3u, "utf8");
  console.log(`Wrote ${M3U_FILE} (${m3u.length} bytes)`);

  const dist = new Map();
  for (const it of finalItems) {
    const name = sanitizeName(it?.name);
    if (!name) continue;
    const c = categorize(name);
    if (c) {
      dist.set(c, (dist.get(c) || 0) + 1);
    }
  }
  console.log("\nActive category distribution:");
  for (const [c, n] of [...dist.entries()].sort((a, b) => b[1] - a[1])) {
    console.log(`  ${c.padEnd(20)}: ${n}`);
  }

  const repairedCount = finalItems.filter((it) => it._repaired).length;
  if (repairedCount > 0) {
    console.log(`\n${repairedCount} links were automatically repaired!`);
  }

  const duration = ((Date.now() - startTime) / 1000).toFixed(1);
  console.log(`\nTotal duration: ${duration}s`);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
