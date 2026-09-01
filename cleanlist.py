import re
import html
import unicodedata
import requests
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse, unquote, urljoin, parse_qs, urlencode, quote
from bs4 import BeautifulSoup

# ============================================================
# KONFIGURATION
# ============================================================
INPUT_M3U = "iptv.m3u"
OUTPUT_M3U = "iptv.m3u"

# --- User Agents ---
CUSTOM_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
VAVOO_USER_AGENT = "Vavoo/2.6 vypn.net App/1.0 Mozilla/5.0 (Windows NT 10.0; Win64; x64)"

# --- Volo TV ---
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

# --- TVizle Proxy (für Famelack & andere) ---
TVIZLE_PROXY_URL = "https://tvizle.tr/api/proxy"
TVIZLE_HEADERS = {
    "User-Agent": CUSTOM_USER_AGENT,
    "Accept": "*/*",
    "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
    "Referer": "https://tvizle.tr/",
    "Origin": "https://tvizle.tr",
}

# --- Famelack Direkt (Fallback) ---
FAMELACK_HEADERS = {
    "User-Agent": CUSTOM_USER_AGENT,
    "Accept": "*/*",
    "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
    "Referer": "https://tvizle.tr/",
}

# --- Allgemein ---
MAX_WORKERS = 5
REQUEST_TIMEOUT = 10
API_RETRY_DELAY = 0.3

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

# ============================================================
# 1. VOLO TV (API + Token-Konstruktion)
# ============================================================

def get_volo_permalink(channel_name):
    """Fragt die Volo-API ab, um den Permalink für einen Kanal zu erhalten."""
    if not channel_name: return None
    
    permalink = re.sub(r'[^a-z0-9\s-]', '', channel_name.lower())
    permalink = re.sub(r'\s+', '-', permalink)
    permalink = re.sub(r'-canli-izle$|-canli-hd-yayin-kesintisiz-izle$|-canli$', '', permalink)
    permalink = re.sub(r'-hd$|-fhd$|-tv$', '', permalink)

    if not permalink or len(permalink) < 3:
        return None

    try:
        time.sleep(API_RETRY_DELAY)
        payload = {"permalink": permalink, "yayin": 1}
        response = requests.post(VOLO_API_URL, headers=VOLO_HEADERS, json=payload, timeout=REQUEST_TIMEOUT)
        if response.status_code != 200: return None
        data = response.json()
        return data.get('permalink')
    except Exception:
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
    permalink = get_volo_permalink(channel_name)
    if not permalink:
        return None
    
    base_url = construct_volo_stream_url(permalink)
    if not base_url:
        return None
    
    try:
        response = requests.head(base_url, headers=VOLO_STREAM_HEADERS, timeout=5)
        if response.status_code == 200:
            print(f"    -> Volo gefunden: {base_url[:60]}...")
            return base_url
    except Exception:
        pass
    
    return None

# ============================================================
# 2. FAMELACK ÜBER TVIZLE PROXY (HAUPTWEG)
# ============================================================

def search_famelack_via_tvizle_proxy(channel_name):
    """
    Sucht nach einem Famelack-Stream über den TVizle-Proxy.
    Muster: https://tvizle.tr/api/proxy?url={encoded_famelack_url}
    """
    if not channel_name:
        return None
    
    sanitized = sanitize_channel_name(channel_name)
    if not sanitized:
        return None
    
    qualities = ["1080p", "720p", "576p", "480p", "360p"]
    cdn_domains = ["rnttwmjcin.turknet.ercdn.net"]
    path_prefix = "lcpmvefbyo"
    
    for domain in cdn_domains:
        for quality in qualities:
            # Konstruiere die direkte Famelack-URL
            famelack_url = f"https://{domain}/{path_prefix}/{sanitized}/{sanitized}_{quality}.m3u8"
            # URL-encodieren für den Proxy
            encoded_url = quote(famelack_url, safe='')
            proxy_url = f"{TVIZLE_PROXY_URL}?url={encoded_url}"
            
            try:
                # Prüfe den Proxy-Endpunkt
                response = requests.head(proxy_url, headers=TVIZLE_HEADERS, timeout=5)
                if response.status_code == 200:
                    print(f"    -> Famelack (via TVizle Proxy) gefunden ({quality}): {proxy_url}")
                    return proxy_url
            except Exception:
                continue
    
    return None

# ============================================================
# 3. FAMELACK DIREKT (FALLBACK)
# ============================================================

def search_famelack_direct(channel_name):
    """
    Sucht nach einem Famelack-Stream direkt (ohne Proxy).
    Muster: https://rnttwmjcin.turknet.ercdn.net/lcpmvefbyo/{sender}/{sender}_{quality}.m3u8
    """
    if not channel_name:
        return None
    
    sanitized = sanitize_channel_name(channel_name)
    if not sanitized:
        return None
    
    qualities = ["1080p", "720p", "576p", "480p", "360p"]
    cdn_domains = ["rnttwmjcin.turknet.ercdn.net"]
    path_prefix = "lcpmvefbyo"
    
    for domain in cdn_domains:
        for quality in qualities:
            url = f"https://{domain}/{path_prefix}/{sanitized}/{sanitized}_{quality}.m3u8"
            try:
                response = requests.head(url, headers=FAMELACK_HEADERS, timeout=5)
                if response.status_code == 200:
                    print(f"    -> Famelack (direkt) gefunden ({quality}): {url}")
                    return url
            except Exception:
                continue
    
    return None

