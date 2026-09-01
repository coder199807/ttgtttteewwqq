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

# --- Backup-M3U ---
BACKUP_M3U_URL = "https://m3u.work/pqfhFNTY.m3u"

# --- User Agents ---
CUSTOM_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
VAVOO_USER_AGENT = "Vavoo/2.6 vypn.net App/1.0 Mozilla/5.0 (Windows NT 10.0; Win64; x64)"

# --- Allgemein ---
REQUEST_TIMEOUT = 15
CHECK_TIMEOUT = 3
MAX_WORKERS = 20
CACHE_FILE = "stream_cache.json"
CACHE_TTL = 3600

# ============================================================
# LIVE-FILTER
# ============================================================

NON_LIVE_KEYWORDS = [
    r'\bS\d{1,2}E\d{1,2}\b',
    r'\bStaffel\s*\d+\b',
    r'\bSeason\s*\d+\b',
    r'\bEpisode\s*\d+\b',
    r'\bFolge\s*\d+\b',
    r'\([12]\d{3}\)',
    r'\[[12]\d{3}\]',
    r'\bFilm\b',
    r'\bMovie\b',
    r'\bDoku\b',
    r'\bDokumentation\b',
    r'\bAufzeichnung\b',
    r'\bRecording\b',
    r'\bRepeat\b',
    r'\bWiederholung\b',
    r'\bBest of\b',
    r'\bHighlights\b',
    r'\bVOD\b',
    r'\bOn Demand\b',
    r'\bCatch Up\b',
    r'\bArchive\b',
    r'\bYouTube\b',
    r'\bSeries\b',
    r'\bSerie\b',
]

LIVE_KEYWORDS = [
    r'\bLIVE\b',
    r'\bCANLI\b',
    r'\bHD\b',
    r'\bFHD\b',
    r'\bUHD\b',
    r'\bTV\b',
]

def is_live_stream(extinf, url):
    text = f"{extinf} {url}".lower()
    
    for pattern in NON_LIVE_KEYWORDS:
        if re.search(pattern, text, re.IGNORECASE):
            return False
    
    has_live_indicator = False
    for pattern in LIVE_KEYWORDS:
        if re.search(pattern, text, re.IGNORECASE):
            has_live_indicator = True
            break
    
    duration_match = re.search(r'#EXTINF:([\d-]+)', extinf)
    if duration_match:
        duration = duration_match.group(1)
        if duration == '-1' or duration == '0':
            return True
        elif duration.isdigit() and int(duration) > 300:
            if has_live_indicator:
                return True
            return False
    
    if '.m3u8' in url and ('live' in url.lower() or 'stream' in url.lower()):
        return True
    
    name = get_extinf_name(extinf)
    if name and len(name) < 50 and 'tv' in name.lower():
        return True
    
    return True

def filter_live_backup_entries(backup_entries):
    print("\n[FILTER] Prüfe Backup-Kanäle auf Live-Streams...")
    
    live_entries = []
    non_live_count = 0
    
    for entry in backup_entries:
        extinf = entry["extinf"]
        url = entry["url"]
        
        if is_live_stream(extinf, url):
            live_entries.append(entry)
        else:
            non_live_count += 1
    
    print(f"[FILTER] {len(live_entries)} Live-Streams gefunden (von {len(backup_entries)} Gesamt)")
    print(f"[FILTER] {non_live_count} Einträge gefiltert")
    
    return live_entries

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
    return re.sub(r'[^a-z0-9]', '', text)

def get_display_name(name):
    if not name:
        return ""
    cleaned = clean_channel_name(name)
    return cleaned.title() if cleaned else name

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
    return url.split("|", 1)[0].strip() if url else ""

def write_m3u(entries):
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
    if not url:
        return False
    try:
        response = requests.head(url, headers=headers, timeout=timeout, allow_redirects=True)
        return 200 <= response.status_code < 300
    except:
        return False

