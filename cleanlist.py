import re
import html
import unicodedata
import requests
import xml.etree.ElementTree as ET
import json

from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse, unquote


# ============================================================
# KONFIGURATION
# ============================================================

INPUT_M3U = "iptv.m3u"
OUTPUT_M3U = "iptv.m3u"

VOLO_BASE_URL = "https://tv.canlitvvolo.com"
VOLO_API_URL = "https://api.canlitvvolo.com/api/tv/stream"

VOLO_RSS_URLS = [
    "https://tv.canlitvvolo.com/feed",
    "https://tv.canlitvvolo.com/rss",
]

CUSTOM_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

VAVOO_USER_AGENT = (
    "Vavoo/2.6 vypn.net App/1.0 "
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
)

VOLO_HEADERS = {
    "User-Agent": CUSTOM_USER_AGENT,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
    "Connection": "keep-alive",
}

RSS_HEADERS = {
    "User-Agent": CUSTOM_USER_AGENT,
    "Accept": (
        "application/rss+xml, application/xml, "
        "text/xml, */*"
    ),
}

# Headers für die API (basierend auf deinem Request)
API_HEADERS = {
    "User-Agent": CUSTOM_USER_AGENT,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "de-DE,de;q=0.7",
    "Content-Type": "application/json;charset=UTF-8",
    "Origin": "https://tv.canlitvvolo.com",
    "Referer": "https://tv.canlitvvolo.com/",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
}

# Der Request-Body für die API (basierend auf deinem Request)
API_PAYLOAD = {
    # Passe diesen Payload an, falls die API weitere Parameter erwartet
    # Beispiel: "action": "get_streams" oder ähnlich
}

MAX_WORKERS = 12
REQUEST_TIMEOUT = 8


# ============================================================
# NAMEN NORMALISIEREN
# ============================================================

def normalize_text(text):
    """
    Grundlegende Normalisierung:
    - HTML entfernen
    - Unicode normalisieren
    - türkische Sonderzeichen vereinheitlichen
    - Kleinbuchstaben
    """

    if not text:
        return ""

    text = html.unescape(str(text))
    text = BeautifulSoup(text, "html.parser").get_text(" ")

    # Türkische Sonderfälle explizit behandeln
    replacements = {
        "ı": "i",
        "İ": "i",
        "ş": "s",
        "Ş": "s",
        "ğ": "g",
        "Ğ": "g",
        "ç": "c",
        "Ç": "c",
        "ö": "o",
        "Ö": "o",
        "ü": "u",
        "Ü": "u",
    }

    for old, new in replacements.items():
        text = text.replace(old, new)

    text = unicodedata.normalize("NFKD", text)
    text = "".join(
        c for c in text
        if not unicodedata.combining(c)
    )

    text = text.lower()

    return text


def get_canonical_key(name):
    """
    Erstellt einen robusten Vergleichsschlüssel.

    Beispiele:

    Show TV
    SHOW TV HD
    ShowTV
    Show TV 720p
    Show TV FHD

    werden zu demselben Schlüssel.
    """

    if not name:
        return ""

    text = normalize_text(name)

    # Häufige technische Angaben entfernen
    technical_words = [
        "4k",
        "uhd",
        "fhd",
        "hd",
        "sd",
        "hevc",
        "h265",
        "h264",
        "1080p",
        "1080",
        "720p",
        "720",
        "576p",
        "576",
        "480p",
        "480",
        "360p",
        "360",
        "backup",
        "backup1",
        "backup2",
        "main",
        "live",
        "stream",
        "tv",
    ]

    for word in technical_words:
        text = re.sub(
            rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])",
            " ",
            text,
        )

    # Senderpräfixe entfernen
    text = re.sub(
        r"^[a-z0-9\s]+:\s*",
        "",
        text,
    )

    # Auflösungen / Zahlen
    text = re.sub(
        r"\b\d{3,4}p\b",
        " ",
        text,
    )

    # Trennzeichen vereinheitlichen
    text = re.sub(r"[\._/\\|:+\-]+", " ", text)

    # Nur Buchstaben/Zahlen behalten
    text = re.sub(r"[^a-z0-9\s]", " ", text)

    # Mehrfache Leerzeichen
    text = re.sub(r"\s+", " ", text).strip()

    return text.replace(" ", "")


