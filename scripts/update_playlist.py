"""Build the molle-iptv playlists.

The point of this workflow is that a visitor who clicks a channel gets a
picture. Two things stand in the way, and both are handled here:

  * roughly half of the public streams out there don't work in a browser at
    all — see stream_status() — so they're tested and the dead ones dropped.
    A stream that merely refuses *this* machine is kept and marked instead:
    the build runs in the United States and the audience does not, and DR1
    and DR2 answer perfectly well on a home connection.
  * the source playlists are barely categorised. A quarter of them arrive as
    "Undefined", and one source labels its groups by country instead. Nobody
    browses 3,000 uncategorised channels, so the category, country and region
    are filled in from iptv-org's API by tvg-id rather than guessed.

Three files come out:

  playlist.m3u8       validated. Only channels a browser can actually play,
                      from any hosting. This is the one to embed.

  playlist-all.m3u8   everything we merged, enriched but unvalidated. Correct
                      for VLC and TiviMate, which don't care about http:// or
                      CORS and can play a great deal more.

  playlist-kodi.m3u8  the same channels as playlist-all, shaped for Kodi's
                      IPTV Simple Client — see kodi_ready().
"""

import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin

import requests

SOURCES = [
    ("iptv-org/index", "https://iptv-org.github.io/iptv/index.m3u"),
    ("Free-TV/IPTV", "https://raw.githubusercontent.com/Free-TV/IPTV/master/playlist.m3u8"),
    # Sampled 2026-11-20 against the two above: new channels / share answering
    ("TDTChannels", "https://www.tdtchannels.com/lists/tv.m3u8"),        # 378 / 65 %, Spain
    ("M3UPT", "https://m3upt.com/iptv"),                                # 864 / 95 %, Portugal
    ("Nexonic", "https://nexonic-technologies.github.io/nexonic-iptv/playlists/all.m3u"),  # 705 / 90 %
]

API = "https://iptv-org.github.io/api"

# iptv-org publishes 30 categories. That is far too many to put in front of
# someone who just wants to watch something, so they collapse into these.
# Anything unmapped or missing becomes "General" — an honest bucket, and
# iptv-org's own largest one — rather than a dumping ground called Undefined.
CATEGORY = {
    "news": "News", "legislative": "News", "weather": "News", "business": "News",
    "sports": "Sports", "outdoor": "Sports",
    "movies": "Movies & Series", "series": "Movies & Series", "classic": "Movies & Series",
    "entertainment": "Entertainment", "comedy": "Entertainment",
    "music": "Music",
    "kids": "Kids & Family", "family": "Kids & Family", "animation": "Kids & Family",
    "documentary": "Documentary", "education": "Documentary",
    "science": "Documentary", "culture": "Documentary",
    "religious": "Religious",
    "lifestyle": "Lifestyle", "cooking": "Lifestyle", "travel": "Lifestyle",
    "auto": "Lifestyle", "shop": "Lifestyle",
    "xxx": "Adult",
}
FALLBACK_CATEGORY = "General"

# Continent-level regions only. iptv-org publishes 42, but most are
# overlapping ("EMEA", "EU", "Balkan", "Nordics") and would file one channel
# under three headings. These five cover every country exactly once, except
# the handful that genuinely straddle two — Turkey, Russia — which take the
# first match in this order.
CONTINENT_ORDER = [("EUR", "Europe"), ("ASIA", "Asia"), ("AFR", "Africa"),
                   ("AMER", "Americas"), ("OCE", "Oceania")]

VALIDATE = os.environ.get("VALIDATE", "1") != "0"
WORKERS = int(os.environ.get("VALIDATE_WORKERS", "64"))
TIMEOUT = float(os.environ.get("VALIDATE_TIMEOUT", "8"))
LIMIT = int(os.environ.get("VALIDATE_LIMIT", "0"))  # 0 = no limit; for local runs
# a stream that could not be reached gets one more go this long after the
# first pass — long enough for a CDN hiccup to pass, short enough not to matter
RETRY_PAUSE = float(os.environ.get("VALIDATE_RETRY_PAUSE", "20"))

UA = "Mozilla/5.0 (compatible; molle-iptv/1.0; +https://github.com/molle-me/tv)"
PROBE_ORIGIN = "https://molle-iptv.example"

# Some geo-gated broadcasters (RAI's relinker is the known one) don't refuse an
# out-of-country request. They redirect it to a short "not available in your
# country" clip and answer 200. The redirect target gives it away: it is the
# same file whichever channel was asked for. (Idea from Free-TV/IPTV.)
GEOBLOCK_DECOYS = ("video_no_available.mp4",)