# ============================================================
# CUSTOM LINKS (MANUELLE FESTLEGUNG MIT MEHREREN LINKS)
# ============================================================

def load_custom_links():
    """
    Lädt die manuell definierten Links aus custom_links.json.
    Unterstützt sowohl einzelne Links als auch Listen.
    """
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
        print(f"[CUSTOM] Keine custom_links.json gefunden. Erstelle Beispiel...")
        example = {
            "atv": [
                "https://dogusdyg-atv.lg.mncdn.com/dogusdyg_atv/live_1080p3000000kbps/index.m3u8",
                "https://rnttwmjcin.turknet.ercdn.net/lcpmvefbyo/atv/atv_1080p.m3u8"
            ],
            "kanal d": "https://dogusdyg-kanald.lg.mncdn.com/dogusdyg_kanald/live_1080p3000000kbps/index.m3u8"
        }
        try:
            with open(CUSTOM_LINKS_FILE, 'w', encoding='utf-8') as f:
                json.dump(example, f, indent=2, ensure_ascii=False)
            print(f"[CUSTOM] Beispiel-Datei erstellt: {CUSTOM_LINKS_FILE}")
        except:
            pass
        return {}

def get_custom_links_for_channel(channel_name, custom_links):
    """
    Gibt alle manuellen Links für einen Kanal zurück (als Liste).
    Unterstützt sowohl einzelne Links als auch Listen.
    """
    if not channel_name or not custom_links:
        return []
    
    cleaned = clean_channel_name(channel_name).lower()
    
    # Prüfe exakte Übereinstimmung
    if cleaned in custom_links:
        links = custom_links[cleaned]
        if isinstance(links, str):
            return [links]
        elif isinstance(links, list):
            return links
    
    # Prüfe Teil-Übereinstimmung
    for key, links in custom_links.items():
        if key in cleaned or cleaned in key:
            if isinstance(links, str):
                return [links]
            elif isinstance(links, list):
                return links
    
    return []

def get_working_custom_link(channel_name, custom_links, headers):
    """
    Testet alle manuellen Links für einen Kanal und gibt den ersten funktionierenden zurück.
    """
    links = get_custom_links_for_channel(channel_name, custom_links)
    if not links:
        return None
    
    print(f"  [CUSTOM] Teste {len(links)} manuelle Links für {channel_name}...")
    
    for i, url in enumerate(links, 1):
        if check_url(url, headers, timeout=2):
            print(f"  [CUSTOM] ✅ Link {i} funktioniert: {url[:60]}...")
            return url
        else:
            print(f"  [CUSTOM] ❌ Link {i} defekt")
    
    print(f"  [CUSTOM] ⚠️ Keiner der manuellen Links funktioniert")
    return None

def update_custom_links(channel_name, new_url, custom_links):
    """
    Aktualisiert die manuellen Links für einen Kanal.
    - Wenn es eine Liste gab, wird der defekte Link ersetzt oder ein neuer hinzugefügt.
    - Wenn es ein einzelner Link war, wird er ersetzt.
    """
    if not channel_name or not new_url or custom_links is None:
        return
    
    cleaned = clean_channel_name(channel_name).lower()
    
    # Prüfe exakte Übereinstimmung
    if cleaned in custom_links:
        existing = custom_links[cleaned]
        if isinstance(existing, list):
            # Füge neuen Link hinzu, wenn er nicht bereits existiert
            if new_url not in existing:
                existing.append(new_url)
                print(f"  [CUSTOM] Neuer Link zu {cleaned} hinzugefügt")
        else:
            # Ersetze einzelnen Link durch Liste mit beiden
            custom_links[cleaned] = [existing, new_url]
            print(f"  [CUSTOM] Link für {cleaned} aktualisiert")
    else:
        # Neuer Kanal
        custom_links[cleaned] = new_url
        print(f"  [CUSTOM] Neuer Kanal {cleaned} hinzugefügt")
    
    try:
        with open(CUSTOM_LINKS_FILE, 'w', encoding='utf-8') as f:
            json.dump(custom_links, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"  [CUSTOM] Fehler beim Speichern: {e}")

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
# BACKUP-M3U
# ============================================================