# ============================================================
# NAMENS-VARIANTEN
# ============================================================

def get_name_variants(name):
    """
    Erzeugt mehrere Vergleichsschlüssel für einen Sender.
    """

    if not name:
        return set()

    variants = set()

    original = str(name).strip()

    key = get_canonical_key(original)

    if key:
        variants.add(key)

    normalized = normalize_text(original)

    # Leerzeichen-Version
    normalized_clean = re.sub(
        r"[^a-z0-9\s]",
        " ",
        normalized,
    )

    normalized_clean = re.sub(
        r"\s+",
        " ",
        normalized_clean,
    ).strip()

    if normalized_clean:
        variants.add(
            normalized_clean.replace(" ", "")
        )

    # Wörter wie "tv" separat behandeln
    no_tv = re.sub(
        r"\btv\b",
        " ",
        normalized_clean,
    )

    no_tv = re.sub(
        r"\s+",
        " ",
        no_tv,
    ).strip()

    if no_tv:
        variants.add(
            no_tv.replace(" ", "")
        )

    return {
        v for v in variants
        if len(v) >= 3
    }


# ============================================================
# URL / KANALNAME
# ============================================================

def get_name_from_url(url):
    """
    Holt einen möglichen Sendernamen aus dem URL-Slug.
    """

    if not url:
        return ""

    try:
        parsed = urlparse(url)
        path = unquote(parsed.path)

        slug = path.rstrip("/").split("/")[-1]

        # Typische Volo-Suffixe entfernen
        slug = re.sub(
            r"(?i)(-canli-izle|-canli-izle$|-canli$)",
            "",
            slug,
        )

        slug = slug.replace("-", " ")

        return slug.strip()

    except Exception:
        return ""


# ============================================================
# HLS AUS HTML EXTRAHIEREN
# ============================================================

def extract_m3u8_urls(text):
    """
    Extrahiert m3u8 URLs aus HTML, JavaScript usw.
    """

    if not text:
        return []

    found = []

    patterns = [
        # normale absolute URLs
        r'https?://[^\s"\'<>\\]+?\.m3u8(?:\?[^\s"\'<>\\]*)?',

        # escaped URLs
        r'https?:\\/\\/[^"\']+?\.m3u8(?:\?[^"\']*)?',

        # JSON / JS Strings
        r'["\']([^"\']+\.m3u8(?:\?[^"\']*)?)["\']',
    ]

    for pattern in patterns:
        try:
            matches = re.findall(
                pattern,
                text,
                flags=re.IGNORECASE,
            )

            for match in matches:
                if isinstance(match, tuple):
                    match = match[0]

                if not match:
                    continue

                url = match.replace("\\/", "/")

                if url.startswith("http"):
                    found.append(url)

        except Exception:
            pass

    # URL-Encoding / HTML-Encoding
    cleaned = []

    for url in found:
        url = html.unescape(url)

        if url not in cleaned:
            cleaned.append(url)

    return cleaned


# ============================================================
# API-STREAMS LADEN (ANGEPASST FÜR POST)
# ============================================================

