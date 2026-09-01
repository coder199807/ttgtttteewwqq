import re
import html
import unicodedata
import requests
import time
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from bs4 import BeautifulSoup
from collections import defaultdict
from scraper import IPTVScraper

# ============================================================
# KONFIGURATION
# ============================================================
INPUT_M3U = "iptv.m3u"
OUTPUT_M3U = "iptv.m3u"

# --- Backup-M3U (wird bei jedem Build neu geladen) ---
BACKUP_M3U_URL = "https://m3u.work/pqfhFNTY.m3u"

# --- User Agents ---
CUSTOM_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
VAVOO_USER_AGENT = "Vavoo/2.6 vypn.net App/1.0 Mozilla/5.0 (Windows NT 10.0; Win64; x64)"

# --- Allgemein ---
REQUEST_TIMEOUT = 10
CHECK_TIMEOUT = 3
MAX_WORKERS = 20
CACHE_FILE = "stream_cache.json"
CACHE_TTL = 3600  # 1 Stunde Cache (für den Build)

# ============================================================
# HILFSFUNKTIONEN
# ============================================================

def normalize_text(text):
    """Normalisiert Text für den Vergleich."""
    if not text:
        return ""
    text = html.unescape(str(text))
    text = BeautifulSoup(text, "html.parser").get_text(" ")
    replacements = {"ı": "i", "İ": "i", "ş": "s", "Ş": "s", "ğ": "g", "Ğ": "g", "ç": "c", "Ç": "c", "ö": "o", "Ö": "o", "ü": "u", "Ü": "u"}
    for old, new in replacements.items():
        text = text.replace(old, new)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower()
    return text

def clean_channel_name(name):
    """Bereinigt den Sendernamen von allen Zusätzen."""
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
    """Erstellt einen robusten Vergleichsschlüssel."""
    if not name:
        return ""
    cleaned = clean_channel_name(name)
    text = normalize_text(cleaned)
    text = re.sub(r'[^a-z0-9]', '', text)
    return text

def get_display_name(name):
    """Gibt den bereinigten Sendernamen zurück."""
    if not name:
        return ""
    cleaned = clean_channel_name(name)
    return cleaned.title() if cleaned else name

def parse_m3u(content):
    """Liest eine M3U-Datei robust ein."""
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
    """Extrahiert den Sendernamen aus der #EXTINF-Zeile."""
    if not extinf or "," not in extinf:
        return ""
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

def check_url(url, headers, timeout=CHECK_TIMEOUT):
    """Prüft ob eine URL erreichbar ist."""
    if not url:
        return False
    try:
        response = requests.head(url, headers=headers, timeout=timeout, allow_redirects=True)
        return 200 <= response.status_code < 300
    except:
        return False

# ============================================================
# CACHE
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
        if time.time() - entry.get('timestamp', 0) < ttl:
            return entry.get('value')
    return None

def set_cache(key, value, cache):
    """Speichert einen Wert im Cache."""
    cache[key] = {
        'timestamp': time.time(),
        'value': value
    }

# ============================================================
# 1. BACKUP-M3U LADEN
# ============================================================

def load_backup_m3u():
    """Lädt die aktuelle Backup-M3U von der URL."""
    print(f"\n[BACKUP] Lade: {BACKUP_M3U_URL}")
    try:
        response = requests.get(BACKUP_M3U_URL, timeout=REQUEST_TIMEOUT)
        if response.status_code != 200:
            print(f"[BACKUP] Fehler: HTTP {response.status_code}")
            return []
        content = response.text
        entries = parse_m3u(content)
        print(f"[BACKUP] {len(entries)} Kanäle geladen.")
        return entries
    except Exception as e:
        print(f"[BACKUP] Fehler beim Laden: {e}")
        return []

def build_backup_index(backup_entries):
    """Baut einen Index der Backup-Kanäle auf."""
    index = {}
    for entry in backup_entries:
        name = get_extinf_name(entry["extinf"])
        if not name:
            continue
        key = get_canonical_key(name)
        if key:
            if key not in index:
                index[key] = entry
    print(f"[BACKUP] Index aufgebaut: {len(index)} eindeutige Kanäle.")
    return index

def find_backup_for_channel(channel_name, backup_index):
    """Findet einen Backup-Stream für einen Kanal."""
    if not channel_name or not backup_index:
        return None
    
    channel_key = get_canonical_key(channel_name)
    if not channel_key:
        return None
    
    # Exakter Match
    if channel_key in backup_index:
        return backup_index[channel_key]
    
    # Teil-Match
    best_match = None
    best_score = 0
    
    for backup_key, entry in backup_index.items():
        if not backup_key:
            continue
        if channel_key in backup_key or backup_key in channel_key:
            shorter = min(len(channel_key), len(backup_key))
            longer = max(len(channel_key), len(backup_key))
            score = shorter / longer
            if score > best_score:
                best_score = score
                best_match = entry
    
    if best_score >= 0.7:
        return best_match
    
    return None

# ============================================================
# 2. SCRAPER INTEGRATION
# ============================================================

def find_stream_with_scraper(channel_name):
    """
    Verwendet den Scraper, um einen funktionierenden Stream zu finden.
    """
    print(f"  [SCRAPER] Suche für: {channel_name}")
    
    try:
        scraper = IPTVScraper()
        
        # Nur die wichtigsten Quellen scrapen (schneller)
        scraper.scrape_tvizle(channel_name)
        scraper.scrape_famelack(channel_name)
        scraper.scrape_volo(channel_name)
        
        if scraper.scraped_links:
            # Teste die gefundenen Links
            for url in scraper.scraped_links[:5]:
                if check_url(url, {"User-Agent": CUSTOM_USER_AGENT}, timeout=3):
                    print(f"  [SCRAPER] ✅ Funktionierender Link gefunden!")
                    return url
    except Exception as e:
        print(f"  [SCRAPER] Fehler: {e}")
    
    return None

