"use strict";

const fs = require("node:fs/promises");
const path = require("node:path");
const zlib = require("node:zlib");

const CATALOG_URL = "https://vavoo.to/mediahubmx-catalog.json";

// Gruppen, die abgerufen werden sollen
const GROUPS = ["Turkey", "Germany"];

const M3U_FILE = path.join(__dirname, "..", "iptv.m3u");
const EPG_FILE = path.join(__dirname, "..", "epg.xml");
const FETCH_TIMEOUT_MS = 20000;

// Upstream EPG (ALL enthält DE, AT, CH, TR & Sport)
const EPG_UPSTREAM_URL =
  process.env.EPG_UPSTREAM_URL ||
  "https://epgshare01.online/epgshare01/epg_ripper_ALL.xml.gz";

// Optional directory of iptv-org/epg grab outputs (XMLTV per site).
const IPTVORG_GRAB_DIR = process.env.IPTVORG_GRAB_DIR || "";

// iptv-org public metadata for channel logos
const IPTVORG_CHANNELS_URL =
  process.env.IPTVORG_CHANNELS_URL ||
  "https://iptv-org.github.io/api/channels.json";
const IPTVORG_LOGOS_URL =
  process.env.IPTVORG_LOGOS_URL || "https://iptv-org.github.io/api/logos.json";

// Cloudflare Workers proxy base (no trailing slash)
const PROXY_BASE = (process.env.PROXY_BASE || "").replace(/\/+$/, "");

// Where players should fetch the generated XMLTV EPG
const EPG_URL =
  process.env.EPG_URL ||
  "https://raw.githubusercontent.com/kadirmetin/vavoo-iptv/main/epg.xml";

// Vavoo browser-like headers
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
  "user-agent":
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36",
};

// Whitelist für relevante deutsche Hauptsender & Sportkanäle
const GERMAN_ALLOWED_KEYWORDS = [
  // Hauptsender
  "RTL", "PROSIEBEN", "PRO7", "SAT.1", "SAT1", "VOX", "ZDF", "ARD", "DAS ERSTE",
  "NICKELODEON", "SUPER RTL", "RTL2", "RTL 2", "RTL II", "NITRO", "RTL NITRO",
  "RTL+", "RTL PLUS", "TELE 5", "SIXX", "PROSIEBEN MAXX", "KABEL EINS", "KABEL 1",
  "DMAX", "DISCOVERY", "WELT", "N24", "NTY", "N-TV", "PHOENIX", "TAGESSCHAU24",
  "DISNEY CHANNEL", "TOGGO", "KIKA",

  // Pay-TV & Cinema
  "SKY ATLANTIC", "SKY ONE", "SKY CRIME", "SKY CINEMA", "SKY REPLAY", "SKY SHOWCASE",
  "13TH STREET", "SYFY", "AXN", "WARNER TV", "TNT",

  // Sport (DE / AT / CH)
  "DAZN", "SKY SPORT", "SKY BULI", "BUNDESLIGA", "SPORT1", "EUROSPORT",
  "MAGENTA SPORT", "MAGENTASPORT", "ORF SPORT", "ORF 1", "ORF 2", "SRF ZWEI", "SRF 2",
  "SRF INFO", "BLUE SPORT", "SERVUSTV", "SERVUS TV", "RED BULL TV", "SPORTDIGITAL"
];