def get_volo_streams_via_api():
    """
    Ruft die Volo-API per POST ab und baut eine Map aus normalisierten Namen zu Stream-URLs auf.
    """
    print()
    print("=" * 60)
    print("VOLO API / STREAMS (POST)")
    print("=" * 60)

    try:
        print(f"[VOLO API] POST an: {VOLO_API_URL}")
        print(f"[VOLO API] Payload: {json.dumps(API_PAYLOAD, indent=2)}")
        
        response = requests.post(
            VOLO_API_URL,
            headers=API_HEADERS,
            json=API_PAYLOAD,  # Wichtig: JSON-Body senden
            timeout=REQUEST_TIMEOUT
        )
        print(f"[VOLO API] HTTP {response.status_code}")

        if response.status_code != 200:
            print(f"[VOLO API] Fehler beim Abruf der API: {response.status_code}")
            return {}

        # Prüfe Content-Type
        content_type = response.headers.get('content-type', '')
        if 'application/json' not in content_type:
            print(f"[VOLO API] Ungültiger Content-Type: {content_type}")
            return {}

        data = response.json()
        print(f"[VOLO API] {len(data) if isinstance(data, list) else '???'} Einträge von der API erhalten.")

        if not data:
            print("[VOLO API] Keine Daten von der API.")
            return {}

        # Logge die ersten 2 Einträge zur Debugging
        if isinstance(data, list) and len(data) > 0:
            print("[VOLO API] Beispiel-Einträge:")
            for i, item in enumerate(data[:2]):
                print(f"  [{i}] {json.dumps(item, indent=2)[:200]}...")

        volo_map = {}

        # Verarbeite die Daten (abhängig vom tatsächlichen Format)
        # Mögliche Formate:
        # 1. Liste von Objekten: [{"name": "...", "stream": "..."}, ...]
        # 2. Dictionary mit "channels"-Key: {"channels": [{"name": "...", "url": "..."}]}
        # 3. Dictionary mit "data"-Key: {"data": {"streams": [...]}}
        
        channels = []
        if isinstance(data, list):
            channels = data
        elif isinstance(data, dict):
            # Versuche verschiedene Keys
            for key in ["channels", "data", "streams", "results", "items"]:
                if key in data and isinstance(data[key], list):
                    channels = data[key]
                    break
            # Falls nichts gefunden, nimm das ganze Dict als Liste
            if not channels:
                channels = [data]

        for item in channels:
            # Versuche verschiedene mögliche Schlüssel für Name und URL
            name = (
                item.get("name") or 
                item.get("title") or 
                item.get("channel") or 
                item.get("display_name") or
                item.get("channel_name")
            )
            
            url = (
                item.get("stream") or 
                item.get("url") or 
                item.get("link") or 
                item.get("m3u8") or
                item.get("stream_url")
            )

            if not name or not url:
                continue

            # Normalisiere den Namen und erstelle Schlüssel
            keys = get_name_variants(name)
            
            # Wenn der Name sehr kurz ist, füge auch den Originalnamen als Key hinzu
            if len(name.strip()) < 3:
                simple_key = normalize_text(name).replace(" ", "")
                if simple_key and len(simple_key) >= 2:
                    keys.add(simple_key)

            for key in keys:
                if len(key) >= 2:  # Etwas großzügiger bei API-Daten
                    if key not in volo_map:
                        volo_map[key] = []
                    # Duplikate vermeiden
                    if not any(x["url"] == url for x in volo_map[key]):
                        volo_map[key].append({
                            "url": url,
                            "names": [name],
                            "source": "api"
                        })

        print(f"[VOLO API] {len(volo_map)} Namensschlüssel aufgebaut.")
        
        # Zeige einige Beispiele
        if volo_map:
            sample_keys = list(volo_map.keys())[:5]
            for key in sample_keys:
                url_preview = volo_map[key][0]['url'][:80]
                print(f"  - {key} -> {url_preview}...")
        else:
            print("[VOLO API] WARNUNG: Keine Namensschlüssel aufgebaut!")
        
        return volo_map

    except requests.exceptions.RequestException as exc:
        print(f"[VOLO API] Netzwerkfehler: {exc}")
        return {}
    except ValueError as exc:
        print(f"[VOLO API] JSON-Parser-Fehler: {exc}")
        return {}
    except Exception as exc:
        print(f"[VOLO API] Allgemeiner Fehler: {exc}")
        import traceback
        traceback.print_exc()
        return {}


