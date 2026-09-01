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
import sys

# ============================================================
# KONFIGURATION
# ============================================================
INPUT_M3U = "iptv.m3u"
OUTPUT_M3U = "iptv.m3u"
DEBUG = True  # Debug-Modus aktivieren

# --- User Agents ---
CUSTOM_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
VAVOO_USER_AGENT = "Vavoo/2.6 vypn.net App/1.0 Mozilla/5.0 (Windows NT 10.0; Win64; x64)"

# --- Vavoo ---
VAVOO_HEADERS = {
    "User-Agent": VAVOO_USER_AGENT,
    "Accept": "*/*",
    "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
}

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

# --- TVizle Proxy ---
TVIZLE_PROXY_URL = "https://tvizle.tr/api/proxy"
TVIZLE_HEADERS = {
    "User-Agent": CUSTOM_USER_AGENT,
    "Accept": "*/*",
    "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
    "Referer": "https://tvizle.tr/",
    "Origin": "https://tvizle.tr",
}

# --- Famelack ---
FAMELACK_HEADERS = {
    "User-Agent": CUSTOM_USER_AGENT,
    "Accept": "*/*",
    "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
    "Referer": "https://tvizle.tr/",
}

# --- TVizle ---
TVIZLE_STREAM_HEADERS = {
    "User-Agent": CUSTOM_USER_AGENT,
    "Accept": "*/*",
    "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
    "Referer": "https://tvizle.tr/",
}

# --- Allgemein ---
MAX_WORKERS = 10
REQUEST_TIMEOUT = 5
CHECK_TIMEOUT = 3

# ============================================================
# DEBUG-FUNKTIONEN
# ============================================================

def debug_print(*args, **kwargs):
    """Nur im Debug-Modus ausgeben."""
    if DEBUG:
        print(*args, **kwargs)

def debug_check_url(url, headers, timeout=CHECK_TIMEOUT):
    """URL-Check mit Debug-Ausgabe."""
    try:
        debug_print(f"    [CHECK] {url[:80]}...")
        response = requests.head(url, headers=headers, timeout=timeout, allow_redirects=True)
        status = response.status_code
        debug_print(f"    [RESULT] HTTP {status}")
        return 200 <= status < 300
    except requests.exceptions.Timeout:
        debug_print(f"    [RESULT] TIMEOUT")
        return False
    except requests.exceptions.ConnectionError:
        debug_print(f"    [RESULT] CONNECTION ERROR")
        return False
    except Exception as e:
        debug_print(f"    [RESULT] ERROR: {e}")
        return False

# ============================================================
# HILFSFUNKTIONEN
# ============================================================

def normalize_text(text):
    """Normalisiert Text."""
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
    """Bereinigt den Sendernamen."""
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
    """Extrahiert den Sendernamen."""
    if not extinf or "," not in extinf: return ""
    return extinf.rsplit(",", 1)[1].strip()

def clean_stream_url(url):
    """Bereinigt die URL."""
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
# 1. VAVOO CHECK
# ============================================================

def check_vavoo(original_url):
    """Prüft ob Vavoo-URL funktioniert."""
    if not original_url:
        return False
    return debug_check_url(original_url, VAVOO_HEADERS)

# ============================================================
# 2. VOLO TV
# ============================================================

@lru_cache(maxsize=200)
def get_volo_permalink_cached(channel_name):
    """Gecachte Volo-API-Abfrage."""
    if not channel_name:
        return None
    
    # Versuche verschiedene Permalink-Varianten
    variants = []
    
    # Variante 1: Normalisiert
    v1 = re.sub(r'[^a-z0-9\s-]', '', channel_name.lower())
    v1 = re.sub(r'\s+', '-', v1)
    v1 = re.sub(r'-canli-izle$|-canli-hd-yayin-kesintisiz-izle$|-canli$', '', v1)
    v1 = re.sub(r'-hd$|-fhd$|-tv$', '', v1)
    if v1 and len(v1) >= 3:
        variants.append(v1)
    
    # Variante 2: Ohne Leerzeichen
    v2 = re.sub(r'[^a-z0-9]', '', channel_name.lower())
    if v2 and len(v2) >= 3 and v2 != v1:
        variants.append(v2)
    
    # Variante 3: Kurzname (erste 3 Buchstaben)
    v3 = re.sub(r'[^a-z]', '', channel_name.lower())[:3]
    if v3 and len(v3) >= 3 and v3 not in variants:
        variants.append(v3)
    
    debug_print(f"    [VOLO] Versuche Permalinks: {variants[:3]}")
    
    for permalink in variants[:3]:  # Max 3 Versuche
        try:
            payload = {"permalink": permalink, "yayin": 1}
            response = requests.post(VOLO_API_URL, headers=VOLO_HEADERS, json=payload, timeout=5)
            if response.status_code == 200:
                data = response.json()
                if data.get('permalink'):
                    debug_print(f"    [VOLO] Permalink gefunden: {data['permalink']}")
                    return data['permalink']
        except:
            continue
    
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
    """Sucht Volo-Stream."""
    debug_print(f"    [VOLO] Suche für: {channel_name}")
    permalink = get_volo_permalink_cached(channel_name)
    if not permalink:
        return None
    base_url = construct_volo_stream_url(permalink)
    if not base_url:
        return None
    if debug_check_url(base_url, VOLO_STREAM_HEADERS):
        return base_url
    return None

