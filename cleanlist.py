import re
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

INPUT_FILE = "iptv.m3u"
OUTPUT_FILE = "iptv.m3u"
CUSTOM_USER_AGENT = "Vavoo/2.6 vypn.net App/1.0 Mozilla/5.0 (Windows NT 10.0; Win64; x64)"

TIMEOUT = 3
MAX_WORKERS = 20

def get_canonical_key(name):
    """ Erstellt den Such-Schlüssel für den Sender """
    if not name: return ""
    text = name.lower()
    text = re.sub(r'(?i)\b(4k|uhd|fhd|hd|sd|hevc|raw|1080p?|720p?|480p?|backup)\b', '', text)
    text = re.sub(r'^[a-z0-9\s]+:\s*', '', text)
    text = re.sub(r'\s*[\.\+\-][a-z0-9]\b', '', text)
    text = re.sub(r'\s*[\+\-]\s*$', '', text)
    text = re.sub(r'[^a-z0-9]', '', text)
    return text

def test_url(url):
    """ Testet live, ob der Stream antwortet """
    clean_url = url.split('|')[0]
    try:
        r = requests.get(clean_url, headers={'User-Agent': CUSTOM_USER_AGENT}, timeout=TIMEOUT, stream=True)
        return r.status_code in [200, 206]
    except Exception:
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
    
    all_entries = []
    pool_by_key = {}

    # 1. Alle Kanäle einlesen und nach Sendernamen bündeln
    for block in raw_blocks[1:]:
        lines = [line.strip() for line in block.strip().splitlines() if line.strip()]
        if not lines: continue
            
        extinf = "#EXTINF:" + lines[0]
        url = lines[-1]
        raw_name = extinf.split(',')[-1] if ',' in extinf else "Unbekannt"
        key = get_canonical_key(raw_name)

        entry = {'extinf': extinf, 'url': url, 'name': raw_name, 'key': key}
        all_entries.append(entry)

        if key not in pool_by_key:
            pool_by_key[key] = []
        pool_by_key[key].append(url)

    # 2. URLs auf Erreichbarkeit testen
    unique_urls = list({e['url'] for e in all_entries})
    working_urls = set()

    print(f"Prüfe {len(unique_urls)} verschiedene Stream-URLs...")
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(test_url, url): url for url in unique_urls}
        for future in as_completed(futures):
            url = futures[future]
            if future.result():
                working_urls.add(url)

    # 3. Datei schreiben: Toten Links wird ein funktionierender Ersatz-Link desselben Senders zugewiesen
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        f.write(header if header.endswith('\n') else header + '\n')
        
        for entry in all_entries:
            key = entry['key']
            chosen_url = entry['url']

            # Wenn der eigene Link tot ist, suche nach einer funktionierenden Alternative im Pool
            if chosen_url not in working_urls:
                candidates = pool_by_key.get(key, [])
                for alt_url in candidates:
                    if alt_url in working_urls:
                        chosen_url = alt_url
                        break

            f.write(entry['extinf'] + '\n')
            f.write(f"#EXTVLCOPT:http-user-agent={CUSTOM_USER_AGENT}\n")
            f.write(f"#EXTHTTP:{{\"User-Agent\":\"{CUSTOM_USER_AGENT}\"}}\n")
            
            final_url = chosen_url
            if '|' not in final_url:
                final_url += f"|User-Agent={CUSTOM_USER_AGENT}"
            f.write(final_url + "\n")

    print(f"Fertig! Es wurden {len(all_entries)} Kanäle geschrieben (defekte Links wurden ausgetauscht).")

if __name__ == "__main__":
    process_m3u()
