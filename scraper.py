import requests
from bs4 import BeautifulSoup
import datetime
import re
import time
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse, parse_qs, urlencode, quote

class IPTVScraper:
    """Scraped IPTV-Streams von verschiedenen Quellen und testet sie."""
    
    def __init__(self, debug=False):
        self.scraped_links = []
        self.working_links = []
        self.debug = debug
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        self.channel_name = ""
        self.results = {
            "streamtest": [],
            "tvizle": [],
            "famelack": [],
            "volo": [],
            "globetv": [],
            "working": []
        }
    
    def log(self, message, level="INFO"):
        """Loggt eine Nachricht mit Zeitstempel."""
        if self.debug:
            timestamp = datetime.datetime.now().strftime("%H:%M:%S")
            print(f"[{timestamp}] [{level}] {message}")
        else:
            print(f"  [SCRAPER] {message}")
    
    def clean_channel_name(self, name):
        """Bereinigt den Kanalnamen für die Suche."""
        if not name:
            return ""
        # Entferne Zusätze wie HD, FHD, etc.
        name = re.sub(r'\s*(?:HD|FHD|UHD|4K|HEVC|RAW|SD|H265|H264|1080p|720p|576p|480p|360p)\s*', ' ', name, flags=re.IGNORECASE)
        name = re.sub(r'\s*\.(?:b|c|s)\s*$', '', name)
        name = re.sub(r'\s*\(BACKUP\)\s*', ' ', name, flags=re.IGNORECASE)
        name = re.sub(r'\s*\[.*?\]\s*', ' ', name)
        name = re.sub(r'\s+', ' ', name).strip()
        return name
    
    def get_channel_variants(self, channel_name):
        """Erzeugt verschiedene Varianten des Kanalnamens für die Suche."""
        cleaned = self.clean_channel_name(channel_name)
        if not cleaned:
            return []
        
        variants = []
        
        # Variante 1: Original (klein)
        variants.append(cleaned.lower())
        
        # Variante 2: Ohne "TV"
        no_tv = re.sub(r'\s*tv\s*', ' ', cleaned.lower()).strip()
        if no_tv and no_tv != cleaned.lower():
            variants.append(no_tv)
        
        # Variante 3: Mit Bindestrich
        hyphen = cleaned.lower().replace(' ', '-')
        if hyphen != cleaned.lower():
            variants.append(hyphen)
        
        # Variante 4: Ohne Leerzeichen
        no_space = cleaned.lower().replace(' ', '')
        if no_space != cleaned.lower():
            variants.append(no_space)
        
        # Variante 5: Nur erste 3-5 Buchstaben
        short = re.sub(r'[^a-z]', '', cleaned.lower())[:5]
        if len(short) >= 3:
            variants.append(short)
        
        # Entferne Duplikate
        variants = list(dict.fromkeys(variants))
        
        self.log(f"Variants: {variants}", "DEBUG")
        return variants
    
    def test_link(self, url, timeout=3):
        """Testet ob ein Link funktioniert."""
        if not url:
            return False
        try:
            response = requests.head(url, headers=self.headers, timeout=timeout, allow_redirects=True)
            return 200 <= response.status_code < 300
        except:
            return False
    
    def test_links_parallel(self, links, max_workers=10):
        """Testet mehrere Links parallel."""
        if not links:
            return []
        
        self.log(f"Teste {len(links)} Links parallel...")
        working = []
        
        def test_single(url):
            if self.test_link(url):
                return url
            return None
        
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(test_single, url): url for url in links}
            for future in as_completed(futures):
                result = future.result()
                if result:
                    working.append(result)
        
        self.log(f"{len(working)} von {len(links)} Links funktionieren.")
        return working
    
    # ============================================================
    # 1. STREAMTEST.IN
    # ============================================================
    
    def scrape_streamtest(self, channel_name, pages=2):
        """Scraped Streamtest.in nach m3u8-Links."""
        self.log(f"Suche auf streamtest.in für: {channel_name}")
        found = 0
        links = []
        
        variants = self.get_channel_variants(channel_name)
        
        for page in range(1, pages + 1):
            try:
                # Suche mit verschiedenen Varianten
                for variant in variants[:3]:
                    url = f"https://streamtest.in/logs/page/{page}?filter={variant}&is_public=true"
                    response = requests.get(url, headers=self.headers, timeout=10)
                    if response.status_code != 200:
                        continue
                    
                    soup = BeautifulSoup(response.text, "html.parser")
                    link_elements = soup.find_all('div', {'class': 'url is-size-6'})
                    
                    for link in link_elements:
                        url_text = link.text.strip()
                        if '.m3u8' in url_text and url_text.startswith('http'):
                            links.append(url_text)
                            found += 1
                            self.log(f"  ✅ {url_text[:80]}...", "DEBUG")
                    
                    time.sleep(0.3)
                    
            except Exception as e:
                self.log(f"Fehler bei Seite {page}: {e}", "ERROR")
                continue
        
        self.log(f"{found} Links auf streamtest.in gefunden.")
        self.results["streamtest"] = links
        return links
    
    # ============================================================
    # 2. TVIZLE.TR
    # ============================================================
    
    def scrape_tvizle(self, channel_name):
        """Scraped TVizle.tr nach m3u8-Links."""
        self.log(f"Suche auf tvizle.tr für: {channel_name}")
        found = 0
        links = []
        
        variants = self.get_channel_variants(channel_name)
        
        for variant in variants[:3]:
            try:
                urls_to_try = [
                    f"https://tvizle.tr/kanal/{variant}",
                    f"https://tvizle.tr/{variant}",
                    f"https://tvizle.tr/canli/{variant}",
                ]
                
                for url in urls_to_try:
                    response = requests.get(url, headers=self.headers, timeout=10)
                    if response.status_code != 200:
                        continue
                    
                    self.log(f"Seite gefunden: {url}", "DEBUG")
                    
                    # Suche nach m3u8-Links
                    m3u8_pattern = r'(https?://[^\s"\']+\.m3u8[^\s"\']*)'
                    matches = re.findall(m3u8_pattern, response.text)
                    
                    for match in matches:
                        if 'ensonhaber.com' in match or 'onrender.com' in match or 'tvizle' in match:
                            links.append(match)
                            found += 1
                            self.log(f"  ✅ {match[:80]}...", "DEBUG")
                    
                    # Suche nach iframe-Quellen
                    soup = BeautifulSoup(response.text, "html.parser")
                    iframes = soup.find_all('iframe', src=True)
                    for iframe in iframes:
                        src = iframe.get('src', '')
                        if '.m3u8' in src or 'ensonhaber.com' in src:
                            links.append(src)
                            found += 1
                            self.log(f"  ✅ iframe: {src[:80]}...", "DEBUG")
                    
                    # Suche nach Player-Elementen
                    players = soup.find_all(['video', 'source', 'div'], {'class': re.compile(r'player|video|stream', re.I)})
                    for player in players:
                        src = player.get('src') or player.get('data-src') or player.get('data-url')
                        if src and '.m3u8' in src:
                            links.append(src)
                            found += 1
                            self.log(f"  ✅ player: {src[:80]}...", "DEBUG")
                    
                    break  # Erfolgreich
                        
            except Exception as e:
                self.log(f"Fehler bei TVizle für {variant}: {e}", "ERROR")
                continue
        
        self.log(f"{found} Links auf tvizle.tr gefunden.")
        self.results["tvizle"] = links
        return links
    
    # ============================================================
    # 3. FAMELACK (ercdn.net)
    # ============================================================
    
    def scrape_famelack(self, channel_name):
        """Scraped Famelack-Streams (ercdn.net)."""
        self.log(f"Suche auf famelack für: {channel_name}")
        found = 0
        links = []
        
        variants = self.get_channel_variants(channel_name)
        
        cdn_domains = [
            "rnttwmjcin.turknet.ercdn.net",
            "cdn1.famelack.com",
            "cdn2.famelack.com",
            "famelack.com",
        ]
        
        path_prefixes = ["lcpmvefbyo", "streams", "live", "hls"]
        qualities = ["1080p", "720p", "576p", "480p", "360p"]
        
        for variant in variants[:3]:
            for domain in cdn_domains:
                for prefix in path_prefixes:
                    for quality in qualities:
                        url = f"https://{domain}/{prefix}/{variant}/{variant}_{quality}.m3u8"
                        try:
                            response = requests.head(url, headers=self.headers, timeout=3)
                            if response.status_code == 200:
                                links.append(url)
                                found += 1
                                self.log(f"  ✅ {url}", "DEBUG")
                                break  # Eine Qualität reicht
                        except:
                            continue
                    if found > 0:
                        break
                if found > 0:
                    break
            if found > 0:
                break
        
        self.log(f"{found} Famelack-Links gefunden.")
        self.results["famelack"] = links
        return links
    
    # ============================================================
    # 4. VOLO TV (canlitvvolo.com)
    # ============================================================
    
    def scrape_volo(self, channel_name):
        """Scraped Volo TV (canlitvvolo.com)."""
        self.log(f"Suche auf volo für: {channel_name}")
        found = 0
        links = []
        
        variants = self.get_channel_variants(channel_name)
        
        for variant in variants[:3]:
            try:
                # Versuche verschiedene URLs
                urls_to_try = [
                    f"https://tv.canlitvvolo.com/{variant}",
                    f"https://tv.canlitvvolo.com/{variant}-canli-izle",
                    f"https://tv.canlitvvolo.com/{variant}-hd",
                ]
                
                for url in urls_to_try:
                    response = requests.get(url, headers=self.headers, timeout=10)
                    if response.status_code != 200:
                        continue
                    
                    self.log(f"Seite gefunden: {url}", "DEBUG")
                    
                    # Suche nach m3u8-Links
                    m3u8_pattern = r'(https?://[^\s"\']+\.m3u8[^\s"\']*)'
                    matches = re.findall(m3u8_pattern, response.text)
                    
                    for match in matches:
                        if 'lg.mncdn.com' in match or 'volo' in match:
                            links.append(match)
                            found += 1
                            self.log(f"  ✅ {match[:80]}...", "DEBUG")
                    
                    # Suche nach iframe-Quellen
                    soup = BeautifulSoup(response.text, "html.parser")
                    iframes = soup.find_all('iframe', src=True)
                    for iframe in iframes:
                        src = iframe.get('src', '')
                        if '.m3u8' in src or 'lg.mncdn.com' in src:
                            links.append(src)
                            found += 1
                            self.log(f"  ✅ iframe: {src[:80]}...", "DEBUG")
                    
                    break  # Erfolgreich
                        
            except Exception as e:
                self.log(f"Fehler bei Volo für {variant}: {e}", "ERROR")
                continue
        
        self.log(f"{found} Volo-Links gefunden.")
        self.results["volo"] = links
        return links
    
    # ============================================================
    # 5. GLOBETV.APP (über iptv-org)
    # ============================================================
    
    def scrape_globetv(self, channel_name):
        """Scraped GlobeTV (über iptv-org)."""
        self.log(f"Suche auf globetv für: {channel_name}")
        found = 0
        links = []
        
        variants = self.get_channel_variants(channel_name)
        
        # GlobeTV nutzt iptv-org als Basis
        iptv_urls = [
            "https://raw.githubusercontent.com/iptv-org/iptv/master/streams/tr.m3u",
            "https://raw.githubusercontent.com/iptv-org/iptv/master/streams/de.m3u",
            "https://raw.githubusercontent.com/iptv-org/iptv/master/streams/at.m3u",
            "https://raw.githubusercontent.com/iptv-org/iptv/master/streams/ch.m3u",
            "https://raw.githubusercontent.com/iptv-org/iptv/master/streams/xx.m3u",
        ]
        
        for iptv_url in iptv_urls:
            try:
                response = requests.get(iptv_url, headers=self.headers, timeout=10)
                if response.status_code != 200:
                    continue
                
                # Parse die M3U
                lines = response.text.splitlines()
                for i, line in enumerate(lines):
                    if line.startswith("#EXTINF:"):
                        # Extrahiere den Namen
                        name_match = re.search(r',([^,]+)$', line)
                        if not name_match:
                            continue
                        name = name_match.group(1).strip()
                        
                        # Prüfe ob der Name zum Kanal passt
                        name_clean = self.clean_channel_name(name).lower()
                        channel_clean = self.clean_channel_name(channel_name).lower()
                        
                        if channel_clean in name_clean or name_clean in channel_clean:
                            # Hole die nächste Zeile (URL)
                            if i + 1 < len(lines):
                                url_line = lines[i + 1].strip()
                                if '.m3u8' in url_line and url_line.startswith('http'):
                                    links.append(url_line)
                                    found += 1
                                    self.log(f"  ✅ {url_line[:80]}...", "DEBUG")
                
                time.sleep(0.5)
                
            except Exception as e:
                self.log(f"Fehler bei iptv-org: {e}", "ERROR")
                continue
        
        self.log(f"{found} GlobeTV-Links gefunden.")
        self.results["globetv"] = links
        return links
    
    # ============================================================
    # 6. ALLE QUELLEN SCRAPEN
    # ============================================================
    
    def scrape_all_sources(self, channel_name, pages=2):
        """Durchläuft alle Quellen und sammelt Links."""
        self.channel_name = channel_name
        self.scraped_links = []
        self.results = {
            "streamtest": [],
            "tvizle": [],
            "famelack": [],
            "volo": [],
            "globetv": [],
            "working": []
        }
        
        self.log(f"\n{'='*60}")
        self.log(f"SAMMLE LINKS FÜR: {channel_name.upper()}")
        self.log(f"{'='*60}")
        
        # 1. Streamtest.in
        self.scrape_streamtest(channel_name, pages)
        
        # 2. TVizle
        self.scrape_tvizle(channel_name)
        
        # 3. Famelack
        self.scrape_famelack(channel_name)
        
        # 4. Volo
        self.scrape_volo(channel_name)
        
        # 5. GlobeTV (iptv-org)
        self.scrape_globetv(channel_name)
        
        # Sammle alle Links
        all_links = []
        for source, links in self.results.items():
            if source != "working":
                all_links.extend(links)
        
        # Entferne Duplikate
        self.scraped_links = list(dict.fromkeys(all_links))
        
        self.log(f"\n{'='*60}")
        self.log(f"ERGEBNIS FÜR: {channel_name.upper()}")
        self.log(f"{'='*60}")
        self.log(f"Streamtest.in:  {len(self.results['streamtest'])} Links")
        self.log(f"TVizle:         {len(self.results['tvizle'])} Links")
        self.log(f"Famelack:       {len(self.results['famelack'])} Links")
        self.log(f"Volo:           {len(self.results['volo'])} Links")
        self.log(f"GlobeTV:        {len(self.results['globetv'])} Links")
        self.log(f"Gesamt:         {len(self.scraped_links)} eindeutige Links")
        
        return self.scraped_links
    
    # ============================================================
    # 7. BESTEN LINK FINDEN
    # ============================================================
    
    def get_best_link(self, channel_name, pages=2, max_tests=10):
        """Gibt den besten funktionierenden Link zurück."""
        self.scrape_all_sources(channel_name, pages)
        
        if not self.scraped_links:
            self.log("Keine Links zum Testen.", "WARN")
            return None
        
        self.log(f"\nTeste {min(len(self.scraped_links), max_tests)} Links...")
        
        # Teste Links parallel
        working = self.test_links_parallel(self.scraped_links[:max_tests])
        
        if working:
            self.log(f"✅ {len(working)} funktionierende Links gefunden!")
            self.results["working"] = working
            return working[0]  # Gib den ersten funktionierenden Link zurück
        
        self.log("❌ Kein funktionierender Link gefunden.", "WARN")
        return None
    
    # ============================================================
    # 8. FÜR EINEN KANAL REPARIEREN
    # ============================================================
    
    def repair_channel(self, channel_name, original_url=None):
        """
        Repariert einen Kanal: Findet einen funktionierenden Link.
        """
        self.log(f"\n🔧 Repariere: {channel_name}")
        
        # Besten Link finden
        best_link = self.get_best_link(channel_name)
        
        if best_link:
            return {
                "channel": channel_name,
                "original_url": original_url,
                "new_url": best_link,
                "source": "scraper",
                "working": True
            }
        else:
            return {
                "channel": channel_name,
                "original_url": original_url,
                "new_url": original_url,
                "source": "failed",
                "working": False
            }