# ============================================================
# 3. FAMELACK
# ============================================================

def search_famelack_via_tvizle_proxy(channel_name):
    """Famelack über TVizle-Proxy."""
    debug_print(f"    [FAMELACK-PROXY] Suche für: {channel_name}")
    sanitized = sanitize_channel_name(channel_name)
    if not sanitized or len(sanitized) < 3:
        return None
    
    # Alle Qualitäten testen
    qualities = ["1080p", "720p", "576p", "480p", "360p"]
    for quality in qualities:
        famelack_url = f"https://rnttwmjcin.turknet.ercdn.net/lcpmvefbyo/{sanitized}/{sanitized}_{quality}.m3u8"
        encoded_url = quote(famelack_url, safe='')
        proxy_url = f"{TVIZLE_PROXY_URL}?url={encoded_url}"
        
        if debug_check_url(proxy_url, TVIZLE_HEADERS):
            debug_print(f"    [FAMELACK-PROXY] Gefunden: {quality}")
            return proxy_url
    
    return None

def search_famelack_direct(channel_name):
    """Famelack direkt."""
    debug_print(f"    [FAMELACK-DIRECT] Suche für: {channel_name}")
    sanitized = sanitize_channel_name(channel_name)
    if not sanitized or len(sanitized) < 3:
        return None
    
    qualities = ["1080p", "720p", "576p", "480p", "360p"]
    for quality in qualities:
        url = f"https://rnttwmjcin.turknet.ercdn.net/lcpmvefbyo/{sanitized}/{sanitized}_{quality}.m3u8"
        if debug_check_url(url, FAMELACK_HEADERS):
            debug_print(f"    [FAMELACK-DIRECT] Gefunden: {quality}")
            return url
    
    return None

# ============================================================
# 4. TVIZLE
# ============================================================

def search_tvizle(channel_name):
    """TVizle-Stream."""
    debug_print(f"    [TVIZLE] Suche für: {channel_name}")
    sanitized = sanitize_channel_name(channel_name)
    if not sanitized or len(sanitized) < 3:
        return None
    
    # Verschiedene Domain-Varianten
    domains = [
        f"flask-api-hls-{sanitized}trkvz-live.onrender.com",
        f"flask-api-hls-{sanitized}hdtrkvz-live.onrender.com",
        f"flask-api-hls-{sanitized}-live.onrender.com",
    ]
    
    qualities = ["1080p", "720p", "576p", "480p", "360p"]
    
    for domain in domains:
        for quality in qualities:
            url = f"https://{domain}/hls_stream/{sanitized}_{quality}.m3u8"
            if debug_check_url(url, TVIZLE_STREAM_HEADERS):
                debug_print(f"    [TVIZLE] Gefunden: {quality}")
                return url
    
    return None

# ============================================================
# 5. REPAIR
# ============================================================