# ============================================================
# EINZELNE VOLO KANALSEITE (UNVERÄNDERT)
# ============================================================

def extract_stream_from_page(channel_url, rss_names=None):
    """
    Öffnet eine Volo-Kanalseite und versucht den echten
    HLS/m3u8 Stream zu finden.
    """

    if rss_names is None:
        rss_names = []

    try:
        response = requests.get(
            channel_url,
            headers=VOLO_HEADERS,
            timeout=REQUEST_TIMEOUT,
        )

        if response.status_code != 200:
            return None

        html_text = response.text

        soup = BeautifulSoup(
            html_text,
            "html.parser",
        )

        possible_names = []

        # RSS-Titel zuerst
        possible_names.extend(rss_names)

        # title
        if soup.title and soup.title.string:
            possible_names.append(
                soup.title.string
            )

        # H1
        for h1 in soup.find_all("h1"):
            text = h1.get_text(" ", strip=True)

            if text:
                possible_names.append(text)

        # OpenGraph title
        for meta in soup.find_all(
            "meta",
            attrs={"property": "og:title"},
        ):
            content = meta.get("content")

            if content:
                possible_names.append(content)

        # URL-Slug
        url_name = get_name_from_url(channel_url)

        if url_name:
            possible_names.append(url_name)

        # ----------------------------------------------------
        # Direkte m3u8 Suche
        # ----------------------------------------------------

        m3u8_urls = extract_m3u8_urls(html_text)

        if m3u8_urls:
            return {
                "url": m3u8_urls[0],
                "names": possible_names,
                "source": "rss",
            }

        # ----------------------------------------------------
        # IFrames durchsuchen
        # ----------------------------------------------------

        iframe_urls = []

        for iframe in soup.find_all(
            "iframe",
            src=True,
        ):
            src = iframe.get("src", "").strip()

            if not src:
                continue

            iframe_url = urljoin(
                VOLO_BASE_URL,
                src,
            )

            if iframe_url not in iframe_urls:
                iframe_urls.append(iframe_url)

        for iframe_url in iframe_urls:

            try:
                iframe_response = requests.get(
                    iframe_url,
                    headers=VOLO_HEADERS,
                    timeout=REQUEST_TIMEOUT,
                )

                if iframe_response.status_code != 200:
                    continue

                iframe_text = iframe_response.text

                iframe_m3u8 = extract_m3u8_urls(
                    iframe_text
                )

                if iframe_m3u8:
                    return {
                        "url": iframe_m3u8[0],
                        "names": possible_names,
                        "source": "rss",
                    }

            except Exception:
                continue

    except Exception:
        pass

    return None


# ============================================================
# RSS PARSEN (UNVERÄNDERT)
# ============================================================

def parse_rss_feed(xml_content):
    """
    Liest RSS und liefert:
    [
        {
            url: "...",
            names: [...]
        }
    ]
    """

    channels = []

    try:
        root = ET.fromstring(xml_content)

    except Exception as exc:
        print(
            f"[VOLO] RSS konnte nicht geparst werden: {exc}"
        )
        return channels

    for item in root.findall(".//item"):

        link = item.find("link")
        title = item.find("title")

        if link is None or not link.text:
            continue

        channel_url = link.text.strip()

        names = []

        if title is not None and title.text:
            names.append(
                html.unescape(
                    title.text.strip()
                )
            )

        # weitere mögliche RSS-Felder
        for child in list(item):

            tag = child.tag.lower()

            if (
                tag.endswith("title")
                or tag.endswith("name")
                or tag.endswith("channel")
            ):
                if child.text:
                    names.append(
                        html.unescape(
                            child.text.strip()
                        )
                    )

        channels.append(
            {
                "url": channel_url,
                "names": list(
                    dict.fromkeys(names)
                ),
            }
        )

    return channels


# ============================================================
# VOLO RSS LADEN (UNVERÄNDERT)
# ============================================================

