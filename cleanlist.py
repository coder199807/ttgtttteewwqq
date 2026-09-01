import re
import requests
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, unquote
from html import unescape


# ============================================================
# CONFIG
# ============================================================

INPUT_M3U = "iptv.m3u"
OUTPUT_M3U = "iptv.m3u"

VOLO_BASE_URL = "https://tv.canlitvvolo.com"

# Der TV-Feed ist die bevorzugte Quelle.
VOLO_RSS_URLS = [
    f"{VOLO_BASE_URL}/tv/feed",
    f"{VOLO_BASE_URL}/feed",
    f"{VOLO_BASE_URL}/rss",
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

MAX_WORKERS = 12
REQUEST_TIMEOUT = 10


# ============================================================
# HTTP SESSION
# ============================================================

session = requests.Session()

session.headers.update({
    "User-Agent": CUSTOM_USER_AGENT,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,application/rss+xml;q=0.8,*/*;q=0.7"
    ),
    "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
})


# ============================================================
# KANALNAME NORMALISIEREN
# ============================================================

def get_canonical_key(name):
    """
    Macht aus unterschiedlichen Schreibweisen einen
    gemeinsamen Vergleichsschlüssel.

    Beispiel:

    RTL HD
    RTL
    RTL FHD

    -> rtl
    """

    if not name:
        return ""

    text = unescape(str(name))
    text = unquote(text)
    text = text.lower().strip()

    # HTML-Reste
    text = re.sub(r"<[^>]+>", " ", text)

    # Qualität / technische Angaben
    text = re.sub(
        r"\b("
        r"4k|uhd|fhd|hd|sd|hevc|raw|"
        r"1080p|720p|576p|480p|360p|"
        r"backup|backup\d+|"
        r"yayin\s*\d+|"
        r"stream\s*\d+|"
        r"source\s*\d+"
        r")\b",
        " ",
        text,
        flags=re.IGNORECASE
    )

    # Häufige Länderpräfixe
    text = re.sub(
        r"^(tr|de|en|fr|uk|us)\s*[:\-]\s*",
        "",
        text
    )

    # Klammerzusätze
    text = re.sub(r"\([^)]*\)", " ", text)
    text = re.sub(r"\[[^\]]*\]", " ", text)

    # Türkische Sonderzeichen vereinheitlichen
    replacements = {
        "ğ": "g",
        "ü": "u",
        "ş": "s",
        "ı": "i",
        "ö": "o",
        "ç": "c",
        "ä": "a",
        "é": "e",
        "è": "e",
        "à": "a",
        "â": "a",
        "î": "i",
        "û": "u",
    }

    for old, new in replacements.items():
        text = text.replace(old, new)

    # Nur Buchstaben/Zahlen behalten
    text = re.sub(r"[^a-z0-9\s]", " ", text)

    # Wörter, die für den Sendernamen irrelevant sind
    stop_words = {
        "tv",
        "television",
        "channel",
        "kanal",
        "canli",
        "canlı",
        "izle",
        "live",
        "watch",
        "official",
    }

    words = [
        x for x in text.split()
        if x not in stop_words
    ]

    return "".join(words)


# ============================================================
# M3U EINLESEN
# ============================================================

def read_m3u():
    """
    Liest die von build.js erzeugte IPTV-Liste.

    Wichtig:
    Die ursprünglichen EXTINF-Zeilen und URLs bleiben erhalten.
    """

    try:
        with open(
            INPUT_M3U,
            "r",
            encoding="utf-8",
            errors="ignore"
        ) as f:
            lines = f.readlines()

    except FileNotFoundError:
        print(f"[ERROR] {INPUT_M3U} nicht gefunden.")
        return []

    entries = []

    current_extinf = None
    current_extra = []

    for raw_line in lines:

        line = raw_line.strip()

        if not line:
            continue

        if line.startswith("#EXTINF:"):

            current_extinf = line
            current_extra = []

            continue

        if current_extinf is None:
            continue

        if line.startswith("#"):

            current_extra.append(line)

            continue

        # Normale URL
        url = line

        if "," in current_extinf:
            name = current_extinf.rsplit(",", 1)[1].strip()
        else:
            name = ""

        entries.append({
            "extinf": current_extinf,
            "name": name,
            "key": get_canonical_key(name),
            "url": url,
            "extra": current_extra.copy(),
        })

        current_extinf = None
        current_extra = []

    return entries


# ============================================================
# STREAM URL ERKENNEN
# ============================================================

def extract_m3u8_urls(text):
    """
    Sucht echte HLS/m3u8 URLs im HTML bzw. JavaScript.
    """

    if not text:
        return []

    text = unescape(text)
    text = text.replace("\\/", "/")
    text = text.replace("\\u0026", "&")

    patterns = [
        r'https?://[^"\'<>\s\\]+\.m3u8(?:\?[^"\'<>\s\\]*)?',
        r'https?://[^"\'<>\s\\]+\.m3u(?:\?[^"\'<>\s\\]*)?',
    ]

    result = []

    for pattern in patterns:

        matches = re.findall(
            pattern,
            text,
            flags=re.IGNORECASE
        )

        for url in matches:

            url = url.strip("\"' ")

            if url not in result:
                result.append(url)

    return result


# ============================================================
# SENDERSEITE UNTERSUCHEN
# ============================================================

def extract_stream_from_page(page_url, rss_name=None):
    """
    Untersucht:

    Senderseite
       ↓
    HTML
       ↓
    JavaScript
       ↓
    iframe
       ↓
    iframe JavaScript
       ↓
    m3u8
    """

    try:

        response = session.get(
            page_url,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True
        )

        if response.status_code != 200:
            return None

        html = response.text

        soup = BeautifulSoup(
            html,
            "html.parser"
        )

        # ----------------------------------------------------
        # Kanalname
        # ----------------------------------------------------

        channel_name = rss_name

        title = soup.find(
            "meta",
            property="og:title"
        )

        if title and title.get("content"):
            channel_name = title["content"].strip()

        elif soup.title:
            channel_name = soup.title.get_text(
                strip=True
            )

        # ----------------------------------------------------
        # Direkte m3u8 URLs
        # ----------------------------------------------------

        streams = extract_m3u8_urls(html)

        # ----------------------------------------------------
        # Scripts
        # ----------------------------------------------------

        for script in soup.find_all("script"):

            script_text = script.string

            if not script_text:
                script_text = script.get_text()

            for stream in extract_m3u8_urls(
                script_text
            ):

                if stream not in streams:
                    streams.append(stream)

        # ----------------------------------------------------
        # IFrames
        # ----------------------------------------------------

        iframe_urls = []

        for iframe in soup.find_all(
            "iframe",
            src=True
        ):

            iframe_url = urljoin(
                page_url,
                iframe["src"]
            )

            if iframe_url not in iframe_urls:
                iframe_urls.append(
                    iframe_url
                )

        # ----------------------------------------------------
        # IFrames untersuchen
        # ----------------------------------------------------

        for iframe_url in iframe_urls:

            try:

                iframe_response = session.get(
                    iframe_url,
                    timeout=REQUEST_TIMEOUT,
                    allow_redirects=True
                )

                if iframe_response.status_code != 200:
                    continue

                iframe_html = iframe_response.text

                for stream in extract_m3u8_urls(
                    iframe_html
                ):

                    if stream not in streams:
                        streams.append(stream)

                iframe_soup = BeautifulSoup(
                    iframe_html,
                    "html.parser"
                )

                for script in iframe_soup.find_all(
                    "script"
                ):

                    script_text = (
                        script.string
                        or script.get_text()
                    )

                    for stream in extract_m3u8_urls(
                        script_text
                    ):

                        if stream not in streams:
                            streams.append(stream)

            except Exception:
                continue

        if not streams:
            return None

        return {
            "name": channel_name,
            "key": get_canonical_key(
                channel_name
            ),
            "streams": streams,
        }

    except Exception:
        return None


# ============================================================
# RSS FINDEN
# ============================================================

def get_volo_rss():
    """
    Probiert die bekannten Feed-Endpunkte.
    """

    for url in VOLO_RSS_URLS:

        try:

            response = session.get(
                url,
                timeout=REQUEST_TIMEOUT
            )

            if response.status_code != 200:
                continue

            content = response.content

            text = response.text.lstrip().lower()

            # XML/RSS erkennen
            if (
                "xml" in response.headers.get(
                    "Content-Type",
                    ""
                ).lower()
                or text.startswith("<?xml")
                or "<rss" in text[:1000]
                or "<feed" in text[:1000]
            ):

                print(
                    f"[VOLO] RSS gefunden: {url}"
                )

                return content

        except Exception as e:

            print(
                f"[VOLO] RSS Fehler bei {url}: {e}"
            )

    return None


# ============================================================
# RSS PARSEN
# ============================================================

def parse_rss(content):
    """
    Unterstützt RSS 2.0 und Atom.
    """

    try:

        root = ET.fromstring(content)

    except ET.ParseError as e:

        print(
            f"[VOLO] RSS Parse-Fehler: {e}"
        )

        return []

    channels = []

    # --------------------------------------------------------
    # RSS 2.0
    # --------------------------------------------------------

    for item in root.findall(".//item"):

        title = item.find("title")
        link = item.find("link")

        name = (
            title.text.strip()
            if title is not None
            and title.text
            else None
        )

        url = (
            link.text.strip()
            if link is not None
            and link.text
            else None
        )

        if url:

            channels.append({
                "name": name,
                "url": url,
            })

    # --------------------------------------------------------
    # Atom
    # --------------------------------------------------------

    if not channels:

        atom = (
            "{http://www.w3.org/2005/Atom}"
        )

        for entry in root.findall(
            f".//{atom}entry"
        ):

            title = entry.find(
                f"{atom}title"
            )

            link = entry.find(
                f"{atom}link"
            )

            name = (
                title.text.strip()
                if title is not None
                and title.text
                else None
            )

            url = (
                link.attrib.get("href")
                if link is not None
                else None
            )

            if url:

                channels.append({
                    "name": name,
                    "url": url,
                })

    return channels


# ============================================================
# VOLO LADEN
# ============================================================

def scrape_volo_streams():
    """
    RSS -> Senderseiten -> m3u8

    Gibt zurück:

        {
            "rtl": {
                "name": "RTL",
                "streams": [...]
            }
        }
    """

    print()
    print("=" * 60)
    print("VOLO RSS / STREAM SCAN")
    print("=" * 60)

    content = get_volo_rss()

    if not content:

        print(
            "[VOLO] Kein RSS erreichbar."
        )

        print(
            "[VOLO] Vavoo bleibt vollständig aktiv."
        )

        return {}

    channels = parse_rss(content)

    print(
        f"[VOLO] {len(channels)} RSS-Einträge gefunden."
    )

    if not channels:
        return {}

    volo_map = {}

    # --------------------------------------------------------
    # Parallel abrufen
    # --------------------------------------------------------

    with ThreadPoolExecutor(
        max_workers=MAX_WORKERS
    ) as executor:

        future_map = {}

        for channel in channels:

            future = executor.submit(
                extract_stream_from_page,
                channel["url"],
                channel["name"]
            )

            future_map[future] = channel

        finished = 0

        for future in as_completed(
            future_map
        ):

            finished += 1

            try:
                result = future.result()

            except Exception:
                continue

            if not result:
                continue

            key = result["key"]

            streams = result["streams"]

            if not key or not streams:
                continue

            if key not in volo_map:

                volo_map[key] = {
                    "name": result["name"],
                    "streams": [],
                }

            for stream in streams:

                if stream not in volo_map[key]["streams"]:

                    volo_map[key]["streams"].append(
                        stream
                    )

            print(
                f"[VOLO] {finished}/{len(channels)} "
                f"{result['name']} "
                f"({len(streams)} Stream)"
            )

    print()
    print(
        f"[VOLO] Fertig: "
        f"{len(volo_map)} Sender mit Stream"
    )

    return volo_map


# ============================================================
# VOLO MATCH
# ============================================================

def find_volo_match(
    channel_name,
    volo_map
):
    """
    Erst exaktes Matching.

    Danach vorsichtiges Teil-Matching.
    """

    key = get_canonical_key(
        channel_name
    )

    if not key:
        return None

    # Exakt
    if key in volo_map:
        return volo_map[key]

    # Teilmatch nur bei ausreichend langen Namen
    if len(key) < 4:
        return None

    candidates = []

    for volo_key, data in volo_map.items():

        if len(volo_key) < 4:
            continue

        if (
            key in volo_key
            or volo_key in key
        ):

            candidates.append(data)

    # Nur eindeutiges Teilmatch akzeptieren
    if len(candidates) == 1:
        return candidates[0]

    return None


# ============================================================
# BESTEN VOLO STREAM WÄHLEN
# ============================================================

def choose_volo_stream(streams):

    if not streams:
        return None

    # Master zuerst
    for stream in streams:

        if "master.m3u8" in stream.lower():
            return stream

    # Playlist
    for stream in streams:

        if "playlist.m3u8" in stream.lower():
            return stream

    # irgendeine m3u8
    for stream in streams:

        if ".m3u8" in stream.lower():
            return stream

    return streams[0]


# ============================================================
# ALTE HEADER ENTFERNEN
# ============================================================

def clean_old_headers(lines):
    """
    Verhindert doppelte User-Agent Header.

    Das eigentliche Format bleibt identisch zu deiner
    funktionierenden Playlist.
    """

    result = []

    for line in lines:

        lower = line.lower()

        if lower.startswith(
            "#extvlcopt:http-user-agent="
        ):
            continue

        if lower.startswith(
            "#exthttp:"
        ):
            continue

        result.append(line)

    return result


# ============================================================
# PLAYLIST SCHREIBEN
# ============================================================

def write_playlist(entries):

    with open(
        OUTPUT_M3U,
        "w",
        encoding="utf-8"
    ) as f:

        f.write("#EXTM3U\n")

        for entry in entries:

            # EXTINF exakt behalten
            f.write(
                entry["extinf"]
                + "\n"
            )

            # Andere Metadaten behalten
            for line in entry.get(
                "extra",
                []
            ):

                f.write(
                    line
                    + "\n"
                )

            # Genau das Format deiner
            # vorher funktionierenden Playlist
            f.write(
                "#EXTVLCOPT:http-user-agent="
                + entry["ua"]
                + "\n"
            )

            f.write(
                '#EXTHTTP:{"User-Agent":"'
                + entry["ua"]
                + '"}\n'
            )

            f.write(
                entry["url"]
                + "\n"
            )


# ============================================================
# HAUPTPROZESS
# ============================================================

def process_hybrid_m3u():

    print()
    print("=" * 60)
    print("IPTV HYBRID BUILD")
    print("=" * 60)

    # --------------------------------------------------------
    # Volo holen
    # --------------------------------------------------------

    volo_streams = scrape_volo_streams()

    # --------------------------------------------------------
    # Vavoo/build.js M3U lesen
    # --------------------------------------------------------

    entries = read_m3u()

    if not entries:

        print(
            "[ERROR] Keine Kanäle in iptv.m3u."
        )

        return

    print()
    print(
        f"[M3U] {len(entries)} Kanäle geladen."
    )

    output = []

    volo_count = 0
    vavoo_count = 0

    # --------------------------------------------------------
    # Jeden vorhandenen Kanal bearbeiten
    # --------------------------------------------------------

    for entry in entries:

        name = entry["name"]

        match = find_volo_match(
            name,
            volo_streams
        )

        # ----------------------------------------------------
        # VOLO PRIORITÄT
        # ----------------------------------------------------

        if match:

            stream = choose_volo_stream(
                match["streams"]
            )

            if stream:

                entry["url"] = stream
                entry["ua"] = (
                    CUSTOM_USER_AGENT
                )

                entry["extra"] = (
                    clean_old_headers(
                        entry.get(
                            "extra",
                            []
                        )
                    )
                )

                volo_count += 1

                print(
                    f"[VOLO] {name}"
                )

                output.append(entry)

                continue

        # ----------------------------------------------------
        # VAVOO FALLBACK
        # ----------------------------------------------------

        entry["ua"] = (
            VAVOO_USER_AGENT
        )

        entry["extra"] = (
            clean_old_headers(
                entry.get(
                    "extra",
                    []
                )
            )
        )

        vavoo_count += 1

        print(
            f"[VAVOO] {name}"
        )

        output.append(entry)

    # --------------------------------------------------------
    # Playlist schreiben
    # --------------------------------------------------------

    write_playlist(output)

    # --------------------------------------------------------
    # Statistik
    # --------------------------------------------------------

    print()
    print("=" * 60)
    print("BUILD FERTIG")
    print("=" * 60)

    print(
        f"Kanäle gesamt : {len(output)}"
    )

    print(
        f"Volo          : {volo_count}"
    )

    print(
        f"Vavoo         : {vavoo_count}"
    )

    print(
        f"Output        : {OUTPUT_M3U}"
    )

    print("=" * 60)


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    process_hybrid_m3u()