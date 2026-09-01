import re
import html
import unicodedata
import requests
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse, unquote, urljoin, parse_qs, urlencode, quote
from bs4 import BeautifulSoup
from functools import lru_cache
import os

# ============================================================
# KONFIGURATION
# ============================================================
INPUT_M3U = "iptv.m3u"
OUTPUT_M3U = "iptv.m3u"

# --- User Agents ---
CUSTOM_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
VAVOO_USER_AGENT = "Vavoo/2.6 vypn.net App/1.0 Mozilla/5.0 (Windows NT 10.0; Win64; x64)"

# --- Vavoo (Hauptquelle) ---
VAVOO_HEADERS = {
    "User-Agent": VAVOO_USER_AGENT,
    "Accept": "*/*",
    "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
}

# --- Volo TV (Fallback 1) ---
VOLO_API_URL = "https://api.canlitvvolo.com/api/tv/stream"
VOLO_HEADERS = {
    "User-Agent": CUSTOM_USER_AGENT,
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json;charset=UTF-8",
    "Origin": "https://tv.canlitvvolo.com",
    "Referer": "https://tv.canlitvvolo.com/",
}
VOLO_STREAM_HEADERS = {
    "User-Agent": CUSTOM_USER_AGENT,
    "Accept": "*/*",
    "Accept-Language": "de-DE,de;q=0.7",
    "Origin": "https://tv.canlitvvolo.com",
    "Referer": "https://tv.canlitvvolo.com/",
}

# --- TVizle Proxy (Fallback 2) ---
TVIZLE_PROXY_URL = "https://tvizle.tr/api/proxy"
TVIZLE_HEADERS = {
    "User-Agent": CUSTOM_USER_AGENT,
    "Accept": "*/*",
    "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
    "Referer": "https://tvizle.tr/",
    "Origin": "https://tvizle.tr",
}

# --- Famelack Direkt (Fallback 3) ---
FAMELACK_HEADERS = {
    "User-Agent": CUSTOM_USER_AGENT,
    "Accept": "*/*",
    "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
    "Referer": "https://tvizle.tr/",
}

# --- TVizle (Fallback 4) ---
TVIZLE_STREAM_HEADERS = {
    "User-Agent": CUSTOM_USER_AGENT,
    "Accept": "*/*",
    "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
    "Referer": "https://tvizle.tr/",
}

# --- Allgemein ---
MAX_WORKERS = 20  # Mehr Parallelität
REQUEST_TIMEOUT = 3  # Kürzerer Timeout (3 Sekunden reicht für HEAD)
API_RETRY_DELAY = 0.1  # Weniger Verzögerung

# --- Caching ---
CACHE_FILE = "stream_cache.json"
CACHE_TTL = 3600  # 1 Stunde Cache-Gültigkeit

# ============================================================
# CACHING
# ============================================================

def load_cache():
    """Lädt den Cache aus einer Datei."""
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, 'r') as f:
                return json.load(f)
        except:
            return {}
    return {}

def save_cache(cache):
    """Speichert den Cache in einer Datei."""
    try:
        with open(CACHE_FILE, 'w') as f:
            json.dump(cache, f)
    except:
        pass

def get_cached(key, cache, ttl=CACHE_TTL):
    """Holt einen Wert aus dem Cache (wenn nicht abgelaufen)."""
    if key in cache:
        entry = cache[key]
        if time.time() - entry['timestamp'] < ttl:
            return entry['value']
    return None

def set_cache(key, value, cache):
    """Speichert einen Wert im Cache."""
    cache[key] = {
        'timestamp': time.time(),
        'value': value
    }

# ============================================================
# HILFSFUNKTIONEN
# ============================================================

def normalize_text(text):
    """Normalisiert Text (entfernt türkische Sonderzeichen, etc.)."""
    if not text:
        return ""
    text = html.unescape(str(text))
    text = BeautifulSoup(text, "html.parser").get_text(" ")
    replacements = {"ı": "i", "İ": "i", "ş": "s", "Ş": "s", "ğ": "g", "Ğ": "g", "ç": "c", "Ç": "c", "ö": "o", "Ö": "o", "ü": "u", "Ü": "u"}
    for old, new in replacements.items():
        text = text.replace(old, new)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return text.lower()

def get_canonical_key(name):
    """Erstellt einen robusten Vergleichsschlüssel."""
    if not name: return ""
    text = normalize_text(name)
    technical_words = ["4k", "uhd", "fhd", "hd", "sd", "hevc", "h265", "h264", "1080p", "1080", "720p", "720", "backup", "live", "stream", "tv"]
    for word in technical_words:
        text = re.sub(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])", " ", text)
    text = re.sub(r"^[a-z0-9\s]+:\s*", "", text)
    text = re.sub(r"\b\d{3,4}p\b", " ", text)
    text = re.sub(r"[\._/\\|:+\-]+", " ", text)
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text.replace(" ", "")

