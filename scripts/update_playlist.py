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

Two files come out:

  playlist.m3u8      validated. Only channels a browser can actually play,
                     from any hosting. This is the one to embed.

  playlist-all.m3u8  everything we merged, enriched but unvalidated. Correct
                     for VLC, Kodi and TiviMate, which don't care about http://
                     or CORS and can play a great deal more.
"""

import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor

import requests

SOURCES = [
    ("iptv-org/index", "https://iptv-org.github.io/iptv/index.m3u"),
    ("Free-TV/IPTV", "https://raw.githubusercontent.com/Free-TV/IPTV/master/playlist.m3u8"),
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

UA = "Mozilla/5.0 (compatible; molle-iptv/1.0; +https://github.com/molle-me/tv)"


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
        return {}, {}, set(), {}, {}

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
    lang = {}
    for feed in feeds:
        codes = feed.get("languages") or []
        if codes:
            lang[f"{feed['channel']}@{feed['id']}"] = languages.get(codes[0], codes[0])

    print(f"Metadata: {len(meta)} channels, {len(lang)} feeds, "
          f"{len(nsfw)} flagged adult")
    return meta, lang, nsfw, countries, country_region


def enrich(extinf, meta, lang, nsfw, countries, country_region):
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


def merge(sources, meta, lang, nsfw, countries, country_region):
    entries, seen = [], set()
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
            url_line = lines[i + 1].strip() if i + 1 < len(lines) else ""
            if not url_line or url_line in seen:
                continue
            entries.append((enrich(line, meta, lang, nsfw, countries, country_region),
                            url_line))
            seen.add(url_line)
        print(f"{name}: +{len(entries) - before} channels")
    return entries


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
    """
    if not url.startswith("https://"):
        return "dead"          # mixed content: nobody can play it in a browser
    probe_origin = "https://molle-iptv.example"
    resp = None
    try:
        resp = requests.get(url, timeout=TIMEOUT, stream=True,
                            headers={"User-Agent": UA, "Origin": probe_origin})
        if resp.status_code in (401, 403, 451):
            return "restricted"
        if resp.status_code != 200:
            return "dead"
        if resp.headers.get("Access-Control-Allow-Origin") not in ("*", probe_origin):
            return "dead"      # useless to every embedder, wherever they are
        if not resp.url.startswith("https://"):
            return "dead"
        body = resp.raw.read(4096, decode_content=True).decode("utf-8", "replace")
        return "dead" if "http://" in body else "ok"
    except Exception:
        return "dead"
    finally:
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass


def validate(entries):
    targets = entries[:LIMIT] if LIMIT else entries
    print(f"Validating {len(targets)} streams, {WORKERS} at a time ({TIMEOUT}s timeout)…")
    with ThreadPoolExecutor(WORKERS) as pool:
        results = list(pool.map(lambda e: stream_status(e[1]), targets))

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


def write(path, entries):
    with open(path, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for extinf, url in entries:
            f.write(f"{extinf}\n{url}\n")
    groups = len({(re.search(r'group-title="([^"]*)"', e) or [None, ""])[1]
                  for e, _ in entries})
    print(f"Wrote {path}: {len(entries)} channels in {groups} groups.")


def main():
    meta, lang, nsfw, countries, country_region = load_metadata()
    entries = merge(SOURCES, meta, lang, nsfw, countries, country_region)
    if not entries:
        print("ERROR: All sources failed.", file=sys.stderr)
        sys.exit(1)

    write("playlist-all.m3u8", dedupe(entries))

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
