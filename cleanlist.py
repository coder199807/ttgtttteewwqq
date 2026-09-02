import re
import html
import unicodedata
import requests
import time
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
import urllib.parse
from collections import defaultdict

# ============================================================
# KONFIGURATION
# ============================================================
INPUT_M3U = "iptv.m3u"
OUTPUT_M3U = "iptv.m3u"
CUSTOM_LINKS_FILE = "custom_links.json"

CUSTOM_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
VAVOO_USER_AGENT = "Vavoo/2.6 vypn.net App/1.0 Mozilla/5.0 (Windows NT 10.0; Win64; x64)"

REQUEST_TIMEOUT = 10
CHECK_TIMEOUT = 3
MAX_WORKERS = 10
CACHE_FILE = "stream_cache.json"
CACHE_TTL = 86400  # 24 Stunden

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
    """Bereinigt den Kanalnamen für die Gruppierung (wie in der Vorlage)."""
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
    """Erstellt einen eindeutigen Schlüssel für die Gruppierung."""
    if not name:
        return ""
    cleaned = clean_channel_name(name)
    text = normalize_text(cleaned)
    key = re.sub(r'[^a-z0-9]', '', text)
    if not key or len(key) < 2:
        return text[:5]
    return key

def get_display_name(name):
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
    current_group = ""

    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith("#EXTM3U"):
            continue
        if line.startswith("#EXTINF:"):
            current_extinf = line
            current_extra = []
            group_match = re.search(r'group-title="([^"]*)"', line)
            if group_match:
                current_group = group_match.group(1)
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
                "group": current_group,
            })
            current_extinf = None
            current_extra = []
            current_group = ""

    return entries

def get_extinf_name(extinf):
    if not extinf or "," not in extinf:
        return ""
    return extinf.rsplit(",", 1)[1].strip()

def clean_stream_url(url):
    return url.split("|", 1)[0].strip() if url else ""

def write_m3u(entries):
    """Schreibt die M3U-Datei."""
    with open(OUTPUT_M3U, "w", encoding="utf-8", newline="\n") as f:
        f.write("#EXTM3U\n")
        for entry in entries:
            extinf = entry["extinf"]
            # Stelle sicher, dass group-title gesetzt ist
            if 'group-title="' not in extinf:
                extinf = re.sub(r'(#EXTINF:-?\d+)', r'\1 group-title="Ulusal"', extinf)
            f.write(extinf + "\n")
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
        if response.status_code == 200:
            return True
        if response.status_code in [403, 405]:
            headers['Range'] = 'bytes=0-8192'
            response = requests.get(url, headers=headers, timeout=timeout, stream=True, allow_redirects=True)
            return response.status_code in [200, 206]
        return False
    except:
        return False

# ============================================================
# CUSTOM LINKS (MANUELL)
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
# 1. VAVOO (HAUPTQUELLE)
# ============================================================

def check_vavoo(original_url):
    if not original_url:
        return False
    return check_url(original_url, {"User-Agent": VAVOO_USER_AGENT}, timeout=2)

# ============================================================
# 2. FAMELACK (SCRAPER)
# ============================================================

def scrape_famelack(channel_name):
    """Scraped Famelack-Streams (ercdn.net)."""
    print(f"  [FAMELACK] Suche für: {channel_name}")
    found = 0
    links = []
    
    clean_name = clean_channel_name(channel_name).lower()
    clean_name = re.sub(r'[^a-z0-9]', '', clean_name)
    
    if not clean_name or len(clean_name) < 3:
        return []
    
    variants = [
        clean_name,
        clean_name.replace('tv', '').strip(),
        clean_name[:5],
    ]
    variants = list(dict.fromkeys([v for v in variants if v and len(v) >= 3]))
    
    cdn_domains = ["rnttwmjcin.turknet.ercdn.net"]
    path_prefixes = ["lcpmvefbyo"]
    qualities = ["1080p", "720p", "576p"]
    
    for variant in variants[:3]:
        for domain in cdn_domains:
            for prefix in path_prefixes:
                for quality in qualities:
                    url = f"https://{domain}/{prefix}/{variant}/{variant}_{quality}.m3u8"
                    try:
                        response = requests.head(url, headers={"User-Agent": CUSTOM_USER_AGENT}, timeout=3)
                        if response.status_code == 200:
                            links.append(url)
                            found += 1
                            print(f"  [FAMELACK] ✅ {quality} gefunden")
                            break
                    except:
                        continue
                if found > 0:
                    break
            if found > 0:
                break
        if found > 0:
            break
    
    print(f"  [FAMELACK] {found} Links gefunden.")
    return links