function isAllowedGermanChannel(channelName) {
  const nameUpper = String(channelName || "").toUpperCase();
  return GERMAN_ALLOWED_KEYWORDS.some((keyword) => nameUpper.includes(keyword));
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

// -- categorization --------------------------------------------------------

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

// Angepasste Kategorien (Ohne Diğer, Yaşam, Österreich/Schweiz, Dizi, Radyo)
const CATEGORY_RULES = [
  {
    name: "Sport",
    re: /DAZN|SKY SPORT|SKY BULI|BUNDESLIGA|EUROSPORT|MAGENTA SPORT|MAGENTASPORT|ORF SPORT|SRF ZWEI|SRF 2|BLUE SPORT|SPORT1|SPORTDIGITAL|BEIN SPO[RT]{0,3}S?|\bBEIN 1\b|S[- ]?SPORTS?|\bS SPORT\b|SPOR SMART|\bNBA\b|TJK TV|TIVIBU ?SPOR|TIVIBUSPOR|TRT SPOR|TABII SPOR|EXXEN SPO[RT]?|\bHT SPOR\b|EKOL SPOR|SPORTS TV|IDMAN TV|GALATASARAY TV|\bFB TV\b|\bGS TV\b|SARAN SPORT|SMART SPOR|\bSPOR\b|\bSPORT\b/i,
  },
  {
    name: "Deutschland",
    re: /\b(RTL|PROSIEBEN|PRO7|SAT\.1|SAT1|VOX|ZDF|ARD|DAS ERSTE|SUPER RTL|RTL2|RTL 2|RTL II|NITRO|RTL NITRO|RTL\+|RTL PLUS|TELE 5|SIXX|KABEL EINS|KABEL 1|WELT|N24|N-TV|PHOENIX|TAGESSCHAU24|TOGGO|SKY ATLANTIC|SKY ONE|SKY CRIME|SKY CINEMA|SKY REPLAY|SKY SHOWCASE|13TH STREET|SYFY|AXN|WARNER TV|TNT)\b/i,
  },
  {
    name: "Çocuk / Kinder",
    re: /CARTOON|BOOMERANG|DISNEY|NICK(?:ELODEON|TOONS|JR|JUNIOR|\b)|BABY ?TV|BABYTV|M[İI]?N ?KA|MINIKA|KIKA|TOGGO|TRT ?[ÇC]?OCUK|\bCOCUK\b|\b[ÇC]OCUK\b|DISNEY CHANNEL/i,
  },
  {
    name: "Belgesel / Doku",
    re: /DISCOVERY|NATIONAL GEOGRAPHIC|NAT ?GEO|\bHISTORY\b|ANIMAL PLANET|DA VINCI(?! KIDS)|VIASAT|BBC EARTH|LOVE NATURE|TRT BELGESEL|EPIC DRAMA|TARIH TV|TARIM TV|TGRT BELGESEL|INVESTIGATION|DMAX|DOCUBOX|DOCU SCREEN|SCIENCE|\bIZ TV\b|YABAN|OUTDOOR|CHASSE|ANIMAUX|AGRO TV|CIFTCI TV|REDBULL TV|\bTLC\b/i,
  },
  {
    name: "Film",
    re: /SINEMA|S[İI]NEMA|S NEMA|CINEMA|SINEMAX|SINEVIZYON|\bMOVIES?\b|MOVIEMAX|MOVIESMART|BEIN MOVIES|BEIN BOX|BOX OFFICE|\bFX\b|FX HD|YESILCAM|YE ?I ?L ?[ÇC] ?AM|YE ?I ?L ?AM|YEŞ?[İI]LC?AM|GLOBAL BOX|PROTURK|FIX CINEMA|KINGBOX|ARENA BOX|SHOWMAX|SHOW MAX|REAL BOX|SMART BOX/i,
  },
  {
    name: "Müzik",
    re: /POWER T[UÜ]RK|POWER ?TV|POWERTURK|KRAL POP|KRAL ?TV|\bKRAL\b|TRT M[UÜ]?Z[İI]?K|NR ?1|NUMBER ?1|NUMBER ONE|MTV LIVE|VINTAGE MUSIC|RETRO T ?RK|M[UÜ]Z[İI]K/i,
  },
  {
    name: "Haber",
    re: /\bHABER\b|\bNEWS\b|BLOOMBERG|\bCNN\b|EKOTURK|A ?PARA|APARA|HALK TV|TELE ?1|SOZCU|TRT WORLD|LIDER HABER|FLASH HABER|GLOBAL HABER|HABERT[UÜ]RK/i,
  },
  {
    name: "Dini",
    re: /D[İI]YANET|\bAK[İIY]?T\b|MEHTAP|H[İI]LAL|KUDUS|SEMERKAND|LALEGUL|MERCAN TV|VUSLAT|KARDELEN|DIYAR TV|\bDOST TV\b|\bYOL TV\b|\bKANAL 7\b|TVNET|TRT DIYANET/i,
  },
  {
    name: "Ulusal (TR)",
    re: /^24$|\bTRT\b|\bTRT 1\b|\bTRT ?2\b|TRT2|\bTRT 3\b|TRT AVAZ|TRT T[UÜ]RK|TRT KURD[İI]?|TRT WORLD|TRT 4K|\bKANAL D\b|\bATV\b|STAR TV|\bSTAR\b|SHOW TV|\bFOX\b|NOW ?TV|\bNOW\b|TV ?8|TV8[.,]5|BEYAZ TV|\b360\b|24 TV|\bA2\b|A HABER|TV ?100|TEVE2|CNN T[UÜ]RK|\bNTV\b/i,
  },
  {
];

function categorize(name) {
  const s = normalizeForCategory(name);
  for (const rule of CATEGORY_RULES) {
    if (rule.re.test(s)) return rule.name;
  }
  // Wenn der Sender in keine der erlaubten Kategorien passt -> null (wird verworfen)
  return null;
}

// -- M3U -------------------------------------------------------------------

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
  const id = item?.ids?.id;
  if (PROXY_BASE && id) return `${PROXY_BASE}/play/${id}`;
  return item.url;
}

function toM3U(items, vavooToEpgId, logoResolver) {
  const header = `#EXTM3U url-tvg="${escapeAttr(EPG_URL)}" x-tvg-url="${escapeAttr(EPG_URL)}"`;
  const lines = [header];
  for (const it of items) {
    if (!it || !it.url) continue;
    const name = sanitizeName(it.name);
    if (!name) continue;
    
    // Prüfen ob Kanal in eine der aktiven Kategorien fällt
    const group = categorize(name);
    if (!group) continue; // Verwirft Sender aus entfernten Kategorien (z.B. Radio, Dizi, etc.)

    const vavooId = it.ids?.id ?? "";
    const logo = resolveLogo(name, it.logo, logoResolver);
    const tvgId = (vavooToEpgId && vavooToEpgId.get(vavooId)) || vavooId;

    lines.push(
      `#EXTINF:-1 tvg-id="${escapeAttr(tvgId)}" tvg-name="${escapeAttr(name)}" tvg-logo="${escapeAttr(logo)}" group-title="${escapeAttr(group)}",${name}`
    );
    lines.push(toStreamUrl(it));
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

// -- XMLTV EPG -------------------------------------------------------------

function xmlEscape(v) {
  return String(v ?? "").replace(/[&<>"']/g, (c) =>
    c === "&"
      ? "&amp;"
      : c === "<"
        ? "&lt;"
        : c === ">"
          ? "&gt;"
          : c === '"'
            ? "&quot;"
            : "&apos;"
  );
}

function xmltvTime(sec) {
  const d = new Date(sec * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  return (
    `${d.getUTCFullYear()}${pad(d.getUTCMonth() + 1)}${pad(d.getUTCDate())}` +
    `${pad(d.getUTCHours())}${pad(d.getUTCMinutes())}${pad(d.getUTCSeconds())} +0000`
  );
}

// -- Upstream EPG ----------------------------------------------------------

async function fetchUpstreamXmltv(url) {
  const res = await fetch(url, { signal: AbortSignal.timeout(60000) });
  if (!res.ok) throw new Error(`upstream EPG HTTP ${res.status}`);
  const buf = Buffer.from(await res.arrayBuffer());
  const isGz =
    url.toLowerCase().endsWith(".gz") || (buf[0] === 0x1f && buf[1] === 0x8b);
  const bytes = isGz ? zlib.gunzipSync(buf) : buf;
  return bytes.toString("utf8");
}

async function loadGrabDir(dir) {
  const combined = { channels: new Map(), progByChannel: new Map() };
  if (!dir) return combined;
  let entries;
  try {
    entries = await fs.readdir(dir);
  } catch {
    return combined;
  }
  for (const f of entries) {
    if (!f.toLowerCase().endsWith(".xml")) continue;
    let xml;
    try {
      xml = await fs.readFile(path.join(dir, f), "utf8");
    } catch {
      continue;
    }
    const parsed = parseXmltv(xml);
    for (const [id, data] of parsed.channels) {
      if (!combined.channels.has(id)) combined.channels.set(id, data);
    }
    for (const p of parsed.programmes) {
      if (!combined.progByChannel.has(p.channel))
        combined.progByChannel.set(p.channel, []);
      combined.progByChannel.get(p.channel).push(p);
    }
  }
  return combined;
}

function parseXmltv(xml) {
  const channels = new Map();
  const programmes = [];

  const chRe = /<channel\s+id="([^"]+)"[^>]*>([\s\S]*?)<\/channel>/gi;
  for (const m of xml.matchAll(chRe)) {
    const id = m[1];
    const body = m[2];
    const names = [
      ...body.matchAll(/<display-name[^>]*>([^<]+)<\/display-name>/gi),
    ]
      .map((n) => n[1].trim())
      .filter(Boolean);
    const icon = body.match(/<icon\s+src="([^"]+)"/i)?.[1] || "";
    channels.set(id, { names, icon });
  }

  const prRe = /<programme\s+([^>]*)>([\s\S]*?)<\/programme>/gi;
  for (const m of xml.matchAll(prRe)) {
    const attrs = m[1];
    const body = m[2];
    const start = attrs.match(/start="([^"]+)"/i)?.[1];
    const stop = attrs.match(/stop="([^"]+)"/i)?.[1];
    const channel = attrs.match(/channel="([^"]+)"/i)?.[1];
    if (!start || !stop || !channel) continue;
    programmes.push({ start, stop, channel, body: body.trim() });
  }

  return { channels, programmes };
}

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