def load_backup_m3u():
    print(f"\n[BACKUP] Lade: {BACKUP_M3U_URL}")
    try:
        response = requests.get(BACKUP_M3U_URL, timeout=REQUEST_TIMEOUT)
        if response.status_code != 200:
            print(f"[BACKUP] Fehler: HTTP {response.status_code}")
            return []
        content = response.text
        entries = parse_m3u(content)
        print(f"[BACKUP] {len(entries)} Kanäle geladen.")
        
        live_entries = filter_live_backup_entries(entries)
        return live_entries
    except Exception as e:
        print(f"[BACKUP] Fehler beim Laden: {e}")
        return []

def build_backup_index(backup_entries):
    index = {}
    for entry in backup_entries:
        name = get_extinf_name(entry["extinf"])
        if not name:
            continue
        key = get_canonical_key(name)
        if key:
            index[key] = entry
    print(f"[BACKUP] Index aufgebaut: {len(index)} eindeutige Live-Kanäle.")
    return index

def find_backup_for_channel(channel_name, backup_index):
    if not channel_name or not backup_index:
        return None
    channel_key = get_canonical_key(channel_name)
    if not channel_key:
        return None
    if channel_key in backup_index:
        return backup_index[channel_key]
    
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
# SCRAPER INTEGRATION
# ============================================================

def find_stream_with_scraper(channel_name):
    """Verwendet den Scraper, um einen funktionierenden Stream zu finden."""
    print(f"  [SCRAPER] Suche für: {channel_name}")
    
    try:
        from scraper import IPTVScraper
        scraper = IPTVScraper(debug=False)
        
        scraper.scrape_tvizle(channel_name)
        scraper.scrape_famelack(channel_name)
        scraper.scrape_volo(channel_name)
        scraper.scrape_globetv(channel_name)
        
        if scraper.scraped_links:
            for url in scraper.scraped_links[:5]:
                if check_url(url, {"User-Agent": CUSTOM_USER_AGENT}, timeout=3):
                    print(f"  [SCRAPER] ✅ Funktionierender Link gefunden!")
                    return url
    except ImportError:
        print(f"  [SCRAPER] scraper.py nicht gefunden.")
    except Exception as e:
        print(f"  [SCRAPER] Fehler: {e}")
    
    return None

# ============================================================
# REPAIR
# ============================================================

def repair_channel(entry, backup_index, cache, custom_links):
    extinf = entry["extinf"]
    original_url = entry["url"]
    channel_name = get_extinf_name(extinf)
    display_name = get_display_name(channel_name)
    cache_key = get_canonical_key(channel_name)
    
    # 1. PRÜFE: Manuelle Links (höchste Priorität)
    custom_url = get_working_custom_link(
        channel_name, 
        custom_links, 
        {"User-Agent": CUSTOM_USER_AGENT}
    )
    if custom_url:
        set_cache(f"stream_{cache_key}", custom_url, cache)
        return {"extinf": extinf, "url": custom_url, "ua": CUSTOM_USER_AGENT, "source": "custom"}
    
    # 2. PRÜFE: Cache
    cached_result = get_cached(f"stream_{cache_key}", cache)
    if cached_result and check_url(cached_result, {"User-Agent": CUSTOM_USER_AGENT}, timeout=2):
        return {"extinf": extinf, "url": cached_result, "ua": CUSTOM_USER_AGENT, "source": "cache"}
    
    # 3. PRÜFE: Backup-M3U
    backup_match = find_backup_for_channel(channel_name, backup_index)
    if backup_match:
        backup_url = backup_match["url"]
        if check_url(backup_url, {"User-Agent": CUSTOM_USER_AGENT}, timeout=2):
            set_cache(f"stream_{cache_key}", backup_url, cache)
            # Aktualisiere custom_links, falls der Kanal dort ist
            if cache_key in custom_links:
                update_custom_links(channel_name, backup_url, custom_links)
            return {"extinf": extinf, "url": backup_url, "ua": CUSTOM_USER_AGENT, "source": "backup"}
    
    # 4. PRÜFE: Scraper
    scraper_url = find_stream_with_scraper(channel_name)
    if scraper_url:
        set_cache(f"stream_{cache_key}", scraper_url, cache)
        if cache_key in custom_links:
            update_custom_links(channel_name, scraper_url, custom_links)
        return {"extinf": extinf, "url": scraper_url, "ua": CUSTOM_USER_AGENT, "source": "scraper"}
    
    # 5. PRÜFE: Vavoo
    if check_url(original_url, {"User-Agent": VAVOO_USER_AGENT}, timeout=2):
        return {"extinf": extinf, "url": original_url, "ua": VAVOO_USER_AGENT, "source": "vavoo"}
    
    return {"extinf": extinf, "url": clean_stream_url(original_url), "ua": VAVOO_USER_AGENT, "source": "failed"}