# ============================================================
# 3. VAVOO CHECK
# ============================================================

def check_vavoo(original_url):
    """Prüft ob Vavoo-URL funktioniert."""
    if not original_url:
        return False
    return check_url(original_url, {"User-Agent": VAVOO_USER_AGENT}, timeout=3)

# ============================================================
# 4. REPAIR LOGIK
# ============================================================

def repair_channel(entry, backup_index, cache):
    """Repariert einen Kanal mit intelligenter Reihenfolge."""
    extinf = entry["extinf"]
    original_url = entry["url"]
    channel_name = get_extinf_name(extinf)
    display_name = get_display_name(channel_name)
    
    # Cache-Key
    cache_key = get_canonical_key(channel_name)
    
    # 1. Prüfe Cache (für schnelle Wiederholungsläufe)
    cached_result = get_cached(f"stream_{cache_key}", cache)
    if cached_result:
        # Prüfe ob der gecachte Link noch funktioniert
        if check_url(cached_result, {"User-Agent": CUSTOM_USER_AGENT}, timeout=2):
            print(f"  ✅ {display_name} (Cache)")
            return {"extinf": extinf, "url": cached_result, "ua": CUSTOM_USER_AGENT, "source": "cache"}
    
    # 2. Versuche Backup-M3U
    backup_match = find_backup_for_channel(channel_name, backup_index)
    if backup_match:
        backup_url = backup_match["url"]
        if check_url(backup_url, {"User-Agent": CUSTOM_USER_AGENT}, timeout=2):
            set_cache(f"stream_{cache_key}", backup_url, cache)
            print(f"  ✅ {display_name} (Backup)")
            return {"extinf": extinf, "url": backup_url, "ua": CUSTOM_USER_AGENT, "source": "backup"}
    
    # 3. Versuche Scraper
    scraper_url = find_stream_with_scraper(channel_name)
    if scraper_url:
        set_cache(f"stream_{cache_key}", scraper_url, cache)
        print(f"  ✅ {display_name} (Scraper)")
        return {"extinf": extinf, "url": scraper_url, "ua": CUSTOM_USER_AGENT, "source": "scraper"}
    
    # 4. Versuche Vavoo
    if check_vavoo(original_url):
        print(f"  ✅ {display_name} (Vavoo)")
        return {"extinf": extinf, "url": original_url, "ua": VAVOO_USER_AGENT, "source": "vavoo"}
    
    # 5. Nichts gefunden
    print(f"  ❌ {display_name} (kein Stream)")
    return {"extinf": extinf, "url": clean_stream_url(original_url), "ua": VAVOO_USER_AGENT, "source": "failed"}

# ============================================================
# HAUPTPROZESS
# ============================================================

def process_hybrid_m3u():
    print("\n" + "="*60)
    print("IPTV REPAIR TOOL (MIT CACHE & SCRAPER)")
    print("Backup-M3U → Scraper → Vavoo")
    print("="*60)

    # 1. Cache laden
    cache = load_cache()
    print(f"[CACHE] Geladen: {len(cache)} Einträge")

    # 2. Backup-M3U laden
    backup_entries = load_backup_m3u()
    backup_index = build_backup_index(backup_entries) if backup_entries else {}

    # 3. Haupt-M3U lesen
    try:
        with open(INPUT_M3U, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except FileNotFoundError:
        print(f"[FEHLER] {INPUT_M3U} nicht gefunden.")
        return

    entries = parse_m3u(content)
    print(f"\n[M3U] {len(entries)} Kanäle gelesen.")

    # 4. Kanäle parallel verarbeiten
    print(f"\n[START] Verarbeite {len(entries)} Kanäle mit {MAX_WORKERS} parallelen Threads...")
    start_time = time.time()
    
    output_entries = []
    stats = {
        "cache": 0,
        "backup": 0,
        "scraper": 0,
        "vavoo": 0,
        "failed": 0
    }

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(repair_channel, entry, backup_index, cache): entry for entry in entries}
        
        for future in as_completed(futures):
            result = future.result()
            output_entries.append(result)
            stats[result["source"]] += 1
            
            # Fortschritt anzeigen
            total = len(entries)
            done = len(output_entries)
            if done % 50 == 0 or done == total:
                print(f"  Fortschritt: {done}/{total} ({done/total*100:.1f}%)")

    # 5. Cache speichern
    save_cache(cache)
    print(f"[CACHE] Gespeichert: {len(cache)} Einträge")

    # 6. Statistik
    elapsed = time.time() - start_time
    print("\n" + "="*60)
    print("STATISTIK")
    print("="*60)
    print(f"Gesamt:                 {len(output_entries)}")
    print(f"Aus Cache:              {stats['cache']}")
    print(f"Durch Backup ersetzt:   {stats['backup']}")
    print(f"Durch Scraper gefunden: {stats['scraper']}")
    print(f"Vavoo (Fallback):       {stats['vavoo']}")
    print(f"Nicht repariert:        {stats['failed']}")
    print(f"Benötigte Zeit:         {elapsed:.1f} Sekunden")
    print("="*60)

    # 7. Neue M3U schreiben
    write_m3u(output_entries)
    print(f"\n[FERTIG] Playlist gespeichert als {OUTPUT_M3U}")

if __name__ == "__main__":
    process_hybrid_m3u()