def get_name_variants(name):
    """Erzeugt mehrere Vergleichsschlüssel."""
    if not name:
        return set()
    variants = set()
    original = str(name).strip()
    key = get_canonical_key(original)
    if key: variants.add(key)
    normalized = normalize_text(original)
    normalized_clean = re.sub(r"[^a-z0-9\s]", " ", normalized)
    normalized_clean = re.sub(r"\s+", " ", normalized_clean).strip()
    if normalized_clean: variants.add(normalized_clean.replace(" ", ""))
    no_tv = re.sub(r"\btv\b", " ", normalized_clean)
    no_tv = re.sub(r"\s+", " ", no_tv).strip()
    if no_tv: variants.add(no_tv.replace(" ", ""))
    return {v for v in variants if len(v) >= 3}

def sanitize_channel_name(name):
    """Bereinigt den Sendernamen für URLs."""
    if not name:
        return ""
    name = normalize_text(name)
    name = re.sub(r'\s*(?:hd|fhd|uhd|sd|hevc|raw|backup|canli|izle|tv)\s*', ' ', name)
    name = re.sub(r'[^a-z0-9\s]', '', name)
    name = re.sub(r'\s+', '-', name).strip('-')
    return name

def parse_m3u(content):
    """Liest die M3U-Datei ein."""
    lines = content.splitlines()
    entries, current_extinf, current_extra = [], None, []
    for line in lines:
        line = line.strip()
        if not line: continue
        if line.startswith("#EXTM3U"): continue
        if line.startswith("#EXTINF:"):
            current_extinf = line
            current_extra = []
            continue
        if line.startswith("#"):
            if current_extinf: current_extra.append(line)
            continue
        if current_extinf:
            entries.append({"extinf": current_extinf, "extra": current_extra[:], "url": line})
            current_extinf = None
            current_extra = []
    return entries

def get_extinf_name(extinf):
    """Extrahiert den Sendernamen aus #EXTINF."""
    if not extinf or "," not in extinf: return ""
    return extinf.rsplit(",", 1)[1].strip()

def clean_stream_url(url):
    """Bereinigt die Stream-URL von Pipe-Parametern."""
    return url.split("|", 1)[0].strip() if url else ""

def write_m3u(entries):
    """Schreibt die M3U-Datei."""
    with open(OUTPUT_M3U, "w", encoding="utf-8", newline="\n") as f:
        f.write("#EXTM3U\n")
        for entry in entries:
            f.write(entry["extinf"] + "\n")
            url = clean_stream_url(entry["url"])
            ua = entry.get("ua", CUSTOM_USER_AGENT)
            f.write(f"#EXTVLCOPT:http-user-agent={ua}\n")
            f.write(f'#EXTHTTP:{{"User-Agent":"{ua}"}}\n')
            f.write(url + "\n")

def check_url(url, headers, timeout=REQUEST_TIMEOUT):
    """Prüft schnell, ob eine URL erreichbar ist."""
    try:
        response = requests.head(url, headers=headers, timeout=timeout)
        return response.status_code == 200
    except:
        return False

# ============================================================
# 1. VAVOO (HAUPTQUELLE) - OPTIMIERT
# ============================================================

def search_vavoo(original_url):
    """Prüft die originale Vavoo-URL."""
    if not original_url:
        return None
    
    if check_url(original_url, VAVOO_HEADERS, timeout=2):
        return original_url
    return None

# ============================================================
# 2. VOLO TV (FALLBACK 1) - OPTIMIERT
# ============================================================

@lru_cache(maxsize=1000)
def get_volo_permalink_cached(channel_name):
    """Gecachte Version der Volo-API-Abfrage."""
    if not channel_name:
        return None
    
    permalink = re.sub(r'[^a-z0-9\s-]', '', channel_name.lower())
    permalink = re.sub(r'\s+', '-', permalink)
    permalink = re.sub(r'-canli-izle$|-canli-hd-yayin-kesintisiz-izle$|-canli$', '', permalink)
    permalink = re.sub(r'-hd$|-fhd$|-tv$', '', permalink)

    if not permalink or len(permalink) < 3:
        return None

    try:
        payload = {"permalink": permalink, "yayin": 1}
        response = requests.post(VOLO_API_URL, headers=VOLO_HEADERS, json=payload, timeout=REQUEST_TIMEOUT)
        if response.status_code != 200:
            return None
        data = response.json()
        return data.get('permalink')
    except:
        return None