# IPTV Simple reads one XMLTV url from the playlist header, and only when the
# user has not set one. Our audience is Danish, so Denmark is the default;
# iptv-epg.org keys its guide on the same channel ids as iptv-org.
KODI_EPG = "https://iptv-epg.org/files/epg-dk.xml.gz"


def fetch_text(url):
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    return resp.text


def fetch_json(url):
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    return resp.json()


def load_metadata():
    """tvg-id -> {language, category, country, region}, plus an adult id set.

    Playlist ids look like "DR1.dk@SD"; channels.json keys on "DR1.dk", so the
    feed suffix is stripped before matching.
    """
    meta, nsfw = {}, set()
    try:
        channels = fetch_json(f"{API}/channels.json")
        countries = {c["code"]: c["name"] for c in fetch_json(f"{API}/countries.json")}
        regions = fetch_json(f"{API}/regions.json")
        feeds = fetch_json(f"{API}/feeds.json")
        languages = {l["code"]: l["name"] for l in fetch_json(f"{API}/languages.json")}
    except Exception as e:
        print(f"WARNING: metadata unavailable, channels stay unenriched: {e}",
              file=sys.stderr)
        return {}, {}, {}, set(), {}, {}

    by_code = {r["code"]: r for r in regions}
    country_region = {}
    for code, name in CONTINENT_ORDER:
        for cc in by_code.get(code, {}).get("countries", []):
            country_region.setdefault(cc, name)  # first continent listed wins

    for ch in channels:
        if ch.get("is_nsfw"):
            nsfw.add(ch["id"])
        cats = ch.get("categories") or []
        cc = ch.get("country") or ""
        meta[ch["id"]] = {
            "category": next((CATEGORY[c] for c in cats if c in CATEGORY),
                             FALLBACK_CATEGORY),
            "country": countries.get(cc, ""),
            "region": country_region.get(cc, ""),
        }

    # language lives on the feed, not the channel: the same channel can carry
    # different languages on different feeds
    # — and so does where it is meant to be watched. Pluto's Danish feed of an
    # American series is "7thHeaven.se@DK": the channel is filed under Sweden,
    # the feed is broadcast to Denmark, with Danish subtitles. Filed by the
    # channel, 136 of the 153 feeds iptv-org lists for Denmark landed under
    # Sweden here. Only country-level areas count; "r/EUR" says nothing useful.
    lang, area = {}, {}
    for feed in feeds:
        key = f"{feed['channel']}@{feed['id']}"
        codes = feed.get("languages") or []
        if codes:
            lang[key] = languages.get(codes[0], codes[0])
        cc = [a[2:] for a in feed.get("broadcast_area") or [] if a.startswith("c/")]
        if cc:
            area[key] = cc

    print(f"Metadata: {len(meta)} channels, {len(lang)} feeds, "
          f"{len(area)} with a country-level broadcast area, {len(nsfw)} flagged adult")
    return meta, lang, area, nsfw, countries, country_region


# A feed sent to more countries than this is a pan-regional one; its own
# country then says more than a dropdown entry in every one of them.
MAX_AREA_COUNTRIES = 4


