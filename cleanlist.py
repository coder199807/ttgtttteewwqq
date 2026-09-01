import re
import requests
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor

VAVOO_M3U = "iptv.m3u"
OUTPUT_M3U = "iptv.m3u"

# Volo RSS Feed URLs
VOLO_RSS_URL = "https://tv.canlitvvolo.com/feed" 

CUSTOM_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
VAVOO_USER_AGENT = "Vavoo/2.6 vypn.net App/1.0 Mozilla/5.0 (Windows NT 10.0; Win64; x64)"

HEADERS = {
    'User-Agent': CUSTOM_USER_AGENT,
    'Accept': 'application/rss+xml, application/xml, text/xml, */*'
}

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

def extract_stream_from_page(url):
    """ Öffnet die KANALSEITE aus dem RSS-Feed und zieht die m3u8 URL """
    try:
        r = requests.get(url, headers={'User-Agent': CUSTOM_USER_AGENT}, timeout=5)
        if r.status_code != 200:
            return None, None
            
        # Direct m3u8 Suche im HTML/JS
        m3u8_matches = re.findall(r'(https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*)', r.text)
        if m3u8_matches:
            slug = url.rstrip('/').split('/')[-1].replace('-canli-izle', '').replace('-canli', '').replace('-', ' ')
            return get_canonical_key(slug), m3u8_matches[0]
            
        # Iframe-Suche falls Embed verwendet wird
        soup = BeautifulSoup(r.text, 'html.parser')
        for iframe in soup.find_all('iframe', src=True):
            iframe_src = iframe['src']
            if not iframe_src.startswith('http'):
                iframe_src = "https://tv.canlitvvolo.com" + (iframe_src if iframe_src.startswith('/') else '/' + iframe_src)
            
            ir = requests.get(iframe_src, headers={'User-Agent': CUSTOM_USER_AGENT}, timeout=4)
            iframe_matches = re.findall(r'(https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*)', ir.text)
            if iframe_matches:
                slug = url.rstrip('/').split('/')[-1].replace('-canli-izle', '').replace('-canli', '').replace('-', ' ')
                return get_canonical_key(slug), iframe_matches[0]
    except Exception:
        pass
    return None, None

def get_volo_streams_via_rss():
    print(f"Lade RSS-Feed von {VOLO_RSS_URL}...")
    volo_map = {}
    
    try:
        res = requests.get(VOLO_RSS_URL, headers=HEADERS, timeout=8)
        if res.status_code != 200:
            # Fallback falls Feed anders strukturiert ist
            res = requests.get("https://tv.canlitvvolo.com/rss", headers=HEADERS, timeout=8)
            
        if res.status_code == 200:
            root = ET.fromstring(res.content)
            channel_links = []

            # Auslesen aller <item><link> im RSS
            for item in root.findall('.//item'):
                link_elem = item.find('link')
                if link_elem is not None and link_elem.text:
                    channel_links.append(link_elem.text)

            print(f"{len(channel_links)} Sender aus dem RSS-Feed extrahiert. Hole Stream-URLs...")

            # Paralell die Links aus dem RSS-Feed auflösen
            with ThreadPoolExecutor(max_workers=10) as executor:
                results = executor.map(extract_stream_from_page, channel_links)
                for key, stream_url in results:
                    if key and stream_url:
                        if key not in volo_map:
                            volo_map[key] = []
                        volo_map[key].append(stream_url)

    except Exception as e:
        print(f"Fehler beim RSS-Parsing: {e}")

    print(f"Gefundene Volo-Sender über RSS: {len(volo_map)}")
    return volo_map

def process_hybrid_m3u():
    volo_streams = get_volo_streams_via_rss()

    try:
        with open(VAVOO_M3U, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
    except FileNotFoundError:
        print(f"Fehler: {VAVOO_M3U} nicht gefunden.")
        return

    raw_blocks = content.split('#EXTINF:')
    header = raw_blocks[0] if raw_blocks[0].startswith('#EXTM3U') else '#EXTM3U\n'
    
    output_entries = []
    replaced_count = 0

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
            replaced_count += 1
        else:
            chosen_url = vavoo_url
            ua = VAVOO_USER_AGENT

        clean_url = chosen_url.split('|')[0]

        output_entries.append({
            'extinf': extinf,
            'url': clean_url,
            'ua': ua
        })

    with open(OUTPUT_M3U, 'w', encoding='utf-8') as f:
        f.write(header if header.endswith('\n') else header + '\n')
        for item in output_entries:
            f.write(item['extinf'] + '\n')
            f.write(f"#EXTVLCOPT:http-user-agent={item['ua']}\n")
            f.write(f"#EXTHTTP:{{\"User-Agent\":\"{item['ua']}\"}}\n")
            f.write(item['url'] + "\n")

    print(f"Fertig! {replaced_count} Sender erfolgreich durch Volo-RSS-Streams ersetzt.")

if __name__ == "__main__":
    process_hybrid_m3u()
