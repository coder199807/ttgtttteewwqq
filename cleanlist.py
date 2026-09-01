import re
import html
import unicodedata
import requests
import json
import time
import random
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse, unquote, urljoin
from bs4 import BeautifulSoup

# ============================================================
# KONFIGURATION
# ============================================================
INPUT_M3U = "iptv.m3u"
OUTPUT_M3U = "iptv.m3u"

# --- User Agents ---
CUSTOM_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
VAVOO_USER_AGENT = "Vavoo/2.6 vypn.net App/1.0 Mozilla/5.0 (Windows NT 10.0; Win64; x64)"

# --- Volo TV Konfiguration ---
VOLO_API_URL = "https://api.canlitvvolo.com/api/tv/stream"
VOLO_HEADERS = {
    "User-Agent": CUSTOM_USER_AGENT,
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json;charset=UTF-8",
    "Origin": "https://tv.canlitvvolo.com",
    "Referer": "https://tv.canlitvvolo.com/",
}

# --- TVizle Konfiguration ---
TVIZLE_BASE_URL = "https://tvizle.tr"
TVIZLE_HEADERS = {
    "User-Agent": CUSTOM_USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
}

# --- Famelack Konfiguration ---
FAMELACK_BASE_URL = "https://rnttwmjcin.turknet.ercdn.net"
FAMELACK_HEADERS = {
    "User-Agent": CUSTOM_USER_AGENT,
    "Accept": "*/*",
    "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
}

# --- Allgemein ---
MAX_WORKERS = 5
REQUEST_TIMEOUT = 15
API_RETRY_DELAY = 0.5

# ============================================================
# HILFSFUNKTIONEN (Normalisierung, M3U-Parsing, etc.)
# ============================================================

def normalize_text(text):
    """Normalisiert Text für den Vergleich (entfernt türkische Sonderzeichen, etc.)."""
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
    """Erstellt einen robusten Vergleichsschlüssel für einen Sendernamen."""
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
    """Erzeugt mehrere Vergleichsschlüssel für einen Sender."""
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
    """Bereinigt den Sendernamen für die Verwendung in URLs."""
    if not name:
        return ""
    # Normalisieren und Sonderzeichen entfernen
    name = normalize_text(name)
    # Entferne häufige Suffixe
    name = re.sub(r'\s*(?:hd|fhd|uhd|sd|hevc|raw|backup|canli|izle|tv)\s*', ' ', name)
    # Nur Buchstaben und Zahlen, Leerzeichen zu Bindestrich
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
    """Extrahiert den Sendernamen aus der #EXTINF-Zeile."""
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
# 1. VOLO TV: PERMALINK KONSTRUIEREN
# ============================================================

def get_volo_permalink(channel_name):
    """Fragt die Volo-API ab, um den Permalink für einen Kanal zu erhalten."""
    if not channel_name: return None
    
    # Erstelle einen Basis-Permalink aus dem Kanalnamen
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

def permalink_to_stream_url(permalink):
    """Konstruiert die Volo-Stream-URL aus dem Permalink."""
    if not permalink: return None
    base = permalink.strip('/').split('-canli')[0]
    stream_name = base.replace('-', '_')
    # Volo-Stream-URL-Muster (muss ggf. angepasst werden)
    return f"https://dogusdyg-{base}.lg.mncdn.com/dogusdyg_{stream_name}/live_1080p3000000kbps/index.m3u8"

# ============================================================
# 2. FAMELACK (ercdn.net)
# ============================================================

def search_famelack(channel_name):
    """
    Sucht nach einem Famelack-Stream für den Kanal.
    Muster: https://rnttwmjcin.turknet.ercdn.net/lcpmvefbyo/{sender}/{sender}_360p.m3u8
    """
    if not channel_name:
        return None
    
    sanitized = sanitize_channel_name(channel_name)
    if not sanitized:
        return None
    
    # Mögliche Qualitätsstufen
    qualities = ["1080p", "720p", "480p", "360p"]
    
    # Mögliche CDN-Domains (falls sich die Domain ändert)
    cdn_domains = [
        "rnttwmjcin.turknet.ercdn.net",
        # Weitere mögliche Domains hier eintragen
    ]
    
    # Mögliche Pfad-Präfixe (der Token-Teil)
    path_prefixes = [
        "lcpmvefbyo",
        # Weitere mögliche Pfade hier eintragen
    ]
    
    for domain in cdn_domains:
        for prefix in path_prefixes:
            for quality in qualities:
                # Versuche verschiedene Kombinationen
                url = f"https://{domain}/{prefix}/{sanitized}/{sanitized}_{quality}.m3u8"
                
                try:
                    response = requests.head(url, headers=FAMELACK_HEADERS, timeout=5)
                    if response.status_code == 200:
                        print(f"    -> Famelack gefunden: {url}")
                        return url
                except Exception:
                    continue
                
                # Alternative: ohne Qualitätsangabe
                url = f"https://{domain}/{prefix}/{sanitized}/{sanitized}.m3u8"
                try:
                    response = requests.head(url, headers=FAMELACK_HEADERS, timeout=5)
                    if response.status_code == 200:
                        print(f"    -> Famelack gefunden: {url}")
                        return url
                except Exception:
                    continue
    
    return None


# ============================================================
# 3. TVIZLE.TR (über ensonhaber.com)
# ============================================================