def enrich(extinf, meta, lang, area, nsfw, countries, country_region):
    """Returns the #EXTINF line with our attributes filled in."""
    tvg_id = (re.search(r'tvg-id="([^"]*)"', extinf) or [None, ""])[1]
    channel_id = tvg_id.split("@")[0]

    info = meta.get(channel_id, {})
    country, region = info.get("country", ""), info.get("region", "")
    if not country:
        # Not in iptv-org's data — but a source may still have given us a bare
        # country code, either in tvg-country or as the ".dk" in the tvg-id.
        # Expanding it fills the country *and* the region for free.
        existing = (re.search(r'tvg-country="([^"]*)"', extinf) or [None, ""])[1]
        suffix = (re.search(r'\.([a-z]{2})(?:@|$)', channel_id) or [None, ""])[1]
        cc = (existing or suffix).upper()
        if cc in countries:
            country = countries[cc]
            region = region or country_region.get(cc, "")

    # where the feed is broadcast beats where the channel is from
    sent_to = [c for c in area.get(tvg_id, []) if c in countries]
    if 0 < len(sent_to) <= MAX_AREA_COUNTRIES:
        country = ";".join(countries[c] for c in sent_to)
        region = country_region.get(sent_to[0], region)

    attrs = {
        "tvg-language": lang.get(tvg_id, ""),
        "tvg-country": country,
        "tvg-region": region,
        # Marked rather than removed. Whoever embeds this decides — the flag
        # is here so hiding them is one filter, not a fork of the playlist.
        "tvg-adult": "1" if channel_id in nsfw else "",
    }
    # Overwrite rather than fill in blanks: one source already ships
    # tvg-country as a bare code ("IT"), and a dropdown holding both "IT" and
    # "Italy" is worse than one holding neither.
    for key, value in attrs.items():
        if not value:
            continue
        if f'{key}="' in extinf:
            extinf = re.sub(f'{key}="[^"]*"', f'{key}="{value}"', extinf, count=1)
        else:
            extinf = extinf.replace("#EXTINF:-1", f'#EXTINF:-1 {key}="{value}"', 1)

    # always restate the group: the sources disagree about what it means, and
    # one of them puts the country there
    category = info.get("category", FALLBACK_CATEGORY)
    if 'group-title="' in extinf:
        extinf = re.sub(r'group-title="[^"]*"', f'group-title="{category}"', extinf, count=1)
    else:
        extinf = extinf.replace("#EXTINF:-1", f'#EXTINF:-1 group-title="{category}"', 1)
    return extinf


def merge(sources, meta, lang, area, nsfw, countries, country_region):
    """Returns (entries, options): (#EXTINF, url) pairs, and url -> option lines.

    The url is not always the line after #EXTINF. iptv-org puts
    "#EXTVLCOPT:http-user-agent=…" between the two for channels whose server
    insists on a browser's User-Agent, and taking the next line blindly once
    published those option lines *as the url*. The options are kept aside
    rather than dropped: VLC and Kodi send them as headers, and for those
    channels that is the difference between a picture and a refusal.
    """
    entries, options, seen = [], {}, set()
    for name, url in sources:
        try:
            lines = fetch_text(url).split("\n")
        except Exception as e:
            print(f"WARNING: Failed to fetch {name}: {e}", file=sys.stderr)
            continue
        before = len(entries)
        for i, line in enumerate(lines):
            if not line.startswith("#EXTINF"):
                continue
            opts, j = [], i + 1
            while j < len(lines) and lines[j].startswith("#") and not lines[j].startswith("#EXTINF"):
                if lines[j].startswith(("#EXTVLCOPT", "#KODIPROP")):
                    opts.append(lines[j].strip())
                j += 1
            url_line = lines[j].strip() if j < len(lines) else ""
            # "://" also turns away placeholders like "[NO PUBLIC STREAM]"
            if "://" not in url_line or url_line.startswith("#") or url_line in seen:
                continue
            entries.append((enrich(line, meta, lang, area, nsfw, countries, country_region),
                            url_line))
            if opts:
                options[url_line] = opts
            seen.add(url_line)
        print(f"{name}: +{len(entries) - before} channels")
    return entries, options


def stream_status(url):
    """"ok" | "restricted" | "dead", from where this build happens to run.

    The distinction matters because the build runs on a GitHub runner in the
    United States while the audience is somewhere else entirely. DR1 and DR2
    answer 200 on a home connection and refuse the runner, and an earlier version of
    this check deleted them for it — channels that had never once failed for
    an actual visitor.

    So an access refusal is not evidence that a stream is dead. It is evidence
    that *this machine* may not watch it, which is a different fact and not
    one worth deleting a channel over. Those are kept and marked; the player
    can tell the visitor a channel may need to be in its home country.

    Nor is a 200 evidence that it plays. A master playlist that answers can
    point at variants that 404, or at segments served without CORS headers —
    hls.js fetches both with XHR, so both need them. The check therefore
    follows the playlist one level down, the way the player will.

    A fourth answer, "unreachable", means the request itself failed: timeout,
    reset, 5xx. That says more about the moment than about the stream, so
    validate() gives those a second chance before calling them dead.
    """
    if not url.startswith("https://"):
        return "dead"          # mixed content: nobody can play it in a browser
    try:
        verdict, final_url, body = hop(url)
        if verdict != "ok":
            return verdict
        if not body.lstrip().startswith("#EXTM3U"):
            return "dead"      # an error page, a DASH manifest, raw TS — hls.js plays none
        children = next_hops(body)
        if not children:
            return "ok"        # nothing listed in what we read; don't guess
        is_master = "#EXT-X-STREAM-INF" in body
        # hls.js moves to another variant when one won't load, so one that
        # works is enough; of the rest, the most forgiving answer stands
        verdicts = []
        for child in children:
            try:
                # a segment is fetched for a kilobyte, not in full
                verdict, _, child_body = hop(urljoin(final_url, child),
                                             None if is_master else {"Range": "bytes=0-1023"})
            except requests.RequestException:
                verdict = "unreachable"
            if verdict == "ok" and is_master and not child_body.lstrip().startswith("#EXTM3U"):
                verdict = "dead"
            if verdict == "ok":
                return "ok"
            verdicts.append(verdict)
        return min(verdicts, key=("restricted", "unreachable", "dead").index)
    except requests.RequestException:
        return "unreachable"
    except Exception:
        return "dead"


