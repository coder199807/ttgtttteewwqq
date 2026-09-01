import re
import html
import unicodedata
import requests
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from bs4 import BeautifulSoup
from collections import defaultdict

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
REQUEST_TIMEOUT = 15
MAX_WORKERS = 10

# ============================================================
# KANAL-ZUORDNUNG (MANUELLE KORREKTUR)
# ============================================================

# Hier definieren wir, welcher Backup-Stream zu welchem Kanal gehört
# Format: "kanalname" : "backup-kanalname"
CHANNEL_MAPPING = {
    # === Türkische Hauptsender ===
    "star tv": "star tv",
    "atv": "atv",
    "kanal d": "kanal d",
    "show tv": "show tv",
    "fox": "fox",
    "now tv": "now tv",
    "tv8": "tv8",
    "teve2": "teve2",
    "kanal 7": "kanal 7",
    "beyaz tv": "beyaz tv",
    "360": "360",
    "tv100": "tv100",
    "a2": "a2",
    "24 tv": "24 tv",
    "kanal 24": "24 tv",
    
    # === Haberkanäle ===
    "trt haber": "trt haber",
    "ntv": "ntv",
    "cnn turk": "cnn turk",
    "haberturk": "haberturk",
    "a haber": "a haber",
    "tele 1": "tele 1",
    "halk tv": "halk tv",
    "sozcu tv": "sozcu tv",
    "tgrts": "tgrts",
    "bloomberg ht": "bloomberg ht",
    
    # === Sportkanäle ===
    "a spor": "a spor",
    "s sport": "s sport",
    "trt spor": "trt spor",
    "beinsports": "beinsports",
    "tivibu": "tivibu",
    "exxen": "exxen",
    "spor smart": "spor smart",
    "fb tv": "fb tv",
    "gs tv": "gs tv",
    
    # === Deutsche Sender ===
    "rtl": "rtl",
    "prosieben": "prosieben",
    "sat.1": "sat.1",
    "vox": "vox",
    "zdf": "zdf",
    "ard": "ard",
    "das erste": "ard",
    "rtl 2": "rtl 2",
    "rtl ii": "rtl 2",
    "nitro": "nitro",
    "rtl nitro": "nitro",
    "kabel eins": "kabel eins",
    "kabel 1": "kabel eins",
    "super rtl": "super rtl",
    "dmax": "dmax",
    "welt": "welt",
    "n-tv": "n-tv",
    "phoenix": "phoenix",
    "tagesschau24": "tagesschau24",
    "sixx": "sixx",
    "tele 5": "tele 5",
    "pro7maxx": "pro7maxx",
    
    # === Österreich/Schweiz ===
    "orf": "orf",
    "srf": "srf",
    "servus tv": "servus tv",
    
    # === Kinder ===
    "trt cocuk": "trt cocuk",
    "cartoon network": "cartoon network",
    "disney": "disney",
    "nickelodeon": "nickelodeon",
    "minika": "minika",
    "kika": "kika",
    
    # === Doku ===
    "discovery": "discovery",
    "nat geo": "nat geo",
    "national geographic": "nat geo",
    "history": "history",
    "animal planet": "animal planet",
    "viasat": "viasat",
    "trt belgesel": "trt belgesel",
    
    # === Film ===
    "sinema": "sinema",
    "movies": "sinema",
    "bein movies": "bein movies",
    "showmax": "showmax",
    "yaban tv": "yaban tv",
    "yesilcam": "yesilcam",
}