def get_volo_streams_via_rss():
    """
    Lädt den Volo RSS Feed und löst die darin enthaltenen
    Kanalseiten zu echten m3u8 Streams auf.
    """

    print()
    print("=" * 60)
    print("VOLO RSS / HLS (FALLBACK)")
    print("=" * 60)

    rss_channels = []

    for rss_url in VOLO_RSS_URLS:

        print(
            f"[VOLO] Lade RSS: {rss_url}"
        )

        try:
            response = requests.get(
                rss_url,
                headers=RSS_HEADERS,
                timeout=REQUEST_TIMEOUT,
            )

            print(
                f"[VOLO] HTTP {response.status_code}"
            )

            if response.status_code != 200:
                continue

            parsed = parse_rss_feed(
                response.content
            )

            if parsed:
                rss_channels = parsed

                print(
                    f"[VOLO] {len(parsed)} RSS-Einträge gefunden."
                )

                break

        except Exception as exc:
            print(
                f"[VOLO] RSS Fehler: {exc}"
            )

    if not rss_channels:
        print(
            "[VOLO] Kein RSS-Feed verfügbar."
        )
        return {}

    # --------------------------------------------------------
    # Doppelte URLs entfernen
    # --------------------------------------------------------

    unique_channels = {}
    
    for channel in rss_channels:

        url = channel["url"]

        if url not in unique_channels:
            unique_channels[url] = channel

        else:
            unique_channels[url]["names"].extend(
                channel.get("names", [])
            )

    rss_channels = list(
        unique_channels.values()
    )

    print(
        f"[VOLO] {len(rss_channels)} eindeutige Kanalseiten."
    )

    # --------------------------------------------------------
    # Parallel auflösen
    # --------------------------------------------------------

    volo_map = {}

    success = 0

    with ThreadPoolExecutor(
        max_workers=MAX_WORKERS
    ) as executor:

        futures = {}

        for channel in rss_channels:

            future = executor.submit(
                extract_stream_from_page,
                channel["url"],
                channel.get("names", []),
            )

            futures[future] = channel

        for future in as_completed(futures):

            channel = futures[future]

            try:
                result = future.result()

            except Exception:
                result = None

            if not result:
                continue

            stream_url = result.get("url")

            if not stream_url:
                continue

            names = result.get(
                "names",
                [],
            )

            # ------------------------------------------------
            # Schlüssel aus ALLEN bekannten Namen erzeugen
            # ------------------------------------------------

            keys = set()

            for name in names:

                keys.update(
                    get_name_variants(name)
                )

            # Zusätzlich URL-Slug
            url_name = get_name_from_url(
                channel["url"]
            )

            keys.update(
                get_name_variants(url_name)
            )

            # Nur brauchbare Keys
            keys = {
                key
                for key in keys
                if len(key) >= 3
            }

            if not keys:
                continue

            success += 1

            entry = {
                "url": stream_url,
                "names": names,
                "source": "rss",
            }

            for key in keys:

                if key not in volo_map:
                    volo_map[key] = []

                # Duplikate vermeiden
                already_exists = any(
                    x["url"] == stream_url
                    for x in volo_map[key]
                )

                if not already_exists:
                    volo_map[key].append(
                        entry
                    )

    print(
        f"[VOLO] {success} Streams erfolgreich gefunden."
    )

    print(
        f"[VOLO] {len(volo_map)} Namensschlüssel aufgebaut."
    )

    return volo_map


# ============================================================
# MATCHING (VERBESSERT)
# ============================================================

