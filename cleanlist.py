import re
import html
import unicodedata
import requests
import time
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from bs4 import BeautifulSoup
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
CHECK_TIMEOUT = 5
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
    if not url:
        return ""
    return url.split("|", 1)[0].strip() if "|" in url else url

def write_m3u(entries):
    with open(OUTPUT_M3U, "w", encoding="utf-8", newline="\n") as f:
        f.write("#EXTM3U\n")
        for entry in entries:
            extinf = entry["extinf"]
            # group-title nur setzen, wenn nicht vorhanden
            if 'group-title="' not in extinf:
                group = entry.get("group", "Ulusal")
                extinf = re.sub(r'(#EXTINF:-?\d+)', rf'\1 group-title="{group}"', extinf)
            f.write(extinf + "\n")
            url = clean_stream_url(entry["url"])
            ua = entry.get("ua", CUSTOM_USER_AGENT)
            # Nur schreiben, wenn URL existiert
            if url:
                f.write(f"#EXTVLCOPT:http-user-agent={ua}\n")
                f.write(f'#EXTHTTP:{{"User-Agent":"{ua}"}}\n')
                f.write(url + "\n")

# ============================================================
# EINFACHE LINK-PRÜFUNG (KEIN SCRAPER)
# ============================================================

def check_url(url, headers, timeout=CHECK_TIMEOUT):
    """Einfache Prüfung ob eine URL erreichbar ist."""
    if not url:
        return False
    
    # Bereinige die URL
    check_url_clean = url.split("|", 1)[0].strip()
    if not check_url_clean:
        return False
    
    try:
        # Einfacher GET-Request mit Stream (lädt nur Header)
        response = requests.get(
            check_url_clean,
            headers=headers,
            timeout=timeout,
            stream=True,
            allow_redirects=True
        )
        response.close()
        
        # Akzeptiere alle 2xx, 3xx Statuscodes
        if 200 <= response.status_code < 400:
            return True
            
    except requests.exceptions.Timeout:
        pass  # Timeout ist kein hartes Nein
    except requests.exceptions.ConnectionError:
        pass
    except Exception:
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
# VAVOO PRÜFUNG
# ============================================================

def check_vavoo(original_url):
    if not original_url:
        return False
    return check_url(original_url, {"User-Agent": VAVOO_USER_AGENT}, timeout=2)

# ============================================================
# GRUPPIERUNG NUR FÜR ULUSAL
# ============================================================

def group_ulusal_channels(entries):
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
    first_name = get_extinf_name(group_entries[0]["extinf"])
    key = get_canonical_key(first_name)
    headers = {"User-Agent": CUSTOM_USER_AGENT}
    
    # 1. Manuelle Links (höchste Priorität)
    custom_url = get_working_custom_link(first_name, custom_links, headers)
    if custom_url:
        set_cache(f"stream_{key}", custom_url, cache)
        return custom_url, "custom"
    
    # 2. Cache
    cached_result = get_cached(f"stream_{key}", cache)
    if cached_result and check_url(cached_result, headers, timeout=3):
        return cached_result, "cache"
    
    # 3. Alle URLs aus der Gruppe testen
    for entry in group_entries:
        url = entry.get("url")
        if url and check_url(url, headers, timeout=3):
            set_cache(f"stream_{key}", url, cache)
            return url, "vavoo"
    
    # 4. Nichts funktioniert – Original behalten
    return group_entries[0].get("url"), "original"

# ============================================================
# HAUPTPROZESS
# ============================================================

def process_hybrid_m3u():
    print("\n" + "="*60)
    print("IPTV REPAIR TOOL (ULUSAL OPTIMIERUNG)")
    print("Nur Ulusal-Kanäle werden gruppiert und optimiert")
    print("Alle anderen Kategorien bleiben unverändert")
    print("="*60)

    # 1. Custom Links laden
    custom_links = load_custom_links()

    # 2. Cache laden
    cache = load_cache()
    print(f"[CACHE] Geladen: {len(cache)} Einträge")

    # 3. M3U lesen
    try:
        with open(INPUT_M3U, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except FileNotFoundError:
        print(f"[FEHLER] {INPUT_M3U} nicht gefunden.")
        return

    all_entries = parse_m3u(content)
    print(f"\n[M3U] {len(all_entries)} Kanäle gelesen.")

    # 4. Aufteilen: Ulusal vs. andere
    ulusal_entries = []
    other_entries = []
    
    for entry in all_entries:
        group = entry.get("group", "")
        if group and group.strip() in ["Ulusal", "ulusal", "ULUSAL"]:
            ulusal_entries.append(entry)
        else:
            other_entries.append(entry)
    
    print(f"[M3U] {len(ulusal_entries)} Kanäle in 'Ulusal'")
    print(f"[M3U] {len(other_entries)} Kanäle in anderen Kategorien (unverändert)")

    # 5. Ulusal gruppieren und optimieren
    groups, order = group_ulusal_channels(ulusal_entries)
    print(f"[M3U] {len(groups)} eindeutige Ulusal-Kanäle (Duplikate entfernt).")

    print(f"\n[START] Verarbeite {len(groups)} Ulusal-Kanäle...")
    start_time = time.time()
    
    processed_ulusal = []
    stats = {"custom": 0, "cache": 0, "vavoo": 0, "original": 0}

    for i, key in enumerate(order, 1):
        group_entries = groups[key]
        base_entry = group_entries[0]
        name = get_extinf_name(base_entry["extinf"])
        display_name = get_display_name(name)

        best_url, source = find_best_link_for_group(group_entries, cache, custom_links)
        stats[source] += 1

        # Ausgabe im Stil der Vorlage
        new_extinf = base_entry["extinf"]
        # Entferne alte group-title und setze "Ulusal"
        new_extinf = re.sub(r'group-title="[^"]*"', '', new_extinf)
        new_extinf = re.sub(r'(#EXTINF:-?\d+)', r'\1 group-title="Ulusal"', new_extinf)

        # Benutzer-Agent für die Ausgabe
        ua = CUSTOM_USER_AGENT if source not in ["vavoo", "original"] else VAVOO_USER_AGENT

        processed_ulusal.append({
            "extinf": new_extinf,
            "extra": base_entry.get("extra", []),
            "url": best_url if best_url else "",
            "group": "Ulusal",
            "ua": ua,
            "source": source,
            "display_name": display_name
        })

        if i % 10 == 0 or i == len(groups):
            print(f"  Fortschritt: {i}/{len(groups)} ({i/len(groups)*100:.1f}%)")

    save_cache(cache)
    print(f"[CACHE] Gespeichert: {len(cache)} Einträge")

    # 6. Zusammenführen: Verarbeitete Ulusal + unveränderte andere
    output_entries = processed_ulusal + other_entries

    elapsed = time.time() - start_time
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
    print(f"Original behalten:      {stats['original']}")
    print(f"Benötigte Zeit:         {elapsed:.1f} Sekunden")
    print("="*60)

    # 7. Neue M3U schreiben
    write_m3u(output_entries)
    print(f"\n[FERTIG] Playlist gespeichert als {OUTPUT_M3U}")
    print(f"[INFO] {len(output_entries)} Kanäle in der Playlist.")

if __name__ == "__main__":
    process_hybrid_m3u()
