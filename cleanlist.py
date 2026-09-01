import re

# Dateinamen im Repo
INPUT_FILE = "iptv.m3u"
OUTPUT_FILE = "iptv.m3u"

# Custom User-Agent Konfiguration
CUSTOM_USER_AGENT = "Vavoo/2.6 vypn.net App/1.0 Mozilla/5.0 (Windows NT 10.0; Win64; x64)"

def get_canonical_key(name):
    """
    Erstellt einen extrem sauberen Vergleichsschlüssel,
    damit z.B. '4K TR: 24 HD .b', '24 .s' und '24 HABER + .b'
    exakt denselben Schlüssel '24' ergeben.
    """
    if not name:
        return ""
    
    text = name.lower()

    # 1. Bekannte Qualitäts- & System-Tags entfernen
    text = re.sub(r'(?i)\b(4k|uhd|fhd|hd|sd|hevc|raw|1080p?|720p?|480p?|backup)\b', '', text)
    
    # 2. Präfixe wie "TR:", "DE:", "4K TR:" entfernen
    text = re.sub(r'^[a-z0-9\s]+:\s*', '', text)
    
    # 3. Suffixe wie .a, .b, .c, .s, + am Satzende entfernen
    text = re.sub(r'\s*[\.\+\-][a-z0-9]\b', '', text)
    text = re.sub(r'\s*[\+\-]\s*$', '', text)

    # 4. Sonderzeichen & zerschossene Umlaute vereinheitlichen (z.B. "S NEMA" -> "SINEMA")
    text = re.sub(r'\bs\s+nema\b', 'sinema', text)
    text = re.sub(r'\bm\s+n\s+ka\b', 'minika', text)
    text = re.sub(r'\bcnn\s+t\s+rk\b', 'cnn turk', text)
    
    # 5. Alle verbleibenden Nicht-Alphanumerischen Zeichen entfernen
    text = re.sub(r'[^a-z0-9]', '', text)
    
    return text

def calculate_score(name, url):
    """ Bewertet Qualität und Stabilität """
    score = 0
    n_lower, u_lower = name.lower(), url.lower()
    
    if '4k' in n_lower or 'uhd' in n_lower: score += 100
    elif 'fhd' in n_lower or '1080' in n_lower: score += 80
    elif 'hd' in n_lower or '720' in n_lower: score += 60
    elif 'sd' in n_lower: score += 20
    else: score += 40
    
    # Abzug für instabile Backup-Endungen
    if re.search(r'\.[b-z]\b', n_lower) or 'backup' in n_lower:
        score -= 30

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
        
        # Tags filtern (alte User-Agents entfernen)
        extra_tags = [line for line in lines[1:-1] if not ("http-user-agent" in line.lower() or "user-agent=" in line.lower())]

        raw_channel_name = extinf_line.split(',')[-1] if ',' in extinf_line else "Unbekannt"
        
        # Erzeugt den absolut eindeutigen Schlüssel für den Vergleich
        canonical_key = get_canonical_key(raw_channel_name)
        
        if not canonical_key:
            canonical_key = raw_channel_name.lower()

        score = calculate_score(raw_channel_name, url)

        # Nur übernehmen, wenn der Schlüssel neu ist ODER dieser Stream einen höheren Score hat
        if canonical_key not in channels or score > channels[canonical_key]['score']:
            
            # Anzeigename säubern (z.B. "4K TR: ATV HD .b" -> "TR: ATV")
            clean_display = re.sub(r'(?i)\b(4k|uhd|fhd|hd|sd|\.[a-z0-9]|backup)\b', '', raw_channel_name)
            clean_display = re.sub(r'\s*[\+\-]\s*$', '', clean_display).strip(" .-_")
            
            extinf_parts = extinf_line.split(",")
            extinf_parts[-1] = clean_display
            cleaned_extinf = ",".join(extinf_parts)

            channels[canonical_key] = {
                'score': score,
                'extinf': cleaned_extinf,
                'tags': extra_tags,
                'url': url
            }

    # Ausgabedatei schreiben
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

    print(f"Erfolg: {len(channels)} eindeutige Kanäle gespeichert.")

if __name__ == "__main__":
    process_m3u()