def hop(url, extra_headers=None):
    """One request, judged as hls.js on someone else's page would see it.

    Returns (verdict, final url, start of the body). Network failures are
    left to raise, so stream_status() can tell them apart from refusals.
    """
    headers = {"User-Agent": UA, "Origin": PROBE_ORIGIN, **(extra_headers or {})}
    with requests.get(url, timeout=TIMEOUT, stream=True, headers=headers) as resp:
        if any(decoy in resp.url for decoy in GEOBLOCK_DECOYS):
            return "restricted", resp.url, ""
        if resp.status_code in (401, 403, 451):
            return "restricted", resp.url, ""
        if resp.status_code == 429 or resp.status_code >= 500:
            return "unreachable", resp.url, ""
        if resp.status_code not in (200, 206):    # 206: a ranged segment
            return "dead", resp.url, ""
        if resp.headers.get("Access-Control-Allow-Origin") not in ("*", PROBE_ORIGIN):
            return "dead", resp.url, ""    # useless to every embedder, wherever they are
        if not resp.url.startswith("https://"):
            return "dead", resp.url, ""
        body = resp.raw.read(65536, decode_content=True).decode("utf-8", "replace")
        if body.lstrip().startswith("#EXTM3U") and "http://" in body:
            return "dead", resp.url, ""    # mixed content one level down
        return "ok", resp.url, body


def next_hops(playlist):
    """The uris the player may fetch next, best first; empty if none.

    For a master playlist: up to three variants. For a media playlist: the
    *last* segment only — in a live window the first one is the oldest and may
    already be gone from the CDN, while the player starts near the end.

    Tags some servers send without their "#" — "EXT-X-VERSION:6" — are not
    uris and are skipped. A plain colon is not enough to tell: Wowza paths
    carry one ("_definst_/smil:channel.smil/chunklist.m3u8").
    """
    uris = [line.strip() for line in playlist.splitlines()
            if line.strip() and not line.startswith("#")
            and not re.match(r"EXT[A-Z-]*:", line.strip())]
    if not uris:
        return []
    return uris[:3] if "#EXT-X-STREAM-INF" in playlist else uris[-1:]


def validate(entries):
    targets = entries[:LIMIT] if LIMIT else entries
    print(f"Validating {len(targets)} streams, {WORKERS} at a time ({TIMEOUT}s timeout)…")
    with ThreadPoolExecutor(WORKERS) as pool:
        results = list(pool.map(lambda e: stream_status(e[1]), targets))
        # One blip from a runner on the other side of the world should not cost
        # a channel twelve hours off the list. Only failures that might be
        # passing get the second go; a 404 is an answer, not a blip.
        again = [i for i, status in enumerate(results) if status == "unreachable"]
        if again:
            print(f"Retrying {len(again)} unreachable streams in {RETRY_PAUSE:.0f}s…")
            time.sleep(RETRY_PAUSE)
            for i, status in zip(again, pool.map(lambda i: stream_status(targets[i][1]), again)):
                results[i] = status
            recovered = sum(results[i] != "unreachable" for i in again)
            print(f"  {recovered} answered the second time.")
    results = ["dead" if status == "unreachable" else status for status in results]

    kept = []
    counts = {"ok": 0, "restricted": 0, "dead": 0}
    for (extinf, url), status in zip(targets, results):
        counts[status] += 1
        if status == "dead":
            continue
        if status == "restricted" and 'tvg-geo="' not in extinf:
            # kept deliberately: this build could not watch it, but the
            # visitor may well be in the country that can
            extinf = extinf.replace("#EXTINF:-1", '#EXTINF:-1 tvg-geo="restricted"', 1)
        kept.append((extinf, url))

    total = max(len(targets), 1)
    print(f"Playable here: {counts['ok']} ({counts['ok']/total*100:.1f}%)  "
          f"kept as geo-restricted: {counts['restricted']}  "
          f"dropped as dead: {counts['dead']}")
    return kept