def find_volo_stream(channel_name, volo_map):
    """
    Findet den passendsten Volo-Stream.

    Reihenfolge:
    1. Exaktes Matching
    2. Varianten-Matching
    3. Teil-Matching mit mehr Toleranz
    """

    if not channel_name or not volo_map:
        return None

    channel_variants = get_name_variants(
        channel_name
    )

    if not channel_variants:
        return None

    # --------------------------------------------------------
    # 1. Exakter Treffer
    # --------------------------------------------------------

    for key in channel_variants:

        if key in volo_map:

            streams = volo_map[key]

            if streams:
                # Bevorzuge API-Quellen
                for stream in streams:
                    if stream.get("source") == "api":
                        return stream
                return streams[0]

    # --------------------------------------------------------
    # 2. Teil-Matching (etwas großzügiger)
    # --------------------------------------------------------

    best_candidate = None
    best_score = 0
    best_source_priority = 0  # API (2) > RSS (1)

    for channel_key in channel_variants:

        if len(channel_key) < 3:
            continue

        for volo_key, streams in volo_map.items():

            if not streams:
                continue

            if len(volo_key) < 3:
                continue

            score = 0
            source_priority = 0

            # Verschiedene Matching-Strategien
            if channel_key == volo_key:
                score = 1.0
            elif channel_key in volo_key or volo_key in channel_key:
                shorter = min(len(channel_key), len(volo_key))
                longer = max(len(channel_key), len(volo_key))
                score = shorter / longer
                
                # Bonus für längere Keys
                if longer >= 8 and score >= 0.6:
                    score += 0.1

            # Prüfe, ob API-Quelle vorhanden ist
            for stream in streams:
                if stream.get("source") == "api":
                    source_priority = 2
                    break
                elif stream.get("source") == "rss":
                    source_priority = max(source_priority, 1)

            # Kombinierter Score
            if score > best_score or (score == best_score and source_priority > best_source_priority):
                best_score = score
                best_source_priority = source_priority
                # Bevorzuge API-Streams innerhalb der Gruppe
                for stream in streams:
                    if stream.get("source") == "api":
                        best_candidate = stream
                        break
                if best_candidate is None:
                    best_candidate = streams[0]

    # Akzeptiere auch niedrigere Scores, wenn es eine API-Quelle ist
    min_score = 0.6 if best_source_priority >= 2 else 0.75

    if best_score >= min_score:
        return best_candidate

    return None


# ============================================================
# M3U EINLESEN (UNVERÄNDERT)
# ============================================================

def parse_m3u(content):
    """
    Liest die vorhandene M3U robust ein.
    """

    lines = content.splitlines()

    entries = []

    current_extinf = None
    current_extra = []

    for line in lines:

        line = line.strip()

        if not line:
            continue

        if line.startswith("#EXTM3U"):
            continue

        if line.startswith("#EXTINF:"):

            # vorherigen Block nicht verlieren
            current_extinf = line
            current_extra = []

            continue

        if line.startswith("#"):
            if current_extinf:
                current_extra.append(line)

            continue

        # URL
        if current_extinf:

            entries.append(
                {
                    "extinf": current_extinf,
                    "extra": current_extra[:],
                    "url": line,
                }
            )

            current_extinf = None
            current_extra = []

    return entries


# ============================================================
# EXTINF NAME
# ============================================================

def get_extinf_name(extinf):
    """
    Holt den Sendernamen aus #EXTINF.
    """

    if not extinf:
        return ""

    if "," not in extinf:
        return ""

    return extinf.rsplit(
        ",",
        1
    )[1].strip()


# ============================================================
# URL BEREINIGEN
# ============================================================

def clean_stream_url(url):
    """
    Entfernt alte Pipe-Parameter.
    """

    if not url:
        return ""

    # Vavoo / andere M3U URLs können
    # |User-Agent=... enthalten.
    return url.split("|", 1)[0].strip()


# ============================================================
# M3U SCHREIBEN (UNVERÄNDERT)
# ============================================================

def write_m3u(entries):
    """
    Schreibt eine Televizo-kompatible M3U.
    """

    with open(
        OUTPUT_M3U,
        "w",
        encoding="utf-8",
        newline="\n",
    ) as f:

        f.write("#EXTM3U\n")

        for entry in entries:

            f.write(
                entry["extinf"]
                + "\n"
            )

            url = clean_stream_url(
                entry["url"]
            )

            ua = entry.get(
                "ua",
                CUSTOM_USER_AGENT,
            )

            # Für Televizo:
            f.write(
                "#EXTVLCOPT:http-user-agent="
                + ua
                + "\n"
            )

            f.write(
                '#EXTHTTP:{"User-Agent":"'
                + ua
                + '"}'
                + "\n"
            )

            f.write(
                url
                + "\n"
            )


