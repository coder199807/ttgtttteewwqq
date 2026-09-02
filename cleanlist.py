import re
import json
import time
import os
import html
import unicodedata
import requests

from concurrent.futures import ThreadPoolExecutor, as_completed


# ============================================================
# KONFIGURATION
# ============================================================

INPUT_M3U = "iptv.m3u"
OUTPUT_M3U = "iptv.m3u"

CUSTOM_LINKS_FILE = "custom_links.json"
CACHE_FILE = "stream_cache.json"

CACHE_TTL = 12 * 60 * 60

MAX_WORKERS = 8
REQUEST_TIMEOUT = 15
STREAM_TIMEOUT = 10

VAVOO_PROXIES = [
    "https://vavoo-proxy.kadirmetin.workers.dev",
    "https://vavoo-proxy.vercel.app",
    "https://vavoo-proxy.netlify.app",
]

VAVOO_BASE = "https://vavoo.to"

# Browser-ähnliche Header.
# Wichtig: Vavoo reagiert empfindlich auf fehlende Header.
VAVOO_HEADERS = {
    "accept": "*/*",
    "accept-language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7,tr;q=0.6",
    "cache-control": "no-cache",
    "pragma": "no-cache",
    "origin": "https://vavoo.to",
    "referer": "https://vavoo.to/live",
    "sec-ch-ua": '"Not=A?Brand";v="99", "Google Chrome";v="151", "Chromium";v="151"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/151.0.0.0 Safari/537.36"
    ),
}

GENERIC_HEADERS = {
    "User-Agent": VAVOO_HEADERS["user-agent"],
    "Accept": "*/*",
    "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
    "Connection": "keep-alive",
}


# ============================================================
# TEXT / CHANNEL-NAMEN
# ============================================================

def normalize_text(value):
    if not value:
        return ""

    value = html.unescape(str(value))

    replacements = {
        "ä": "ae",
        "ö": "oe",
        "ü": "ue",
        "ß": "ss",
        "Ä": "ae",
        "Ö": "oe",
        "Ü": "ue",
        "İ": "I",
        "ı": "i",
        "Ş": "S",
        "ş": "s",
        "Ğ": "G",
        "ğ": "g",
        "Ç": "C",
        "ç": "c",
    }

    for old, new in replacements.items():
        value = value.replace(old, new)

    value = unicodedata.normalize("NFKD", value)
    value = "".join(
        c for c in value
        if not unicodedata.combining(c)
    )

    value = value.lower()

    return value


def clean_channel_name(name):
    if not name:
        return ""

    name = html.unescape(name)

    # Präfixe
    name = re.sub(
        r"^\s*(?:4K\s*TR|4K|TR|DE|AT|CH|VF)\s*:\s*",
        "",
        name,
        flags=re.I,
    )

    # technische Suffixe
    name = re.sub(
        r"\s*\.(?:b|c|s)\b",
        "",
        name,
        flags=re.I,
    )

    name = re.sub(
        r"\s*\((?:BACKUP|H265|H\.265)\)",
        "",
        name,
        flags=re.I,
    )

    # eckige Zusatzinfos
    name = re.sub(r"\[[^\]]*\]", "", name)

    # Qualitätsangaben
    name = re.sub(
        r"\b(?:HD|FHD|UHD|4K|HEVC|H265|H264|RAW|SD|"
        r"1080P|720P|576P|2160P)\b",
        "",
        name,
        flags=re.I,
    )

    # HTML / Whitespaces
    name = re.sub(r"\s+", " ", name)

    return name.strip()


# ============================================================
# STABILER EINDEUTIGER KEY
# ============================================================

def canonical_name(name):
    """
    Nur für normale Sender ohne Vavoo-ID.
    """

    name = normalize_text(clean_channel_name(name))

    return re.sub(
        r"[^a-z0-9]+",
        "",
        name,
    )


# ============================================================
# M3U PARSER
# ============================================================

def parse_m3u(filename):
    entries = []

    with open(
        filename,
        "r",
        encoding="utf-8",
        errors="ignore",
    ) as f:

        lines = [line.rstrip("\r\n") for line in f]

    current_extinf = None

    for line in lines:

        if line.startswith("#EXTINF:"):
            current_extinf = line

        elif (
            current_extinf
            and line.strip()
            and not line.startswith("#")
        ):
            entries.append({
                "extinf": current_extinf,
                "url": line.strip(),
            })

            current_extinf = None

    return entries


def get_extinf_name(extinf):
    if not extinf:
        return ""

    if "," in extinf:
        return extinf.split(",", 1)[1].strip()

    return ""


# ============================================================
# VAVOO ID
# ============================================================

def extract_vavoo_id(url):
    if not url:
        return None

    match = re.search(
        r"/play/([a-fA-F0-9]+)",
        url,
    )

    if match:
        return match.group(1).lower()

    return None


# ============================================================
# URL NICHT BESCHÄDIGEN
# ============================================================

