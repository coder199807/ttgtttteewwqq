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
MAX_WORKERS = 50  # Mehr Parallelität für schnelle HEAD-Requests
REQUEST_TIMEOUT = 2  # Kurzer Timeout für HEAD-Requests
API_TIMEOUT = 5  # Längerer Timeout für API-Requests
CHECK_TIMEOUT = 1.5  # Sehr kurzer Timeout für Vavoo-Check

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

def check_url_fast(url, headers, timeout=CHECK_TIMEOUT):
    """Sehr schneller URL-Check (nur für Vavoo)."""
    try:
        response = requests.head(url, headers=headers, timeout=timeout, allow_redirects=True)
        # 200-299 = OK, 403/404 = defekt
        return 200 <= response.status_code < 300
    except:
        return False

def check_url(url, headers, timeout=REQUEST_TIMEOUT):
    """Normaler URL-Check für Fallbacks."""
    try:
        response = requests.head(url, headers=headers, timeout=timeout, allow_redirects=True)
        return 200 <= response.status_code < 300
    except:
        return False

# ============================================================
# 1. VAVOO - SCHNELLER CHECK
# ============================================================

def check_vavoo(original_url):
    """Prüft ob Vavoo-URL funktioniert (sehr schnell)."""
    if not original_url:
        return False
    # Nur checken, wenn die URL nicht offensichtlich defekt ist
    if 'workers.dev' in original_url or 'play/' in original_url:
        return check_url_fast(original_url, VAVOO_HEADERS)
    return False

# ============================================================
# 2. VOLO TV - CACHED
# ============================================================

@lru_cache(maxsize=500)
def get_volo_permalink_cached(channel_name):
    """Gecachte Volo-API-Abfrage."""
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
        response = requests.post(VOLO_API_URL, headers=VOLO_HEADERS, json=payload, timeout=API_TIMEOUT)
        if response.status_code != 200:
            return None
        data = response.json()
        return data.get('permalink')
    except:
        return None

def construct_volo_stream_url(permalink):
    """Konstruiert die Volo-Stream-URL."""
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
    """Sucht Volo-Stream (nur wenn Kanalname sinnvoll ist)."""
    # Nur für türkische Kanäle
    if not any(word in channel_name.lower() for word in ['tv', 'haber', 'spor', 'trt', 'kanal']):
        return None
    
    permalink = get_volo_permalink_cached(channel_name)
    if not permalink:
        return None
    base_url = construct_volo_stream_url(permalink)
    if not base_url:
        return None
    if check_url(base_url, VOLO_STREAM_HEADERS, timeout=REQUEST_TIMEOUT):
        return base_url
    return None

# ============================================================
# 3. FAMELACK - OPTIMIERT
# ============================================================

def search_famelack_via_tvizle_proxy(channel_name):
    """Famelack über TVizle-Proxy."""
    if not channel_name:
        return None
    sanitized = sanitize_channel_name(channel_name)
    if not sanitized or len(sanitized) < 3:
        return None
    
    # Nur die beste Qualität testen (1080p reicht)
    famelack_url = f"https://rnttwmjcin.turknet.ercdn.net/lcpmvefbyo/{sanitized}/{sanitized}_1080p.m3u8"
    encoded_url = quote(famelack_url, safe='')
    proxy_url = f"{TVIZLE_PROXY_URL}?url={encoded_url}"
    
    if check_url(proxy_url, TVIZLE_HEADERS, timeout=REQUEST_TIMEOUT):
        return proxy_url
    return None

def search_famelack_direct(channel_name):
    """Famelack direkt."""
    if not channel_name:
        return None
    sanitized = sanitize_channel_name(channel_name)
    if not sanitized or len(sanitized) < 3:
        return None
    
    url = f"https://rnttwmjcin.turknet.ercdn.net/lcpmvefbyo/{sanitized}/{sanitized}_1080p.m3u8"
    if check_url(url, FAMELACK_HEADERS, timeout=REQUEST_TIMEOUT):
        return url
    return None

# ============================================================
# 4. TVIZLE - OPTIMIERT
# ============================================================

def search_tvizle(channel_name):
    """TVizle-Stream."""
    if not channel_name:
        return None
    sanitized = sanitize_channel_name(channel_name)
    if not sanitized or len(sanitized) < 3:
        return None
    
    # Nur 1080p testen
    url = f"https://flask-api-hls-{sanitized}trkvz-live.onrender.com/hls_stream/{sanitized}_1080p.m3u8"
    if check_url(url, TVIZLE_STREAM_HEADERS, timeout=REQUEST_TIMEOUT):
        return url
    return None

# ============================================================
# 5. SMART REPAIR - NUR BEI BEDARF
# ============================================================

