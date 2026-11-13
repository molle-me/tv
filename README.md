# molle-iptv

A self-updating playlist of free, public TV channels — and, more to the point,
a workflow that throws away the ones that don't work.

Anyone can merge a few public m3u files. The problem is that roughly half the
streams in them can't play in a web browser at all, so a visitor clicks a
channel and gets nothing, with no explanation. This repo exists to fix that
part. Every 12 hours a GitHub Action merges the sources, fills in the missing
metadata, tests every stream, and publishes only the ones that answered.

Used live at [molle.me](https://molle.me).

## The two playlists

| File | Channels | For |
|---|---|---|
| `playlist.m3u8` | verified | **Web pages.** Every channel was reachable over https with open CORS at build time. This is the one to embed. |
| `playlist-all.m3u8` | everything | **VLC, Kodi, TiviMate.** Native players don't care about https or CORS, so they can play a great deal more. |

Both are deduplicated and carry `group-title`, `tvg-country`, `tvg-region` and
`tvg-language` on every entry.

## Use it on your own site

```html
<script src="https://cdn.jsdelivr.net/npm/hls.js@1.5.17/dist/hls.min.js"></script>
<video id="tv" controls playsinline></video>
<script>
  const PLAYLIST =
    'https://raw.githubusercontent.com/molle-me/tv/main/playlist.m3u8';

  fetch(PLAYLIST).then(r => r.text()).then(text => {
    const lines = text.split('\n');
    const channels = [];
    for (let i = 0; i < lines.length; i++) {
      if (!lines[i].startsWith('#EXTINF')) continue;
      const name = lines[i].split(',').pop().trim();
      const url  = (lines[i + 1] || '').trim();
      const attr = k => (lines[i].match(new RegExp(k + '="([^"]*)"')) || [])[1] || '';
      if (url.startsWith('http')) {
        channels.push({ name, url, country: attr('tvg-country'),
                        region: attr('tvg-region'), group: attr('group-title') });
      }
    }
    // channels[] is now ready to filter by region, country or group
    play(channels[0].url);
  });

  function play(url) {
    const video = document.getElementById('tv');
    if (Hls.isSupported()) {
      const hls = new Hls();
      hls.loadSource(url);
      hls.attachMedia(video);
      hls.on(Hls.Events.MANIFEST_PARSED, () => video.play().catch(() => {}));
    } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
      video.src = url;                       // Safari plays HLS natively
    }
  }
</script>
```

Point a region dropdown at `tvg-region` (five continents), a country dropdown
at `tvg-country`, and a genre dropdown at `group-title` (ten categories). That
is the whole navigation — no other setup, no server, no API key.

## Why a channel gets dropped

`is_browser_playable()` in `scripts/update_playlist.py` is the whole policy,
and each rule is a real failure that used to reach visitors:

1. **Not https.** An `http://` stream is blocked as mixed content on any https
   page. Nothing in the page can override this — it is enforced by the browser
   itself. Retrying those over https recovers about 5%, so it isn't worth it.
   They stay in `playlist-all.m3u8`, where VLC plays them happily.
2. **CORS that doesn't allow you.** hls.js fetches over XHR, so a stream needs
   `Access-Control-Allow-Origin`. `*` works for everyone, and so does a server
   that echoes back whichever origin asked. One pinned to somebody else's
   origin — Pluto returns `http://pluto.tv` — is useless to every embedder.
3. **`http://` inside the manifest.** A playlist can answer `200` over https
   and still point every variant at `http://`. The failure then lands one hop
   later, where it looks like nothing happened at all.

**Geo-restricted channels are kept, not dropped.** The build runs on a GitHub
runner in the United States; your visitors are somewhere else. A stream that
answers `401`, `403` or `451` is not dead — it simply won't serve *that
machine*. DR1 and DR2 refuse the runner and answer `200` on a home connection, and
an earlier version of this check deleted them for it. Those channels stay in
the list carrying `tvg-geo="restricted"`, so a player can tell the visitor a
channel may need to be watched from its home country instead of pretending it
doesn't exist.

## How the list grows: the source scout

The build only merges what is in `SOURCES`, so the list used to grow only when
somebody opened a PR to add a source. `scripts/discover_sources.py`, run by the
weekly **Discover Sources** workflow (Mondays 06:00 UTC, or on demand), now
does that maintenance itself:

1. **Health-check every existing source.** Two sources once 404'd silently for
   weeks and the list shrank by half before anyone noticed. A source that does
   not answer now opens an issue instead.
2. **Score candidates.** Each candidate in `CANDIDATES` is fetched, compared
   against the channels we already have, and probe-tested on a sample of its
   streams. It is promoted into `SOURCES` only if it adds at least 40 new URLs
   and at least 30% of the sample answers — so a junk source costs the list
   nothing, it is just reported in `sources-report.md` and forgotten.
3. **Nothing is removed automatically.** A source that is down this week may
   be back next week, and validation already protects the published playlists.

To suggest a source, add its URL to `CANDIDATES` in
`scripts/discover_sources.py` and the scout will judge it like any other.

## Running it yourself

```bash
pip install -r requirements.txt
python scripts/update_playlist.py
```

| Variable | Default | |
|---|---|---|
| `VALIDATE` | `1` | `0` skips validation — much faster when you only want to see the merge |
| `VALIDATE_WORKERS` | `64` | parallel requests |
| `VALIDATE_TIMEOUT` | `8` | seconds per stream |
| `VALIDATE_LIMIT` | `0` | cap the number checked, for quick local runs |

A full validated build takes a while — it opens one request per stream.

## Legal

This merges playlists of streams that broadcasters publish freely and
publicly. It hosts no video and circumvents nothing. Check what applies where
you are before redistributing, and don't add sources for paid services.

## License

Apache 2.0