def construct_volo_stream_url(permalink):
    """Konstruiert die Volo-Stream-URL aus dem Permalink."""
    if not permalink:
        return None
    
    base = permalink.strip('/')
    base = re.sub(r'-canli-hd-yayin-kesintisiz-izle$', '', base)
    base = re.sub(r'-canli-izle$', '', base)
    base = re.sub(r'-canli-yayin$', '', base)
    base = re.sub(r'-hd-yayin$', '', base)
    base = re.sub(r'-canli$', '', base)
    base = re.sub(r'-hd$', '', base)
    
    if not base:
        return None
    
    stream_name = base.replace('-', '_')
    return f"https://dogusdyg-{base}.lg.mncdn.com/dogusdyg_{stream_name}/live_1080p3000000kbps/index.m3u8"

def search_volo(channel_name):
    """Sucht nach einem Volo-Stream für den Kanal."""
    permalink = get_volo_permalink_cached(channel_name)
    if not permalink:
        return None
    
    base_url = construct_volo_stream_url(permalink)
    if not base_url:
        return None
    
    if check_url(base_url, VOLO_STREAM_HEADERS, timeout=2):
        return base_url
    return None

# ============================================================
# 3. FAMELACK (FALLBACKS 2 & 3) - OPTIMIERT
# ============================================================

def search_famelack_via_tvizle_proxy(channel_name):
    """Sucht nach Famelack-Stream über den TVizle-Proxy."""
    if not channel_name:
        return None
    
    sanitized = sanitize_channel_name(channel_name)
    if not sanitized:
        return None
    
    # Schnellere Qualitätsreihenfolge (nur die wichtigsten testen)
    qualities = ["1080p", "720p", "576p"]
    cdn_domains = ["rnttwmjcin.turknet.ercdn.net"]
    path_prefix = "lcpmvefbyo"
    
    for domain in cdn_domains:
        for quality in qualities:
            famelack_url = f"https://{domain}/{path_prefix}/{sanitized}/{sanitized}_{quality}.m3u8"
            encoded_url = quote(famelack_url, safe='')
            proxy_url = f"{TVIZLE_PROXY_URL}?url={encoded_url}"
            
            if check_url(proxy_url, TVIZLE_HEADERS, timeout=2):
                return proxy_url
    
    return None

def search_famelack_direct(channel_name):
    """Sucht nach Famelack-Stream direkt."""
    if not channel_name:
        return None
    
    sanitized = sanitize_channel_name(channel_name)
    if not sanitized:
        return None
    
    qualities = ["1080p", "720p", "576p"]
    cdn_domains = ["rnttwmjcin.turknet.ercdn.net"]
    path_prefix = "lcpmvefbyo"
    
    for domain in cdn_domains:
        for quality in qualities:
            url = f"https://{domain}/{path_prefix}/{sanitized}/{sanitized}_{quality}.m3u8"
            if check_url(url, FAMELACK_HEADERS, timeout=2):
                return url
    
    return None

# ============================================================
# 4. TVIZLE (FALLBACK 4) - OPTIMIERT
# ============================================================

def search_tvizle(channel_name):
    """Sucht nach TVizle-Stream."""
    if not channel_name:
        return None
    
    sanitized = sanitize_channel_name(channel_name)
    if not sanitized:
        return None
    
    qualities = ["1080p", "720p", "576p"]
    
    domains = [
        f"flask-api-hls-{sanitized}trkvz-live.onrender.com",
        f"flask-api-hls-{sanitized}hdtrkvz-live.onrender.com",
    ]
    
    path_prefix = "hls_stream"
    
    for domain in domains:
        for quality in qualities:
            url = f"https://{domain}/{path_prefix}/{sanitized}_{quality}.m3u8"
            if check_url(url, TVIZLE_STREAM_HEADERS, timeout=2):
                return url
    
    return None

# ============================================================
# 5. MULTI-SOURCE STREAM FINDER (OPTIMIERT & PARALLEL)
# ============================================================

