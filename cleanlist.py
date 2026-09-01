import re
import requests
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor

VAVOO_M3U = "iptv.m3u"
OUTPUT_M3U = "iptv.m3u"
VOLO_BASE_URL = "https://tv.canlitvvolo.com"

CUSTOM_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
VAVOO_USER_AGENT = "Vavoo/2.6 vypn.net App/1.0 Mozilla/5.0 (Windows NT 10.0; Win64; x64)"

def get_canonical_key(name):
    """ Erstellt einen einheitlichen Vergleichsschlüssel für Namen """
    if not name: return ""
    text = name.lower()
    text = re.sub(r'(?i)\b(4k|uhd|fhd|hd|sd|hevc|raw|1080p?|720p?|480p?|backup)\b', '', text)
    text = re.sub(r'^[a-z0-9\s]+:\s*', '', text)
    text = re.sub(r'\s*[\.\+\-][a-z0-9]\b', '', text)
    text = re.sub(r'\s*[\+\-]\s*$', '', text)
    text = re.sub(r'[^a-z0-9]', '', text)
    return text

def scrape_volo_streams():
    """ Crawlt die Sender von tv.canlitvvolo.com und extrahiert m3u8 Links """
    print("Starte Scraping von CanliTVVolo...")
    volo_map = {}
    
    try:
        res = requests.get(VOLO_BASE_URL, headers={'User-Agent': CUSTOM_USER_AGENT}, timeout=5)
        if res.status_code != 200:
            print("Volo nicht erreichbar, fahre nur mit Vavoo fort.")
            return volo_map

        soup = BeautifulSoup(res.text, 'html.parser')
        channel_links = []
        for a in soup.find_all('a', href=True):
            href = a['href']
            if href.startswith('/') and len(href) > 2 and not any(x in href for x in ['#', 'privacy', 'contact', 'css', 'js']):
                channel_links.append(VOLO_BASE_URL + href)

        channel_links = list(set(channel_links))

        def extract_stream(url):
            try:
                r = requests.get(url, headers={'User-Agent': CUSTOM_USER_AGENT}, timeout=4)
                m3u8_matches = re.findall(r'https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*', r.text)
                if m3u8_matches:
                    channel_name = url.split('/')[-1].replace('-', ' ')
                    key = get_canonical_key(channel_name)
                    return key, m3u8_matches[0]
            except Exception:
                pass
            return None, None

        with ThreadPoolExecutor(max_workers=10) as executor:
            results = executor.map(extract_stream, channel_links)
            for key, stream_url in results:
                if key and stream_url:
                    if key not in volo_map:
                        volo_map[key] = []
                    volo_map[key].append(stream_url)

    except Exception as e:
        print(f"Fehler beim Scraping: {e}")

    print(f"Gefundene Volo-Sender: {len(volo_map)}")
    return volo_map

def process_hybrid_m3u():
    volo_streams = scrape_volo_streams()

    try:
        with open(VAVOO_M3U, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
    except FileNotFoundError:
        print(f"Fehler: {VAVOO_M3U} nicht gefunden.")
        return

    raw_blocks = content.split('#EXTINF:')
    header = raw_blocks[0] if raw_blocks[0].startswith('#EXTM3U') else '#EXTM3U\n'
    
    output_entries = []

    for block in raw_blocks[1:]:
        lines = [line.strip() for line in block.strip().splitlines() if line.strip()]
        if not lines: continue
            
        extinf = "#EXTINF:" + lines[0]
        vavoo_url = lines[-1]
        raw_name = extinf.split(',')[-1] if ',' in extinf else ""
        key = get_canonical_key(raw_name)

        if key in volo_streams and len(volo_streams[key]) > 0:
            chosen_url = volo_streams[key][0]
            ua = CUSTOM_USER_AGENT
        else:
            chosen_url = vavoo_url
            ua = VAVOO_USER_AGENT

        # Entferne eventuell bereits vorhandene Pipe-Parameter aus der URL
        clean_url = chosen_url.split('|')[0]

        output_entries.append({
            'extinf': extinf,
            'url': clean_url,
            'ua': ua
        })

    # M3U Schreiben ohne Pipe-Header an den URLs
    with open(OUTPUT_M3U, 'w', encoding='utf-8') as f:
        f.write(header if header.endswith('\n') else header + '\n')
        for item in output_entries:
            f.write(item['extinf'] + '\n')
            # User-Agent steht jetzt AUSSCHLIESSLICH im Tag-Header
            f.write(f"#EXTVLCOPT:http-user-agent={item['ua']}\n")
            f.write(f"#EXTHTTP:{{\"User-Agent\":\"{item['ua']}\"}}\n")
            f.write(item['url'] + "\n")

    print(f"Fertig! Hybride Liste mit {len(output_entries)} Kanälen ohne Pipe-UA generiert.")

if __name__ == "__main__":
    process_hybrid_m3u()