def smart_repair_channel(channel_name, original_url):
    """
    Intelligente Reparatur: Nur wenn Vavoo defekt ist.
    """
    # 1. Vavoo-Check (sehr schnell)
    if check_vavoo(original_url):
        return {"url": original_url, "source": "vavoo_ok", "ua": VAVOO_USER_AGENT}
    
    # 2. Wenn Vavoo defekt ist, Fallbacks probieren
    print(f"  [Repair] Vavoo defekt für: {channel_name}")
    
    # Volo
    stream_url = search_volo(channel_name)
    if stream_url:
        return {"url": stream_url, "source": "volo", "ua": CUSTOM_USER_AGENT}
    
    # Famelack (Proxy)
    stream_url = search_famelack_via_tvizle_proxy(channel_name)
    if stream_url:
        return {"url": stream_url, "source": "famelack_proxy", "ua": CUSTOM_USER_AGENT}
    
    # Famelack (Direkt)
    stream_url = search_famelack_direct(channel_name)
    if stream_url:
        return {"url": stream_url, "source": "famelack_direct", "ua": CUSTOM_USER_AGENT}
    
    # TVizle
    stream_url = search_tvizle(channel_name)
    if stream_url:
        return {"url": stream_url, "source": "tvizle", "ua": CUSTOM_USER_AGENT}
    
    # Nichts gefunden
    return None

# ============================================================
# 6. PARALLELE VERARBEITUNG
# ============================================================

def process_channel(entry):
    """Verarbeitet einen einzelnen Kanal."""
    extinf = entry["extinf"]
    original_url = entry["url"]
    channel_name = get_extinf_name(extinf)
    
    # 1. Schnell prüfen ob Vavoo funktioniert
    if check_vavoo(original_url):
        return {
            "extinf": extinf,
            "url": original_url,
            "ua": VAVOO_USER_AGENT,
            "source": "vavoo_ok"
        }
    
    # 2. Nur wenn Vavoo defekt ist, aufwändig reparieren
    repaired = smart_repair_channel(channel_name, original_url)
    
    if repaired:
        return {
            "extinf": extinf,
            "url": repaired["url"],
            "ua": repaired.get("ua", CUSTOM_USER_AGENT),
            "source": repaired["source"]
        }
    else:
        # Fallback: Original behalten
        return {
            "extinf": extinf,
            "url": clean_stream_url(original_url),
            "ua": VAVOO_USER_AGENT,
            "source": "failed"
        }

# ============================================================
# HAUPTPROZESS
# ============================================================

def process_hybrid_m3u():
    print("\n" + "="*60)
    print("HYBRID IPTV REPAIR TOOL (SMART)")
    print("Vavoo prüfen → Nur defekte Kanäle reparieren")
    print("="*60)

    # 1. M3U einlesen
    try:
        with open(INPUT_M3U, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except FileNotFoundError:
        print(f"[FEHLER] {INPUT_M3U} nicht gefunden.")
        return
    
    entries = parse_m3u(content)
    print(f"[M3U] {len(entries)} Kanäle gelesen.")

    # 2. Alle Kanäle parallel prüfen/reparieren
    print(f"\n[START] Prüfe {len(entries)} Kanäle mit {MAX_WORKERS} parallelen Threads...")
    start_time = time.time()
    
    output_entries = []
    repair_stats = {
        "vavoo_ok": 0,
        "volo": 0,
        "famelack_proxy": 0,
        "famelack_direct": 0,
        "tvizle": 0,
        "failed": 0
    }
    
    # Batch-Processing: In Gruppen von 100 für bessere Übersicht
    batch_size = 100
    total_batches = (len(entries) + batch_size - 1) // batch_size
    
    for batch_idx in range(0, len(entries), batch_size):
        batch = entries[batch_idx:batch_idx + batch_size]
        print(f"\n  Batch {batch_idx//batch_size + 1}/{total_batches} ({len(batch)} Kanäle)")
        
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(process_channel, entry): entry for entry in batch}
            
            for future in as_completed(futures):
                result = future.result()
                output_entries.append(result)
                repair_stats[result["source"]] += 1
        
        # Zwischenstand
        done = len(output_entries)
        print(f"  Fortschritt: {done}/{len(entries)} ({done/len(entries)*100:.1f}%)")

    # 3. Statistik
    elapsed = time.time() - start_time
    print("\n" + "="*60)
    print("STATISTIK")
    print("="*60)
    print(f"Gesamt:                 {len(output_entries)}")
    print(f"Vavoo (funktioniert):   {repair_stats['vavoo_ok']}")
    print(f"Repariert via Volo:     {repair_stats['volo']}")
    print(f"Repariert via Famelack (Proxy): {repair_stats['famelack_proxy']}")
    print(f"Repariert via Famelack (Direkt): {repair_stats['famelack_direct']}")
    print(f"Repariert via TVizle:   {repair_stats['tvizle']}")
    print(f"Nicht repariert:        {repair_stats['failed']}")
    print(f"Benötigte Zeit:         {elapsed:.1f} Sekunden")
    print(f"Durchsatz:             {len(entries)/elapsed:.1f} Kanäle/Sekunde")
    print("="*60)

    # 4. Neue M3U schreiben
    write_m3u(output_entries)
    print(f"[FERTIG] Playlist gespeichert als {OUTPUT_M3U}")

if __name__ == "__main__":
    process_hybrid_m3u()