# ============================================================
# 4. TVIZLE (EIGENE STREAMS)
# ============================================================

def search_tvizle(channel_name):
    """
    Sucht nach einem TVizle-Stream (eigene Infrastruktur).
    Muster: https://flask-api-hls-{sender}...onrender.com/hls_stream/{sender}_{quality}.m3u8
    """
    if not channel_name:
        return None
    
    sanitized = sanitize_channel_name(channel_name)
    if not sanitized:
        return None
    
    qualities = ["1080p", "720p", "576p", "480p", "360p"]
    
    # Dynamische Domains
    domains = [
        f"flask-api-hls-{sanitized}trkvz-live.onrender.com",
        f"flask-api-hls-{sanitized}hdtrkvz-live.onrender.com",
        f"flask-api-hls-{sanitized}-live.onrender.com",
        "flask-api-hls-atvavrupahdtrkvz-live.onrender.com",  # Fallback
    ]
    
    path_prefix = "hls_stream"
    
    for domain in domains:
        for quality in qualities:
            url = f"https://{domain}/{path_prefix}/{sanitized}_{quality}.m3u8"
            try:
                response = requests.head(url, headers=TVIZLE_HEADERS, timeout=5)
                if response.status_code == 200:
                    print(f"    -> TVizle gefunden ({quality}): {url}")
                    return url
            except Exception:
                continue
        
        url = f"https://{domain}/{path_prefix}/{sanitized}.m3u8"
        try:
            response = requests.head(url, headers=TVIZLE_HEADERS, timeout=5)
            if response.status_code == 200:
                print(f"    -> TVizle gefunden: {url}")
                return url
        except Exception:
            continue
    
    return None

# ============================================================
# 5. MULTI-SOURCE STREAM FINDER
# ============================================================

def find_stream_from_sources(channel_name):
    """
    Durchläuft alle definierten Quellen.
    Priorität: 1. Volo, 2. Famelack (via Proxy), 3. Famelack (direkt), 4. TVizle
    """
    print(f"  [Repair] Suche nach Stream für: {channel_name}")
    
    # 1. Volo TV
    stream_url = search_volo(channel_name)
    if stream_url:
        return {"url": stream_url, "source": "volo"}
    
    # 2. Famelack via TVizle Proxy (zuverlässigster Weg)
    stream_url = search_famelack_via_tvizle_proxy(channel_name)
    if stream_url:
        return {"url": stream_url, "source": "famelack_proxy"}
    
    # 3. Famelack direkt (Fallback)
    stream_url = search_famelack_direct(channel_name)
    if stream_url:
        return {"url": stream_url, "source": "famelack_direct"}
    
    # 4. TVizle (eigene Streams)
    stream_url = search_tvizle(channel_name)
    if stream_url:
        return {"url": stream_url, "source": "tvizle"}

    print(f"    -> Keine Quelle gefunden.")
    return None

# ============================================================
# HAUPTPROZESS
# ============================================================

def process_hybrid_m3u():
    print("\n" + "="*60)
    print("HYBRID IPTV REPAIR TOOL")
    print("Quellen: Volo | Famelack (Proxy) | Famelack (Direkt) | TVizle")
    print("="*60)

    try:
        with open(INPUT_M3U, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except FileNotFoundError:
        print(f"[FEHLER] {INPUT_M3U} nicht gefunden.")
        return
    
    entries = parse_m3u(content)
    print(f"[M3U] {len(entries)} Kanäle gelesen.")

    output_entries = []
    repair_stats = {
        "volo": 0,
        "famelack_proxy": 0,
        "famelack_direct": 0,
        "tvizle": 0,
        "failed": 0
    }
    
    for i, entry in enumerate(entries, 1):
        extinf = entry["extinf"]
        original_url = entry["url"]
        channel_name = get_extinf_name(extinf)
        
        print(f"\n[{i}/{len(entries)}] Verarbeite: {channel_name}")
        
        repaired = find_stream_from_sources(channel_name)
        
        if repaired:
            output_entries.append({
                "extinf": extinf,
                "url": repaired["url"],
                "ua": CUSTOM_USER_AGENT,
            })
            repair_stats[repaired["source"]] += 1
        else:
            output_entries.append({
                "extinf": extinf,
                "url": clean_stream_url(original_url),
                "ua": VAVOO_USER_AGENT,
            })
            repair_stats["failed"] += 1

    print("\n" + "="*60)
    print("STATISTIK")
    print("="*60)
    print(f"Gesamt:                 {len(output_entries)}")
    print(f"Repariert via Volo:     {repair_stats['volo']}")
    print(f"Repariert via Famelack (Proxy): {repair_stats['famelack_proxy']}")
    print(f"Repariert via Famelack (Direkt): {repair_stats['famelack_direct']}")
    print(f"Repariert via TVizle:   {repair_stats['tvizle']}")
    print(f"Nicht repariert:        {repair_stats['failed']}")
    print("="*60)

    write_m3u(output_entries)
    print(f"[FERTIG] Playlist gespeichert als {OUTPUT_M3U}")

if __name__ == "__main__":
    process_hybrid_m3u()
