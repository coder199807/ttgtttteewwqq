import re
from collections import defaultdict

# Dateipfade
INPUT_FILE = "iptv.m3u"        # Name deiner Quell-Playlist im Repo
OUTPUT_FILE = "iptv.m3u"  # Ausgabedatei für den GitHub-Workflow

def clean_channel_name(name):
    """
    Entfernt Qualitätsbezeichnungen und Suffixe, um den Grundnamen zu isolieren.
    Aus '4K TR: ATV HD .b' wird z.B. 'TR: ATV'
    """
    cleaned = name
    # Auflösungen & Qualitäten entfernen
    cleaned = re.sub(r'(?i)\b(4k|uhd|fhd|hd|sd|hevc|raw|1080p?|720p?|480p?)\b', '', cleaned)
    # Suffixe wie .a, .b, .c, .s am Satzende oder Freistehend entfernen
    cleaned = re.sub(r'\s*\.[a-z0-9]\b', '', cleaned, flags=re.IGNORECASE)
    # Backup-Tags entfernen
    cleaned = re.sub(r'\s*\(\s*backup\s*\)', '', cleaned, flags=re.IGNORECASE)
    # Mehrfache Leerzeichen säubern
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned

def calculate_priority_score(name, url):
    """
    Berechnet einen Score für die Qualität/Stabilität.
    Höhere Zahl = Höhere Priorität.
    """
    score = 0
    name_lower = name.lower()
    url_lower = url.lower()

    # Qualichts-Ranking
    if '4k' in name_lower or 'uhd' in name_lower:
        score += 100
    elif 'fhd' in name_lower or '1080' in name_lower:
        score += 80
    elif 'hd' in name_lower or '720' in name_lower:
        score += 60
    elif 'sd' in name_lower:
        score += 20
    else:
        score += 40  # Standard-Wert, falls keine Qualität angegeben ist

    # Malus für volatile Suffix-Backups (.b, .c sind oft instabiler als Hauptstreams)
    if re.search(r'\.[b-z]\b', name_lower):
        score -= 15

    # Bonus für direkte HLS-Streams gegenüber Script/Worker-Weiterleitungen
    if '.m3u8' in url_lower:
        score += 10

    return score

def parse_and_clean_m3u(input_path, output_path):
    channels = defaultdict(list)
    header_line = "#EXTM3U\n"

    try:
        with open(input_path, 'r', encoding='utf-8', errors='ignore') as f:
            lines = f.readlines()
    except FileNotFoundError:
        print(f"Fehler: {input_path} wurde nicht gefunden.")
        return

    if lines and lines[0].startswith("#EXTM3U"):
        header_line = lines[0]

    current_metadata = []
    
    for line in lines:
        line_str = line.strip()
        if not line_str:
            continue

        if line_str.startswith("#EXTINF:"):
            current_metadata = [line_str]
        elif line_str.startswith("#") and current_metadata:
            # Ergänzende Tags wie #EXTVLCOPT oder #EXTHTTP mitnehmen
            current_metadata.append(line_str)
        elif not line_str.startswith("#") and current_metadata:
            url = line_str
            extinf = current_metadata[0]
            
            # Kanalnamen aus der EXTINF-Zeile extrahieren (nach dem letzten Komma)
            channel_name = extinf.split(",")[-1] if "," in extinf else "Unbekannt"
            clean_name = clean_channel_name(channel_name)
            
            score = calculate_priority_score(channel_name, url)

            channels[clean_name].append({
                'score': score,
                'metadata': current_metadata,
                'url': url,
                'clean_name': clean_name
            })
            current_metadata = []

    # Beste Variante pro Kanal auswählen und neu schreiben
    written_count = 0
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(header_line)
        
        for clean_name, variants in channels.items():
            # Sortiere Varianten absteigend nach Score
            best_variant = max(variants, key=lambda x: x['score'])
            
            # Passen den Sendernamen in der EXTINF-Zeile an das saubere Format an
            extinf_parts = best_variant['metadata'][0].split(",")
            extinf_parts[-1] = best_variant['clean_name']
            best_variant['metadata'][0] = ",".join(extinf_parts)

            # Schreiben in die Ausgabedatei
            for meta_line in best_variant['metadata']:
                f.write(meta_line + "\n")
            f.write(best_variant['url'] + "\n")
            written_count += 1

    print(f"Fertig! Ursprüngliche Gruppen reduziert auf {written_count} eindeutige Kanäle.")

if __name__ == "__main__":
    parse_and_clean_m3u(INPUT_FILE, OUTPUT_FILE)