ATTRS = re.compile(r'\s[\w-]+="[^"]*"')
NOISE = re.compile(r'\([^)]*\)|\[[^\]]*\]|\b(?:HD|SD|FHD|UHD|4K)\b|[ⒼⓈ®™]', re.I)


def channel_name(extinf):
    bare = ATTRS.sub("", extinf[extinf.index(":") + 1:])
    return bare.split(",", 1)[1].strip() if "," in bare else ""


def dedupe(entries):
    """One row per channel.

    The same channel arrives from several sources under names that differ only
    in decoration — "DR1", "DR1 Ⓖ", "DR1 (1080p) [Geo-blocked]" — so URL
    deduplication never catches them and the list reads as repetitive. Strip
    the decoration, then keep the highest resolution of each.

    Run this *after* validation, so a working 720p beats a dead 1080p.
    """
    best = {}       # key -> (position, extinf, url, resolution)
    unnamed = []    # nothing to compare on; keep every one, untouched
    for position, (extinf, url) in enumerate(entries):
        name = channel_name(extinf)
        # \W is unicode-aware here, so Cyrillic and Greek names survive it
        key = re.sub(r'[\W_]+', '', NOISE.sub(" ", name), flags=re.UNICODE).casefold()
        if not key:
            unnamed.append((position, extinf, url))
            continue
        res = int((re.search(r'\((\d{3,4})[pi]\)', name) or [None, 0])[1])
        if key not in best or res > best[key][3]:
            # keep the first sighting's position, so the order stays stable
            position = best[key][0] if key in best else position
            best[key] = (position, extinf, url, res)

    rows = [(p, e, u) for p, e, u, _ in best.values()] + unnamed
    rows.sort(key=lambda r: r[0])
    kept = [(e, u) for _, e, u in rows]
    removed = len(entries) - len(kept)
    if removed:
        print(f"Deduplicated {removed} repeated channels ({removed/len(entries)*100:.1f}%).")
    return kept


def kodi_ready(entries):
    """The entries with their tvg-id reduced to what an XMLTV guide uses.

    iptv-org names a *feed*, "DR1.dk@SD"; every guide names the channel,
    "DR1.dk". IPTV Simple matches the two exactly, so with the feed left on not
    one programme would ever be found. Only Kodi's copy is changed: tv.html
    keys channels on the full id, and SD and HD feeds must stay distinct there.
    """
    return [(re.sub(r'(tvg-id="[^"@]*)@[^"]*"', r'\1"', extinf, count=1), url)
            for extinf, url in entries]


def write(path, entries, options=None, header="#EXTM3U"):
    """Options go between #EXTINF and the url, where VLC and Kodi look for
    them. playlist.m3u8 is written without: its readers take the line after
    #EXTINF as the url."""
    options = options or {}
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"{header}\n")
        for extinf, url in entries:
            for opt in options.get(url, []):
                extinf = f"{extinf}\n{opt}"
            f.write(f"{extinf}\n{url}\n")
    groups = len({(re.search(r'group-title="([^"]*)"', e) or [None, ""])[1]
                  for e, _ in entries})
    print(f"Wrote {path}: {len(entries)} channels in {groups} groups.")


def main():
    meta, lang, area, nsfw, countries, country_region = load_metadata()
    entries, options = merge(SOURCES, meta, lang, area, nsfw, countries, country_region)
    if not entries:
        print("ERROR: All sources failed.", file=sys.stderr)
        sys.exit(1)

    everything = dedupe(entries)
    write("playlist-all.m3u8", everything, options)
    write("playlist-kodi.m3u8", kodi_ready(everything), options,
          header=f'#EXTM3U x-tvg-url="{KODI_EPG}"')

    if not VALIDATE:
        print("VALIDATE=0 — publishing the merge unchecked.")
        write("playlist.m3u8", dedupe(entries))
        return

    # Validate everything *before* deduplicating, so that when a channel comes
    # in several variants the survivor is the best one that actually works,
    # not the highest number that happens to be dead.
    playable = dedupe(validate(entries))
    if not playable:
        # Never publish an empty playlist over a good one: a network problem
        # here would otherwise wipe the list every embedder points at.
        print("ERROR: nothing passed validation; leaving playlist.m3u8 alone.",
              file=sys.stderr)
        sys.exit(1)
    write("playlist.m3u8", playable)


if __name__ == "__main__":
    main()
