import re
import requests
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, unquote
from html import unescape


# ============================================================
# KONFIGURATION
# ============================================================

INPUT_M3U = "iptv.m3u"
OUTPUT_M3U = "iptv.m3u"

VOLO_BASE_URL = "https://tv.canlitvvolo.com"

VOLO_RSS_URLS = [
    f"{VOLO_BASE_URL}/feed",
    f"{VOLO_BASE_URL}/rss",
    f"{VOLO_BASE_URL}/tv/feed",
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
# NAMEN NORMALISIEREN
# ============================================================

def get_canonical_key(name):
    """
    Macht unterschiedliche Schreibweisen vergleichbar.

    Beispiele:

    4K TR: A SPOR HD .b
    A Spor HD
    A-SPOR
    A Spor

    -> aspor
    """

    if not name:
        return ""

    text = unescape(str(name))
    text = unquote(text)

    # HTML entfernen
    text = re.sub(r"<[^>]+>", " ", text)

    # Unicode vereinheitlichen
    replacements = {
        "ğ": "g",
        "Ğ": "g",
        "ü": "u",
        "Ü": "u",
        "ş": "s",
        "Ş": "s",
        "ı": "i",
        "İ": "i",
        "ö": "o",
        "Ö": "o",
        "ç": "c",
        "Ç": "c",
        "ä": "a",
        "Ä": "a",
        "é": "e",
        "è": "e",
        "à": "a",
        "â": "a",
        "î": "i",
        "û": "u",
    }

    for old, new in replacements.items():
        text = text.replace(old, new)

    text = text.lower()

    # --------------------------------------------------------
    # Technische Angaben entfernen
    # --------------------------------------------------------

    text = re.sub(
        r"\b("
        r"4k|8k|uhd|fhd|hd|sd|"
        r"hevc|h265|h264|"
        r"1080p|1080i|720p|576p|480p|360p|"
        r"backup|yedek|"
        r"raw|"
        r"stream|source|"
        r"live|"
        r"canli|canlı|"
        r"izle|"
        r")\b",
        " ",
        text,
        flags=re.IGNORECASE
    )

    # --------------------------------------------------------
    # Präfixe entfernen
    # --------------------------------------------------------

    text = re.sub(
        r"^(4k|hd|fhd|uhd|tr|turkey)"
        r"[\s:_\-]+",
        "",
        text,
        flags=re.IGNORECASE
    )

    # z.B. "4K TR:"
    text = re.sub(
        r"^(4k|tr|turkey)"
        r"[\s:_\-]+"
        r"(tr|turkey)?"
        r"[\s:_\-]*",
        "",
        text,
        flags=re.IGNORECASE
    )

    # --------------------------------------------------------
    # Klammern entfernen
    # --------------------------------------------------------

    text = re.sub(r"\([^)]*\)", " ", text)
    text = re.sub(r"\[[^\]]*\]", " ", text)

    # --------------------------------------------------------
    # Vavoo interne Suffixe entfernen
    #
    # Beispiele:
    # .b
    # .c
    # .s
    # -b
    # -c
    # -s
    # --------------------------------------------------------

    text = re.sub(
        r"[\s._\-]+[bcs]\s*$",
        "",
        text,
        flags=re.IGNORECASE
    )

    # Auch mehrfach
    text = re.sub(
        r"[\s._\-]+[a-z]\s*$",
        "",
        text,
        flags=re.IGNORECASE
    )

    # --------------------------------------------------------
    # "YAYIN 1", "YAYIN 2" usw.
    # --------------------------------------------------------

    text = re.sub(
        r"\byayin\s*\d+\b",
        " ",
        text,
        flags=re.IGNORECASE
    )

    # --------------------------------------------------------
    # Sonderzeichen entfernen
    # --------------------------------------------------------

    text = re.sub(
        r"[^a-z0-9\s]",
        " ",
        text
    )

    # Mehrere Leerzeichen
    text = re.sub(
        r"\s+",
        " ",
        text
    ).strip()

    # Wörter, die keinen Sender identifizieren
    stop_words = {
        "tv",
        "television",
        "channel",
        "kanal",
        "live",
        "watch",
        "online",
        "official",
        "turkiye",
        "turkey",
    }

    words = [
        word
        for word in text.split()
        if word not in stop_words
    ]

    return "".join(words)


# ============================================================
# ZUSÄTZLICHE NAMENSVARIANTEN
# ============================================================

def get_name_variants(name):
    """
    Erzeugt mehrere Schlüssel für ein robustes Matching.
    """

    variants = set()

    if not name:
        return variants

    original = unescape(str(name)).strip()

    key = get_canonical_key(original)

    if key:
        variants.add(key)

    # Wörter einzeln bereinigen
    cleaned = re.sub(
        r"[^a-zA-Z0-9ğüşıöçĞÜŞİÖÇ]+",
        " ",
        original
    )

    words = cleaned.split()

    # Varianten ohne einzelne Buchstaben am Ende
    while words and len(words[-1]) == 1:
        words.pop()

    if words:
        variant = get_canonical_key(
            " ".join(words)
        )

        if variant:
            variants.add(variant)

    # Häufige Schreibweise: A Spor -> ASpor
    compact = re.sub(
        r"[^a-zA-Z0-9]",
        "",
        original
    ).lower()

    compact = get_canonical_key(compact)

    if compact:
        variants.add(compact)

    return variants


# ============================================================
# BEKANNTE ALIASE
# ============================================================

ALIASES = {
    "aspor": {
        "aspor",
        "asporhd",
        "asporfhd",
    },

    "ahaber": {
        "ahaber",
        "ahaberhd",
    },

    "atv": {
        "atv",
        "atvhd",
    },

    "kanald": {
        "kanald",
        "kanaldhd",
    },

    "showtv": {
        "showtv",
        "showtvhd",
    },

    "startv": {
        "startv",
        "startvhd",
    },

    "tv8": {
        "tv8",
        "tv8hd",
    },

    "tv8buçuk": {
        "tv8bucuk",
        "tv8buçuk",
        "tv8buçukhd",
    },

    "trt1": {
        "trt1",
        "trt1hd",
    },

    "trt2": {
        "trt2",
        "trt2hd",
    },

    "trtspor": {
        "trtspor",
        "trtsporhd",
    },

    "trtspor2": {
        "trtspor2",
        "trtspor2hd",
    },

    "trtbelgesel": {
        "trtbelgesel",
        "trtbelgeselhd",
    },

    "trtavaz": {
        "trtavaz",
        "trtavazhd",
    },

    "trthaber": {
        "trthaber",
        "trthaberhd",
    },

    "trtmuzik": {
        "trtmuzik",
        "trtmuzikhd",
    },

    "trtcocuk": {
        "trtcocuk",
        "trtcocukhd",
    },

    "360": {
        "360",
        "360tv",
        "360hd",
    },

    "24": {
        "24",
        "24tv",
        "24hd",
    },
}


def alias_key(key):
    """
    Gibt den Haupt-Alias eines Senders zurück.
    """

    if not key:
        return None

    for canonical, aliases in ALIASES.items():

        if key == canonical:
            return canonical

        if key in aliases:
            return canonical

    return None


# ============================================================
# M3U EINLESEN
# ============================================================

def read_m3u():

    try:
        with open(
            INPUT_M3U,
            "r",
            encoding="utf-8",
            errors="ignore"
        ) as f:
            lines = f.readlines()

    except FileNotFoundError:

        print(
            f"[ERROR] {INPUT_M3U} nicht gefunden."
        )

        return []

    entries = []

    current_extinf = None
    current_extra = []

    for raw in lines:

        line = raw.strip()

        if not line:
            continue

        if line.startswith("#EXTM3U"):
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

        url = line

        if "," in current_extinf:

            name = (
                current_extinf
                .rsplit(",", 1)[1]
                .strip()
            )

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
# M3U8 EXTRAHIEREN
# ============================================================

def extract_m3u8_urls(text):

    if not text:
        return []

    text = unescape(text)

    text = text.replace(
        "\\/",
        "/"
    )

    text = text.replace(
        "\\u0026",
        "&"
    )

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

            url = url.strip(
                "\"' "
            )

            if url not in result:

                result.append(url)

    return result


# ============================================================
# VOLO SENDERSEITE
# ============================================================

def extract_stream_from_page(
    page_url,
    rss_name=None
):

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

        # RSS-Namen bevorzugen
        channel_name = rss_name

        # Nur wenn RSS keinen Namen liefert
        if not channel_name:

            meta = soup.find(
                "meta",
                property="og:title"
            )

            if meta and meta.get("content"):

                channel_name = (
                    meta["content"]
                    .strip()
                )

            elif soup.title:

                channel_name = (
                    soup.title
                    .get_text(
                        strip=True
                    )
                )

        streams = []

        # ----------------------------------------------------
        # Direktes HTML
        # ----------------------------------------------------

        for stream in extract_m3u8_urls(html):

            if stream not in streams:
                streams.append(stream)

        # ----------------------------------------------------
        # Scripts
        # ----------------------------------------------------

        for script in soup.find_all(
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

        # ----------------------------------------------------
        # Video / Source Tags
        # ----------------------------------------------------

        for tag in soup.find_all(
            ["video", "source"]
        ):

            for attr in [
                "src",
                "data-src",
                "data-url",
                "data-file",
            ]:

                value = tag.get(attr)

                if not value:
                    continue

                if ".m3u8" in value.lower():

                    value = urljoin(
                        page_url,
                        value
                    )

                    if value not in streams:
                        streams.append(value)

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
                    timeout=REQUEST_TIMEOUT
                )

                if iframe_response.status_code != 200:
                    continue

                iframe_html = (
                    iframe_response.text
                )

                for stream in extract_m3u8_urls(
                    iframe_html
                ):

                    if stream not in streams:

                        streams.append(
                            stream
                        )

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

                            streams.append(
                                stream
                            )

            except Exception:
                continue

        if not streams:
            return None

        return {
            "name": channel_name or rss_name,
            "key": get_canonical_key(
                channel_name or rss_name
            ),
            "variants": get_name_variants(
                channel_name or rss_name
            ),
            "streams": streams,
        }

    except Exception:

        return None


# ============================================================
# RSS FINDEN
# ============================================================

def get_volo_rss():

    for url in VOLO_RSS_URLS:

        try:

            response = session.get(
                url,
                timeout=REQUEST_TIMEOUT
            )

            if response.status_code != 200:
                continue

            text = response.text.lstrip().lower()

            if (
                text.startswith("<?xml")
                or "<rss" in text[:2000]
                or "<feed" in text[:2000]
            ):

                print(
                    f"[VOLO] Feed gefunden: {url}"
                )

                return response.content

        except Exception as e:

            print(
                f"[VOLO] Feed-Fehler {url}: {e}"
            )

    return None


# ============================================================
# RSS PARSEN
# ============================================================

def parse_rss(content):

    try:

        root = ET.fromstring(
            content
        )

    except ET.ParseError as e:

        print(
            f"[VOLO] XML-Fehler: {e}"
        )

        return []

    channels = []

    # --------------------------------------------------------
    # RSS 2.0
    # --------------------------------------------------------

    for item in root.findall(
        ".//item"
    ):

        title = item.find(
            "title"
        )

        link = item.find(
            "link"
        )

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

        namespace = (
            "{http://www.w3.org/2005/Atom}"
        )

        for entry in root.findall(
            f".//{namespace}entry"
        ):

            title = entry.find(
                f"{namespace}title"
            )

            link = entry.find(
                f"{namespace}link"
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
# VOLO STREAMS LADEN
# ============================================================

def scrape_volo_streams():

    print()
    print("=" * 70)
    print("VOLO RSS / STREAM SCAN")
    print("=" * 70)

    content = get_volo_rss()

    if not content:

        print(
            "[VOLO] Kein Feed erreichbar."
        )

        return {}

    channels = parse_rss(
        content
    )

    print(
        f"[VOLO] RSS-Einträge: {len(channels)}"
    )

    if not channels:
        return {}

    volo_map = {}

    with ThreadPoolExecutor(
        max_workers=MAX_WORKERS
    ) as executor:

        futures = {}

        for channel in channels:

            future = executor.submit(
                extract_stream_from_page,
                channel["url"],
                channel["name"]
            )

            futures[future] = channel

        for future in as_completed(
            futures
        ):

            try:

                result = future.result()

            except Exception:

                continue

            if not result:
                continue

            key = result["key"]

            if not key:
                continue

            streams = result["streams"]

            if not streams:
                continue

            # ------------------------------------------------
            # Hauptschlüssel
            # ------------------------------------------------

            if key not in volo_map:

                volo_map[key] = {
                    "name": result["name"],
                    "variants": set(),
                    "streams": [],
                }

            # Varianten
            volo_map[key]["variants"].update(
                result["variants"]
            )

            # Streams
            for stream in streams:

                if stream not in volo_map[key]["streams"]:

                    volo_map[key]["streams"].append(
                        stream
                    )

            # ------------------------------------------------
            # Alias zusätzlich registrieren
            # ------------------------------------------------

            alias = alias_key(key)

            if alias:

                if alias not in volo_map:

                    volo_map[alias] = {
                        "name": result["name"],
                        "variants": set(),
                        "streams": [],
                    }

                volo_map[alias]["variants"].update(
                    result["variants"]
                )

                for stream in streams:

                    if stream not in volo_map[alias]["streams"]:

                        volo_map[alias]["streams"].append(
                            stream
                        )

    print(
        f"[VOLO] Sender mit Stream: "
        f"{len(volo_map)}"
    )

    return volo_map


# ============================================================
# MATCHING
# ============================================================

def find_volo_match(
    vavoo_name,
    volo_map
):

    if not vavoo_name:
        return None, "none"

    vavoo_variants = get_name_variants(
        vavoo_name
    )

    if not vavoo_variants:
        return None, "none"

    # --------------------------------------------------------
    # 1. Exakter Match
    # --------------------------------------------------------

    for variant in vavoo_variants:

        if variant in volo_map:

            return (
                volo_map[variant],
                "exact"
            )

    # --------------------------------------------------------
    # 2. Alias-Match
    # --------------------------------------------------------

    for variant in vavoo_variants:

        alias = alias_key(
            variant
        )

        if alias and alias in volo_map:

            return (
                volo_map[alias],
                "alias"
            )

    # --------------------------------------------------------
    # 3. Volo-Varianten
    # --------------------------------------------------------

    for volo_key, data in volo_map.items():

        variants = data.get(
            "variants",
            set()
        )

        for variant in vavoo_variants:

            if variant in variants:

                return (
                    data,
                    "variant"
                )

    # --------------------------------------------------------
    # 4. Sicheres Teil-Matching
    # --------------------------------------------------------

    for volo_key, data in volo_map.items():

        if len(volo_key) < 4:
            continue

        for variant in vavoo_variants:

            if len(variant) < 4:
                continue

            if (
                variant in volo_key
                or volo_key in variant
            ):

                # Nur akzeptieren wenn
                # der Unterschied nicht zu groß ist
                difference = abs(
                    len(variant)
                    - len(volo_key)
                )

                if difference <= 5:

                    return (
                        data,
                        "partial"
                    )

    return None, "none"


# ============================================================
# BESTEN STREAM WÄHLEN
# ============================================================

def choose_volo_stream(streams):

    if not streams:
        return None

    # Master bevorzugen
    for stream in streams:

        if "master.m3u8" in stream.lower():

            return stream

    # Playlist bevorzugen
    for stream in streams:

        if "playlist.m3u8" in stream.lower():

            return stream

    # m3u8
    for stream in streams:

        if ".m3u8" in stream.lower():

            return stream

    return streams[0]


# ============================================================
# ALTE HEADER ENTFERNEN
# ============================================================

def clean_old_headers(lines):

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

        f.write(
            "#EXTM3U\n"
        )

        for entry in entries:

            f.write(
                entry["extinf"]
                + "\n"
            )

            for line in entry.get(
                "extra",
                []
            ):

                f.write(
                    line
                    + "\n"
                )

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
    print("=" * 70)
    print("IPTV HYBRID BUILD")
    print("=" * 70)

    # --------------------------------------------------------
    # Volo
    # --------------------------------------------------------

    volo_streams = scrape_volo_streams()

    # --------------------------------------------------------
    # Vavoo / build.js
    # --------------------------------------------------------

    entries = read_m3u()

    if not entries:

        print(
            "[ERROR] Keine IPTV-Einträge gefunden."
        )

        return

    print()
    print(
        f"[M3U] Kanäle: {len(entries)}"
    )

    output = []

    volo_count = 0
    vavoo_count = 0

    exact_count = 0
    alias_count = 0
    variant_count = 0
    partial_count = 0

    unmatched = []

    # --------------------------------------------------------
    # Jeden Kanal bearbeiten
    # --------------------------------------------------------

    for entry in entries:

        name = entry["name"]

        match, match_type = find_volo_match(
            name,
            volo_streams
        )

        # ----------------------------------------------------
        # VOLO
        # ----------------------------------------------------

        if match:

            stream = choose_volo_stream(
                match.get(
                    "streams",
                    []
                )
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

                if match_type == "exact":
                    exact_count += 1

                elif match_type == "alias":
                    alias_count += 1

                elif match_type == "variant":
                    variant_count += 1

                elif match_type == "partial":
                    partial_count += 1

                print(
                    f"[VOLO:{match_type.upper():7}] "
                    f"{name}"
                )

                output.append(
                    entry
                )

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

        unmatched.append(
            name
        )

        print(
            f"[VAVOO       ] {name}"
        )

        output.append(
            entry
        )

    # --------------------------------------------------------
    # Schreiben
    # --------------------------------------------------------

    write_playlist(
        output
    )

    # --------------------------------------------------------
    # Statistik
    # --------------------------------------------------------

    print()
    print("=" * 70)
    print("BUILD STATISTIK")
    print("=" * 70)

    print(
        f"Kanäle gesamt       : {len(output)}"
    )

    print(
        f"Volo                : {volo_count}"
    )

    print(
        f"  Exact             : {exact_count}"
    )

    print(
        f"  Alias             : {alias_count}"
    )

    print(
        f"  Variant            : {variant_count}"
    )

    print(
        f"  Partial            : {partial_count}"
    )

    print(
        f"Vavoo Fallback      : {vavoo_count}"
    )

    print(
        f"Volo Sender erkannt : {len(volo_streams)}"
    )

    # --------------------------------------------------------
    # Nicht gematchte Sender
    # --------------------------------------------------------

    print()
    print("=" * 70)
    print("NICHT VON VOLO GEMATCHTE SENDER")
    print("=" * 70)

    if unmatched:

        for name in unmatched:

            print(
                f"  - {name}"
            )

    else:

        print(
            "  Keine!"
        )

    print()
    print(
        f"[OK] {OUTPUT_M3U} geschrieben."
    )

    print("=" * 70)


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    process_hybrid_m3u()