def repair_channel(entry, index, total):
    """Repariert einen Kanal mit Debug-Ausgabe."""
    extinf = entry["extinf"]
    original_url = entry["url"]
    channel_name = get_extinf_name(extinf)
    
    debug_print(f"\n[{index}/{total}] {channel_name}")
    debug_print(f"  Original URL: {original_url[:80]}...")
    
    # 1. Vavoo-Check
    if check_vavoo(original_url):
        debug_print(f"  ✅ Vavoo funktioniert")
        return {"extinf": extinf, "url": original_url, "ua": VAVOO_USER_AGENT, "source": "vavoo_ok"}
    
    debug_print(f"  ❌ Vavoo defekt, suche Fallback...")
    
    # 2. Volo
    stream_url = search_volo(channel_name)
    if stream_url:
        debug_print(f"  ✅ Volo gefunden")
        return {"extinf": extinf, "url": stream_url, "ua": CUSTOM_USER_AGENT, "source": "volo"}
    
    # 3. Famelack (Proxy)
    stream_url = search_famelack_via_tvizle_proxy(channel_name)
    if stream_url:
        debug_print(f"  ✅ Famelack (Proxy) gefunden")
        return {"extinf": extinf, "url": stream_url, "ua": CUSTOM_USER_AGENT, "source": "famelack_proxy"}
    
    # 4. Famelack (Direkt)
    stream_url = search_famelack_direct(channel_name)
    if stream_url:
        debug_print(f"  ✅ Famelack (Direkt) gefunden")
        return {"extinf": extinf, "url": stream_url, "ua": CUSTOM_USER_AGENT, "source": "famelack_direct"}
    
    # 5. TVizle
    stream_url = search_tvizle(channel_name)
    if stream_url:
        debug_print(f"  ✅ TVizle gefunden")
        return {"extinf": extinf, "url": stream_url, "ua": CUSTOM_USER_AGENT, "source": "tvizle"}
    
    debug_print(f"  ❌ Keine Quelle gefunden")
    return {"extinf": extinf, "url": clean_stream_url(original_url), "ua": VAVOO_USER_AGENT, "source": "failed"}

# ============================================================
# HAUPTPROZESS
# ============================================================

def process_hybrid_m3u():
    print("\n" + "="*60)
    print("HYBRID IPTV REPAIR TOOL (DEBUG)")
    print("Vavoo → Volo → Famelack (Proxy) → Famelack (Direkt) → TVizle")
    print("="*60)
    
    if DEBUG:
        print("⚠️  DEBUG-Modus AKTIV - Viele Ausgaben!")
        print("   Deaktiviere DEBUG für schnellere Ausführung.")
        print()

    # 1. M3U einlesen
    try:
        with open(INPUT_M3U, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except FileNotFoundError:
        print(f"[FEHLER] {INPUT_M3U} nicht gefunden.")
        return
    
    entries = parse_m3u(content)
    print(f"[M3U] {len(entries)} Kanäle gelesen.\n")

    # 2. Teste erstmal nur die ersten 20 Kanäle im Debug-Modus
    test_limit = 20 if DEBUG else len(entries)
    print(f"[DEBUG] Teste nur die ersten {test_limit} Kanäle...\n")
    
    output_entries = []
    repair_stats = {
        "vavoo_ok": 0,
        "volo": 0,
        "famelack_proxy": 0,
        "famelack_direct": 0,
        "tvizle": 0,
        "failed": 0
    }
    
    start_time = time.time()
    
    for i, entry in enumerate(entries[:test_limit], 1):
        result = repair_channel(entry, i, test_limit)
        output_entries.append(result)
        repair_stats[result["source"]] += 1
    
    # 3. Statistik
    elapsed = time.time() - start_time
    print("\n" + "="*60)
    print("STATISTIK")
    print("="*60)
    print(f"Getestet:               {test_limit}")
    print(f"Vavoo (funktioniert):   {repair_stats['vavoo_ok']}")
    print(f"Repariert via Volo:     {repair_stats['volo']}")
    print(f"Repariert via Famelack (Proxy): {repair_stats['famelack_proxy']}")
    print(f"Repariert via Famelack (Direkt): {repair_stats['famelack_direct']}")
    print(f"Repariert via TVizle:   {repair_stats['tvizle']}")
    print(f"Nicht repariert:        {repair_stats['failed']}")
    print(f"Benötigte Zeit:         {elapsed:.1f} Sekunden")
    print("="*60)

    # 4. Speichern (nur wenn alle Kanäle verarbeitet wurden)
    if test_limit == len(entries):
        write_m3u(output_entries)
        print(f"[FERTIG] Playlist gespeichert als {OUTPUT_M3U}")
    else:
        print("[DEBUG] Nur Testlauf - keine M3U geschrieben.")
        print("  Entferne DEBUG = False für vollständigen Lauf.")

if __name__ == "__main__":
    process_hybrid_m3u()
