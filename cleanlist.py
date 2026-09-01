import re

# Dateinamen im Repo
INPUT_FILE = "iptv.m3u"
OUTPUT_FILE = "iptv.m3u"

# Custom User-Agent & Header Konfiguration (Vavoo & vypn.net)
CUSTOM_USER_AGENT = "Vavoo/2.6 vypn.net App/1.0 Mozilla/5.0 (Windows NT 10.0; Win64; x64)"

def clean_string(text):
    """ Entfernt Qualitäts-Tags, Suffixe und Sonderzeichen zur Gruppenbildung """
    if not text:
        return ""
    # Auflösungen & Tags entfernen
    cleaned = re.sub(r'(?i)\b(4k|uhd|fhd|hd|sd|hevc|raw|1080p?|720p?|480p?)\b', '', text)
    # Suffixe wie .a, .b, .c, .s am Satzende oder freistehend entfernen
    cleaned = re.sub(r'\s*\.[a-z0-9]\b', '', cleaned, flags=re.IGNORECASE)
    # Backup-Tags & Klammern entfernen
    cleaned = re.sub(r'\s*\(\s*backup\s*\)', '', cleaned, flags=re.IGNORECASE)
    # Entfernt doppelte Leerzeichen & führende/nachfolgende Punkte/Striche
    cleaned = re.sub(r'\s+', ' ', cleaned).strip(" .-_")
    return cleaned.lower() # Lowercase für exakten Match im Dictionary

def calculate_score(name, url):
    """ Bewertet die Stabilität und Qualität des Streams """
    score = 0
    n_lower, u_lower = name.lower(), url.lower()
    
    # Qualitäts-Punkte
    if '4k' in n_lower or 'uhd' in n_lower: score += 100
    elif 'fhd' in n_lower or '1080' in n_lower: score += 80
    elif 'hd' in n_lower or '720' in n_lower: score += 60
    elif 'sd' in n_lower: score += 20
    else: score += 40
    
    # Abzug für potenziell instabile Backup-Suffixe (.b, .c)
    if re.search(r'\.[b-z]\b', n_lower):
        score -= 25

    # Punkte für zuverlässige Stream-Formate
    if '.m3u8' in u_lower: score += 20
    if 'workers.dev' in u_lower: score -= 10
    
    return score

def process_m3u():
    try:
        with open(INPUT_FILE, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
    except FileNotFoundError:
        print(f"Fehler: Datei {INPUT_FILE} nicht gefunden.")
        return

    raw_blocks = content.split('#EXTINF:')
    header = raw_blocks[0] if raw_blocks[0].startswith('#EXTM3U') else '#EXTM3U\n'
    
    channels = {}

    for block in raw_blocks[1:]:
        lines = [line.strip() for line in block.strip().splitlines() if line.strip()]
        if not lines:
            continue
            
        extinf_line = "#EXTINF:" + lines[0]
        url = lines[-1]
        
        # Tags filtern
        extra_tags = [line for line in lines[1:-1] if not ("http-user-agent" in line.lower() or "user-agent=" in line.lower())]

        # Kanalnamen extrahieren (nach dem Komma)
        raw_channel_name = extinf_line.split(',')[-1] if ',' in extinf_line else "Unbekannt"
        
        # Eindeutigen Vergleichsschlüssel erzeugen (Namen säubern)
        unique_key = clean_string(raw_channel_name)
        score = calculate_score(raw_channel_name, url)

        # WICHTIG: Wenn der Schlüssel schon existiert, behalten wir NUR die Variante mit dem höchsten Score!
        if unique_key not in channels or score > channels[unique_key]['score']:
            
            # Bereinigten Sendernamen in die EXTINF-Zeile zurückschreiben
            display_name = re.sub(r'(?i)\b(4k|uhd|fhd|hd|sd|\.[a-z0-9])\b', '', raw_channel_name).strip(" .-_")
            extinf_parts = extinf_line.split(",")
            extinf_parts[-1] = display_name
            cleaned_extinf = ",".join(extinf_parts)

            channels[unique_key] = {
                'score': score,
                'extinf': cleaned_extinf,
                'tags': extra_tags,
                'url': url
            }

    # Ausgabedatei schreiben (garantiert nur 1 Eintrag pro Unique Key)
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        f.write(header if header.endswith('\n') else header + '\n')
        for ch in channels.values():
            f.write(ch['extinf'] + '\n')
            f.write(f"#EXTVLCOPT:http-user-agent={CUSTOM_USER_AGENT}\n")
            f.write(f"#EXTHTTP:{{\"User-Agent\":\"{CUSTOM_USER_AGENT}\"}}\n")
            
            for tag in ch['tags']:
                f.write(tag + '\n')
                
            final_url = ch['url']
            if '|' not in final_url:
                final_url += f"|User-Agent={CUSTOM_USER_AGENT}"
                
            f.write(final_url + "\n")

    print(f"Erfolg: Es wurden {len(channels)} eindeutige Kanäle gespeichert.")

if __name__ == "__main__":
    process_m3u()