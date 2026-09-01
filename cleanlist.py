import re
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

INPUT_FILE = "iptv.m3u"
OUTPUT_FILE = "iptv.m3u"
CUSTOM_USER_AGENT = "Vavoo/2.6 vypn.net App/1.0 Mozilla/5.0 (Windows NT 10.0; Win64; x64)"

# Einstellungen für die Prüfung
MAX_WORKERS = 20
TIMEOUT = 3

def test_stream_url(url):
    """Prüft schnell, ob eine Stream-URL live antwortet (HTTP 200/206/302)"""
    headers = {'User-Agent': CUSTOM_USER_AGENT}
    clean_url = url.split('|')[0]
    
    try:
        response = requests.get(clean_url, headers=headers, timeout=TIMEOUT, stream=True)
        # 200 OK oder 206 Partial Content bedeuten, dass der Stream läuft
        if response.status_code in [200, 206]:
            return True
    except Exception:
        pass
    return False

def process_m3u():
    try:
        with open(INPUT_FILE, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
    except FileNotFoundError:
        print(f"Fehler: {INPUT_FILE} nicht gefunden.")
        return

    raw_blocks = content.split('#EXTINF:')
    header = raw_blocks[0] if raw_blocks[0].startswith('#EXTM3U') else '#EXTM3U\n'
    
    entries = []

    # 1. Alle Kanäle 1:1 einlesen (nichts löschen oder zusammenfassen)
    for block in raw_blocks[1:]:
        lines = [line.strip() for line in block.strip().splitlines() if line.strip()]
        if not lines: 
            continue
            
        extinf_line = "#EXTINF:" + lines[0]
        url = lines[-1]
        extra_tags = [l for l in lines[1:-1] if not ("http-user-agent" in l.lower() or "user-agent=" in l.lower())]

        entries.append({
            'extinf': extinf_line,
            'tags': extra_tags,
            'url': url,
            'working_url': None
        })

    print(f"{len(entries)} Kanäle geladen. Starte automatischen Stream-Check...")

    # 2. Parallel alle URLs auf Funktion prüfen
    def check_entry(entry):
        if test_stream_url(entry['url']):
            entry['working_url'] = entry['url']
        return entry

    working_count = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(check_entry, entry) for entry in entries]
        for future in as_completed(futures):
            res = future.result()
            if res['working_url']:
                working_count += 1

    # 3. Datei neu schreiben (1:1 Struktur, aber mit garantierten User-Agents)
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        f.write(header if header.endswith('\n') else header + '\n')
        
        for item in entries:
            f.write(item['extinf'] + '\n')
            f.write(f"#EXTVLCOPT:http-user-agent={CUSTOM_USER_AGENT}\n")
            f.write(f"#EXTHTTP:{{\"User-Agent\":\"{CUSTOM_USER_AGENT}\"}}\n")
            
            for tag in item['tags']:
                f.write(tag + '\n')
            
            # Verwendet die bestehende URL inkl. korrekter Syntax
            final_url = item['url']
            if '|' not in final_url:
                final_url += f"|User-Agent={CUSTOM_USER_AGENT}"
                
            f.write(final_url + "\n")

    print(f"Fertig! Alle {len(entries)} Kanäle wurden beibehalten. ({working_count} derzeit online)")

if __name__ == "__main__":
    process_m3u()