def get_mapped_name(channel_name):
    """
    Wendet die manuelle Korrektur-Tabelle an.
    Gibt den korrigierten Kanalnamen zurück oder None.
    """
    if not channel_name:
        return None
    
    # Bereinige den Namen
    cleaned = clean_channel_name(channel_name).lower()
    
    # Prüfe ob es einen Eintrag in der Mapping-Tabelle gibt
    if cleaned in CHANNEL_MAPPING:
        return CHANNEL_MAPPING[cleaned]
    
    # Prüfe Teil-Matches
    for key, value in CHANNEL_MAPPING.items():
        if key in cleaned or cleaned in key:
            return value
    
    return None

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
    """
    Bereinigt den Sendernamen von allen Zusätzen.
    """
    if not name:
        return ""
    
    text = str(name)
    
    # Entferne Präfixe
    text = re.sub(r'^(?:4K\s*TR:|4K:|TR:|DE:|AT:|CH:|VF:|HD\s*:)\s*', '', text, flags=re.IGNORECASE)
    
    # Entferne Suffixe
    text = re.sub(r'\s*\.(?:b|c|s)\s*$', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\s*\(BACKUP\)\s*', ' ', text, flags=re.IGNORECASE)
    text = re.sub(r'\s*\(H265\)\s*', ' ', text, flags=re.IGNORECASE)
    text = re.sub(r'\s*\[.*?\]\s*', ' ', text, flags=re.IGNORECASE)
    
    # Entferne Qualitätsangaben
    text = re.sub(r'\s*(?:HD|FHD|UHD|4K|HEVC|RAW|SD|H265|H264|HEVC|X265|X264|1080p|720p|576p|480p|360p)\s*', ' ', text, flags=re.IGNORECASE)
    
    # Entferne doppelte Leerzeichen
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

# ============================================================
# BACKUP-M3U LADEN
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

# ============================================================
# MATCHING MIT KORREKTUR-TABELLE
# ============================================================

def build_backup_index(backup_entries):
    """
    Baut einen Index der Backup-Kanäle auf.
    Key = bereinigter Sendername, Value = Eintrag
    """
    index = {}
    for entry in backup_entries:
        name = get_extinf_name(entry["extinf"])
        if not name:
            continue
        key = get_canonical_key(name)
        if key:
            index[key] = entry
    
    print(f"[BACKUP] Index aufgebaut: {len(index)} eindeutige Kanäle.")
    return index

def find_backup_for_channel(channel_name, backup_index):
    """
    Findet für jeden Kanal einen funktionierenden Backup-Link.
    Verwendet zuerst die manuelle Mapping-Tabelle.
    """
    if not channel_name:
        return None
    
    # 1. Prüfe ob es eine manuelle Korrektur gibt
    mapped_name = get_mapped_name(channel_name)
    if mapped_name:
        mapped_key = get_canonical_key(mapped_name)
        if mapped_key and mapped_key in backup_index:
            print(f"    ✅ Korrektur: {get_display_name(channel_name)} → {mapped_name}")
            return backup_index[mapped_key]
    
    # 2. Normales Matching
    channel_key = get_canonical_key(channel_name)
    channel_display = get_display_name(channel_name)
    
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
# HAUPTPROZESS
# ============================================================

def process_hybrid_m3u():
    print("\n" + "="*60)
    print("IPTV REPAIR TOOL MIT KANAL-KORREKTUR")
    print("JEDER KANAL BEKOMMT DEN RICHTIGEN STREAM")
    print("="*60)

    # 1. Backup-M3U laden
    backup_entries = load_backup_m3u()
    if not backup_entries:
        print("[FEHLER] Backup-M3U konnte nicht geladen werden.")
        return

    backup_index = build_backup_index(backup_entries)

    # 2. Haupt-M3U lesen
    try:
        with open(INPUT_M3U, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except FileNotFoundError:
        print(f"[FEHLER] {INPUT_M3U} nicht gefunden.")
        return

    entries = parse_m3u(content)
    print(f"\n[M3U] {len(entries)} Kanäle gelesen.")

    # 3. Kanäle verarbeiten
    output_entries = []
    replaced_count = 0
    failed_count = 0
    corrected_count = 0
    match_details = []

    print("\n[START] Verarbeite Kanäle...")

    for i, entry in enumerate(entries, 1):
        extinf = entry["extinf"]
        original_url = entry["url"]
        channel_name = get_extinf_name(extinf)
        display_name = get_display_name(channel_name)

        if i % 50 == 0:
            print(f"  Fortschritt: {i}/{len(entries)} ({i/len(entries)*100:.1f}%)")

        # Backup-Match finden (mit Korrektur)
        backup_match = find_backup_for_channel(channel_name, backup_index)

        if backup_match:
            backup_url = backup_match["url"]
            output_entries.append({
                "extinf": extinf,
                "url": backup_url,
                "ua": CUSTOM_USER_AGENT,
            })
            replaced_count += 1
            
            # Prüfe ob es eine Korrektur war
            mapped_name = get_mapped_name(channel_name)
            if mapped_name:
                corrected_count += 1
            
            if len(match_details) < 30:
                backup_name = get_extinf_name(backup_match["extinf"])
                match_details.append(f"  ✅ {display_name} → {get_display_name(backup_name)}")
        else:
            output_entries.append({
                "extinf": extinf,
                "url": clean_stream_url(original_url),
                "ua": VAVOO_USER_AGENT,
            })
            failed_count += 1
            if len(match_details) < 30:
                match_details.append(f"  ❌ {display_name} → kein Match")

    # 4. Statistik
    print("\n" + "="*60)
    print("STATISTIK")
    print("="*60)
    print(f"Gesamt:                 {len(output_entries)}")
    print(f"Durch Backup ersetzt:   {replaced_count}")
    print(f"  Davon korrigiert:     {corrected_count}")
    print(f"Nicht ersetzt:          {failed_count}")
    print(f"Backup-Kanäle:          {len(backup_entries)}")
    print(f"Backup-Index:           {len(backup_index)}")
    print("="*60)

    # 5. Details anzeigen
    print("\n[DETAILS] Erste 30 Ergebnisse:")
    for detail in match_details[:30]:
        print(detail)

    # 6. Neue M3U schreiben
    write_m3u(output_entries)
    print(f"\n[FERTIG] Playlist gespeichert als {OUTPUT_M3U}")

if __name__ == "__main__":
    process_hybrid_m3u()
