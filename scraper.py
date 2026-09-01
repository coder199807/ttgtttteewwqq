import requests
from bs4 import BeautifulSoup
import datetime
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

class IPTVScraper:
    """Scraped IPTV-Streams von verschiedenen Quellen."""
    
    def __init__(self):
        self.scraped_links = []
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
    
    def scrape_streamtest(self, channel_name, pages=3):
        """
        Scraped Streamtest.in nach m3u8-Links für einen Kanal.
        """
        print(f"  [SCRAPER] Suche auf streamtest.in für: {channel_name}")
        found = 0
        
        for page in range(1, pages + 1):
            try:
                url = f"https://streamtest.in/logs/page/{page}?filter={channel_name}&is_public=true"
                response = requests.get(url, headers=self.headers, timeout=10)
                if response.status_code != 200:
                    continue
                
                soup = BeautifulSoup(response.text, "html.parser")
                links = soup.find_all('div', {'class': 'url is-size-6'})
                
                for link in links:
                    url_text = link.text.strip()
                    if '.m3u8' in url_text and url_text.startswith('http'):
                        self.scraped_links.append(url_text)
                        found += 1
                
                time.sleep(0.5)  # Rate-Limiting
                
            except Exception as e:
                print(f"    Fehler bei Seite {page}: {e}")
                continue
        
        print(f"  [SCRAPER] {found} Links auf streamtest.in gefunden.")
        return self.scraped_links
    
    def scrape_tvizle(self, channel_name):
        """
        Scraped TVizle.tr nach m3u8-Links.
        """
        print(f"  [SCRAPER] Suche auf tvizle.tr für: {channel_name}")
        found = 0
        
        try:
            # TVizle Kanal-Seite
            url = f"https://tvizle.tr/kanal/{channel_name.lower().replace(' ', '-')}"
            response = requests.get(url, headers=self.headers, timeout=10)
            
            if response.status_code == 200:
                # Suche nach m3u8-Links im HTML
                m3u8_pattern = r'(https?://[^\s"\']+\.m3u8[^\s"\']*)'
                matches = re.findall(m3u8_pattern, response.text)
                
                for match in matches:
                    if 'ensonhaber.com' in match or 'onrender.com' in match:
                        self.scraped_links.append(match)
                        found += 1
                
                # Suche nach iframe-Quellen
                soup = BeautifulSoup(response.text, "html.parser")
                iframes = soup.find_all('iframe', src=True)
                for iframe in iframes:
                    src = iframe.get('src', '')
                    if '.m3u8' in src or 'ensonhaber.com' in src:
                        self.scraped_links.append(src)
                        found += 1
                        
        except Exception as e:
            print(f"    Fehler bei TVizle: {e}")
        
        print(f"  [SCRAPER] {found} Links auf tvizle.tr gefunden.")
        return self.scraped_links
    
    def scrape_famelack(self, channel_name):
        """
        Scraped Famelack-Streams (ercdn.net) für einen Kanal.
        """
        print(f"  [SCRAPER] Suche auf famelack für: {channel_name}")
        found = 0
        
        # Bereinige Kanalname
        clean_name = channel_name.lower()
        clean_name = re.sub(r'[^a-z0-9\s]', '', clean_name)
        clean_name = re.sub(r'\s+', '-', clean_name).strip('-')
        
        # Mögliche CDN-Domains
        cdn_domains = [
            "rnttwmjcin.turknet.ercdn.net",
        ]
        
        path_prefix = "lcpmvefbyo"
        qualities = ["1080p", "720p", "576p", "480p", "360p"]
        
        for domain in cdn_domains:
            for quality in qualities:
                url = f"https://{domain}/{path_prefix}/{clean_name}/{clean_name}_{quality}.m3u8"
                try:
                    response = requests.head(url, headers=self.headers, timeout=3)
                    if response.status_code == 200:
                        self.scraped_links.append(url)
                        found += 1
                        break  # Eine Qualität reicht
                except:
                    continue
        
        print(f"  [SCRAPER] {found} Famelack-Links gefunden.")
        return self.scraped_links
    
    def scrape_volo(self, channel_name):
        """
        Scraped Volo TV (canlitvvolo.com) für einen Kanal.
        """
        print(f"  [SCRAPER] Suche auf volo für: {channel_name}")
        found = 0
        
        # Erstelle Permalink
        permalink = channel_name.lower()
        permalink = re.sub(r'[^a-z0-9\s-]', '', permalink)
        permalink = re.sub(r'\s+', '-', permalink)
        permalink = re.sub(r'-canli-izle$|-canli$', '', permalink)
        
        if not permalink:
            return self.scraped_links
        
        # Versuche Volo-Seite zu scrapen
        try:
            url = f"https://tv.canlitvvolo.com/{permalink}"
            response = requests.get(url, headers=self.headers, timeout=10)
            
            if response.status_code == 200:
                # Suche nach m3u8-Links
                m3u8_pattern = r'(https?://[^\s"\']+\.m3u8[^\s"\']*)'
                matches = re.findall(m3u8_pattern, response.text)
                
                for match in matches:
                    if 'lg.mncdn.com' in match:
                        self.scraped_links.append(match)
                        found += 1
                        
        except Exception as e:
            print(f"    Fehler bei Volo: {e}")
        
        print(f"  [SCRAPER] {found} Volo-Links gefunden.")
        return self.scraped_links
    
    def scrape_all_sources(self, channel_name, pages=3):
        """
        Durchläuft alle Quellen und sammelt Links.
        """
        print(f"\n[SCRAPER] Sammle Links für: {channel_name}")
        
        # 1. Streamtest.in
        self.scrape_streamtest(channel_name, pages)
        
        # 2. TVizle
        self.scrape_tvizle(channel_name)
        
        # 3. Famelack
        self.scrape_famelack(channel_name)
        
        # 4. Volo
        self.scrape_volo(channel_name)
        
        # Entferne Duplikate
        self.scraped_links = list(dict.fromkeys(self.scraped_links))
        
        print(f"[SCRAPER] Insgesamt {len(self.scraped_links)} eindeutige Links gefunden.")
        return self.scraped_links
    
    def get_best_link(self, channel_name, pages=3):
        """
        Gibt den besten funktionierenden Link für einen Kanal zurück.
        """
        # Scrape alle Quellen
        self.scrape_all_sources(channel_name, pages)
        
        if not self.scraped_links:
            return None
        
        # Teste alle gefundenen Links
        print(f"  [SCRAPER] Teste {len(self.scraped_links)} Links...")
        
        for url in self.scraped_links[:10]:  # Max 10 testen
            try:
                response = requests.head(url, headers=self.headers, timeout=3)
                if response.status_code == 200:
                    print(f"  [SCRAPER] ✅ Funktioniert: {url[:60]}...")
                    return url
            except:
                continue
        
        return self.scraped_links[0] if self.scraped_links else None


# ============================================================
# TEST-FUNKTION
# ============================================================

def test_scraper():
    """Testet den Scraper mit einem Beispiel-Kanal."""
    scraper = IPTVScraper()
    
    channel = "atv"
    print(f"\n🔍 Teste Scraper für: {channel}")
    
    link = scraper.get_best_link(channel, pages=2)
    
    if link:
        print(f"\n✅ Bester Link: {link}")
    else:
        print("\n❌ Kein Link gefunden.")

if __name__ == "__main__":
    test_scraper()