function buildMatchIndex(upstreamChannels) {
  const idx = new Map();
  for (const [id, data] of upstreamChannels) {
    for (const raw of data.names) {
      const k1 = normalizeForMatch(raw);
      const k2 = normalizeStripQuality(k1);
      if (k1 && !idx.has(k1)) idx.set(k1, id);
      if (k2 && !idx.has(k2)) idx.set(k2, id);
    }
  }
  return idx;
}

function matchUpstreamId(vavooName, idx) {
  const k1 = normalizeForMatch(vavooName);
  if (idx.has(k1)) return idx.get(k1);
  const k2 = normalizeStripQuality(k1);
  if (idx.has(k2)) return idx.get(k2);
  return null;
}

function toXMLTV(
  items,
  vavooToEpgId,
  idSource,
  grabChannels,
  grabProgByChannel,
  upstreamChannels,
  upstreamProgByChannel,
  logoResolver
) {
  const seenChannel = new Set();
  const channels = [];
  const programmes = [];

  for (const it of items) {
    const vavooId = it?.ids?.id;
    if (!vavooId) continue;
    const name = sanitizeName(it.name);
    if (!name) continue;

    // Nur EPG erzeugen für Sender, die in den aktiven Kategorien liegen
    if (!categorize(name)) continue;

    const routedId = vavooToEpgId.get(vavooId) || vavooId;
    if (seenChannel.has(routedId)) continue;
    seenChannel.add(routedId);

    const src = idSource.get(routedId) || "inline";
    let sourceCh = null;
    let sourceProgs = [];
    if (src === "grab") {
      sourceCh = grabChannels.get(routedId) || null;
      sourceProgs = grabProgByChannel.get(routedId) || [];
    } else if (src === "epgshare01") {
      sourceCh = upstreamChannels.get(routedId) || null;
      sourceProgs = upstreamProgByChannel.get(routedId) || [];
    }

    const displayName = sourceCh?.names?.[0] || name;
    const iptvorgLogo = logoResolver ? logoResolver(name) : "";
    const icon = iptvorgLogo || sourceCh?.icon || it.logo || "";
    const iconTag = icon ? `\n    <icon src="${xmlEscape(icon)}"/>` : "";
    channels.push(
      `  <channel id="${xmlEscape(routedId)}">\n` +
      `    <display-name>${xmlEscape(displayName)}</display-name>${iconTag}\n` +
      `  </channel>`
    );

    if (sourceProgs.length > 0) {
      for (const p of sourceProgs) {
        programmes.push(
          `  <programme start="${xmlEscape(p.start)}" stop="${xmlEscape(p.stop)}" channel="${xmlEscape(routedId)}">\n    ${p.body}\n  </programme>`
        );
      }
    } else if (Array.isArray(it.epg)) {
      for (const p of it.epg) {
        if (!p || typeof p.start !== "number" || typeof p.stop !== "number")
          continue;
        const title = String(p.name ?? "").trim();
        if (!title) continue;
        programmes.push(
          `  <programme start="${xmltvTime(p.start)}" stop="${xmltvTime(p.stop)}" channel="${xmlEscape(routedId)}">\n    <title>${xmlEscape(title)}</title>\n  </programme>`
        );
      }
    }
  }

  return (
    `<?xml version="1.0" encoding="UTF-8"?>\n` +
    `<tv generator-info-name="vavoo-iptv" generator-info-url="https://github.com/kadirmetin/vavoo-iptv">\n` +
    `${channels.join("\n")}\n` +
    `${programmes.join("\n")}\n` +
    `</tv>\n`
  );
}

