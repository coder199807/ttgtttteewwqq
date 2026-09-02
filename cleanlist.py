import re
import html
import unicodedata
import requests
import time
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from bs4 import BeautifulSoup

# ============================================================
# KONFIGURATION
# ============================================================
INPUT_M3U = "iptv.m3u"
OUTPUT_M3U = "iptv.m3u"
CUSTOM_LINKS_FILE = "custom_links.json"

CUSTOM_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
VAVOO_USER_AGENT = "Vavoo/2.6 vypn.net App/1.0 Mozilla/5.0 (Windows NT 10.0; Win64; x64)"

REQUEST_TIMEOUT = 10
CHECK_TIMEOUT = 8  # Erhöht
MAX_WORKERS = 10
CACHE_FILE = "stream_cache.json"
CACHE_TTL = 86400

# Bekannte funktionierende Vavoo-Proxies (falls der originale Link nicht geht)
VAVOO_PROXIES = [
    "https://vavoo-proxy.kadirmetin.workers.dev",
    "https://vavoo-proxy.vercel.app",
    "https://vavoo-proxy.netlify.app",
]

# ============================================================
# HILFSFUNKTIONEN
# ============================================================

def normalize_text(text):
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

def clean_channel_name(name):
    if not name:
        return ""
    text = str(name)
    text = re.sub(r'^(?:4K\s*TR:|4K:|TR:|DE:|AT:|CH:|VF:|HD\s*:)\s*', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\s*\.(?:b|c|s)\s*$', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\s*\(BACKUP\)\s*', ' ', text, flags=re.IGNORECASE)
    text = re.sub(r'\s*\(H265\)\s*', ' ', text, flags=re.IGNORECASE)
    text = re.sub(r'\s*\[.*?\]\s*', ' ', text, flags=re.IGNORECASE)
    text = re.sub(r'\s*(?:HD|FHD|UHD|4K|HEVC|RAW|SD|H265|H264|1080p|720p|576p|480p|360p)\s*', ' ', text, flags=re.IGNORECASE)
    text = re.sub(r'\s+', ' ', text).strip()
    return text

def get_canonical_key(name):
    if not name:
        return ""
    cleaned = clean_channel_name(name)
    text = normalize_text(cleaned)
    key = re.sub(r'[^a-z0-9]', '', text)
    if not key or len(key) < 2:
        return text[:5] if text else ""
    return key

def parse_m3u(content):
    lines = content.splitlines()
    entries = []
    current_extinf = None
    current_extra = []

    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith("#EXTM3U"):
            continue
        if line.startswith("#EXTINF:"):
            current_extinf = line
            current_extra = []
            continue
        if line.startswith("#"):
            if current_extinf:
                current_extra.append(line)
            continue
        if current_extinf:
            entries.append({
                "extinf": current_extinf,
                "extra": current_extra[:],
                "url": line,
            })
            current_extinf = None
            current_extra = []

    return entries

def get_extinf_name(extinf):
    if not extinf or "," not in extinf:
        return ""
    return extinf.rsplit(",", 1)[1].strip()

def clean_stream_url(url):
    if not url:
        return ""
    return url.split("|", 1)[0].strip() if "|" in url else url

def write_m3u(entries):
    with open(OUTPUT_M3U, "w", encoding="utf-8", newline="\n") as f:
        f.write("#EXTM3U\n")
        for entry in entries:
            f.write(entry["extinf"] + "\n")
            url = clean_stream_url(entry["url"])
            ua = entry.get("ua", CUSTOM_USER_AGENT)
            if url:
                f.write(f"#EXTVLCOPT:http-user-agent={ua}\n")
                f.write(f'#EXTHTTP:{{"User-Agent":"{ua}"}}\n')
                f.write(url + "\n")

# ============================================================
# LINK-PRÜFUNG (robust)
# ============================================================