# ============================================================
# HAUPTPROZESS
# ============================================================

def process_hybrid_m3u():
    print("\n" + "="*60)
    print("IPTV REPAIR TOOL (MULTI-CUSTOM LINKS)")
    print("Custom (mehrere) → Cache → Backup-M3U → Scraper → Vavoo")
    print("="*60)

    # 1. Manuelle Links laden
    custom_links = load_custom_links()

    # 2. Cache laden
    cache = load_cache()
    print(f"[CACHE] Geladen: {len(cache)} Einträge")

    # 3. Backup-M3U laden
    backup_entries = load_backup_m3u()
    if not backup_entries:
        print("[FEHLER] Backup-M3U konnte nicht geladen werden.")
        backup_index = {}
    else:
        backup_index = build_backup_index(backup_entries)

    # 4. Haupt-M3U lesen
    try:
        with open(INPUT_M3U, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except FileNotFoundError:
        print(f"[FEHLER] {INPUT_M3U} nicht gefunden.")
        return

    entries = parse_m3u(content)
    print(f"\n[M3U] {len(entries)} Kanäle gelesen.")

    # 5. Kanäle verarbeiten
    print(f"\n[START] Verarbeite {len(entries)} Kanäle...")
    start_time = time.time()
    
    output_entries = []
    stats = {"custom": 0, "cache": 0, "backup": 0, "scraper": 0, "vavoo": 0, "failed": 0}

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(repair_channel, entry, backup_index, cache, custom_links): entry for entry in entries}
        
        for future in as_completed(futures):
            result = future.result()
            output_entries.append(result)
            stats[result["source"]] += 1
            
            total = len(entries)
            done = len(output_entries)
            if done % 100 == 0 or done == total:
                print(f"  Fortschritt: {done}/{total} ({done/total*100:.1f}%)")

    # 6. Cache speichern
    save_cache(cache)
    print(f"[CACHE] Gespeichert: {len(cache)} Einträge")

    # 7. Statistik
    elapsed = time.time() - start_time
    print("\n" + "="*60)
    print("STATISTIK")
    print("="*60)
    print(f"Gesamt:                 {len(output_entries)}")
    print(f"Manuelle Links:         {stats['custom']}")
    print(f"Aus Cache:              {stats['cache']}")
    print(f"Durch Backup ersetzt:   {stats['backup']}")
    print(f"Durch Scraper gefunden: {stats['scraper']}")
    print(f"Vavoo (Fallback):       {stats['vavoo']}")
    print(f"Nicht repariert:        {stats['failed']}")
    print(f"Benötigte Zeit:         {elapsed:.1f} Sekunden")
    print("="*60)

    # 8. Neue M3U schreiben
    write_m3u(output_entries)
    print(f"\n[FERTIG] Playlist gespeichert als {OUTPUT_M3U}")

if __name__ == "__main__":
    process_hybrid_m3u()