def search_tvizle(channel_name):
    """
    Sucht nach einem TVizle-Stream für den Kanal.
    Muster: https://tv.ensonhaber.com/{sender}/{sender}_1080p.m3u8
    """
    if not channel_name:
        return None
    
    sanitized = sanitize_channel_name(channel_name)
    if not sanitized:
        return None
    
    # Mögliche Qualitätsstufen
    qualities = ["1080p", "720p", "480p", "360p"]
    
    # Basis-URL für TVizle
    base_url = "https://tv.ensonhaber.com"
    
    for quality in qualities:
        # Versuche verschiedene Qualitäten
        url = f"{base_url}/{sanitized}/{sanitized}_{quality}.m3u8"
        
        try:
            response = requests.head(url, headers=TVIZLE_HEADERS, timeout=5)
            if response.status_code == 200:
                print(f"    -> TVizle gefunden: {url}")
                return url
        except Exception:
            continue
        
        # Alternative: ohne Qualitätsangabe
        url = f"{base_url}/{sanitized}/{sanitized}.m3u8"
        try:
            response = requests.head(url, headers=TVIZLE_HEADERS, timeout=5)
            if response.status_code == 200:
                print(f"    -> TVizle gefunden: {url}")
                return url
        except Exception:
            continue
    
    # Fallback: Versuche die Seite zu scrapen
    try:
        search_url = f"{TVIZLE_BASE_URL}/kanal/{sanitized}"
        response = requests.get(search_url, headers=TVIZLE_HEADERS, timeout=REQUEST_TIMEOUT)
        if response.status_code == 200:
            # Suche nach .m3u8-Links im HTML
            m3u8_pattern = r'(https?://[^\s"\']+\.m3u8[^\s"\']*)'
            matches = re.findall(m3u8_pattern, response.text)
            if matches:
                # Bevorzuge Links mit höherer Qualität
                for match in matches:
                    if '1080p' in match or '720p' in match:
                        print(f"    -> TVizle (gescraped) gefunden: {match}")
                        return match
                print(f"    -> TVizle (gescraped) gefunden: {matches[0]}")
                return matches[0]
    except Exception as e:
        print(f"  [TVizle] Scrape-Fehler für {channel_name}: {e}")
    
    return None


# ============================================================
# 4. MULTI-SOURCE STREAM FINDER
# ============================================================

def find_stream_from_sources(channel_name):
    """
    Durchläuft alle definierten Quellen, um einen gültigen Stream-Link zu finden.
    """
    print(f"  [Repair] Suche nach Stream für: {channel_name}")
    
    # 1. Quelle: Volo TV
    permalink = get_volo_permalink(channel_name)
    if permalink:
        stream_url = permalink_to_stream_url(permalink)
        if stream_url:
            # Prüfe, ob die URL erreichbar ist
            try:
                response = requests.head(stream_url, headers=VOLO_HEADERS, timeout=5)
                if response.status_code == 200:
                    print(f"    -> Gefunden via Volo: {stream_url[:60]}...")
                    return {"url": stream_url, "source": "volo"}
            except Exception:
                pass
    
    # 2. Quelle: Famelack
    stream_url = search_famelack(channel_name)
    if stream_url:
        return {"url": stream_url, "source": "famelack"}
    
    # 3. Quelle: TVizle
    stream_url = search_tvizle(channel_name)
    if stream_url:
        return {"url": stream_url, "source": "tvizle"}

    print(f"    -> Keine Quelle gefunden.")
    return None


# ============================================================
# HAUPTPROZESS
# ============================================================

def process_hybrid_m3u():
    print("\n" + "="*60 + "\nHYBRID IPTV REPAIR TOOL\n" + "="*60)

    # 1. M3U einlesen
    try:
        with open(INPUT_M3U, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except FileNotFoundError:
        print(f"[FEHLER] {INPUT_M3U} nicht gefunden.")
        return
    entries = parse_m3u(content)
    print(f"[M3U] {len(entries)} Kanäle gelesen.")

    # 2. Jeden Kanal reparieren
    output_entries = []
    repair_stats = {
        "volo": 0,
        "famelack": 0,
        "tvizle": 0,
        "failed": 0
    }
    
    for i, entry in enumerate(entries, 1):
        extinf = entry["extinf"]
        original_url = entry["url"]
        channel_name = get_extinf_name(extinf)
        
        print(f"\n[{i}/{len(entries)}] Verarbeite: {channel_name}")
        
        # Versuche, den Stream zu reparieren
        repaired = find_stream_from_sources(channel_name)
        
        if repaired:
            output_entries.append({
                "extinf": extinf,
                "url": repaired["url"],
                "ua": CUSTOM_USER_AGENT,
            })
            repair_stats[repaired["source"]] = repair_stats.get(repaired["source"], 0) + 1
        else:
            # Fallback: Originalen, defekten Link behalten
            output_entries.append({
                "extinf": extinf,
                "url": clean_stream_url(original_url),
                "ua": VAVOO_USER_AGENT,
            })
            repair_stats["failed"] += 1

    # 3. Statistik und Ausgabe
    print("\n" + "="*60 + "\nSTATISTIK\n" + "="*60)
    print(f"Gesamt:             {len(output_entries)}")
    print(f"Repariert via Volo: {repair_stats['volo']}")
    print(f"Repariert via Famelack: {repair_stats['famelack']}")
    print(f"Repariert via TVizle: {repair_stats['tvizle']}")
    print(f"Nicht repariert:    {repair_stats['failed']}")
    print("="*60)

    # 4. Neue M3U schreiben
    write_m3u(output_entries)
    print(f"[FERTIG] Playlist gespeichert als {OUTPUT_M3U}")

if __name__ == "__main__":
    process_hybrid_m3u()
