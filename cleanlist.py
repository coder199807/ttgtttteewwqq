# ============================================================
# SCRAPER INTEGRATION
# ============================================================

from scraper import IPTVScraper

def find_stream_with_scraper(channel_name):
    """
    Verwendet den Scraper, um einen funktionierenden Stream zu finden.
    """
    print(f"  [SCRAPER] Starte Suche für: {channel_name}")
    
    scraper = IPTVScraper()
    
    # Nur die wichtigsten Quellen scrapen (schneller)
    scraper.scrape_tvizle(channel_name)
    scraper.scrape_famelack(channel_name)
    scraper.scrape_volo(channel_name)
    
    if scraper.scraped_links:
        # Teste die gefundenen Links
        for url in scraper.scraped_links[:5]:
            try:
                response = requests.head(url, headers=CUSTOM_HEADERS, timeout=3)
                if response.status_code == 200:
                    print(f"  [SCRAPER] ✅ Funktionierender Link gefunden!")
                    return url
            except:
                continue
    
    return None

# ============================================================
# HAUPTPROZESS (ANGEPASST)
# ============================================================

def process_hybrid_m3u():
    print("\n" + "="*60)
    print("IPTV REPAIR TOOL MIT SCRAPER")
    print("Backup-M3U → Scraper → Vavoo")
    print("="*60)

    # 1. Backup-M3U laden
    backup_entries = load_backup_m3u()
    backup_index = build_backup_index(backup_entries) if backup_entries else {}

    # 2. Haupt-M3U lesen
    try:
        with open(INPUT_M3U, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except FileNotFoundError:
        print(f"[FEHLER] {INPUT_M3U} nicht gefunden.")
        return

    entries = parse_m3u(content)
    print(f"\n[M3U] {len(entries)} Kanäle gelesen.")

    # 3. Kanäle verarbeiten
    output_entries = []
    stats = {
        "backup": 0,
        "scraper": 0,
        "vavoo": 0,
        "failed": 0
    }

    print("\n[START] Verarbeite Kanäle...")

    for i, entry in enumerate(entries, 1):
        extinf = entry["extinf"]
        original_url = entry["url"]
        channel_name = get_extinf_name(extinf)
        display_name = get_display_name(channel_name)

        if i % 20 == 0:
            print(f"  Fortschritt: {i}/{len(entries)} ({i/len(entries)*100:.1f}%)")

        # 1. Versuche Backup-M3U
        backup_match = find_backup_for_channel(channel_name, backup_index)
        if backup_match:
            output_entries.append({
                "extinf": extinf,
                "url": backup_match["url"],
                "ua": CUSTOM_USER_AGENT,
            })
            stats["backup"] += 1
            continue

        # 2. Versuche Scraper (nur wenn Backup fehlschlägt)
        scraper_url = find_stream_with_scraper(channel_name)
        if scraper_url:
            output_entries.append({
                "extinf": extinf,
                "url": scraper_url,
                "ua": CUSTOM_USER_AGENT,
            })
            stats["scraper"] += 1
            continue

        # 3. Fallback: Vavoo
        output_entries.append({
            "extinf": extinf,
            "url": clean_stream_url(original_url),
            "ua": VAVOO_USER_AGENT,
        })
        stats["vavoo"] += 1

    # 4. Statistik
    print("\n" + "="*60)
    print("STATISTIK")
    print("="*60)
    print(f"Gesamt:                 {len(output_entries)}")
    print(f"Durch Backup ersetzt:   {stats['backup']}")
    print(f"Durch Scraper gefunden: {stats['scraper']}")
    print(f"Vavoo (Fallback):       {stats['vavoo']}")
    print(f"Nicht repariert:        {stats['failed']}")
    print("="*60)

    # 5. Neue M3U schreiben
    write_m3u(output_entries)
    print(f"\n[FERTIG] Playlist gespeichert als {OUTPUT_M3U}")