# ============================================================
# BATCH-REPAIR FÜR MEHRERE KANÄLE
# ============================================================

def batch_repair(channels, pages=1, max_workers=5):
    """
    Repariert mehrere Kanäle parallel.
    """
    print(f"\n{'='*60}")
    print(f"BATCH-REPAIR FÜR {len(channels)} KANÄLE")
    print(f"{'='*60}")
    
    scraper = IPTVScraper(debug=False)
    results = []
    stats = {
        "working": 0,
        "failed": 0
    }
    
    def repair_single(channel):
        return scraper.repair_channel(channel)
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(repair_single, channel): channel for channel in channels}
        
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            if result["working"]:
                stats["working"] += 1
            else:
                stats["failed"] += 1
            
            print(f"  Fortschritt: {len(results)}/{len(channels)} ({len(results)/len(channels)*100:.1f}%)")
    
    print(f"\n{'='*60}")
    print(f"BATCH-REPAIR STATISTIK")
    print(f"{'='*60}")
    print(f"Gesamt:         {len(results)}")
    print(f"Repariert:      {stats['working']}")
    print(f"Nicht repariert: {stats['failed']}")
    print(f"{'='*60}")
    
    return results


# ============================================================
# TEST-FUNKTION
# ============================================================

if __name__ == "__main__":
    import sys
    
    # Teste mit einem oder mehreren Kanälen
    if len(sys.argv) > 1:
        channels = sys.argv[1].split(',')
        pages = int(sys.argv[2]) if len(sys.argv) > 2 else 2
        results = batch_repair(channels, pages)
        
        print("\n📋 DETAILS:")
        for r in results:
            status = "✅" if r["working"] else "❌"
            print(f"  {status} {r['channel']} → {r['new_url'][:60] if r['new_url'] else 'Kein Link'}...")
    else:
        # Einzelner Test
        scraper = IPTVScraper(debug=True)
        channel = "atv"
        print(f"\n🔍 Teste Scraper für: {channel}")
        link = scraper.get_best_link(channel, pages=2)
        if link:
            print(f"\n✅ Bester Link: {link}")
        else:
            print("\n❌ Kein Link gefunden.")
