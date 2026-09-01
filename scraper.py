import requests
from bs4 import BeautifulSoup
import datetime
import re
import time
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse, parse_qs, urlencode, quote, urljoin

class IPTVScraper:
    """Scraped IPTV-Streams von verschiedenen Quellen."""
    
    def __init__(self, debug=False):
        self.scraped_links = []
        self.debug = debug
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
        }
        self.results = {
            "famelack": [],
            "tvgarden": [],
            "canlitv": [],
            "working": []
        }
    
    def log(self, message, level="INFO"):
        if self.debug:
            timestamp = datetime.datetime.now().strftime("%H:%M:%S")
            print(f"[{timestamp}] [{level}] {message}")
        else:
            print(f"  [SCRAPER] {message}")
    
    def clean_channel_name(self, name):
        if not name:
            return ""
        name = re.sub(r'\s*(?:HD|FHD|UHD|4K|HEVC|RAW|SD|H265|H264|1080p|720p|576p|480p|360p)\s*', ' ', name, flags=re.IGNORECASE)
        name = re.sub(r'\s*\.(?:b|c|s)\s*$', '', name)
        name = re.sub(r'\s*\(BACKUP\)\s*', ' ', name, flags=re.IGNORECASE)
        name = re.sub(r'\s*\[.*?\]\s*', ' ', name)
        name = re.sub(r'\s+', ' ', name).strip()
        return name
    
    def get_channel_variants(self, channel_name):
        cleaned = self.clean_channel_name(channel_name)
        if not cleaned:
            return []
        
        variants = []
        variants.append(cleaned.lower())
        
        no_tv = re.sub(r'\s*tv\s*', ' ', cleaned.lower()).strip()
        if no_tv and no_tv != cleaned.lower():
            variants.append(no_tv)
        
        hyphen = cleaned.lower().replace(' ', '-')
        if hyphen != cleaned.lower():
            variants.append(hyphen)
        
        no_space = cleaned.lower().replace(' ', '')
        if no_space != cleaned.lower():
            variants.append(no_space)
        
        variants = list(dict.fromkeys(variants))
        return variants
    
    def test_link(self, url, timeout=3):
        if not url:
            return False
        try:
            response = requests.head(url, headers=self.headers, timeout=timeout, allow_redirects=True)
            if response.status_code == 200:
                return True
            if response.status_code in [403, 405]:
                headers = self.headers.copy()
                headers['Range'] = 'bytes=0-8192'
                response = requests.get(url, headers=headers, timeout=timeout, stream=True, allow_redirects=True)
                return response.status_code in [200, 206]
            return False
        except:
            return False
    
    # ============================================================
    # 1. FAMELACK (ercdn.net)
    # ============================================================
    
    def scrape_famelack(self, channel_name):
        self.log(f"Suche auf famelack für: {channel_name}")
        found = 0
        links = []
        
        variants = self.get_channel_variants(channel_name)
        
        cdn_domains = ["rnttwmjcin.turknet.ercdn.net"]
        path_prefixes = ["lcpmvefbyo"]
        qualities = ["1080p", "720p", "576p"]
        
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
                                self.log(f"  ✅ Famelack: {quality} gefunden")
                                break
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
    # 2. TVGARDEN.WORLD (KORRIGIERT)
    # ============================================================
    
    def scrape_tvgarden(self, channel_name):
        self.log(f"Suche auf tvgarden.world für: {channel_name}")
        found = 0
        links = []
        
        variants = self.get_channel_variants(channel_name)
        
        # TVGarden hat eine dynamische Seite mit JavaScript
        # Wir versuchen die API oder die HTML-Struktur zu parsen
        
        try:
            # Versuche die Hauptseite zu laden
            base_url = "https://tvgarden.world"
            response = requests.get(base_url, headers=self.headers, timeout=10)
            
            if response.status_code == 200:
                soup = BeautifulSoup(response.text, "html.parser")
                
                # Suche nach allen m3u8-Links im HTML
                m3u8_pattern = r'(https?://[^\s"\']+\.m3u8[^\s"\']*)'
                all_matches = re.findall(m3u8_pattern, response.text)
                
                # Suche nach iframes mit Streams
                iframes = soup.find_all('iframe', src=True)
                for iframe in iframes:
                    src = iframe.get('src', '')
                    if '.m3u8' in src:
                        all_matches.append(src)
                
                # Suche nach video-Elementen
                videos = soup.find_all(['video', 'source'], src=True)
                for video in videos:
                    src = video.get('src', '')
                    if '.m3u8' in src:
                        all_matches.append(src)
                
                # Filtere die Links nach Kanalnamen
                for match in all_matches:
                    for variant in variants[:3]:
                        if variant in match.lower() or variant.replace('-', '') in match.lower():
                            links.append(match)
                            found += 1
                            self.log(f"  ✅ TVGarden: {match[:80]}...")
                            break
                
                # Fallback: Durchsuche die gesamte Seite nach Kanalnamen
                if found == 0:
                    for variant in variants[:3]:
                        # Suche nach Links die den Kanalnamen enthalten
                        pattern = rf'(https?://[^\s"\']+\.m3u8[^\s"\']*{re.escape(variant)}[^\s"\']*)'
                        matches = re.findall(pattern, response.text, re.IGNORECASE)
                        for match in matches:
                            links.append(match)
                            found += 1
                            self.log(f"  ✅ TVGarden (Fallback): {match[:80]}...")
                        
                        # Suche nach iframes die den Kanalnamen enthalten
                        iframe_pattern = rf'<iframe[^>]*src=["\']([^"\']*{re.escape(variant)}[^"\']*)["\']'
                        iframe_matches = re.findall(iframe_pattern, response.text, re.IGNORECASE)
                        for match in iframe_matches:
                            full_url = urljoin(base_url, match)
                            links.append(full_url)
                            found += 1
                            self.log(f"  ✅ TVGarden (iframe): {full_url[:80]}...")
        
        except Exception as e:
            self.log(f"Fehler bei TVGarden: {e}")
        
        self.log(f"{found} TVGarden-Links gefunden.")
        self.results["tvgarden"] = links
        return links
    
    # ============================================================
    # 3. CANLITV.DIRECT
    # ============================================================
    
    def scrape_canlitv_direct(self, channel_name):
        self.log(f"Suche auf canlitv.direct für: {channel_name}")
        found = 0
        links = []
        
        variants = self.get_channel_variants(channel_name)
        
        for variant in variants[:3]:
            try:
                # Versuche verschiedene URL-Formate
                urls_to_try = [
                    f"https://web.canlitv.direct/{variant}",
                    f"https://web.canlitv.direct/tv/{variant}",
                    f"https://web.canlitv.direct/kanal/{variant}",
                    f"https://web.canlitv.direct/canli/{variant}",
                ]
                
                for url in urls_to_try:
                    response = requests.get(url, headers=self.headers, timeout=10)
                    if response.status_code != 200:
                        continue
                    
                    self.log(f"Seite gefunden: {url}")
                    
                    # Suche nach m3u8-Links
                    m3u8_pattern = r'(https?://[^\s"\']+\.m3u8[^\s"\']*)'
                    matches = re.findall(m3u8_pattern, response.text)
                    
                    for match in matches:
                        links.append(match)
                        found += 1
                        self.log(f"  ✅ Canlitv: {match[:80]}...")
                    
                    # Suche nach iframe-Quellen
                    soup = BeautifulSoup(response.text, "html.parser")
                    iframes = soup.find_all('iframe', src=True)
                    for iframe in iframes:
                        src = iframe.get('src', '')
                        if '.m3u8' in src:
                            links.append(src)
                            found += 1
                            self.log(f"  ✅ Canlitv (iframe): {src[:80]}...")
                    
                    # Suche nach player-Elementen
                    players = soup.find_all(['video', 'source'], src=True)
                    for player in players:
                        src = player.get('src', '')
                        if '.m3u8' in src:
                            links.append(src)
                            found += 1
                            self.log(f"  ✅ Canlitv (video): {src[:80]}...")
                    
                    break
                        
            except Exception as e:
                self.log(f"Fehler bei Canlitv für {variant}: {e}")
                continue
        
        self.log(f"{found} Canlitv-Links gefunden.")
        self.results["canlitv"] = links
        return links
    
    # ============================================================
    # 4. ALLE QUELLEN SCRAPEN
    # ============================================================
    
    def scrape_all_sources(self, channel_name, pages=2):
        """Durchläuft alle Quellen und sammelt Links."""
        self.scraped_links = []
        self.results = {
            "famelack": [],
            "tvgarden": [],
            "canlitv": [],
            "working": []
        }
        
        self.log(f"\n{'='*60}")
        self.log(f"SAMMLE LINKS FÜR: {channel_name.upper()}")
        self.log(f"{'='*60}")
        
        # 1. Famelack
        self.scrape_famelack(channel_name)
        
        # 2. TVGarden
        self.scrape_tvgarden(channel_name)
        
        # 3. Canlitv.direct
        self.scrape_canlitv_direct(channel_name)
        
        all_links = []
        for source, links in self.results.items():
            if source != "working":
                all_links.extend(links)
        
        self.scraped_links = list(dict.fromkeys(all_links))
        
        self.log(f"\n{'='*60}")
        self.log(f"ERGEBNIS FÜR: {channel_name.upper()}")
        self.log(f"{'='*60}")
        self.log(f"Famelack:       {len(self.results['famelack'])} Links")
        self.log(f"TVGarden:       {len(self.results['tvgarden'])} Links")
        self.log(f"Canlitv:        {len(self.results['canlitv'])} Links")
        self.log(f"Gesamt:         {len(self.scraped_links)} eindeutige Links")
        
        return self.scraped_links
    
    # ============================================================
    # 5. BESTEN LINK FINDEN
    # ============================================================
    
    def get_best_link(self, channel_name, pages=2, max_tests=10):
        """Gibt den besten funktionierenden Link zurück."""
        self.scrape_all_sources(channel_name, pages)
        
        if not self.scraped_links:
            self.log("Keine Links zum Testen.", "WARN")
            return None
        
        self.log(f"\nTeste {min(len(self.scraped_links), max_tests)} Links...")
        
        working = []
        for url in self.scraped_links[:max_tests]:
            if self.test_link(url):
                working.append(url)
                self.log(f"  ✅ Funktioniert: {url[:80]}...")
        
        if working:
            self.log(f"✅ {len(working)} funktionierende Links gefunden!")
            self.results["working"] = working
            return working[0]
        
        self.log("❌ Kein funktionierender Link gefunden.", "WARN")
        return None


# ============================================================
# TEST-FUNKTION
# ============================================================

if __name__ == "__main__":
    import sys
    
    scraper = IPTVScraper(debug=True)
    channel = sys.argv[1] if len(sys.argv) > 1 else "a para"
    print(f"\n🔍 Teste Scraper für: {channel}")
    link = scraper.get_best_link(channel, pages=2)
    if link:
        print(f"\n✅ Bester Link: {link}")
    else:
        print("\n❌ Kein Link gefunden.")
