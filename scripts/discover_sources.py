"""Find new playlist sources; retire dead ones.

The main build only merges what is in SOURCES, so the list only grows when
somebody adds a source. This script runs weekly and does that maintenance:

  1. Health-check every source already in SOURCES. A source that no longer
     answers is a real problem — half the list once silently shrank when two
     sources 404'd — so an issue is opened instead of waiting for a human to
     notice the channel count falling.

  2. Score candidate sources. Each candidate is fetched, deduplicated against
     what we already have, sampled and probe-tested. A candidate only earns its
     place by adding enough new channels that actually answer. Everything
     that fails is reported and forgotten; nothing is added on a hunch.

  3. Promote survivors into SOURCES in this file's parent (update_playlist.py)
     and write sources-report.md so the run is auditable.

Nothing is ever removed from SOURCES automatically: a source that is down
today may be back next week, and validation already protects the published
playlists from whatever junk a bad source carries.
"""

import os
import random
import re
import sys
from concurrent.futures import ThreadPoolExecutor

import requests

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "update_playlist.py")

TIMEOUT = 10
SAMPLE = 20                # streams probed per candidate
MIN_SAMPLE_OK = 0.30       # at least this fraction of the sample must answer
MIN_NEW_CHANNELS = 40      # and it must add at least this many new URLs
MAX_NEW_SOURCES_PER_RUN = 2

UA = "Mozilla/5.0 (compatible; molle-iptv-scout/1.0; +https://github.com/molle-me/tv)"

# Curated candidates. These are public aggregator playlists; each is judged by
# the same sample validation as everything else and dropped if it fails.
CANDIDATES = [
    "https://iptv-org.github.io/iptv/countries/dk.m3u",
    "https://iptv-org.github.io/iptv/countries/no.m3u",
    "https://iptv-org.github.io/iptv/countries/se.m3u",
    "https://iptv-org.github.io/iptv/categories/news.m3u",
    "https://iptv-org.github.io/iptv/categories/sports.m3u",
]


def read_sources():
    with open(SCRIPT, encoding="utf-8") as f:
        src = f.read()
    block = re.search(r"SOURCES\s*=\s*\[(.*?)\]", src, re.S).group(1)
    return src, re.findall(r'\("([^"]+)",\s*"([^"]+)"\)', block)


def stream_urls(text):
    return [l.strip() for l in text.split("\n")
            if l.strip().startswith("http")]


def probe(url):
    try:
        r = requests.get(url, timeout=TIMEOUT, stream=True,
                         headers={"User-Agent": UA})
        return r.status_code in (200, 301, 302)
    except Exception:
        return False
    finally:
        try:
            r.close()
        except Exception:
            pass


def health_check(sources):
    dead = []
    for name, url in sources:
        try:
            r = requests.get(url, timeout=TIMEOUT, stream=True,
                             headers={"User-Agent": UA})
            ok = r.status_code == 200
            r.close()
        except Exception:
            ok = False
        print(f"{'OK  ' if ok else 'DEAD'} {name} ({url})")
        if not ok:
            dead.append((name, url))
    return dead


def evaluate_candidate(url, existing_urls):
    try:
        r = requests.get(url, timeout=30, headers={"User-Agent": UA})
        if r.status_code != 200:
            return None, f"fetch failed: HTTP {r.status_code}"
        urls = stream_urls(r.text)
    except Exception as e:
        return None, f"fetch failed: {e}"

    new_urls = [u for u in urls if u not in existing_urls]
    if len(new_urls) < MIN_NEW_CHANNELS:
        return None, f"only {len(new_urls)} new channels"

    random.seed(42)
    sample = random.sample(new_urls, min(SAMPLE, len(new_urls)))
    with ThreadPoolExecutor(8) as pool:
        oks = list(pool.map(probe, sample))
    ratio = sum(oks) / max(len(oks), 1)
    if ratio < MIN_SAMPLE_OK:
        return None, f"sample {ratio:.0%} answered (< {MIN_SAMPLE_OK:.0%})"

    print(f"PASS {url}: {len(new_urls)} new, sample {ratio:.0%} ok")
    return {"url": url, "new": len(new_urls), "sample_ok": ratio}, None


def promote(src_text, promoted):
    for url in promoted:
        name = re.sub(r"https?://(www\.)?", "", url).split("/")[0]
        entry = f'    ("{name}", "{url}"),\n'
        src_text = src_text.replace("SOURCES = [\n", "SOURCES = [\n" + entry, 1)
    return src_text


def main():
    src_text, sources = read_sources()
    existing_urls = set()
    for _, url in sources:
        try:
            r = requests.get(url, timeout=30, headers={"User-Agent": UA})
            existing_urls.update(stream_urls(r.text))
        except Exception as e:
            print(f"WARNING: could not read current source {url}: {e}",
                  file=sys.stderr)

    report = ["# Source scout report\n"]
    report.append(f"Existing channels across {len(sources)} sources: "
                  f"{len(existing_urls)}\n")

    dead = health_check(sources)
    if dead:
        report.append("## Dead sources\n")
        for name, url in dead:
            report.append(f"- **{name}** `{url}` did not answer. "
                          "Removed from SOURCES? No — check it manually.\n")
    else:
        report.append("All existing sources answer.\n")

    promoted = []
    for url in CANDIDATES:
        if len(promoted) >= MAX_NEW_SOURCES_PER_RUN:
            break
        result, reason = evaluate_candidate(url, existing_urls)
        if result:
            promoted.append(result["url"])
            report.append(f"- ✅ **{url}**: {result['new']} new channels, "
                          f"sample {result['sample_ok']:.0%} answered\n")
        else:
            report.append(f"- ❌ {url}: {reason}\n")
            print(f"SKIP {url}: {reason}")

    if promoted:
        src_text = promote(src_text, promoted)
        with open(SCRIPT, "w", encoding="utf-8") as f:
            f.write(src_text)

    with open("sources-report.md", "w", encoding="utf-8") as f:
        f.write("\n".join(report) + "\n")

    # Hand the counts to the workflow step that opens an issue.
    with open(os.environ.get("GITHUB_ENV", "/dev/null"), "a") as env:
        env.write(f"DEAD_SOURCES={len(dead)}\n")
        env.write(f"PROMOTED={len(promoted)}\n")


if __name__ == "__main__":
    main()