def check_url(url, headers, timeout=CHECK_TIMEOUT):
    if not url:
        return False
    clean_url = url.split("|", 1)[0].strip()
    if not clean_url:
        return False
    try:
        # Versuche HEAD zuerst (schneller)
        resp_head = requests.head(clean_url, headers=headers, timeout=timeout, allow_redirects=True)
        if resp_head.status_code in [200, 302, 301, 307, 308]:
            return True
        # Fallback: GET mit Range
        headers_get = headers.copy()
        headers_get['Range'] = 'bytes=0-8192'
        resp_get = requests.get(clean_url, headers=headers_get, timeout=timeout, stream=True, allow_redirects=True)
        resp_get.close()
        if resp_get.status_code in [200, 206, 302, 301, 307, 308]:
            return True
    except:
        pass
    return False

# ============================================================
# CUSTOM LINKS
# ============================================================

def load_custom_links():
    if os.path.exists(CUSTOM_LINKS_FILE):
        try:
            with open(CUSTOM_LINKS_FILE, 'r', encoding='utf-8') as f:
                custom_links = json.load(f)
                total = sum(len(v) if isinstance(v, list) else 1 for v in custom_links.values())
                print(f"[CUSTOM] {len(custom_links)} Kanäle mit {total} manuellen Links geladen.")
                return custom_links
        except Exception as e:
            print(f"[CUSTOM] Fehler beim Laden: {e}")
            return {}
    else:
        print(f"[CUSTOM] Keine custom_links.json gefunden.")
        return {}

def get_custom_links_for_channel(channel_name, custom_links):
    if not channel_name or not custom_links:
        return []
    cleaned = clean_channel_name(channel_name).lower()
    if cleaned in custom_links:
        links = custom_links[cleaned]
        return [links] if isinstance(links, str) else links
    for key, links in custom_links.items():
        if key in cleaned or cleaned in key:
            return [links] if isinstance(links, str) else links
    return []

def get_working_custom_link(channel_name, custom_links, headers):
    links = get_custom_links_for_channel(channel_name, custom_links)
    if not links:
        return None
    for url in links:
        if check_url(url, headers, timeout=2):
            return url
    return None

# ============================================================
# CACHE
# ============================================================

def load_cache():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, 'r') as f:
                return json.load(f)
        except:
            return {}
    return {}

def save_cache(cache):
    try:
        with open(CACHE_FILE, 'w') as f:
            json.dump(cache, f)
    except:
        pass

def get_cached(key, cache, ttl=CACHE_TTL):
    if key in cache:
        entry = cache[key]
        if time.time() - entry.get('timestamp', 0) < ttl:
            return entry.get('value')
    return None

def set_cache(key, value, cache):
    cache[key] = {
        'timestamp': time.time(),
        'value': value
    }

# ============================================================
# VAVOO MIT PROXY-FALLBACK
# ============================================================

def extract_vavoo_id(url):
    # Extrahiert ID aus URLs wie:
    # https://young-dew-a7a9.pandatiger.workers.dev/play/2470626656489869fa852
    # oder https://vavoo.to/vavoo-iptv/play/...
    # oder https://snowy-moon-e7c4.pandatiger.workers.dev/?url=https://vavoo.to/vavoo-iptv/play/...
    match = re.search(r'/play/([a-f0-9]+)', url)
    if match:
        return match.group(1)
    # Fallback: Parameter url=... im Query
    match = re.search(r'[?&]url=https?://[^/]+/play/([a-f0-9]+)', url)
    if match:
        return match.group(1)
    return None

def check_vavoo_with_proxy(original_url):
    if not original_url:
        return None
    # Zuerst originale URL testen
    if check_url(original_url, {"User-Agent": VAVOO_USER_AGENT}, timeout=3):
        return original_url
    # ID extrahieren
    vavoo_id = extract_vavoo_id(original_url)
    if not vavoo_id:
        return None
    # Proxies durchgehen
    for proxy in VAVOO_PROXIES:
        proxy_url = f"{proxy}/play/{vavoo_id}"
        if check_url(proxy_url, {"User-Agent": VAVOO_USER_AGENT}, timeout=3):
            return proxy_url
    return None