# ============================================================
# HAUPTPROZESS (ANGEPASST)
# ============================================================

def process_hybrid_m3u():

    print()
    print("=" * 60)
    print("HYBRID IPTV BUILDER")
    print("VOLO (API) -> VAVOO BACKUP")
    print("=" * 60)

    # --------------------------------------------------------
    # 1. Volo holen: Zuerst API, dann RSS als Fallback
    # --------------------------------------------------------

    volo_streams = get_volo_streams_via_api()
    
    if not volo_streams:
        print("[INFO] API lieferte keine Daten. Verwende RSS-Feed als Fallback.")
        volo_streams = get_volo_streams_via_rss()
    else:
        print(f"[INFO] Verwende {len(volo_streams)} Streams von der API.")

    # --------------------------------------------------------
    # 2. Bestehende M3U lesen
    # --------------------------------------------------------

    try:

        with open(
            INPUT_M3U,
            "r",
            encoding="utf-8",
            errors="ignore",
        ) as f:

            content = f.read()

    except FileNotFoundError:

        print(
            f"[FEHLER] {INPUT_M3U} nicht gefunden."
        )

        return

    entries = parse_m3u(
        content
    )

    print()
    print(
        f"[M3U] {len(entries)} Kanäle gelesen."
    )

    # --------------------------------------------------------
    # 3. Sender ersetzen
    # --------------------------------------------------------

    output_entries = []

    volo_count = 0
    vavoo_count = 0

    matched_names = []

    for entry in entries:

        extinf = entry["extinf"]
        original_url = entry["url"]

        channel_name = get_extinf_name(
            extinf
        )

        match = find_volo_stream(
            channel_name,
            volo_streams,
        )

        # ----------------------------------------------------
        # VOLO
        # ----------------------------------------------------

        if match:

            chosen_url = match["url"]
            source = match.get("source", "unknown")

            output_entries.append(
                {
                    "extinf": extinf,
                    "url": chosen_url,
                    "ua": CUSTOM_USER_AGENT,
                }
            )

            volo_count += 1

            matched_names.append(
                (
                    channel_name,
                    chosen_url,
                    source,
                )
            )

        # ----------------------------------------------------
        # VAVOO BACKUP
        # ----------------------------------------------------

        else:

            chosen_url = clean_stream_url(
                original_url
            )

            output_entries.append(
                {
                    "extinf": extinf,
                    "url": chosen_url,
                    "ua": VAVOO_USER_AGENT,
                }
            )

            vavoo_count += 1

    # --------------------------------------------------------
    # 4. Schreiben
    # --------------------------------------------------------

    write_m3u(
        output_entries
    )

    # --------------------------------------------------------
    # 5. Statistik
    # --------------------------------------------------------

    print()
    print("=" * 60)
    print("FERTIG")
    print("=" * 60)

    print(
        f"Gesamt:             {len(output_entries)}"
    )

    print(
        f"Volo verwendet:     {volo_count}"
    )

    print(
        f"Vavoo verwendet:    {vavoo_count}"
    )

    print(
        f"Volo Schlüssel:     {len(volo_streams)}"
    )

    print("=" * 60)

    # --------------------------------------------------------
    # Optional: Treffer anzeigen
    # --------------------------------------------------------

    if matched_names:

        print()
        print("[VOLO] Verwendete Sender:")

        for name, url, source in sorted(
            matched_names,
            key=lambda x: x[0].lower(),
        ):

            source_label = "API" if source == "api" else "RSS"
            print(
                f"  ✓ {name} [{source_label}] -> {url}"
            )

    print()


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    process_hybrid_m3u()