def split_stream_url(url):
    """
    IPTV-URLs können hinter | zusätzliche Parameter enthalten.

    Beispiel:

    https://example.com/live.m3u8|User-Agent=Mozilla/5.0

    NICHT abschneiden.
    """

    if "|" not in url:
        return url, ""

    base, options = url.split("|", 1)

    return base, "|" + options


def build_stream_url(base, options):
    return base + options


# ============================================================
# CACHE
# ============================================================

def load_cache():
    if not os.path.exists(CACHE_FILE):
        return {}

    try:
        with open(
            CACHE_FILE,
            "r",
            encoding="utf-8",
        ) as f:
            data = json.load(f)

        if isinstance(data, dict):
            return data

    except Exception:
        pass

    return {}


def save_cache(cache):
    tmp = CACHE_FILE + ".tmp"

    with open(
        tmp,
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(
            cache,
            f,
            ensure_ascii=False,
            indent=2,
        )

    os.replace(tmp, CACHE_FILE)


def cache_key(entry):
    """
    Vavoo:
        IMMER ID benutzen.

    Dadurch sind beispielsweise:

        360 .c
        360 .s

    zwei komplett unterschiedliche Streams.
    """

    vavoo_id = extract_vavoo_id(entry["url"])

    if vavoo_id:
        return "vavoo:" + vavoo_id

    return "url:" + entry["url"]


def get_cached(cache, key):
    item = cache.get(key)

    if not isinstance(item, dict):
        return None

    timestamp = item.get("timestamp")

    if not timestamp:
        return None

    if time.time() - timestamp > CACHE_TTL:
        return None

    url = item.get("url")

    if not url:
        return None

    return url


def put_cache(cache, key, url):
    cache[key] = {
        "timestamp": time.time(),
        "url": url,
    }


# ============================================================
# VAVOO STREAM TEST
# ============================================================

def test_vavoo(url):
    """
    Vavoo niemals mit HEAD testen.

    Stattdessen normaler GET.
    """

    try:
        response = requests.get(
            url,
            headers=VAVOO_HEADERS,
            timeout=STREAM_TIMEOUT,
            allow_redirects=True,
            stream=True,
        )

        status = response.status_code

        if status in (
            200,
            206,
            301,
            302,
            307,
            308,
        ):
            final_url = response.url or url

            response.close()

            return final_url

        response.close()

    except requests.RequestException:
        pass

    return None


# ============================================================
# NORMALE STREAMS
# ============================================================

def test_generic(url):
    base_url, options = split_stream_url(url)

    headers = dict(GENERIC_HEADERS)

    # IPTV User-Agent aus URL übernehmen
    if options:
        match = re.search(
            r"User-Agent=([^|]+)",
            options,
            flags=re.I,
        )

        if match:
            headers["User-Agent"] = match.group(1)

    try:
        response = requests.get(
            base_url,
            headers=headers,
            timeout=STREAM_TIMEOUT,
            allow_redirects=True,
            stream=True,
        )

        if response.status_code in (
            200,
            206,
            301,
            302,
            307,
            308,
        ):
            final_url = response.url or base_url

            response.close()

            return build_stream_url(
                final_url,
                options,
            )

        response.close()

    except requests.RequestException:
        pass

    return None


# ============================================================
# PROXY TEST
# ============================================================

def test_vavoo_proxies(vavoo_id):
    if not vavoo_id:
        return None

    for proxy in VAVOO_PROXIES:

        proxy = proxy.rstrip("/")

        candidates = [
            f"{proxy}/play/{vavoo_id}",
            f"{proxy}/vavoo-iptv/play/{vavoo_id}",
        ]

        for candidate in candidates:

            try:
                response = requests.get(
                    candidate,
                    headers=GENERIC_HEADERS,
                    timeout=STREAM_TIMEOUT,
                    allow_redirects=True,
                    stream=True,
                )

                if response.status_code in (
                    200,
                    206,
                    301,
                    302,
                    307,
                    308,
                ):
                    final_url = response.url or candidate

                    response.close()

                    return final_url

                response.close()

            except requests.RequestException:
                continue

    return None


# ============================================================
# EINZELNEN KANAL REPARIEREN
# ============================================================

def repair_channel(entry, cache):
    name = get_extinf_name(entry["extinf"])
    original_url = entry["url"]

    vavoo_id = extract_vavoo_id(original_url)

    key = cache_key(entry)

    print(
        f"[CHECK] {name}"
        + (
            f" [{vavoo_id}]"
            if vavoo_id
            else ""
        )
    )

    # --------------------------------------------------------
    # 1. CACHE
    # --------------------------------------------------------

    cached = get_cached(cache, key)

    if cached:

        if vavoo_id:
            result = test_vavoo(cached)
        else:
            result = test_generic(cached)

        if result:
            print(f"[CACHE OK] {name}")
            return result

        # kaputten Cache SOFORT löschen
        cache.pop(key, None)


    # --------------------------------------------------------
    # 2. CUSTOM LINKS
    # --------------------------------------------------------

    # wird weiter unten über custom_links verarbeitet


    # --------------------------------------------------------
    # 3. ORIGINAL VAVOO
    # --------------------------------------------------------

    if vavoo_id:

        result = test_vavoo(original_url)

        if result:
            put_cache(cache, key, result)

            print(f"[VAVOO OK] {name}")

            return result


        # ----------------------------------------------------
        # 4. PROXIES
        # ----------------------------------------------------

        result = test_vavoo_proxies(vavoo_id)

        if result:
            put_cache(cache, key, result)

            print(f"[PROXY OK] {name}")

            return result

    else:

        # ----------------------------------------------------
        # NORMALER STREAM
        # ----------------------------------------------------

        result = test_generic(original_url)

        if result:
            put_cache(cache, key, result)

            print(f"[STREAM OK] {name}")

            return result


    print(f"[FAILED] {name}")

    return None


# ============================================================
# CUSTOM LINKS
# ============================================================

def load_custom_links():
    if not os.path.exists(CUSTOM_LINKS_FILE):
        return {}

    try:
        with open(
            CUSTOM_LINKS_FILE,
            "r",
            encoding="utf-8",
        ) as f:

            data = json.load(f)

        if isinstance(data, dict):
            return data

    except Exception as e:
        print(
            f"[WARN] custom_links.json konnte "
            f"nicht gelesen werden: {e}"
        )

    return {}


def find_custom_link(entry, custom_links):
    name = get_extinf_name(entry["extinf"])

    key = canonical_name(name)

    # Erst exakter Name
    if name in custom_links:
        return custom_links[name]

    # Dann normalisierter Key
    if key in custom_links:
        return custom_links[key]

    return None


# ============================================================
# M3U SCHREIBEN
# ============================================================

def write_m3u(entries, filename):
    with open(
        filename,
        "w",
        encoding="utf-8",
        newline="\n",
    ) as f:

        f.write("#EXTM3U\n")

        for entry in entries:

            f.write(entry["extinf"] + "\n")
            f.write(entry["url"] + "\n")


# ============================================================
# HAUPTPROZESS
# ============================================================

def process():
    print("=" * 70)
    print("IPTV STREAM REPAIR")
    print("=" * 70)

    if not os.path.exists(INPUT_M3U):
        raise FileNotFoundError(
            f"{INPUT_M3U} nicht gefunden."
        )

    entries = parse_m3u(INPUT_M3U)

    print(
        f"[INFO] {len(entries)} Kanäle gefunden."
    )

    cache = load_cache()
    custom_links = load_custom_links()

    # Ergebnisliste mit gleicher Reihenfolge
    results = [None] * len(entries)

    # --------------------------------------------------------
    # CUSTOM LINKS ZUERST
    # --------------------------------------------------------

    jobs = []

    for index, entry in enumerate(entries):

        custom = find_custom_link(
            entry,
            custom_links,
        )

        if custom:

            if isinstance(custom, str):
                custom = [custom]

            if isinstance(custom, list):

                found = None

                for candidate in custom:

                    if not isinstance(candidate, str):
                        continue

                    vavoo_id = extract_vavoo_id(candidate)

                    if vavoo_id:
                        found = test_vavoo(candidate)
                    else:
                        found = test_generic(candidate)

                    if found:
                        break

                if found:

                    results[index] = {
                        "extinf": entry["extinf"],
                        "url": found,
                    }

                    print(
                        f"[CUSTOM OK] "
                        f"{get_extinf_name(entry['extinf'])}"
                    )

                    continue

        jobs.append((index, entry))


    # --------------------------------------------------------
    # PARALLEL TEST
    # --------------------------------------------------------

    with ThreadPoolExecutor(
        max_workers=MAX_WORKERS
    ) as executor:

        futures = {
            executor.submit(
                repair_channel,
                entry,
                cache,
            ): index

            for index, entry in jobs
        }

        for future in as_completed(futures):

            index = futures[future]
            entry = entries[index]

            try:
                repaired_url = future.result()

            except Exception as e:
                print(
                    f"[ERROR] "
                    f"{get_extinf_name(entry['extinf'])}: "
                    f"{e}"
                )
                repaired_url = None

            if repaired_url:

                results[index] = {
                    "extinf": entry["extinf"],
                    "url": repaired_url,
                }


    # --------------------------------------------------------
    # NUR FUNKTIONIERENDE KANÄLE
    # --------------------------------------------------------

    final_entries = [
        result
        for result in results
        if result is not None
    ]

    # Cache speichern
    save_cache(cache)

    # M3U schreiben
    write_m3u(
        final_entries,
        OUTPUT_M3U,
    )

    print()
    print("=" * 70)
    print("FERTIG")
    print("=" * 70)

    print(
        f"Original: {len(entries)}"
    )

    print(
        f"Funktionierend: {len(final_entries)}"
    )

    print(
        f"Entfernt: "
        f"{len(entries) - len(final_entries)}"
    )

    print(
        f"Ausgabe: {OUTPUT_M3U}"
    )


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    process()