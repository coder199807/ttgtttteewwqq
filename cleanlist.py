import re
import json
import html
import requests
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse, unquote

# ============================================================
# KONFIGURATION
# ============================================================

INPUT_M3U = "iptv.m3u"
OUTPUT_M3U = "iptv.m3u"

VOLO_BASE_URL = "https://tv.canlitvvolo.com"
VOLO_RSS_URLS = [
    f"{VOLO_BASE_URL}/feed",
    f"{VOLO_BASE_URL}/tv/feed",
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

VOLO_HEADERS = {
    "User-Agent": CUSTOM_USER_AGENT,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,application/rss+xml;q=0.8,*/*;q=0.7"
    ),
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
}

MAX_WORKERS = 12
REQUEST_TIMEOUT = 10


# ============================================================
# SESSION
# ============================================================

session = requests.Session()
session.headers.update(VOLO_HEADERS)


# ============================================================
# KANALNAMEN NORMALISIEREN
# ============================================================

def get_canonical_key(name):
    """
    Erzeugt einen möglichst robusten Vergleichsschlüssel.

    Beispiele:
        "RTL HD"          -> "rtl"
        "RTL Deutschland" -> "rtl"
        "ZDF HD"          -> "zdf"
        "A Spor HD"       -> "aspor"
    """

    if not name:
        return ""

    text = html.unescape(str(name)).strip().lower()

    # URL-Encoding entfernen
    text = unquote(text)

    # Häufige Qualitäts-/Versionsangaben entfernen
    text = re.sub(
        r"\b("
        r"4k|uhd|fhd|hd|sd|hevc|raw|"
        r"1080p|720p|576p|480p|360p|"
        r"backup|backup\d+|"
        r"yay[ıi]n\s*\d+|"
        r"stream\s*\d+|"
        r"source\s*\d+"
        r")\b",
        " ",
        text,
        flags=re.IGNORECASE,
    )

    # Häufige Präfixe entfernen
    text = re.sub(
        r"^(tr|de|en|fr|uk|us|turkey|türkiye)\s*[:\-]\s*",
        "",
        text,
        flags=re.IGNORECASE,
    )

    # Klammern entfernen
    text = re.sub(r"\([^)]*\)", " ", text)
    text = re.sub(r"\[[^\]]*\]", " ", text)

    # Sonderzeichen durch Leerzeichen ersetzen
    text = re.sub(r"[^a-z0-9ğüşıöçäëéèàâîû\s]", " ", text)

    # Türkische Zeichen vereinheitlichen
    replacements = {
        "ğ": "g",
        "ü": "u",
        "ş": "s",
        "ı": "i",
        "ö": "o",
        "ç": "c",
        "ä": "a",
        "ë": "e",
        "é": "e",
        "è": "e",
        "à": "a",
        "â": "a",
        "î": "i",
        "û": "u",
    }

    for old, new in replacements.items():
        text = text.replace(old, new)

    # Wörter entfernen, die für das Matching wenig bringen
    stop_words = {
        "tv",
        "television",
        "kanal",
        "channel",
        "live",
        "canli",
        "canlı",
        "izle",
        "watch",
        "hd",
        "official",
    }

    words = [
        word
        for word in text.split()
        if word not in stop_words
    ]

    text = "".join(words)

    return text


# ============================================================
# M3U PARSER
# ============================================================

def parse_m3u(filename):
    """
    Liest eine M3U-Datei und gibt die einzelnen Einträge zurück.
    """

    entries = []

    try:
        with open(
            filename,
            "r",
            encoding="utf-8",
            errors="ignore",
        ) as f:
            lines = f.readlines()

    except FileNotFoundError:
        print(f"[FEHLER] {filename} wurde nicht gefunden.")
        return []

    current_extinf = None
    current_extra = []

    for line in lines:
        line = line.strip()

        if not line:
            continue

        if line.startswith("#EXTINF:"):
            current_extinf = line
            current_extra = []

        elif current_extinf and line.startswith("#"):
            current_extra.append(line)

        elif current_extinf and not line.startswith("#"):
            url = line

            raw_name = (
                current_extinf.rsplit(",", 1)[-1].strip()
                if "," in current_extinf
                else ""
            )

            entries.append({
                "extinf": current_extinf,
                "name": raw_name,
                "key": get_canonical_key(raw_name),
                "url": url,
                "extra": current_extra.copy(),
            })

            current_extinf = None
            current_extra = []

    return entries


# ============================================================
# URL / STREAM ERKENNUNG
# ============================================================

def clean_stream_url(url):
    """
    Entfernt überflüssige Parameter, ohne die Stream-URL
    unnötig zu verändern.
    """

    if not url:
        return None

    url = html.unescape(url).strip()
    url = url.replace("\\/", "/")

    # JSON-Escaping
    url = url.replace("\\u0026", "&")

    # Quotes entfernen
    url = url.strip("\"' ")

    return url


def is_stream_url(url):
    """
    Erkennt mögliche HLS-/M3U-/Video-URLs.
    """

    if not url:
        return False

    u = url.lower()

    return any(
        pattern in u
        for pattern in [
            ".m3u8",
            ".m3u",
            "/hls/",
            "playlist.m3u8",
            "manifest",
            "master.m3u8",
        ]
    )


def extract_stream_urls(text):
    """
    Sucht Stream-URLs in HTML, JavaScript und JSON.
    """

    if not text:
        return []

    found = []

    decoded = html.unescape(text)
    decoded = decoded.replace("\\/", "/")
    decoded = decoded.replace("\\u0026", "&")

    # Direkte HTTP(S)-URLs
    patterns = [
        r'https?://[^"\'<>\s\\]+\.m3u8(?:\?[^"\'<>\s\\]*)?',
        r'https?://[^"\'<>\s\\]+\.m3u(?:\?[^"\'<>\s\\]*)?',
        r'https?://[^"\'<>\s\\]+/hls/[^"\'<>\s\\]*',
        r'https?://[^"\'<>\s\\]+/manifest[^"\'<>\s\\]*',
    ]

    for pattern in patterns:
        matches = re.findall(
            pattern,
            decoded,
            flags=re.IGNORECASE,
        )

        for match in matches:
            url = clean_stream_url(match)

            if url and url not in found:
                found.append(url)

    return found


# ============================================================
# IFRAME EXTRAKTION
# ============================================================

def get_iframes(page_url, page_html):
    """
    Findet alle relevanten IFrames.
    """

    soup = BeautifulSoup(page_html, "html.parser")

    urls = []

    for iframe in soup.find_all("iframe", src=True):
        src = iframe.get("src", "").strip()

        if not src:
            continue

        full_url = urljoin(page_url, src)

        if full_url not in urls:
            urls.append(full_url)

    return urls


# ============================================================
# JSON / SCRIPT ANALYSE
# ============================================================

def extract_from_scripts(soup):
    """
    Sucht Stream-URLs in <script>-Elementen.
    """

    streams = []

    for script in soup.find_all("script"):
        content = script.string or script.get_text()

        if not content:
            continue

        for url in extract_stream_urls(content):
            if url not in streams:
                streams.append(url)

    return streams


# ============================================================
# VOLO SENDERSEITE
# ============================================================

def extract_stream_from_page(channel_url, fallback_name=None):
    """
    Öffnet eine Volo-Senderseite und versucht mehrere Ebenen:

        Senderseite
        -> HTML
        -> JavaScript
        -> JSON
        -> iframe
        -> iframe HTML
        -> weitere Stream-URLs
    """

    try:
        response = session.get(
            channel_url,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
        )

        if response.status_code != 200:
            return {
                "name": fallback_name,
                "key": get_canonical_key(fallback_name),
                "streams": [],
            }

        page_html = response.text

        soup = BeautifulSoup(
            page_html,
            "html.parser",
        )

        # ----------------------------------------------------
        # Namen aus HTML bestimmen
        # ----------------------------------------------------

        channel_name = fallback_name

        og_title = soup.find(
            "meta",
            property="og:title",
        )

        if og_title and og_title.get("content"):
            channel_name = og_title["content"].strip()

        if not channel_name and soup.title:
            channel_name = soup.title.get_text(
                strip=True
            )

        # ----------------------------------------------------
        # Streams direkt aus HTML
        # ----------------------------------------------------

        streams = extract_stream_urls(page_html)

        # ----------------------------------------------------
        # Streams aus Scripts
        # ----------------------------------------------------

        for url in extract_from_scripts(soup):
            if url not in streams:
                streams.append(url)

        # ----------------------------------------------------
        # IFrames
        # ----------------------------------------------------

        iframe_urls = get_iframes(
            channel_url,
            page_html,
        )

        for iframe_url in iframe_urls:

            try:
                iframe_response = session.get(
                    iframe_url,
                    timeout=REQUEST_TIMEOUT,
                    allow_redirects=True,
                )

                if iframe_response.status_code != 200:
                    continue

                iframe_html = iframe_response.text

                iframe_streams = extract_stream_urls(
                    iframe_html
                )

                for stream in iframe_streams:
                    if stream not in streams:
                        streams.append(stream)

                iframe_soup = BeautifulSoup(
                    iframe_html,
                    "html.parser",
                )

                for stream in extract_from_scripts(
                    iframe_soup
                ):
                    if stream not in streams:
                        streams.append(stream)

            except Exception:
                continue

        return {
            "name": channel_name,
            "key": get_canonical_key(channel_name),
            "streams": streams,
        }

    except Exception:
        return {
            "name": fallback_name,
            "key": get_canonical_key(fallback_name),
            "streams": [],
        }


# ============================================================
# RSS LADEN
# ============================================================

def get_rss_url():
    """
    Probiert mehrere bekannte Feed-Pfade.
    """

    for url in VOLO_RSS_URLS:

        try:
            response = session.get(
                url,
                timeout=REQUEST_TIMEOUT,
            )

            if response.status_code != 200:
                continue

            content_type = response.headers.get(
                "Content-Type",
                "",
            ).lower()

            text = response.text.lstrip()

            if (
                "xml" in content_type
                or text.startswith("<?xml")
                or "<rss" in text[:500].lower()
                or "<feed" in text[:500].lower()
            ):
                print(
                    f"[OK] RSS gefunden: {url}"
                )
                return url, response.content

        except Exception:
            continue

    return None, None


# ============================================================
# VOLO RSS
# ============================================================

def get_volo_streams_via_rss():
    """
    Liest den Volo-RSS-Feed und löst die Senderseiten
    parallel auf.
    """

    print()
    print("=" * 60)
    print("VOLO RSS")
    print("=" * 60)

    rss_url, content = get_rss_url()

    if not rss_url:
        print("[WARNUNG] Kein Volo-RSS-Feed gefunden.")
        return {}

    try:
        root = ET.fromstring(content)

    except ET.ParseError as e:
        print(
            f"[FEHLER] RSS konnte nicht geparst werden: {e}"
        )
        return {}

    channels = []

    # --------------------------------------------------------
    # RSS 2.0
    # --------------------------------------------------------

    for item in root.findall(".//item"):

        title_elem = item.find("title")
        link_elem = item.find("link")

        title = (
            title_elem.text.strip()
            if title_elem is not None
            and title_elem.text
            else None
        )

        link = (
            link_elem.text.strip()
            if link_elem is not None
            and link_elem.text
            else None
        )

        if link:
            channels.append({
                "name": title,
                "url": link,
            })

    # --------------------------------------------------------
    # Atom Feed als Fallback
    # --------------------------------------------------------

    if not channels:

        for entry in root.findall(".//{http://www.w3.org/2005/Atom}entry"):

            title_elem = entry.find(
                "{http://www.w3.org/2005/Atom}title"
            )

            link_elem = entry.find(
                "{http://www.w3.org/2005/Atom}link"
            )

            title = (
                title_elem.text.strip()
                if title_elem is not None
                and title_elem.text
                else None
            )

            link = None

            if link_elem is not None:
                link = link_elem.attrib.get("href")

            if link:
                channels.append({
                    "name": title,
                    "url": link,
                })

    print(
        f"[OK] {len(channels)} Einträge im RSS gefunden."
    )

    if not channels:
        return {}

    # --------------------------------------------------------
    # Senderseiten parallel auflösen
    # --------------------------------------------------------

    volo_map = {}

    with ThreadPoolExecutor(
        max_workers=MAX_WORKERS
    ) as executor:

        futures = {
            executor.submit(
                extract_stream_from_page,
                channel["url"],
                channel["name"],
            ): channel
            for channel in channels
        }

        completed = 0

        for future in as_completed(futures):

            completed += 1

            try:
                result = future.result()

            except Exception:
                continue

            key = result.get("key")
            streams = result.get("streams", [])

            if not key:
                continue

            if not streams:
                continue

            if key not in volo_map:
                volo_map[key] = {
                    "name": result.get("name"),
                    "streams": [],
                }

            for stream in streams:

                if stream not in volo_map[key]["streams"]:
                    volo_map[key]["streams"].append(
                        stream
                    )

            print(
                f"[VOLO] {completed}/{len(channels)} "
                f"{result.get('name')} -> "
                f"{len(streams)} Stream(s)"
            )

    print()
    print(
        f"[OK] Volo-Kanäle mit Stream: "
        f"{len(volo_map)}"
    )

    return volo_map


# ============================================================
# BESTES MATCHING
# ============================================================

def find_volo_match(
    channel_name,
    volo_map,
):
    """
    Sucht zunächst exakte Matches.

    Danach wird ein vorsichtiges Teilstring-Matching
    verwendet, damit z.B. RTL HD und RTL gefunden werden.
    """

    key = get_canonical_key(channel_name)

    if not key:
        return None

    # Exakt
    if key in volo_map:
        return volo_map[key]

    # Teilstring
    candidates = []

    for volo_key, data in volo_map.items():

        if not volo_key:
            continue

        if key in volo_key or volo_key in key:

            # Nicht bei extrem kurzen Keys matchen
            if len(key) >= 4 and len(volo_key) >= 4:
                candidates.append(
                    (volo_key, data)
                )

    if len(candidates) == 1:
        return candidates[0][1]

    return None


# ============================================================
# VOLO URL AUSWÄHLEN
# ============================================================

def choose_volo_stream(streams):
    """
    Bevorzugt Master-/HLS-Streams.
    """

    if not streams:
        return None

    priority = [
        "master.m3u8",
        "playlist.m3u8",
        ".m3u8",
        ".m3u",
    ]

    for pattern in priority:

        for stream in streams:

            if pattern in stream.lower():
                return stream

    return streams[0]


# ============================================================
# EXTINF BEREINIGEN
# ============================================================

def remove_old_stream_headers(extra_lines):
    """
    Entfernt alte User-Agent/HTTP-Header, damit beim
    Wechsel Volo <-> Vavoo keine alten Header übrig bleiben.
    """

    cleaned = []

    for line in extra_lines:

        lower = line.lower()

        if lower.startswith(
            "#extvlcopt:http-user-agent="
        ):
            continue

        if lower.startswith("#exthttp:"):
            continue

        cleaned.append(line)

    return cleaned


# ============================================================
# M3U SCHREIBEN
# ============================================================

def write_m3u(entries):
    """
    Schreibt die fertige M3U.
    """

    with open(
        OUTPUT_M3U,
        "w",
        encoding="utf-8",
    ) as f:

        f.write("#EXTM3U\n")

        for entry in entries:

            f.write(
                entry["extinf"].rstrip()
                + "\n"
            )

            # Zusätzliche M3U-Metadaten
            for line in entry.get(
                "extra",
                []
            ):
                f.write(
                    line.rstrip()
                    + "\n"
                )

            # User-Agent
            ua = entry.get(
                "user_agent"
            )

            if ua:
                f.write(
                    "#EXTVLCOPT:http-user-agent="
                    + ua
                    + "\n"
                )

                f.write(
                    '#EXTHTTP:{"User-Agent":"'
                    + ua
                    + '"}\n'
                )

            f.write(
                entry["url"].rstrip()
                + "\n"
            )


# ============================================================
# DUPLIKATE
# ============================================================

def remove_duplicate_entries(entries):
    """
    Entfernt doppelte Kanäle anhand des Canonical Keys.
    """

    result = []
    seen = set()

    for entry in entries:

        key = entry.get("key")

        if not key:
            result.append(entry)
            continue

        if key in seen:
            continue

        seen.add(key)
        result.append(entry)

    return result


# ============================================================
# HYBRID M3U
# ============================================================

def process_hybrid_m3u():

    print("=" * 60)
    print("VOLO + VAVOO IPTV BUILDER")
    print("=" * 60)

    # --------------------------------------------------------
    # 1. Volo laden
    # --------------------------------------------------------

    volo_streams = get_volo_streams_via_rss()

    # --------------------------------------------------------
    # 2. Bestehende M3U laden
    # --------------------------------------------------------

    print()
    print("=" * 60)
    print("LADE BESTEHENDE M3U")
    print("=" * 60)

    entries = parse_m3u(INPUT_M3U)

    if not entries:
        print(
            "[FEHLER] Keine IPTV-Einträge gefunden."
        )
        return

    print(
        f"[OK] {len(entries)} vorhandene Kanäle"
    )

    # --------------------------------------------------------
    # 3. Bestehende Kanäle bearbeiten
    # --------------------------------------------------------

    output_entries = []

    replaced_count = 0
    fallback_count = 0

    existing_keys = set()

    for entry in entries:

        name = entry["name"]
        key = entry["key"]

        existing_keys.add(key)

        volo_match = find_volo_match(
            name,
            volo_streams,
        )

        # ----------------------------------------------------
        # VOLO bevorzugen
        # ----------------------------------------------------

        if volo_match:

            chosen_url = choose_volo_stream(
                volo_match["streams"]
            )

            if chosen_url:

                entry["url"] = chosen_url
                entry["user_agent"] = (
                    CUSTOM_USER_AGENT
                )

                entry["extra"] = (
                    remove_old_stream_headers(
                        entry.get("extra", [])
                    )
                )

                replaced_count += 1

                print(
                    f"[VOLO] {name}"
                )

                output_entries.append(entry)
                continue

        # ----------------------------------------------------
        # VAVOO Fallback
        # ----------------------------------------------------

        entry["user_agent"] = (
            VAVOO_USER_AGENT
        )

        entry["extra"] = (
            remove_old_stream_headers(
                entry.get("extra", [])
            )
        )

        fallback_count += 1

        print(
            f"[VAVOO] {name}"
        )

        output_entries.append(entry)

    # --------------------------------------------------------
    # 4. Volo-Kanäle hinzufügen, die in M3U fehlen
    # --------------------------------------------------------

    added_volo_count = 0

    for volo_key, volo_data in volo_streams.items():

        if volo_key in existing_keys:
            continue

        chosen_url = choose_volo_stream(
            volo_data.get("streams", [])
        )

        if not chosen_url:
            continue

        channel_name = volo_data.get(
            "name"
        ) or volo_key

        extinf = (
            '#EXTINF:-1 group-title="Volo",'
            + channel_name
        )

        new_entry = {
            "extinf": extinf,
            "name": channel_name,
            "key": volo_key,
            "url": chosen_url,
            "extra": [],
            "user_agent": CUSTOM_USER_AGENT,
        }

        output_entries.append(new_entry)

        added_volo_count += 1

        print(
            f"[NEU VOLO] {channel_name}"
        )

    # --------------------------------------------------------
    # 5. Duplikate entfernen
    # --------------------------------------------------------

    before = len(output_entries)

    output_entries = remove_duplicate_entries(
        output_entries
    )

    duplicates_removed = (
        before - len(output_entries)
    )

    # --------------------------------------------------------
    # 6. Schreiben
    # --------------------------------------------------------

    write_m3u(output_entries)

    # --------------------------------------------------------
    # Statistik
    # --------------------------------------------------------

    print()
    print("=" * 60)
    print("FERTIG")
    print("=" * 60)

    print(
        f"Vorhandene Kanäle:       {len(entries)}"
    )

    print(
        f"Durch Volo ersetzt:      {replaced_count}"
    )

    print(
        f"Vavoo als Fallback:      {fallback_count}"
    )

    print(
        f"Neue Volo-Kanäle:        {added_volo_count}"
    )

    print(
        f"Duplikate entfernt:      {duplicates_removed}"
    )

    print(
        f"Gesamt in Ausgabe:       {len(output_entries)}"
    )

    print()
    print(
        f"Ausgabe: {OUTPUT_M3U}"
    )


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    process_hybrid_m3u()