# ============================================================
# KANAL REPARIEREN
# ============================================================

def repair_channel(entry, cache, custom_links):
    extinf = entry["extinf"]
    original_url = entry["url"]
    channel_name = get_extinf_name(extinf)
    cache_key = get_canonical_key(channel_name) if channel_name else None
    headers = {"User-Agent": CUSTOM_USER_AGENT}

    # 1. Manuelle Links
    if cache_key:
        custom_url = get_working_custom_link(channel_name, custom_links, headers)
        if custom_url:
            set_cache(f"stream_{cache_key}", custom_url, cache)
            return {"extinf": extinf, "url": custom_url, "ua": CUSTOM_USER_AGENT, "source": "custom"}

    # 2. Cache
    if cache_key:
        cached_result = get_cached(f"stream_{cache_key}", cache)
        if cached_result and check_url(cached_result, headers, timeout=3):
            return {"extinf": extinf, "url": cached_result, "ua": CUSTOM_USER_AGENT, "source": "cache"}

    # 3. Vavoo mit Proxy-Fallback
    vavoo_url = check_vavoo_with_proxy(original_url)
    if vavoo_url:
        # Cache speichern
        if cache_key:
            set_cache(f"stream_{cache_key}", vavoo_url, cache)
        return {"extinf": extinf, "url": vavoo_url, "ua": VAVOO_USER_AGENT, "source": "vavoo"}

    # 4. Nichts funktioniert – Original behalten
    return {"extinf": extinf, "url": clean_stream_url(original_url), "ua": VAVOO_USER_AGENT, "source": "original"}

# ============================================================
# HAUPTPROZESS
# ============================================================

def process_hybrid_m3u():
    print("\n" + "="*60)
    print("IPTV REPAIR TOOL (VAVOO + PROXY FALLBACK)")
    print("Custom Links → Cache → Vavoo (mit Proxy) → Original")
    print("="*60)

    custom_links = load_custom_links()
    cache = load_cache()
    print(f"[CACHE] Geladen: {len(cache)} Einträge")

    try:
        with open(INPUT_M3U, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except FileNotFoundError:
        print(f"[FEHLER] {INPUT_M3U} nicht gefunden.")
        return

    entries = parse_m3u(content)
    print(f"\n[M3U] {len(entries)} Kanäle gelesen.")

    print(f"\n[START] Verarbeite {len(entries)} Kanäle...")
    start_time = time.time()

    output_entries = []
    stats = {"custom": 0, "cache": 0, "vavoo": 0, "original": 0}

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(repair_channel, entry, cache, custom_links): entry for entry in entries}
        for i, future in enumerate(as_completed(futures), 1):
            result = future.result()
            output_entries.append(result)
            stats[result["source"]] += 1
            if i % 50 == 0 or i == len(entries):
                print(f"  Fortschritt: {i}/{len(entries)} ({i/len(entries)*100:.1f}%)")

    save_cache(cache)
    print(f"[CACHE] Gespeichert: {len(cache)} Einträge")

    elapsed = time.time() - start_time
    print("\n" + "="*60)
    print("STATISTIK")
    print("="*60)
    print(f"Gesamt Kanäle:          {len(entries)}")
    print(f"Manuelle Links:         {stats['custom']}")
    print(f"Aus Cache:              {stats['cache']}")
    print(f"Vavoo (funktioniert):   {stats['vavoo']}")
    print(f"Original behalten:      {stats['original']}")
    print(f"Benötigte Zeit:         {elapsed:.1f} Sekunden")
    print("="*60)

    write_m3u(output_entries)
    print(f"\n[FERTIG] Playlist gespeichert als {OUTPUT_M3U}")

if __name__ == "__main__":
    process_hybrid_m3u()