# ============================================================
# 3. TVGARDEN.WORLD (SCRAPER)
# ============================================================

def scrape_tvgarden(channel_name):
    """Scraped tvgarden.world nach m3u8-Links."""
    print(f"  [TVGARDEN] Suche für: {channel_name}")
    found = 0
    links = []
    
    clean_name = clean_channel_name(channel_name).lower()
    
    try:
        search_url = f"{TVGARDEN_BASE}/api/channels/search?q={urllib.parse.quote(clean_name)}"
        response = requests.get(search_url, headers={"User-Agent": CUSTOM_USER_AGENT}, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            if data and isinstance(data, list):
                for channel in data:
                    stream_url = channel.get('stream_url') or channel.get('url')
                    if stream_url and '.m3u8' in stream_url:
                        links.append(stream_url)
                        found += 1
                        print(f"  [TVGARDEN] ✅ {stream_url[:80]}...")
        
        if not links:
            page_url = f"{TVGARDEN_BASE}/tv"
            response = requests.get(page_url, headers={"User-Agent": CUSTOM_USER_AGENT}, timeout=10)
            if response.status_code == 200:
                soup = BeautifulSoup(response.text, "html.parser")
                m3u8_pattern = r'(https?://[^\s"\']+\.m3u8[^\s"\']*)'
                matches = re.findall(m3u8_pattern, response.text)
                for match in matches:
                    if clean_name in match.lower() or clean_name in match.lower():
                        links.append(match)
                        found += 1
                        print(f"  [TVGARDEN] ✅ {match[:80]}...")
                
    except Exception as e:
        print(f"  [TVGARDEN] Fehler: {e}")
    
    print(f"  [TVGARDEN] {found} Links gefunden.")
    return links

# ============================================================
# 4. CANLITV.DIRECT (SCRAPER)
# ============================================================

def scrape_canlitv_direct(channel_name):
    """Scraped web.canlitv.direct nach m3u8-Links."""
    print(f"  [CANLITV] Suche für: {channel_name}")
    found = 0
    links = []
    
    clean_name = clean_channel_name(channel_name).lower()
    search_name = clean_name.replace(' ', '-')
    
    try:
        urls_to_try = [
            f"{CANLITV_DIRECT_BASE}/{search_name}",
            f"{CANLITV_DIRECT_BASE}/tv/{search_name}",
            f"{CANLITV_DIRECT_BASE}/kanal/{search_name}",
        ]
        
        for url in urls_to_try:
            response = requests.get(url, headers={"User-Agent": CUSTOM_USER_AGENT}, timeout=10)
            if response.status_code == 200:
                m3u8_pattern = r'(https?://[^\s"\']+\.m3u8[^\s"\']*)'
                matches = re.findall(m3u8_pattern, response.text)
                for match in matches:
                    links.append(match)
                    found += 1
                    print(f"  [CANLITV] ✅ {match[:80]}...")
                break
                        
    except Exception as e:
        print(f"  [CANLITV] Fehler: {e}")
    
    print(f"  [CANLITV] {found} Links gefunden.")
    return links

# ============================================================
# 5. SCRAPER - ALLE QUELLEN
# ============================================================

def find_stream_with_scraper(channel_name):
    """Durchsucht alle Scrape-Quellen nach einem Link."""
    print(f"  [SCRAPER] Suche für: {channel_name}")
    
    all_links = []
    
    famelack_links = scrape_famelack(channel_name)
    all_links.extend(famelack_links)
    
    tvgarden_links = scrape_tvgarden(channel_name)
    all_links.extend(tvgarden_links)
    
    canlitv_links = scrape_canlitv_direct(channel_name)
    all_links.extend(canlitv_links)
    
    all_links = list(dict.fromkeys(all_links))
    
    for url in all_links[:5]:
        if check_url(url, {"User-Agent": CUSTOM_USER_AGENT}, timeout=3):
            print(f"  [SCRAPER] ✅ Funktionierender Link gefunden!")
            return url
    
    return None

# ============================================================
# REPAIR FÜR EINZELNEN KANAL
# ============================================================

def repair_channel(entry, cache, custom_links):
    """Repariert einen einzelnen Kanal."""
    extinf = entry["extinf"]
    original_url = entry["url"]
    channel_name = get_extinf_name(extinf)
    display_name = get_display_name(channel_name)
    cache_key = get_canonical_key(channel_name)
    
    # 1. Manuelle Links
    custom_url = get_working_custom_link(channel_name, custom_links, {"User-Agent": CUSTOM_USER_AGENT})
    if custom_url:
        set_cache(f"stream_{cache_key}", custom_url, cache)
        return {"extinf": extinf, "url": custom_url, "ua": CUSTOM_USER_AGENT, "source": "custom"}
    
    # 2. Vavoo
    if check_vavoo(original_url):
        return {"extinf": extinf, "url": original_url, "ua": VAVOO_USER_AGENT, "source": "vavoo"}
    
    # 3. Cache
    cached_result = get_cached(f"stream_{cache_key}", cache)
    if cached_result and check_url(cached_result, {"User-Agent": CUSTOM_USER_AGENT}, timeout=2):
        return {"extinf": extinf, "url": cached_result, "ua": CUSTOM_USER_AGENT, "source": "cache"}
    
    # 4. Scraper
    scraper_url = find_stream_with_scraper(channel_name)
    if scraper_url:
        set_cache(f"stream_{cache_key}", scraper_url, cache)
        return {"extinf": extinf, "url": scraper_url, "ua": CUSTOM_USER_AGENT, "source": "scraper"}
    
    # 5. Original behalten
    return {"extinf": extinf, "url": clean_stream_url(original_url), "ua": VAVOO_USER_AGENT, "source": "failed"}

# ============================================================
# GRUPPIERUNG NUR FÜR ULUSAL
# ============================================================

def group_ulusal_channels(entries):
    """
    Gruppiert NUR die Ulusal-Kanäle nach normalisiertem Namen.
    Behält die Reihenfolge des ersten Auftretens bei.
    """
    groups = {}
    order = []
    
    for entry in entries:
        name = get_extinf_name(entry["extinf"])
        if not name:
            continue
        key = get_canonical_key(name)
        if not key:
            continue
        
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(entry)
    
    return groups, order

def find_best_link_for_group(group_entries, cache, custom_links):
    """Findet den besten Link für eine Gruppe von Ulusal-Kanälen."""
    first_name = get_extinf_name(group_entries[0]["extinf"])
    key = get_canonical_key(first_name)
    
    # 1. Manuelle Links
    custom_url = get_working_custom_link(first_name, custom_links, {"User-Agent": CUSTOM_USER_AGENT})
    if custom_url:
        return custom_url, "custom"
    
    # 2. Cache
    cached_result = get_cached(f"stream_{key}", cache)
    if cached_result and check_url(cached_result, {"User-Agent": CUSTOM_USER_AGENT}, timeout=2):
        return cached_result, "cache"
    
    # 3. Alle URLs aus der Gruppe testen
    for entry in group_entries:
        url = entry.get("url")
        if url and check_url(url, {"User-Agent": VAVOO_USER_AGENT}, timeout=2):
            return url, "vavoo"
    
    # 4. Scraper
    scraper_url = find_stream_with_scraper(first_name)
    if scraper_url:
        set_cache(f"stream_{key}", scraper_url, cache)
        return scraper_url, "scraper"
    
    return group_entries[0].get("url"), "failed"

# ============================================================
# M3U CHECKER
# ============================================================

def validate_m3u_with_checker(m3u_content):
    """Validiert die M3U-Playlist lokal."""
    print("\n[M3U CHECKER] Validiere Playlist...")
    
    entries = parse_m3u(m3u_content)
    working_entries = []
    failed_entries = []
    
    print(f"[M3U CHECKER] Prüfe {len(entries)} Streams...")
    
    def check_single(entry):
        url = entry.get("url")
        if not url:
            return None
        if check_url(url, {"User-Agent": VAVOO_USER_AGENT}, timeout=3):
            return entry
        return None
    
    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = {executor.submit(check_single, entry): entry for entry in entries}
        for future in as_completed(futures):
            result = future.result()
            if result:
                working_entries.append(result)
            else:
                failed_entries.append(futures[future])
    
    print(f"[M3U CHECKER] {len(working_entries)} funktionierende Streams, {len(failed_entries)} defekte Streams")
    return m3u_content

# ============================================================
# HAUPTPROZESS
# ============================================================

def process_hybrid_m3u():
    print("\n" + "="*60)
    print("IPTV REPAIR TOOL")
    print("Nur Ulusal-Kanäle werden gruppiert und optimiert")
    print("Alle anderen Kategorien bleiben unverändert")
    print("="*60)

    start_total = time.time()
    
    custom_links = load_custom_links()
    cache = load_cache()
    print(f"[CACHE] Geladen: {len(cache)} Einträge")

    try:
        with open(INPUT_M3U, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except FileNotFoundError:
        print(f"[FEHLER] {INPUT_M3U} nicht gefunden.")
        return

    content = validate_m3u_with_checker(content)
    all_entries = parse_m3u(content)
    print(f"\n[M3U] {len(all_entries)} Kanäle gelesen.")

    # Aufteilen: Ulusal vs. andere Kategorien
    ulusal_entries = []
    other_entries = []
    
    for entry in all_entries:
        group = entry.get("group", "")
        if group and group.strip() in ["Ulusal", "ulusal", "ULUSAL"]:
            ulusal_entries.append(entry)
        else:
            other_entries.append(entry)
    
    print(f"[M3U] {len(ulusal_entries)} Kanäle in 'Ulusal'")
    print(f"[M3U] {len(other_entries)} Kanäle in anderen Kategorien (bleiben unverändert)")

    # Ulusal-Kanäle gruppieren und optimieren
    groups, order = group_ulusal_channels(ulusal_entries)
    print(f"[M3U] {len(groups)} eindeutige Ulusal-Kanäle nach Normalisierung (Duplikate entfernt).")

    print(f"\n[START] Verarbeite {len(groups)} Ulusal-Kanäle...")
    start_time = time.time()
    
    processed_ulusal = []
    stats = {"custom": 0, "cache": 0, "vavoo": 0, "scraper": 0, "failed": 0}

    for key in order:
        group_entries = groups[key]
        base_entry = group_entries[0]
        name = get_extinf_name(base_entry["extinf"])
        display_name = get_display_name(name)

        best_url, source = find_best_link_for_group(group_entries, cache, custom_links)
        stats[source] += 1

        if best_url and source not in ["failed"]:
            set_cache(f"stream_{key}", best_url, cache)

        # Ausgabe im Stil der Vorlage
        new_extinf = base_entry["extinf"]
        # Entferne alte group-title
        new_extinf = re.sub(r'group-title="[^"]*"', '', new_extinf)
        # Setze group-title="Ulusal"
        new_extinf = re.sub(r'(#EXTINF:-?\d+)', r'\1 group-title="Ulusal"', new_extinf)

        processed_ulusal.append({
            "extinf": new_extinf,
            "extra": base_entry.get("extra", []),
            "url": best_url if best_url else "",
            "group": "Ulusal",
            "ua": CUSTOM_USER_AGENT if source not in ["vavoo", "failed"] else VAVOO_USER_AGENT,
            "source": source,
            "display_name": display_name
        })

        total = len(groups)
        done = len(processed_ulusal)
        if done % 10 == 0 or done == total:
            print(f"  Fortschritt: {done}/{total} ({done/total*100:.1f}%)")

    save_cache(cache)
    print(f"[CACHE] Gespeichert: {len(cache)} Einträge")

    # Zusammenführen: Verarbeitete Ulusal + unveränderte andere Kategorien
    output_entries = processed_ulusal + other_entries

    elapsed = time.time() - start_time
    total_elapsed = time.time() - start_total
    print("\n" + "="*60)
    print("STATISTIK")
    print("="*60)
    print(f"Original Kanäle:        {len(all_entries)}")
    print(f"Ulusal Kanäle:          {len(ulusal_entries)}")
    print(f"  → Gruppiert zu:       {len(groups)} (Duplikate entfernt)")
    print(f"Andere Kategorien:      {len(other_entries)} (unverändert)")
    print(f"Manuelle Links:         {stats['custom']}")
    print(f"Aus Cache:              {stats['cache']}")
    print(f"Vavoo (funktioniert):   {stats['vavoo']}")
    print(f"Durch Scraper gefunden: {stats['scraper']}")
    print(f"Kein funktionierender:  {stats['failed']}")
    print(f"Benötigte Zeit:         {elapsed:.1f} Sekunden")
    print(f"Gesamtzeit:             {total_elapsed:.1f} Sekunden")
    print("="*60)

    write_m3u(output_entries)
    print(f"\n[FERTIG] Playlist gespeichert als {OUTPUT_M3U}")
    print(f"[INFO] {len(output_entries)} Kanäle in der Playlist.")

if __name__ == "__main__":
    process_hybrid_m3u()