// -- iptv-org logo index ---------------------------------------------------

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

async function main() {
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

  let upstreamChannels = new Map();
  let upstreamProgByChannel = new Map();
  try {
    console.log(`Loading upstream EPG from: ${EPG_UPSTREAM_URL}`);
    const xml = await fetchUpstreamXmltv(EPG_UPSTREAM_URL);
    const parsed = parseXmltv(xml);
    upstreamChannels = parsed.channels;
    for (const p of parsed.programmes) {
      if (!upstreamProgByChannel.has(p.channel))
        upstreamProgByChannel.set(p.channel, []);
      upstreamProgByChannel.get(p.channel).push(p);
    }
    console.log(`Upstream EPG loaded: ${upstreamChannels.size} channels, ${parsed.programmes.length} programmes`);
  } catch (err) {
    console.warn(
      `Upstream EPG unavailable (${err.message}); falling back to Vavoo inline EPG only.`
    );
  }

  const grab = await loadGrabDir(IPTVORG_GRAB_DIR);

  let logoIdx = new Map();
  try {
    logoIdx = await buildLogoIndex();
  } catch (err) {
    console.warn(`Logo index unavailable (${err.message}); logos will be empty.`);
  }
  const logoResolver = makeLogoResolver(logoIdx);

  const grabIdx = buildMatchIndex(grab.channels);
  const upstreamIdx = buildMatchIndex(upstreamChannels);
  const vavooToEpgId = new Map();
  const idSource = new Map();

  let matchedCount = 0;
  for (const it of items) {
    const vavooId = it?.ids?.id;
    if (!vavooId) continue;
    const name = sanitizeName(it.name);
    if (!name) continue;

    const grabId = matchUpstreamId(name, grabIdx);
    if (grabId) {
      vavooToEpgId.set(vavooId, grabId);
      idSource.set(grabId, "grab");
      matchedCount++;
    } else {
      const upstreamId = matchUpstreamId(name, upstreamIdx);
      if (upstreamId) {
        vavooToEpgId.set(vavooId, upstreamId);
        idSource.set(upstreamId, "epgshare01");
        matchedCount++;
      } else {
        vavooToEpgId.set(vavooId, vavooId);
      }
    }
  }

  console.log(`EPG Matching: ${matchedCount} / ${items.length} channels matched to EPG data.`);

  const m3u = toM3U(items, vavooToEpgId, logoResolver);
  await fs.writeFile(M3U_FILE, m3u, "utf8");
  console.log(`Wrote ${M3U_FILE} (${m3u.length} bytes)`);

  const epg = toXMLTV(
    items,
    vavooToEpgId,
    idSource,
    grab.channels,
    grab.progByChannel,
    upstreamChannels,
    upstreamProgByChannel,
    logoResolver
  );
  await fs.writeFile(EPG_FILE, epg, "utf8");
  console.log(`Wrote ${EPG_FILE} successfully.`);

  const dist = new Map();
  for (const it of items) {
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
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
