import re
import html
import unicodedata
import requests
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from bs4 import BeautifulSoup

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
# HILFSFUNKTIONEN
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
    text = text.lower()
    # Entferne häufige technische Angaben für besseres Matching
    text = re.sub(r'\s*(?:hd|fhd|uhd|sd|hevc|raw|backup|canli|izle|tv)\s*', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text

def get_canonical_key(name):
    """Erstellt einen robusten Vergleichsschlüssel aus dem Sendernamen."""
    if not name:
        return ""
    text = normalize_text(name)
    # Entferne alles außer Buchstaben und Zahlen
    text = re.sub(r'[^a-z0-9]', '', text)
    return text

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
        # URL
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
# MATCHING: KANALNAMEN NORMALISIEREN
# ============================================================

def build_backup_index(backup_entries):
    """
    Baut einen Index der Backup-Kanäle auf.
    Key = normalisierter Name, Value = Eintrag
    """
    index = {}
    for entry in backup_entries:
        name = get_extinf_name(entry["extinf"])
        if not name:
            continue
        key = get_canonical_key(name)
        if key:
            # Falls es mehrere Einträge mit gleichem Key gibt, nimm den ersten
            if key not in index:
                index[key] = entry
    print(f"[BACKUP] Index aufgebaut: {len(index)} eindeutige Kanäle.")
    return index

def find_matching_backup(channel_name, backup_index):
    """
    Findet den passendsten Backup-Stream für einen Kanal.
    """
    if not channel_name or not backup_index:
        return None
    
    # 1. Exakter Match (normalisiert)
    key = get_canonical_key(channel_name)
    if key in backup_index:
        return backup_index[key]
    
    # 2. Teil-Match (wenn der Name ähnlich ist)
    channel_key = get_canonical_key(channel_name)
    if len(channel_key) < 4:
        return None
    
    best_match = None
    best_score = 0
    
    for backup_key, entry in backup_index.items():
        if not backup_key or len(backup_key) < 4:
            continue
        
        # Prüfe ob einer den anderen enthält
        if channel_key in backup_key or backup_key in channel_key:
            score = min(len(channel_key), len(backup_key)) / max(len(channel_key), len(backup_key))
            if score > best_score:
                best_score = score
                best_match = entry
    
    # Nur wenn die Ähnlichkeit > 70% ist
    if best_score >= 0.7:
        return best_match
    
    return None

# ============================================================
# HAUPTPROZESS
# ============================================================

def process_hybrid_m3u():
    print("\n" + "="*60)
    print("IPTV REPAIR TOOL")
    print("Vavoo → Backup-M3U (m3u.work)")
    print("="*60)

    # 1. Backup-M3U laden
    backup_entries = load_backup_m3u()
    if not backup_entries:
        print("[FEHLER] Backup-M3U konnte nicht geladen werden.")
        print("[INFO] Verwende nur Vavoo-Streams (keine Reparatur).")
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

    # 3. Kanäle ersetzen
    output_entries = []
    replaced_count = 0
    failed_count = 0

    print("\n[START] Verarbeite Kanäle...")

    for i, entry in enumerate(entries, 1):
        extinf = entry["extinf"]
        original_url = entry["url"]
        channel_name = get_extinf_name(extinf)

        # Zeige Fortschritt
        if i % 50 == 0:
            print(f"  Fortschritt: {i}/{len(entries)} ({i/len(entries)*100:.1f}%)")

        # Suche in Backup-M3U
        backup_match = find_matching_backup(channel_name, backup_index)

        if backup_match:
            # Backup-Stream verwenden
            backup_url = backup_match["url"]
            output_entries.append({
                "extinf": extinf,
                "url": backup_url,
                "ua": CUSTOM_USER_AGENT,
            })
            replaced_count += 1
        else:
            # Originalen Vavoo-Stream behalten (oder defekt)
            output_entries.append({
                "extinf": extinf,
                "url": clean_stream_url(original_url),
                "ua": VAVOO_USER_AGENT,
            })
            failed_count += 1

    # 4. Statistik
    print("\n" + "="*60)
    print("STATISTIK")
    print("="*60)
    print(f"Gesamt:                 {len(output_entries)}")
    print(f"Durch Backup ersetzt:   {replaced_count}")
    print(f"Nicht ersetzt:          {failed_count}")
    print(f"Backup-Kanäle:          {len(backup_entries)}")
    print(f"Backup-Index:           {len(backup_index)}")
    print("="*60)

    # 5. Neue M3U schreiben
    write_m3u(output_entries)
    print(f"\n[FERTIG] Playlist gespeichert als {OUTPUT_M3U}")

if __name__ == "__main__":
    process_hybrid_m3u()