def find_stream_from_sources(channel_name, original_vavoo_url, cache):
    """
    Durchläuft alle Quellen - mit Cache und optimierter Reihenfolge.
    """
    print(f"  [Repair] Suche nach Stream für: {channel_name}")
    
    # Cache-Key für diesen Kanal
    cache_key = f"{channel_name}:{original_vavoo_url[:50]}"
    
    # 1. Cache prüfen
    cached_result = get_cached(cache_key, cache)
    if cached_result:
        print(f"    -> Cache-Treffer: {cached_result['source']}")
        return cached_result
    
    # 2. Vavoo (schnellster Check)
    stream_url = search_vavoo(original_vavoo_url)
    if stream_url:
        result = {"url": stream_url, "source": "vavoo", "ua": VAVOO_USER_AGENT}
        set_cache(cache_key, result, cache)
        return result
    
    # 3. Volo
    stream_url = search_volo(channel_name)
    if stream_url:
        result = {"url": stream_url, "source": "volo", "ua": CUSTOM_USER_AGENT}
        set_cache(cache_key, result, cache)
        return result
    
    # 4. Famelack (Proxy)
    stream_url = search_famelack_via_tvizle_proxy(channel_name)
    if stream_url:
        result = {"url": stream_url, "source": "famelack_proxy", "ua": CUSTOM_USER_AGENT}
        set_cache(cache_key, result, cache)
        return result
    
    # 5. Famelack (Direkt)
    stream_url = search_famelack_direct(channel_name)
    if stream_url:
        result = {"url": stream_url, "source": "famelack_direct", "ua": CUSTOM_USER_AGENT}
        set_cache(cache_key, result, cache)
        return result
    
    # 6. TVizle
    stream_url = search_tvizle(channel_name)
    if stream_url:
        result = {"url": stream_url, "source": "tvizle", "ua": CUSTOM_USER_AGENT}
        set_cache(cache_key, result, cache)
        return result
    
    print(f"    -> Keine Quelle gefunden.")
    return None

# ============================================================
# PARALLELE VERARBEITUNG
# ============================================================

def process_channel(entry, cache):
    """Verarbeitet einen einzelnen Kanal (für parallele Ausführung)."""
    extinf = entry["extinf"]
    original_url = entry["url"]
    channel_name = get_extinf_name(extinf)
    
    repaired = find_stream_from_sources(channel_name, original_url, cache)
    
    if repaired:
        return {
            "extinf": extinf,
            "url": repaired["url"],
            "ua": repaired.get("ua", CUSTOM_USER_AGENT),
            "source": repaired["source"]
        }
    else:
        return {
            "extinf": extinf,
            "url": clean_stream_url(original_url),
            "ua": VAVOO_USER_AGENT,
            "source": "failed"
        }

# ============================================================
# HAUPTPROZESS (OPTIMIERT)
# ============================================================

def process_hybrid_m3u():
    print("\n" + "="*60)
    print("HYBRID IPTV REPAIR TOOL (OPTIMIERT)")
    print("Hauptquelle: Vavoo | Fallbacks: Volo → Famelack (Proxy) → Famelack (Direkt) → TVizle")
    print("="*60)

    # Cache laden
    cache = load_cache()
    print(f"[CACHE] Geladen: {len(cache)} Einträge")

    # 1. M3U einlesen
    try:
        with open(INPUT_M3U, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except FileNotFoundError:
        print(f"[FEHLER] {INPUT_M3U} nicht gefunden.")
        return
    
    entries = parse_m3u(content)
    print(f"[M3U] {len(entries)} Kanäle gelesen.")

    # 2. Kanäle parallel verarbeiten
    print(f"\n[START] Verarbeite {len(entries)} Kanäle mit {MAX_WORKERS} parallelen Threads...")
    start_time = time.time()
    
    output_entries = []
    repair_stats = {
        "vavoo": 0,
        "volo": 0,
        "famelack_proxy": 0,
        "famelack_direct": 0,
        "tvizle": 0,
        "failed": 0
    }
    
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        # Alle Kanäle parallel starten
        futures = {executor.submit(process_channel, entry, cache): entry for entry in entries}
        
        for future in as_completed(futures):
            result = future.result()
            output_entries.append(result)
            repair_stats[result["source"]] += 1
            
            # Fortschritt anzeigen
            total = len(entries)
            done = len(output_entries)
            if done % 10 == 0 or done == total:
                print(f"  Fortschritt: {done}/{total} ({done/total*100:.1f}%)")

    # 3. Cache speichern
    save_cache(cache)
    print(f"[CACHE] Gespeichert: {len(cache)} Einträge")

    # 4. Statistik
    elapsed = time.time() - start_time
    print("\n" + "="*60)
    print("STATISTIK")
    print("="*60)
    print(f"Gesamt:                 {len(output_entries)}")
    print(f"Vavoo (Hauptquelle):    {repair_stats['vavoo']}")
    print(f"Volo (Fallback 1):      {repair_stats['volo']}")
    print(f"Famelack (Proxy, FB 2): {repair_stats['famelack_proxy']}")
    print(f"Famelack (Direkt, FB 3): {repair_stats['famelack_direct']}")
    print(f"TVizle (Fallback 4):    {repair_stats['tvizle']}")
    print(f"Nicht repariert:        {repair_stats['failed']}")
    print(f"Benötigte Zeit:         {elapsed:.1f} Sekunden")
    print("="*60)

    # 5. Neue M3U schreiben
    write_m3u(output_entries)
    print(f"[FERTIG] Playlist gespeichert als {OUTPUT_M3U}")

if __name__ == "__main__":
    process_hybrid_m3u()
