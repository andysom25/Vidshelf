# Vidshelf - Code Reference & Debugging Log

> **This is an engineering log, not user documentation** — see
> [README.md](README.md) for installation and usage. Below is a chronological
> record of what broke, why, how it was fixed, and how to verify it, kept
> because several bugs here were expensive to diagnose and easy to reintroduce.
> If you're changing code in this repo, skim it first; if you're troubleshooting,
> search it before opening an issue. Newest entries are at the bottom.

## Architecture Overview

```
┌──────────────────────────────────────────────────────────┐
│  app.py (Flask Web Server)                                │
│  ┌─────────────────────────────────────────────────────┐  │
│  │ Route Handlers:                                      │  │
│  │ ● /api/music-videos/download  → Music video download │  │
│  │ ● /api/download               → Channel video DL     │  │
│  │ ● /api/channels/*             → Channel management   │  │
│  │ ● /api/music-videos/search    → Search music videos  │  │
│  └─────────────────────────────────────────────────────┘  │
│  Each starts a daemon thread → download_video()           │
├──────────────────────────────────────────────────────────┤
│  downloader.py (Download Engine)                          │
│  ├── download_video(video_id, download_path,              │
│  │                  plex_media_path, ...)                 │
│  │    └── Uses yt-dlp to download                        │
│  │    └── Then copies/moves to plex_media_path            │
│  ├── get_active_downloads()                               │
│  ├── _progress_hook()                                     │
│  └── active_downloads.json (progress tracking)            │
├──────────────────────────────────────────────────────────┤
│  docker-compose.yml / Dockerfile                          │
│  └── /app/music_videos_final = named volume "music_videos_final" │
│      backed by a NATIVE Linux cifs mount of the NAS share  │
│      (NOT a bind mount of a Windows drive letter/UNC path -│
│      that silently fails, see RESOLVED section below)      │
├──────────────────────────────────────────────────────────┤
│  artwork_sync.py (Artist Artwork + Plex OAuth/Collections) │
│  ├── ArtworkWatcher — background thread, polls root_path   │
│  │    for new artist folders every watch_interval seconds  │
│  ├── sync_artist_artwork() — pulls art from TheAudioDB /   │
│  │    Fanart.tv / MusicBrainz / Wikipedia (in that order)  │
│  ├── plex_oauth_start()/plex_oauth_check_pin() — PIN OAuth  │
│  ├── plex_ensure_smart_collection() — creates a Plex        │
│  │    *smart* collection per artist (title-filter based,    │
│  │    auto-includes future matching videos)                │
│  └── plex_upload_collection_poster()/_art() — pushes        │
│       folder.jpg/fanart.jpg onto the collection             │
├──────────────────────────────────────────────────────────┤
│  templates/ (Frontend)                                    │
│  ├── dashboard.html (SPA - all JS in one file)            │
│  └── login.html                                           │
├──────────────────────────────────────────────────────────┤
│  config.json (Persistent Configuration)                   │
│  ├── channels[]                                           │
│  ├── plex_base_path                                       │
│  ├── music_video_plex_path (used by music videos)         │
│  ├── artwork_sync{} (root_path, watch_interval,            │
│  │    plex_collection_sync_on_artwork, fanarttv_api_key)   │
│  └── plex{} (server_url, token, music_video_library_key)  │
└──────────────────────────────────────────────────────────┘
```

## File Responsibilities

| File | Role | Key Functions |
|------|------|---------------|
| `app.py` | Flask server, API endpoints | `api_music_videos_download()`, `_do_download()`, `/api/plex/*` routes |
| `downloader.py` | Download engine | `download_video()`, `_progress_hook()` |
| `artwork_sync.py` | Artist artwork + Plex OAuth/collections | `ArtworkWatcher`, `plex_oauth_start()`, `plex_ensure_smart_collection()` |
| `templates/dashboard.html` | Frontend SPA | `downloadMusicVideo()`, `searchMusicVideos()`, `startPlexOAuth()` |
| `docker-compose.yml` | Container orchestration | Native `cifs` volume mounts NAS share to `/app/music_videos_final` |
| `config.json` | Configuration | `music_video_plex_path`, `channels[]`, `artwork_sync{}`, `plex{}` |

## File Pathing Logic

### Music Videos (fixed and verified end-to-end 2026-07-09):
1. User searches → `POST /api/music-videos/search` → `search_music_videos()`
2. User clicks Download → `POST /api/music-videos/download` → `api_music_videos_download()`
3. `_do_download()` thread starts with:
   - `download_path = './downloads/music_videos'` (real bind mount of host `D:\...\downloads`)
   - `final_path = '/app/music_videos_final'` (real `cifs` volume onto the NAS - see RESOLVED section)
4. Calls `download_video(video_id, download_path, final_path, ...)` 
5. yt-dlp downloads to `download_path` (real local bind mount)
6. `downloader.py` then does a manual buffered copy (`open()` + `shutil.copyfileobj()`, NOT `shutil.copy2`) from `download_path` → `final_path`

### Channel Videos (Working flow):
1. User clicks Download → `POST /api/download` → `api_download()`
2. `_do_download()` thread starts with:
   - `download_path = ch['download_path']` (from config)
   - `plex_media_path = resolved absolute path`
3. Calls `download_video(video_id, download_path, plex_media_path, ...)`
4. If paths differ → `shutil.copy2` + `os.remove` (copy-then-delete strategy)

## RESOLVED (verified 2026-07-09): "No space left on device" on CIFS/NAS mount

This had **two separate, stacked bugs**. Fixing the first one (sendfile) made
the symptom look identical, which is why it initially seemed like the fix
hadn't worked. Both are now fixed and a real download was confirmed to land
on the actual NAS.

### Bug #1: `shutil.copy2` silently uses `os.sendfile()` on Linux

`os.sendfile()` is unreliable against CIFS/Samba mounts — it can misreport
free space partway through a transfer and raise `ENOSPC`. Swapping
`shutil.move` for `shutil.copy2()` looked like a fix but wasn't: CPython's
`shutil.copyfile()` (called by `copy2()`) has used `os.sendfile()` as an
internal fast-path since Python 3.8 (`shutil._fastcopy_sendfile`), and only
falls back to a plain read/write loop if the *very first* `sendfile()` call
fails outright. Once a few chunks succeed, a later `ENOSPC` from the same
syscall propagates straight through instead of triggering the fallback.

**Fix**: [downloader.py](downloader.py) now does a manual copy —
`open(src,'rb')`/`open(dst,'wb')` + `shutil.copyfileobj()` — which never calls
`sendfile`. Never revert this to `shutil.copy2`/`move`/`copyfile` for a copy
that touches a network filesystem.

### Bug #2 (the actual blocker, found after fixing #1): the Y:\ "mount" was never real

`docker-compose.yml` used to bind-mount a Windows drive letter straight into
the container: `Y:\:/app/music_videos_final`. **Docker Desktop on Windows
cannot bind-mount a network-mapped drive or a UNC path** — it doesn't error,
it silently substitutes a small local decoy volume instead. Proof (see
`df -h` inside the old container): `/app/music_videos_final` was `/dev/sdd`,
type **ext4**, 137MB total, already 100% full. The container had never once
talked to the actual NAS — every write failed because that decoy volume was
almost entirely consumed by earlier test downloads (which likewise never
reached the real share; they were recovered from the decoy volume and moved
onto the real NAS by hand during this investigation).

I also confirmed a UNC-path bind mount (`\\192.168.1.100\ppv\MusicVideos:/app/music_videos_final`)
has the exact same failure mode — Docker silently substitutes another decoy
volume rather than actually reaching the share. Neither drive-letter nor
UNC-path bind mounts work for network shares on Docker Desktop for Windows.

**Fix**: use Docker's native `cifs` volume driver, which mounts the SMB share
using the Linux kernel's own CIFS client (bypassing the Windows redirector
entirely). This is now in `docker-compose.yml`:

```yaml
services:
  vidshelf:
    volumes:
      - ./downloads:/app/downloads
      - music_videos_final:/app/music_videos_final
    cap_add:
      - SYS_ADMIN   # required for the cifs mount

volumes:
  music_videos_final:
    driver_opts:
      type: cifs
      o: "username=${NAS_SMB_USER},password=${NAS_SMB_PASS},vers=3.0,uid=1000,gid=1000,file_mode=0777,dir_mode=0777"
      device: "${NAS_SMB_DEVICE}"
```

`NAS_SMB_USER`/`NAS_SMB_PASS`/`NAS_SMB_DEVICE` live in a local `.env` file
(gitignored, not committed — see `.env.example` for the template).
`docker-compose` loads `.env` automatically for variable substitution.
(`NAS_SMB_DEVICE` was hardcoded directly in `docker-compose.yml` until this
repo was prepared for public release — moved to an env var alongside the
credentials so the file doesn't bake in one specific NAS's address/share
path.)

**How this was verified as real** (not another decoy): `df -h` inside the
container showed `//192.168.1.100/ppv/MusicVideos` type `cifs`, ~103T total /
4.6T available — matching the real NAS capacity Windows itself reports for
`Y:`. A file written from the container took a few seconds to become visible
from Windows — that's a stale Windows-side SMB directory cache (a separate,
harmless quirk; a fresh `net use Y: /delete` + remap clears it instantly), not
a sign the mount is fake. A real end-to-end music video download through the
UI completed with no `ENOSPC` and the file appeared in
`/app/music_videos_final`, confirmed both from inside the container and from
Windows via `Y:`.

### Bug #3 (cosmetic, but caused a false "it's still broken" report): unbuffered stdout

After both real bugs were fixed, a follow-up test download looked like it
failed again — `docker logs` showed a thread's "completed successfully" and
"Paths differ, copying" debug lines appearing *before* that same thread's
"thread started" line. This is Python's stdout buffering: under Docker,
`stdout` isn't a TTY, so `print()` is fully block-buffered rather than
line-buffered, and with multiple concurrent download threads writing to the
same buffer without flushing, lines from different requests can get held and
flushed out of chronological order. The download had actually succeeded (the
file was sitting on the real NAS, confirmed via `df -h`/`ls` and from Windows)
— the logs just lied about the order.

**Fix**: `ENV PYTHONUNBUFFERED=1` added to [Dockerfile](Dockerfile). Log lines
now print in real chronological order. If `docker logs` ever again shows a
"completed" message before its matching "started" message, suspect this
regressed (e.g. someone removed the env var, or set `PYTHONUNBUFFERED=0`) -
don't waste time re-diagnosing the download logic itself first.

### Attempted Fixes (chronological):

| Attempt | Change | Result |
|---------|--------|--------|
| 1. Download to Y:\ directly | `download_video(id, final_path, final_path)` | Failed - hit the decoy-volume bug (Bug #2), misdiagnosed as yt-dlp/CIFS incompatibility |
| 2. Download locally, copy to Y:\ | `download_video(id, local_path, final_path)` | Still failed - copy step also wrote into the same decoy volume |
| 3. Debug logging | Added print statements in `downloader.py` | Confirmed *where* it failed, not *why* |
| 4. shutil.copy2 instead of shutil.move | Replaced `shutil.move` with `copy2`+`os.remove` | Did not fix it - copy2 uses os.sendfile() internally on Linux, same ENOSPC issue (Bug #1) |
| 5. Manual buffered copy | `open()`+`shutil.copyfileobj()` instead of `copy2()` | Fixed Bug #1, but download still failed with the *identical* error - looked like no progress |
| 6. Diagnosed the decoy volume | `docker exec vidshelf df -h` showed `/dev/sdd` ext4 137MB 100% full | Found Bug #2 - the real blocker |
| 7. Native `cifs` volume driver | Replaced the Y:\ bind mount with a `driver_opts: type: cifs` named volume | **Confirmed working** - real end-to-end download landed on the real NAS |
| 8. `PYTHONUNBUFFERED=1` | Added to Dockerfile | Fixed Bug #3 - log lines from concurrent threads now print in real order, so a working download can no longer look broken in `docker logs` |

### Current Code State (all three fixes applied and verified):
- `docker-compose.yml`: `music_videos_final` named volume with `type: cifs` driver opts, credentials from `.env`
- `Dockerfile`: `ENV PYTHONUNBUFFERED=1`
- `app.py` `api_music_videos_download()`: `download_path = './downloads/music_videos'` (local), `final_path = '/app/music_videos_final'` (real NAS via cifs)
- `downloader.py` `download_video()`: manual buffered copy (`open()` + `shutil.copyfileobj()` + `shutil.copystat()`), never `shutil.copy2`/`move`

If this ever regresses, check all three things: (1) did someone reintroduce
`shutil.copy2`/`move`/`copyfile` for the CIFS hop, (2) is
`/app/music_videos_final` still the `cifs`-typed named volume, or did
docker-compose.yml drift back to a bind mount of a drive letter/UNC path, and
(3) is `PYTHONUNBUFFERED=1` still set (if logs look out-of-order, check this
before assuming the download logic itself is broken).
`docker exec vidshelf df -h` distinguishes (2) instantly — a real NAS
mount shows type `cifs` and multi-terabyte capacity; a decoy shows `ext4`
and a tiny (double-digit-to-low-hundreds MB) size.

## Container Setup

- **NAS share**: `\\192.168.1.100\ppv\MusicVideos` (Samba/SMB share, ~103TB pool)
- **Container**: `/app/music_videos_final` = native `cifs` named volume (see RESOLVED section) — genuinely reaches the NAS, not a Windows-side passthrough
- **Local**: `./downloads/music_videos` = real bind mount of the host's `D:\...\downloads\music_videos` (confirmed via `df -h`: `D:\` filesystem, real capacity)
- Credentials for the cifs mount live in `.env` (gitignored) as `NAS_SMB_USER`/`NAS_SMB_PASS`

## Key Debugging Commands

```bash
# Check container logs
docker logs vidshelf --tail 200

# Check what type of mount /app/music_videos_final actually is - the single
# most useful command for this whole class of bug. Real NAS = type cifs,
# multi-TB. Decoy volume = type ext4, tiny (was 137MB before the fix).
docker exec vidshelf df -h

# Check mounted path inside container
docker exec vidshelf ls -la /app/music_videos_final/

# Check local downloads
docker exec vidshelf ls -la /app/downloads/music_videos/

# Rebuild and restart
docker-compose down && docker-compose up -d --build

# If docker-compose.yml's volumes: section changed, `down && up` is required -
# `up -d --build` alone won't pick up a new/changed named volume definition.

# Stream logs in real-time
docker logs vidshelf -f
```

## Next Steps / What Needs Investigation

Nothing outstanding for music video downloads - verified working end-to-end
on 2026-07-09, including a real download landing on the NAS and being
visible from both the container and Windows. If a fresh session picks this
up and sees something that *looks* broken, check in this order before
re-diagnosing from scratch:

1. **Logs look out of order / a "completed" line appears before its
   "started" line?** That's Bug #3 (stdout buffering) if `PYTHONUNBUFFERED=1`
   got dropped from the Dockerfile, or is just cosmetic and expected to look
   slightly odd under concurrent downloads even with the fix. Check the
   filesystem directly (`ls`, `df -h`) rather than trusting log order alone.
2. `docker exec vidshelf df -h` — is `/app/music_videos_final` actually the
   real NAS (`cifs`, multi-TB) or a decoy (`ext4`, tiny)?
3. Is the copy implementation in `downloader.py` avoiding
   `shutil.copy2`/`move`/`copyfile`?
4. If checking from Windows (`Y:`, Explorer) and a file the container just
   wrote doesn't show up, that's very likely a stale SMB directory cache —
   `net use Y: /delete` then remap before concluding the file isn't really
   there.

If similar "No space left on device" symptoms show up elsewhere (e.g. the
channel-video download flow, if a user ever points `plex_media_path` at a
network share instead of a local path), the same two checks (#2 and #3
above) are the fastest way in.

## Code Flow Diagram (Music Video Download)

```
Web UI (dashboard.html)
  │
  └─ downloadMusicVideo(videoId, title)
      │  POST /api/music-videos/download
      │  JSON: { video_id, title, artist }
      ▼
app.py: api_music_videos_download()
  │  Sets:
  │    download_path = './downloads/music_videos'
  │    final_path    = '/app/music_videos_final'
  │
  ├─ Spawn daemon thread: _do_download()
  │     │
  │     ├─ os.makedirs(download_path, exist_ok=True)
  │     ├─ os.makedirs(final_path, exist_ok=True)
  │     │
  │     └─ download_video(id, download_path, final_path, ...)
  │           │
  │           ├─ yt-dlp downloads to download_path (real local bind mount, D:\...\downloads)
  │           │   outtmpl → ./downloads/music_videos/%(title)s-%(id)s.%(ext)s
  │           │
  │           └─ Manual buffered copy from download_path → final_path
  │               (real cifs-mounted NAS share; open()+shutil.copyfileobj(),
  │                NOT shutil.copy2 - see RESOLVED section for why both matter)
  │
  └─ Return { success: true } to frontend
```

## RESOLVED (verified 2026-07-20): Plex OAuth login + automatic Smart Collections

This feature failed for a *stack* of independent bugs, the same way the CIFS
download issue above did — fixing one revealed the next, and several looked
like the exact same generic failure until diagnosed individually. If a fresh
agent is asked to touch Plex OAuth, collection creation, or artist artwork
again and something looks broken, read this whole section before
re-diagnosing from scratch — nearly all of it was already hit once.

### Bug A: Malformed Plex OAuth redirect URL

**Symptom**: user clicks "Connect to Plex", logs in on the Plex tab, and
either sees plex.tv's generic **"We were unable to complete this request. You
may now close this window."** error, or the login appears to succeed but
`/api/plex/oauth/check` polls `pending` forever.

The URL app.plex.tv needs is extremely format-sensitive and was wrong twice:

- **Original (broken)**: `PLEX_BASE_AUTH_URL = "https://app.plex.tv/auth#!"`
  then `f"{PLEX_BASE_AUTH_URL}/?clientID=..."` → produced
  `.../auth#!/?clientID=...` (bang, then a stray `/`, then the query string).
- **First fix attempt (also wrong)**: assumed the hash-bang itself was the
  legacy/broken part and stripped it to `.../auth#?clientID=...`. This still
  hit "We were unable to complete this request" — hash-bang isn't legacy
  cruft, the *position* of the slash was the actual bug.
- **Correct form** (verified against `python-plexapi`'s
  `MyPlexPinLogin.oauthUrl()` in `myplex.py`, and confirmed with an actual
  browser login — "Thanks! You have successfully signed in."):
  `https://app.plex.tv/auth/#!?clientID=...&code=...&context[device][product]=...`
  — slash **before** the hash, bang immediately after, then the query string
  directly with no extra slash.
- Also required: the PIN-creation POST to `https://plex.tv/api/v2/pins` must
  include `strong=true` as a query param, or the returned `code` isn't valid
  for this URL-embedded flow (it's meant for the separate plex.tv/link
  manual-entry flow instead).
- Implementation: `plex_oauth_start()` in [artwork_sync.py](artwork_sync.py).

**If this regresses**: don't re-derive the URL format from first principles —
diff whatever your code builds against `python-plexapi`'s `myplex.py` source
on GitHub (`pkkid/python-plexapi`), which is the same ground truth other
working integrations (Tautulli, Overseerr) build on. A generic Plex "We were
unable to complete this request" error on the auth page almost always means
the URL shape is wrong, not that OAuth is broken account-side.

### Bug B: `plex_get_servers()` missing `X-Plex-Client-Identifier`

**Symptom**: OAuth login "succeeds" (account name shows up), but the
dashboard's Plex Integration card shows "Connected" with **Server** and
**Library Key** left blank, and `config.json`'s `plex.server_url` never gets
populated no matter how many times you click through.

`GET https://plex.tv/api/v2/resources` (used to list the account's Plex
servers) returned `400 {"errors":[{"code":1000,"message":"X-Plex-Client-Identifier
is missing"}]}` because `plex_get_servers()` only sent `X-Plex-Token` +
`Accept`. The bare `except Exception: return []` swallowed this completely, so
it looked like "no servers on the account" rather than a request error.
`plex_get_account_info()` (a different function, hits `/api/v2/user`) doesn't
need this header, which is why account info populated fine while servers did
not — **don't assume one plex.tv endpoint's header requirements apply to the
next one; check each new endpoint independently.**

Fix: add `'X-Plex-Client-Identifier': PLEX_CLIENT_ID` to the headers dict in
`plex_get_servers()`.

### Bug C: library auto-discovery misses a misspelled library title

**Symptom**: `plex_find_library_key()` auto-discovers and saves a
`music_video_library_key` that looks plausible but collection/video lookups
come back empty or hit the wrong library entirely (e.g. a movies library).

The auto-discovery matches on the substring `"music video"` in a library's
`title` (case-insensitive). This account's actual library is titled
**"Muisc Videos"** (transposed typo) — the substring never matches, so
discovery silently falls back to "first library with type=movie", which
happened to be an unrelated 4K movies library.

**Always sanity-check the saved `music_video_library_key` by hand** against
`GET {server_url}/library/sections` (see Key Debugging Commands below) rather
than trusting auto-discovery, especially the first time this runs against a
new Plex account/server, or after "Discover Library" is clicked and nothing
seems to fail.

### Bug D: OAuth "Connect to Plex" button missing from the HTML entirely

**Symptom**: the dashboard's "Step 1: Authenticate with Plex" box shows only
descriptive text — no button, nothing to click.

`templates/dashboard.html`'s JS (`startPlexOAuth()`, `checkPlexOAuth()`)
referenced `document.getElementById('plex-oauth-btn')`, but no element with
that ID existed anywhere in the markup — it was never added when the section
was built. No console error results from this; the element reference calls
just silently no-op or the button is simply absent. If Plex UI is ever
reworked, grep the JS for every `getElementById('plex-...')` call and confirm
each ID actually exists in the HTML — this bug produces no error signal at
all, just a missing control that's easy to miss in a screenshot-free review.

### Design change: static collections → Smart Collections (2026-07-20)

Collections were originally created as **static** Plex collections
(`smart=0`, an explicit `uri` list of `server://.../library/metadata/<ratingKey>`
item URIs). This required re-running the sync manually every time a new video
was added for an artist — new videos never appeared in the collection on
their own.

Rewritten to create **smart** collections instead:

- `smart=1`
- `uri` = a saved search filter, not an item list:
  `server://<machineIdentifier>/com.plexapp.plugins.library/library/sections/<key>/all?type=1&title=<urlencoded "ArtistName -">`
- `machineIdentifier` comes from `GET {server_url}/` (root endpoint) →
  `MediaContainer.machineIdentifier` — see `plex_get_machine_identifier()`.

Plex then auto-includes any future video whose title matches the filter —
no code needs to run again for that artist.

**Filter syntax notes** (confirmed by testing directly against a live PMS,
since Plex's filter documentation is thin and easy to get subtly wrong):
- `title=<value>` as a plain query param on `/library/sections/<key>/all`
  performs a **contains** match. This works.
- `title.begins=<value>` is **not** a real filter for this endpoint — Plex
  silently ignores the unrecognized param and returns the entire unfiltered
  library. It returned 29/29 items (the whole library) in testing, not 5.
- The filter used is `title` contains `"<ArtistName> -"` (**with** the
  trailing " -"), not just the bare artist name — this avoids false-positive
  matches on unrelated videos that merely mention the artist's name in
  passing, and matches the same "ArtistName - Song" convention the rest of
  this codebase already assumes (see `plex_find_videos_by_artist()`).
- The exact working request format was cross-checked against a real
  pre-existing smart collection ("Foo Fighters", created outside this
  codebase before this fix) — its `content` field
  (`server://.../library/sections/14/all?type=1&title=Foo%20Fighters`)
  confirmed the URI shape was right before rolling it out everywhere.

Function: `plex_ensure_smart_collection()` in `artwork_sync.py` — replaces
the old `plex_create_or_update_collection()`. **If you see the old name
referenced anywhere (a stale import, an old branch), that's dead code from
before this rewrite — don't resurrect the static-collection behavior.**

### Design change: automatic on new-artist-folder detection, not just manual clicks

Even with `config.json`'s `artwork_sync.plex_collection_sync_on_artwork` set
to `true`, collection creation + poster/art upload only ran when a user
manually clicked "Sync Collections" / "Create Collection" (or the bulk sync
button) in the dashboard — the background `ArtworkWatcher` thread, which is
what actually auto-detects brand-new artist folders every `watch_interval`
seconds, never checked this flag or called `plex_sync_artist_collection()` at
all.

Fixed by:
1. Having `ArtworkWatcher._run()` call `plex_sync_artist_collection()` after
   `sync_artist_artwork()` succeeds for a newly-detected folder, gated on the
   same `plex_collection_sync_on_artwork` flag app.py's manual endpoints use.
2. Changing `ArtworkWatcher.__init__` to take a `load_config` **callable**
   (e.g. `app.py`'s `load_config` function itself) instead of a static
   `artwork_sync` config dict snapshot taken once at startup. The watcher now
   re-reads `config.json` fresh every poll cycle, so toggling
   `plex_collection_sync_on_artwork`, updating the Plex token, or changing
   `server_url` from the dashboard takes effect on the *next* poll — no app
   restart needed. (Before this change, any config edit made after the
   watcher thread started was invisible to it until the container restarted.)

`config.json`'s `artwork_sync.plex_collection_sync_on_artwork` is currently
`true`. **If artist art/collections stop appearing automatically for new
folders, check this flag first** before assuming the watcher or Plex API
integration itself is broken.

### `config.json` Plex-related fields, quick reference

```jsonc
"artwork_sync": {
    "root_path": "/app/music_videos_final",   // watched by ArtworkWatcher
    "watch_interval": 120,                     // poll interval, seconds
    "plex_refresh_on_sync": false,              // trigger a library scan after artwork sync
    "plex_collection_sync_on_artwork": true,    // MUST be true for automatic collection+art on new folders
    "fanarttv_api_key": ""                      // optional — unlocks the Fanart.tv artwork source
},
"plex": {
    "server_url": "http://192.168.1.101:32400", // the actual PMS, NOT plex.tv
    "token": "...",                              // X-Plex-Token, obtained via OAuth
    "music_video_library_key": "14"              // VERIFY by hand — see Bug C above
}
```

### Key Debugging Commands (Plex)

```bash
# Confirm what's actually saved (this is bind-mounted, so it reflects
# whatever the app has written — no rebuild needed to see current state)
docker exec vidshelf cat /app/config.json

# List every library with its real title/key/type — the fastest way to
# sanity-check music_video_library_key against Bug C
docker exec vidshelf python3 -c "
import json, requests
c = json.load(open('/app/config.json'))
r = requests.get(c['plex']['server_url'] + '/library/sections',
                  headers={'X-Plex-Token': c['plex']['token'], 'Accept': 'application/json'})
for d in r.json()['MediaContainer']['Directory']:
    print(d['key'], d['title'], d['type'])
"

# List collections in the configured library and confirm smart=1 on each
docker exec vidshelf python3 -c "
import json, requests
c = json.load(open('/app/config.json'))
key = c['plex']['music_video_library_key']
r = requests.get(f\"{c['plex']['server_url']}/library/sections/{key}/collections\",
                  headers={'X-Plex-Token': c['plex']['token'], 'Accept': 'application/json'})
for m in r.json()['MediaContainer'].get('Metadata', []):
    print(m['ratingKey'], m['title'], 'smart=', m.get('smart'), 'childCount=', m.get('childCount'))
"

# Manually trigger a full collection+art sync for one artist, bypassing the UI entirely
docker exec vidshelf python3 -c "
import json, sys; sys.path.insert(0, '/app')
import artwork_sync as a
c = json.load(open('/app/config.json'))
print(a.plex_sync_artist_collection(c, 'ArtistName', '/app/music_videos_final/ArtistName_Folder'))
"
```

### Verifying a fix to this subsystem specifically

- `config.json` is bind-mounted directly (see `docker-compose.yml`) — edits
  take effect immediately, no rebuild needed.
- `artwork_sync.py`, `app.py`, and everything under `templates/` are baked
  into the image via `COPY . .` in the `Dockerfile` — **any** edit to these
  requires `docker-compose up -d --build` (a plain container restart alone
  will not pick up the change; you will be testing stale code and get
  confusing "the fix didn't work" results).
- After any OAuth-flow change specifically, test by actually completing the
  login in a browser — don't just review the code. This flow produced the
  exact same generic Plex error message ("We were unable to complete this
  request") for two completely different root causes in a row; only a live
  login attempt distinguishes which one you're looking at.

### RECURRING FAILURE MODE (hit again 2026-07-20, hours after the fixes above): a "broken collection setup" report that was actually a config wipe + a misdiagnosis-driven auth regression

**Symptom reported**: "my other agent broke what we just fixed with collection
setup." `GET /api/plex/collections/status` was returning 400.

**What was actually wrong** (confirmed via `docker logs` and direct
inspection — not by guessing):

1. `config.json`'s `plex.server_url` and `plex.music_video_library_key` had
   gone back to `""`, while `plex.token` was still populated (with a valid,
   working token for the same account). **The underlying Plex data was fine
   the whole time** — all previously-created smart collections were still
   present and still `smart=1` when queried directly against the PMS. The app
   just couldn't reach it because two of the three required `plex{}` fields
   were blank, so every Plex-touching endpoint returned "Plex not
   configured"/400. Likely cause: someone re-ran the OAuth "Connect to Plex"
   flow (there were two `POST /api/plex/oauth/start` calls back to back in
   the logs) but never completed Step 2 (select server) or Step 3 (discover
   library) afterward — `/api/plex/oauth/check` only ever writes `token`, it
   never touches `server_url` or `music_video_library_key` (see Bug B above),
   so a half-finished reconnect leaves exactly this shape of partial config.
2. Separately, **8 endpoints had their `if 'username' not in session: return
   401` auth check deleted** and replaced with the comment `# Authentication
   disabled for testing collection sync`: `/api/channels`,
   `/api/channel/videos`, `/api/download`, `/api/downloads/progress`,
   `/api/channels/add`, `/api/channels/remove`,
   `/api/plex/collections/sync`, `/api/plex/collections/status`. This is a
   real security regression (unauthenticated channel/download control on
   whatever this container's port is reachable from) and it doesn't even fix
   anything — the 400s were caused by #1 (blank config), not a session/auth
   problem. This is exactly the kind of thing that happens when an agent
   sees an error, guesses "must be an auth issue" instead of reading the
   actual `error` field in the JSON response, and starts stripping code to
   "test" rather than checking `config.json` and the logs first.

**Fix**: restored `server_url`/`music_video_library_key` in `config.json`
(after re-verifying the existing token still worked against both
`plex.tv/api/v2/user` and the PMS server directly — don't assume a token is
still the problem just because other fields got cleared), and restored the
auth guard on all 8 endpoints (`grep -n "Authentication disabled" app.py`
finds all of them if this happens again — every match needs the same
two-line `if 'username' not in session: return jsonify({'error':
'Unauthorized'}), 401` restored above it).

**If "Plex stuff is broken again" gets reported a third time**: check these
in order, don't start editing code yet —
1. `docker exec vidshelf cat /app/config.json` — is `plex.server_url` /
   `music_video_library_key` actually populated? (This has now caused the
   *same* reported symptom twice.)
2. `grep -n "Authentication disabled\|# TODO.*auth\|# FIXME.*auth" app.py` —
   did an auth check get stripped again?
3. Only after both of those check out clean, look at whether the OAuth URL
   format or smart-collection filter syntax actually regressed (Bugs A/B/C/D
   and the smart-collection rewrite, above).

## ADDED (2026-07-20): automatic Plex video-title cleanup

Smart collections worked, but the video titles shown in Plex's grid view were
hard to read — truncated mid-word — because the raw title Plex displays is
effectively the untouched YouTube title plus our own appended video ID, e.g.
`Fastball - Fire Escape (Official video) www.fastballtheband.com-7DwuYqXre1w`.
This library uses Plex's generic scanner (not a metadata agent), so `title`
comes straight from the filename, and `downloader.py`'s `outtmpl`
(`'%(title)s-%(id)s.%(ext)s'`, `downloader.py:94`) bakes in the raw YouTube
title *and* the 11-char video ID (needed for unique filenames — can't just
drop it).

**Fix**: clean the Plex-displayed `title` via Plex's metadata edit API,
*without* touching the on-disk filename and *without* breaking the
smart-collection filter (which matches on `title contains "<Artist> -"` —
see Bugs A–D above). New functions in `artwork_sync.py`:

- `_clean_video_title(raw_title)` — pure string function. Strips the
  trailing YouTube ID (`-[A-Za-z0-9_-]{11}$` — YouTube IDs are always exactly
  11 chars from that set), a trailing embedded artist-website URL
  (`\s*www\.\S+\s*$`), and generic `"Official [HD/Music] Video"` boilerplate
  (removes the *phrase*, not the whole parentheses, so mixed content like
  `"(US Version - Official HD Video)"` correctly becomes `"(US Version)"`
  instead of losing the meaningful part). Returns the input unchanged if
  cleaning would produce an empty or identical result.
- `plex_set_item_title(config, library_key, rating_key, new_title)` — the
  actual Plex mutation. Verified against `python-plexapi`'s
  `mixins/edit.py` (`EditFieldMixin.editField`) and `library.py`'s
  `LibrarySection._edit` (same source-of-truth approach used for the OAuth
  URL and smart-collection filter above) rather than guessed:
  ```
  PUT {server_url}/library/sections/{library_key}/all
      ?id={ratingKey}&type=1&title.value={new title}&title.locked=1
  ```
  `title.locked=1` is required — without it, Plex's scanner reverts the title
  on the next library rescan.
- `plex_clean_video_titles(config, library_key=None)` — scans the whole
  music-video library and cleans any item whose title isn't already locked
  (checked via the item's `Field` list, e.g.
  `[{"locked": true, "name": "title"}]` — same shape already seen on
  collections' `thumb` field). This makes it idempotent: a second run cleans
  0 items, and it never clobbers a title someone edited manually in Plex.
  Returns `{'scanned': N, 'cleaned': N, 'examples': [{'before', 'after'}, ...], 'errors': [...]}`.

Exposed via `POST /api/plex/titles/clean` (`app.py`) and a
**"🧹 Clean Up Titles"** button next to Sync Collections/Collection Status in
`templates/dashboard.html` (`cleanUpPlexTitles()`).

**Verified** (2026-07-20): ran against the real library — 36/36 titles
cleaned correctly on the first pass (including the `"(US Version)"` edge
case), second run cleaned 0/36 (idempotent), and all 4 existing smart
collections kept their exact same `childCount` afterward (proof the
`"<Artist> - "` prefix survived cleaning and the filter still matches).

### Wired into the automatic ArtworkWatcher flow (same day)

Initially shipped as a manual-only button (safer first pass, since
`title.locked` edits aren't casually reversible). After confirming it worked,
wired into `ArtworkWatcher._run()` (`artwork_sync.py`) so it now runs
automatically too — same trigger as artwork/collection sync
(`plex_collection_sync_on_artwork` in `config.json`). Implementation detail:
`plex_clean_video_titles()` is called **once per poll cycle** (not once per
new folder) — it scans the whole library regardless of which artist
triggered it, and is cheap to re-run since locked titles are skipped, so
batching it after the folder loop avoids redundant full-library scans when
several new folders appear in the same poll.

**Verified live, twice, in production** (not just synthetically): a test
folder (`AAA_Watcher_Test`, cleaned up afterward — collection deleted via
`DELETE /library/collections/<key>`, folder removed) confirmed the wiring
fires end-to-end; then, in the same session, the user downloaded real videos
for two new artists (Bjork, Barenaked Ladies) through the actual UI, and the
automatic chain (artwork → smart collection → poster/art → title cleanup)
fired correctly for both with no manual steps — visible in
`/app/music_videos_final/artwork-sync.log` as e.g.:
```
Detected 1 new artist folder(s): {'Bjork'}
Syncing artwork for new folder: Bjork
...
Smart collection 'Bjork' created (key=603268)
...
Title cleanup: scanned 37, cleaned 1
```
**Note**: `docker logs` (stdout) did not reliably show these lines in this
instance even though `PYTHONUNBUFFERED=1` is set (see gotcha #3 in
`CLAUDE.md`) — the log FILE
(`docker exec vidshelf cat /app/music_videos_final/artwork-sync.log`) is
the authoritative source for `ArtworkWatcher` activity if `docker logs`
looks quiet; don't conclude the watcher isn't running just because `docker
logs` shows no "Detected new folder" lines.

## FIXED (2026-07-20): manual artwork-swap "Search Images" returned unrelated stock photos

A separate manual-override feature was added (in-progress, not yet
committed): [artwork_swap.py](artwork_swap.py) +
[templates/swap_art.html](templates/swap_art.html), reachable at `/swap-art`,
lets a user pick an artist and paste/select a replacement collection image.
Its `GET /api/artwork/search_noauth` endpoint (`app.py`) was supposed to help
find a replacement image by artist name, but it never called any real image
source — it fabricated `https://source.unsplash.com/featured/400x400?<artist>&sig=<i>`
URLs. Unsplash's `/featured` endpoint returns essentially random stock
photography regardless of the query string, so "Search Images" always showed
5 generic unrelated photos, never anything related to the artist.

**Fix**: added `search_artist_images(artist_name, api_key='')` to
`artwork_sync.py`, which pools real results from the same four sources
`sync_artist_artwork()` already uses for the automatic watcher —
`search_theaudiodb()`, `search_fanarttv()` (only if a `fanarttv_api_key` is
configured), `search_musicbrainz()`, `search_wikipedia()` — collecting every
`folder`/`poster`/`fanart`/`background`/`logo`/`banner` URL each source
returns into one deduped list. `api_artwork_search_noauth()` in `app.py` now
calls this with the artist name and `config['artwork_sync']['fanarttv_api_key']`
instead of generating placeholder URLs.

**If this looks broken again**: confirm `search_artist_images` is still being
called (not a reintroduced Unsplash/placeholder shortcut) and that the images
returned actually resolve to artist-specific pictures, not generic stock
photos — the fake version was visually indistinguishable from a legitimate
"no artwork found for obscure artist" case at a glance, so verify by testing
with a well-known artist (e.g. "Foo Fighters") who should reliably get a hit
from at least TheAudioDB or Wikipedia.

**Also fixed in the same pass**: `templates/swap_art.html` had its entire
`<script>` block accidentally duplicated (a second copy of `loadArtists()`
and the `swap-form` submit handler, pasted again right before `</body>`).
Since both copies called `addEventListener('submit', ...)` on the same form,
every "Swap Artwork" click fired `POST /api/artwork/swap_noauth` **twice**.
Removed the duplicate block. If the swap ever appears to run twice per click
again (e.g. artwork visibly flickers/re-downloads, or Plex logs two upload
calls), check for this duplication before assuming the backend is looping.

**Note on `_noauth` endpoints**: `/api/artwork/search_noauth` and
`/api/artwork/swap_noauth` are intentionally unauthenticated (no
`session`/`username` check) — this is a separate, deliberately public
mini-page, not an instance of the auth-stripping regression described in the
"RECURRING FAILURE MODE" section above (which was about *removing* auth from
previously-protected endpoints like `/api/download`). Don't conflate the two;
if these become a concern, that's a design decision for the user, not a
regression to silently "fix" by adding a login check.

## FIXED (2026-07-20, same session): search only returned ~2 results; swap button always failed

Follow-up reports on the same manual artwork-swap feature, both confirmed
against the real running container/NAS/Plex server (not just read from code):

### Search returned only ~1-2 images instead of enough to page through

`search_artist_images()` (added in the fix above) only took the **first**
image of each type from each source. TheAudioDB genuinely only has one match
per artist (so that part was fine, ~4 images), but Fanart.tv's
`artistthumb`/`artistbackground`/`musiclogo`/`musicbanner` fields and Cover
Art Archive's `images` field are **arrays** — the original code took index
`[0]` of each and discarded the rest, and Wikipedia only ever has one
infobox photo anyway. Net result: 1-2 images typically survived, not enough
to be useful.

**Fix**: added `_lookup_mbid()`, `_fanarttv_images()`, `_coverartarchive_images()`,
and `_wikimedia_commons_images()` to `artwork_sync.py` — separate from the
existing single-best-image `search_theaudiodb()`/`search_fanarttv()`/
`search_musicbrainz()`/`search_wikipedia()` functions (which stay untouched;
they're still used by the verified automatic-sync path in
`sync_artist_artwork()`, don't refactor them to share code with the functions
below without re-verifying that path). The new functions pull **every**
array entry, not just `[0]`, plus a proper Wikimedia Commons photo search.
`search_artist_images()` now pools all of it. Tested live against the real
container: "Foo Fighters" went from ~2 results to **11**.

The initial version of `_wikimedia_commons_images()` used a bare
`srsearch=<artist> artist` full-text query against Commons — this matched
the literal word "artist" anywhere in any file's description, returning
completely unrelated files (a "Kaki King" video screenshot, a PDF, a DjVu
document — for a *Foo Fighters* search). **Fixed** to
`srsearch=intitle:"<artist>"` (exact-phrase match against the file title
only) plus a regex filter requiring a real image extension
(`.jpg/.jpeg/.png/.gif/.webp`) before returning a URL. Verified: same search
now returns real "2021 Shaky Knees - Foo Fighters (N).jpg" concert photos,
all actual images.

**Added pagination** so "Load More" doesn't re-hit 4 external APIs on every
click: `app.py`'s `api_artwork_search_noauth()` now fetches the artist's
full image list once, caches it in `_ARTWORK_SEARCH_CACHE` (in-process dict,
keyed by lowercased artist name, 10-minute TTL), and slices
`ARTWORK_SEARCH_PAGE_SIZE` (5) images per `page` query param. Response now
includes `page`/`page_size`/`total`/`has_more`. `templates/swap_art.html`'s
`searchImages()` was rewritten (`fetchImagePage()` + a dynamically added
"Load 5 More" button inside `#image-results`) to call this with an
incrementing `page` and append rather than replace results.

**If search result count regresses to ~1-2 again**: check whether someone
changed a `[0]` back into the collection functions, or whether the
`_ARTWORK_SEARCH_CACHE` TTL/pagination got bypassed. If results come back
irrelevant again (wrong artist entirely, non-image files), check
`_wikimedia_commons_images()` — that's almost certainly the Commons query
losing the `intitle:` exact-phrase wrapper or the image-extension filter.

### The "Swap Artwork" button always failed

Root cause in `artwork_swap.py`'s `plex_swap_collection_artwork()`, two
separate bugs stacked (same "each fix reveals the next" pattern documented
elsewhere in this file):

**Bug 1 — `os.replace()` called twice on the same source file.** The
original code looped `for fname in ('folder.jpg', 'poster.jpg'): os.replace(temp_path, dest)`.
`os.replace()` is a *move*, not a copy — it consumes `temp_path`. The first
iteration moved the downloaded image to `folder.jpg`; the second iteration
then tried to move the now-nonexistent `temp_path` to `poster.jpg` and threw
`FileNotFoundError`, caught by the broad `except Exception`, returning
`{'success': False, 'error': 'Failed to replace poster.jpg: ...'}` — this
fired on **every single swap attempt**, which is why the button always
failed. Fixed by moving once to `folder.jpg`, then doing a manual buffered
copy (`open()`+`shutil.copyfileobj()` — never `shutil.copy2`/`copyfile`,
see gotcha #2 in `CLAUDE.md`, these files live on the CIFS-mounted NAS
share) from `folder.jpg` to `poster.jpg`.

**Bug 2 — ignored the saved, hand-verified `music_video_library_key`.**
`plex_swap_collection_artwork()` called `plex_find_library_key(config)`
unconditionally, re-running title-substring auto-discovery every time
instead of using `config['plex']['music_video_library_key']` first. This is
exactly Bug C earlier in this file (this account's library is titled
"Muisc Videos", a transposed typo that breaks the `"music video"` substring
match) — every other Plex-touching endpoint in `app.py` already knows to
prefer the saved key and only falls back to discovery if it's blank; this
one function didn't follow that pattern. Even with Bug 1 fixed, this could
silently point the collection lookup at the wrong library and fail with
"Plex collection not found for artist X". Fixed to match the pattern used
everywhere else: `library_key = config.get('plex', {}).get('music_video_library_key', '')`,
falling back to `plex_find_library_key()` only if empty.

**Verified end-to-end against the real container/NAS/Plex** (not just
code-reviewed): swapped Foo Fighters' artwork via
`POST /api/artwork/swap_noauth`, confirmed `folder.jpg`/`poster.jpg` on the
NAS both updated to the new 4.2MB file with a fresh mtime, and queried the
live PMS collections endpoint directly — the "Foo Fighters" collection's
`thumb` field changed to a new version hash
(`/library/metadata/602963/thumb/1784590556`), which is Plex's own signal
that a new poster was actually accepted, not just that the API call
returned 200.

**If the swap button fails again**: check for exactly these two things first
— (1) `grep -n "os.replace" artwork_swap.py` — is `temp_path` still being
passed to more than one `os.replace()` call, and (2) is `library_key` in
`plex_swap_collection_artwork()` still preferring
`config['plex']['music_video_library_key']` before falling back to
`plex_find_library_key()`. Both bugs produced generic, unhelpful error
strings (`Failed to replace poster.jpg: ...` / `Plex collection not found for
artist ...`) that don't point at the real cause — don't trust the error
message's literal wording over checking these two spots directly.

## CHANGED (2026-07-20, same session): folded the Swap Artwork page into the dashboard SPA

The swap-artwork UI was a standalone page (`templates/swap_art.html`, served
at `GET /swap-art`) embedded into the dashboard via
`<iframe src="/swap-art">`. That meant two separate HTML documents with two
copies of the dark theme, two separate JS files, and no shared components —
the duplicated-`<script>`-block bug fixed earlier in this file only existed
because of that split in the first place, and any future dashboard-wide
style/behavior change (toasts, spinners, button states) would never reach
the iframed page automatically.

**Fix**: deleted `templates/swap_art.html` and the `/swap-art` route
(`swap_art_page()` in `app.py`) entirely. The swap-artwork UI is now real
markup inside `page-swap-art` in `templates/dashboard.html`, using the
dashboard's existing `.form-group`/`.btn`/`.section` classes, `showToast()`
for feedback, and the same `.loading`/`.spinner` pattern every other page
uses — instead of its own bespoke inline-styled markup. Its JS
(`loadSwapArtArtists()`, `searchArtworkImages()`/`fetchSwapArtImagePage()`,
`swapArtwork()`) lives in a `<script>` block at the bottom of
`dashboard.html`, alongside the existing Create-Collection-modal script, and
is wired into the shared page-navigation dispatcher (`if (page ===
'swap-art') loadSwapArtArtists();`) the same way `channels`/`settings`/
`downloads` already are.

Two small real improvements came along with the move, not just a copy/paste:

- **Current-artwork preview.** Added `GET /api/artwork/current_image?artist=`
  (session-gated, unlike the `_noauth` search/swap endpoints — see the note
  above on why those two stay public by design) which `send_file()`s the
  artist's existing `folder.jpg` so the picker shows what's about to be
  replaced, next to the search results. Selecting an artist or completing a
  swap refreshes this preview (`img.dataset.bust = Date.now()` as a
  cache-busting query param, since browsers would otherwise keep showing the
  pre-swap image at the same URL).
- **Visible selection state + button feedback.** Clicking a search result
  now highlights it (`.selected` border, via a new `.artwork-result-grid`
  CSS rule) instead of only silently filling the URL box, and the "Swap
  Artwork" button disables itself and shows "⏳ Swapping..." while the
  request is in flight — the standalone page had neither, so a slow request
  looked like nothing had happened.
- Extended the shared `.form-group input` CSS rule to also cover `select`
  (`.form-group input, .form-group select { ... }`) — this was previously
  input-only, so the Create-Collection modal's artist `<select>` (`id=
  "artist-select"`, untouched) was rendering with unstyled browser defaults;
  it now picks up the same dark theme for free.

**Watch for an ID collision if this page is touched again**: the
Create-Collection modal already used `id="artist-select"`, so the new page's
dropdown was deliberately named `id="swap-art-artist-select"` (and every
other new element is `swap-art-`-prefixed) to avoid colliding with it. Don't
rename either back to a bare `artist-select`/`image-results`/etc.

**Verified end-to-end against the real running container** (not just
code-reviewed): logged in via `curl`, confirmed
`GET /api/artwork/current_image` returns a real 700x700 JPEG when
authenticated and `401` without a session cookie, confirmed
`GET /dashboard` now contains the inline `swap-art-*` markup with no
`<iframe src="/swap-art">` left, and confirmed the old `/swap-art` route now
404s. `docker-compose up -d --build` is required after this change (baked
into the image via `COPY . .`), same as any other `app.py`/`templates/`
edit — see "Verifying a fix to this subsystem specifically" above.

## ADDED (2026-07-20, same session): dashboard-wide UI enhancement pass

A full UI audit turned up 8 issues, fixed in one pass, all in
`templates/dashboard.html` (no backend changes):

1. **Responsive layout.** The `.sidebar` was `position: fixed; width: 240px`
   unconditionally, `.main-content` had `margin-left: 240px` unconditionally
   — zero `@media` queries existed anywhere, so anything under ~768px wide
   crushed the page. Added a `#mobile-nav-toggle` hamburger button (only
   visible below 768px), a `#sidebar-backdrop` overlay, and
   `@media (max-width: 768px)` rules that slide the sidebar off-canvas
   (`transform: translateX(-100%)`, toggled via `.sidebar.open`).
   `toggleSidebar()`/`closeSidebar()` in the JS; the nav-link click handler
   now also calls `closeSidebar()` so navigating on mobile auto-closes it.
2. **Modal dismissal.** All 4 modals (`add-channel-modal`,
   `folder-browser-modal`, `confirm-modal`, `collection-modal`) previously
   only closed via their own Cancel/✕ button. Added one generic mechanism
   instead of four one-offs: `dismissModal(overlay)` +
   a `click` listener on every `.modal-overlay` (closes only when the click
   target *is* the overlay, i.e. the dark backdrop, not the card) + one
   document-level `keydown` listener for Escape. `dismissModal()` special-
   cases `folder-browser-modal` to also reset `_folderBrowserTarget = null`,
   matching what `closeFolderBrowser()` already did.
3. **Double-click guards.** `downloadVideo()`, `downloadMusicVideo()`, and
   `addChannel()` fired `fetch` with no `btn.disabled` guard — a fast
   double-click sent two identical requests. Fixed using the same
   `btn.disabled = true` / `finally { btn.disabled = false }` shape already
   used elsewhere in this file (`searchMusicVideos()`, the Plex OAuth
   buttons). Combined naturally with #4 below since both needed the button
   element itself passed into the handler.
4. **Unescaped strings in dynamically-built `innerHTML`/`onclick`.** The
   channel-videos and music-video-results card templates interpolated
   arbitrary YouTube titles/channel names directly into template-literal
   HTML and into `onclick="downloadVideo('${v.id}', '${channelUrl}')"` —
   style attribute strings, with only an ad hoc `.replace(/'/g, "\\'")` for
   single quotes on the music-video title (nothing for `"`, `<`, `&`, and
   nothing at all on the channel-video path). A title containing a `"`
   broke the `onclick` attribute and visibly corrupted the card. Fixed by:
   - Adding a shared `escapeHtml(str)` helper (creates a detached `<div>`,
     sets `textContent`, reads back `innerHTML` — the standard safe-escape
     trick) and using it everywhere a title/channel name is rendered as
     HTML (`.video-title`, `alt="..."`).
   - Switching `downloadVideo`/`downloadMusicVideo`'s buttons from
     interpolating values into the `onclick` string to `data-video-id`/
     `data-title`/`data-channel-url` attributes plus `onclick="downloadVideo(this)"`
     — the function now reads `btn.dataset.*` instead of taking string
     params. This sidesteps quote-escaping in the JS call entirely (browsers
     auto-decode HTML entities in `dataset` reads) and gives the double-click
     guard in #3 direct access to the button.
   - Left `confirmRemoveChannel`/`loadChannelVideos`/`downloadAllChannelVideos`/
     `selectPlexServer`'s interpolated `onclick`s alone — their values come
     from admin-entered channel URLs / Plex API data, not arbitrary YouTube
     titles, materially lower risk, not the bug that was actually observed.
5. **Sidebar nav grouping.** Added a `.sidebar-section-label` ("Tools")
   above the Create-Collection/Swap-Artwork links so one-off admin tools
   are visually separated from the core Channels/Downloads/Music-Videos
   workflow links.
6. **Toast notifications.** `showToast()` previously auto-dismissed after a
   fixed 3s with no way to re-read or dismiss early, and `.toast` elements
   were each individually `position: fixed; bottom:24px; right:24px` with
   no awareness of each other — **two toasts shown close together would
   render exactly on top of each other**, a latent bug that became more
   likely to actually surface once hover-to-pause was added (a paused toast
   stays on screen longer, increasing the odds a second one appears while
   it's still up). Fixed by moving the fixed positioning to `#toast-container`
   (now `display:flex; flex-direction:column; gap:10px`) and making
   individual `.toast` elements `position: relative` children of it. Also
   added: a per-toast `✕` close button, pause-on-hover/resume-on-leave for
   the dismiss timer, `white-space:normal; word-break:break-word` so long
   error messages wrap instead of clipping at `max-width:400px`, and
   `aria-live="polite"` on `#toast-container` so screen readers announce
   new toasts.
7. **Settings page inline-style cleanup.** The Settings page had the same
   `style="color:#707080;font-size:0.9em;margin-bottom:12px;"` block
   repeated verbatim 4 times instead of a shared class. Added `.text-muted`/
   `.mb-12` utility classes and replaced those 4 occurrences. Scoped to this
   one repeated pattern, not a full rewrite of every inline style in the
   file.
8. **Color contrast.** `#707080` (used everywhere as the muted/secondary
   text color — labels, meta text, help text) is only ~3.9:1 against the
   `#0f0f1a`/`#1a1a2e` backgrounds, under WCAG AA's 4.5:1 for normal text.
   Replaced every occurrence (`grep -c` confirmed 32, all either a CSS
   `color:` rule or an equivalent inline-style/JS color token serving the
   same muted-text role — none were background/border colors) with
   `#8888a0` (~4.6–4.9:1, passes AA), visually near-identical.

**Verification performed**: `node --check` on the concatenated inline
`<script>` blocks (syntax-valid), `docker-compose up -d --build`, then
logged in via `curl` and confirmed the served `/dashboard` HTML contains
every new element/function (`#mobile-nav-toggle`, `#sidebar-backdrop`,
`.sidebar-section-label`, `#add-channel-submit-btn`, `aria-live="polite"`,
`dismissModal`, `escapeHtml`, `toggleSidebar`, the `onclick="downloadVideo(this)"`/
`downloadMusicVideo(this)` call sites).

**Not verified — no headless browser tooling available in this session**
(`chromium-cli` not installed, no working Playwright): the actual visual/
interactive behavior — sidebar slide animation, hover-pause timing on
toasts, Escape/click-outside actually dismissing a modal in a live DOM,
double-click actually being suppressed. The code was written and reviewed
carefully and the underlying mechanisms (class toggles, event listeners,
`btn.disabled`) are standard and match patterns already working elsewhere
in this file, but this hasn't been exercised in an actual browser. **If any
of items 1/2/3/6 above seem not to work, that's the first thing to
actually click through in a browser** before assuming the logic is wrong —
this specific gap (backend/API changes get curl-verified in this project;
this pass could only get static/structural verification, not interaction
verification) is new for this file.

## REMOVED (2026-07-20, same session): dead files, dead code, and a Docker secrets-leak gap

A full audit of the backend and project files (not just `dashboard.html`)
found several genuinely unused files and functions, plus one real hygiene
gap. All confirmed dead via cross-file grep/`ast.parse` before removal, not
assumed. If anything below seems to be needed again, `git log`/`git show`
on the commit that removed it has the original content.

**Deleted files** (git-tracked, confirmed unreferenced by anything):
- `index.html` (repo root) — an orphaned early prototype page. `app.py`'s
  `/` route (`index()`) only ever redirects to `/login` or `/dashboard`;
  nothing ever called `render_template('index.html')` or served it. It was
  already excluded from the Docker image via `.dockerignore` but never
  actually deleted from the repo until now.
- `youtube_downloader.py` — a standalone CLI prototype of channel-download
  logic ("For simplicity, we'll just print the video IDs...") from before
  `app.py`'s web-based channel/download system existed. Not imported by
  `app.py`, the Dockerfile, or docker-compose.
- `test_plex.py` — **did not even parse**: `ast.parse()` failed with
  `SyntaxError: unterminated string literal` — the file was truncated
  mid-string. Not referenced anywhere, and couldn't have run if it was.
- `sync_music_videos.ps1` — a manual Windows-side script that copied local
  downloads to the NAS share and deleted the source afterward. Superseded
  by the app's own automatic download→NAS copy (see the CIFS mount RESOLVED
  section above, verified 2026-07-09) and risked racing with it or deleting
  files it shouldn't if ever run again.

**Removed dead code**:
- `app.py`'s import from `artwork_sync` dropped 4 names that were imported
  but never referenced anywhere else in `app.py`: `plex_find_videos_by_artist`,
  `plex_ensure_smart_collection`, `plex_upload_collection_poster`,
  `plex_upload_collection_art`. (These are still genuinely used — just by
  `artwork_sync.py` internally and by `artwork_swap.py` — app.py just never
  needed its own copy of the name.)
- `app.py`'s `get_music_video_plex_path()` — fully implemented, never
  called; `api_music_videos_download()` has always hardcoded
  `final_path = '/app/music_videos_final'` directly instead of calling it.
- `downloader.py`'s `clear_completed_downloads()` — fully implemented
  (prune completed/error entries older than 1 hour), never called from
  anywhere. The dashboard's "Clear Download History" button
  (`api_downloads_clear()`) does an unrelated full manual wipe of both
  tracker files instead. **Note**: this means `active_downloads.json` has
  no automatic pruning at all — it only ever shrinks via that manual button.
  Not currently a problem (it was empty at audit time) but if it grows
  unbounded later, this is why, and reviving `clear_completed_downloads()`
  (calling it periodically, e.g. from the existing `ArtworkWatcher` poll
  loop or a new timer) is the fix, not writing new pruning logic from
  scratch.
- Stale `# TODO: replace with your Plex client identifier` / `# TODO:
  replace with your Plex product name` comments on `artwork_sync.py`'s
  `PLEX_CLIENT_ID`/`PLEX_PRODUCT` constants — these are the real, working,
  already-deployed values (confirmed by the entire OAuth Bug A–D saga
  above), not placeholders waiting to be filled in. If a future session
  sees these TODOs re-added, don't treat them as an actual pending task.
- `app.py`'s `except (FileNotFoundError, Exception): pass` simplified to
  `except Exception: pass` — `Exception` already covers `FileNotFoundError`.

**Docker image hygiene — `config.json` was baked into image layers**:
`config.json` (containing the real Plex token) was never in `.dockerignore`,
even though `.gitignore` already excluded it from git and
`docker-compose.yml` always bind-mounts the host's `./config.json` over
whatever the image contains at runtime. That bind mount means this was
**not a functional bug** — the running container never actually used the
baked-in copy — but it meant every image build embedded the real token in
an image layer regardless, which would leak if that image were ever
pushed/exported/shared. Added `config.json` to `.dockerignore`. Verified
after rebuild: `docker exec vidshelf sh -c "ls /app/config.json"` still
shows the file (via the bind mount, exactly as before) — this fix only
changes what the *build* captures, not what the running container sees.
Also removed `.dockerignore`'s now-pointless `index.html` entry since that
file no longer exists in the repo at all.

**Verified**: all four edited/touched Python files re-parsed cleanly
(`ast.parse`), `docker-compose up -d --build` succeeded, container started
with the same "Background watcher started" log line as before, and a
`curl` smoke test through a real logged-in session confirmed
`/api/artists`, `/api/music-video-path`, `/api/downloads/clear`, and
`/api/plex/collections/status` all still return 200 — i.e. removing the
unused imports/functions didn't accidentally touch anything still in use.

## FIXED (2026-07-20, same session): `escapeHtml()` didn't actually escape quotes — found by real browser testing

The prior UI pass's `escapeHtml()` (added to fix unescaped video titles
breaking card markup) used the classic DOM round-trip trick:
```js
const div = document.createElement('div');
div.textContent = str;
return div.innerHTML;
```
This escapes `&`/`<`/`>` but **not `"` or `'`** — a browser's HTML
serializer only quote-escapes inside attribute-value contexts, and this
trick always serializes as a text node, so quotes pass through untouched.
The bug: this file uses `escapeHtml()` in *both* contexts —
`<div class="video-title">${escapeHtml(title)}</div>` (text content, fine)
**and** `alt="${escapeHtml(title)}"` / `data-title="${escapeHtml(title)}"`
(quoted attribute values, NOT fine) — so a title containing a literal `"`
would still break out of the attribute, which was the exact bug this
function was supposed to fix in the first place.

**How this was caught**: setting up real Playwright browser testing this
session (see below) and asserting `escapeHtml('Foo "Bar" <script>...')`
actually contains `&quot;` — it didn't, until fixed. Static review and
`node --check` (syntax only) had both missed this; it takes actually
calling the function and checking its output to catch a semantically wrong
but syntactically valid helper.

**Fix**: replaced the DOM-trick with explicit manual replacement of all
five characters (`&`, `<`, `>`, `"`, `'`) via `.replace()`, safe in both
text-content and attribute-value contexts.

**If a future escaping helper is added to this file**: don't reuse the
`div.textContent`/`.innerHTML` trick assuming it's a complete HTML escape —
it isn't, for exactly this reason. Prefer the explicit five-character
`.replace()` chain, or DOM-build attributes via `setAttribute()`/`dataset`
instead of string interpolation (which never has this problem, since the
browser handles the encoding).

## Browser-based UI testing (added 2026-07-20)

Prior sessions verified UI changes only via `curl` + static structural
checks (confirming markup/functions are present in the served HTML) since
no headless-browser tooling was available. This session installed
**Playwright** (Chromium) to actually drive the app and click through it —
which is what caught the `escapeHtml()` bug above; the structural checks
would never have caught it since the buggy code was syntactically fine and
did get served correctly.

**Setup** (not committed to the repo — this is a Node/Playwright toolchain
being used to test a Python/Flask project, kept out of the repo so it
doesn't need to live alongside `requirements.txt`):
```bash
mkdir -p /path/to/scratchpad/pw && cd /path/to/scratchpad/pw
npm init -y && npm install playwright
npx playwright install chromium   # ~300MB download, one-time
```
A driver script (`test_ui.js` in that same scratch dir) logs in via the
real `/login` form, then drives the dashboard SPA: resizes to a mobile
viewport and checks the hamburger/backdrop, opens modals and checks
Escape/click-outside-to-close, triggers toasts and checks the close button
+ hover-pause behavior, and clicks through every sidebar page collecting
`console`/`pageerror` events. Run with `node test_ui.js` from that
directory (the dev container must already be up on `localhost:5000`).

**Gotchas hit writing the driver** (both test-script bugs, not app bugs —
noted here so the next session doesn't have to rediscover them):
- Dashboard pages other than the initially-active one are
  `style="display:none"` until their nav link is clicked — a locator for a
  button that lives on, e.g., the Channels page will time out
  ("element is not visible") if you haven't clicked
  `a[data-page="channels"]` first in the same test run.
- `page.goto()` to `/dashboard` again mid-test is a **full page reload**,
  which resets the SPA back to whatever page is active-by-default
  (Dashboard) — any page-specific state/navigation from earlier in the
  test is lost and must be re-established (re-click the relevant nav link)
  after any `goto()`.
- "Create Plex Collection" in the sidebar opens a **modal**, not a page
  (see the `id="create-collection-link"` special-case click handler in
  `dashboard.html`) — a naive loop that clicks every `data-page` link in
  sequence needs to explicitly close this modal (Escape) before continuing,
  or the modal overlay intercepts every subsequent click on the rest of the
  page. This is correct modal behavior (it's supposed to block the page
  while open), not a bug.

No persistent Playwright project files were added to this repo — the
scratch install above needs to be redone (or better, promoted into a real
project-local `package.json`/`.claude/skills/` entry via
`/run-skill-generator` if this becomes routine) in a future session that
wants to do this again.

## ADDED (2026-07-20, same session): Artists page

The user asked whether there should be a way to browse tracked artists and
their downloaded videos, the way the Channels page already does for
channels. There wasn't — `/api/artists` only ever fed a dropdown (Create
Collection, Swap Artwork), nothing showed video-level detail per artist.

**Backend** (`app.py`):
- `GET /api/artists/summary` — walks `artwork_sync.root_path`, returns per
  folder: `artist` (display name via `folder_to_artist()`), `folder`,
  `video_count` (files ending in `.mp4`/`.mkv`/`.webm` — the same extension
  tuple already used by `artwork_sync.py`'s watcher and `downloader.py`,
  reused for consistency rather than inventing a new list), and
  `has_artwork` (reused from `artwork_sync.has_artwork()`, now promoted to
  a top-level import in `app.py` instead of the old function-local
  `from artwork_sync import has_artwork, has_metadata` inside
  `api_artwork_status()` — that local import now only pulls in
  `has_metadata`, which nothing else needed).
- `GET /api/artists/videos?artist=` — lists the actual video files in one
  artist's folder: filename, a cleaned display `title`, `size_bytes`,
  `modified_at` (unix timestamp). The title reuses `artwork_sync.py`'s
  `_clean_video_title()` (also newly promoted to a top-level `app.py`
  import) against the filename with its extension stripped — the same
  function that cleans Plex's displayed titles, applied here to the raw
  filename instead of a Plex API title, since the transformation needed
  (strip trailing YouTube ID + boilerplate) is identical either way.

**Frontend** (`templates/dashboard.html`): a new "Artists" sidebar link
(between Music Videos and the "Tools" section — it's a content-browsing
page, not an admin tool) opens `page-artists`, a list of collapsible
`.artist-row` entries: thumbnail (reuses the `current_image` endpoint built
for the Swap Artwork picker), name, video count, and an expand chevron.
Expanding lazily fetches `/api/artists/videos` and caches the rendered HTML
per artist in `_artistVideosCache` (module-level object) so re-expanding
the same artist doesn't refetch.

**Also promoted while adding this**: `formatBytes(bytes)` was a
function-local helper duplicated inside the system-info loader; moved to a
shared top-level function next to `escapeHtml()`/`showToast()` since the
new Artists video list needed the identical byte-formatting logic. (The
*separate* KB/MB/GB block inside `loadDashboardStats()`'s disk-usage stat
card was deliberately left alone — it takes its input already in KB from a
different API field, not raw bytes, so it isn't actually the same logic
despite looking similar at a glance.)

**Verified with the Playwright harness** (see above): logged in, navigated
to Artists, confirmed real artist rows render (7, from the actual NAS
folder), expanding one shows real video titles/sizes/dates (not
placeholders), collapsing works, and no console errors. Screenshot
confirmed visually correct too — one cosmetic pre-existing quirk was
visible in the screenshot ("Barenaked Ladies - Pinch Me []" — a stray empty
`[]`): `_clean_video_title()` only strips *parenthetical* boilerplate, not
square brackets, so a `[]` left over in the original downloaded filename
survives cleaning. This is existing behavior in `_clean_video_title()`
(already shipped, used for Plex title cleanup) newly visible because this
page is the first place to surface raw filenames in the UI — not a
regression introduced by this page, and not fixed as part of this pass.

## ADDED (2026-07-20, same session): immediate collection sync on every music video download

The user asked to not have to manually click "Create Plex Collection"
anymore. It turned out this was *mostly* already automatic (see the
"Design change: automatic on new-artist-folder detection" section above)
but had a real gap:

- `ArtworkWatcher._run()` only ever compares the current folder list
  against `_known_folders` — it triggers artwork/collection sync **only**
  when a folder that didn't exist before shows up. It polls every
  `watch_interval` seconds (120s by default), so even for a genuinely new
  artist there was up to a 2-minute delay before the collection appeared.
- More importantly, `api_music_videos_download()` — the actual download
  route the "Music Videos" search+download UI calls — **never called
  `sync_artist_artwork()` or `plex_sync_artist_collection()` at all**. It
  only downloaded the file and called `mark_video_downloaded()`. For an
  artist whose folder already existed (so the watcher would never treat it
  as "new" again), there was no automatic path to a first collection at
  all if one hadn't been created yet — only the manual button worked.

**Fix**: `api_music_videos_download()`'s `_do_download()` thread now calls
`sync_artist_artwork(artist_final_path, artwork_cfg)` and
`plex_sync_artist_collection(config, artist, artist_final_path)` right
after a successful download, gated on the same
`artwork_cfg.get('plex_collection_sync_on_artwork', False)` flag every
other call site already checks. Both functions are safe to call on every
single download:
- `sync_artist_artwork()` already no-ops if artwork exists (`has_artwork()`
  check), unless `force=True` is passed (it isn't here).
- `plex_ensure_smart_collection()` (called via `plex_sync_artist_collection()`)
  looks up an existing collection by title first and returns its key
  immediately if found — it never creates a duplicate.

This means every download — new artist or existing — now gets its
artwork/collection ensured within seconds, not up to 2 minutes later, and
not dependent on the artist's folder having been "new" at some point.

**Note on Plex `childCount` lag**: right after a brand-new collection is
created, Plex's own smart-filter `childCount` can still read `0` for a
little while — the collection exists and is `smart=1` immediately, but the
filter only actually matches the new video once Plex's *own* library scan
indexes the freshly-copied file. `plex_sync_artist_collection()` already
calls `trigger_plex_refresh()` at the end to kick that off, but the scan
itself is asynchronous on Plex's side. This is pre-existing behavior
(same as the watcher-triggered path always had), not something this change
affects — don't mistake `childCount: 0` immediately after a download for
the sync having failed; check again after Plex's scan has had a moment to
run.

**Verified end-to-end for real** (not code-reviewed only): downloaded a
real "Weezer - Undone (The Sweater Song)" video for **Weezer**, an artist
with no prior folder or collection, through the actual
`POST /api/music-videos/download` API. Confirmed via `docker logs` that
"Plex collection sync completed for artist 'Weezer'" printed ~20 seconds
after the request (well under the watcher's 120s poll), and confirmed
directly against the live Plex server that a `smart=1` "Weezer" collection
now exists with a real poster thumb, and that `folder.jpg`/`fanart.jpg`
landed on the NAS. This is real, live data in the account's actual Plex
library now — not cleaned up afterward, since it's genuine content, not
scratch/test state.

## ADDED (2026-07-20, same session): per-video title-card posters + artist-name casing normalization

The user asked how to make collections/titles look nicer once already in
Plex. Two concrete issues, visible in a real screenshot of the "Death Cab
for Cutie" collection page:

1. Video posters were Plex's own auto-extracted mid-video freeze-frame
   thumbnail (dark, inconsistent, no text) — there was never a real poster
   image for individual videos, only for artist collections
   (`plex_upload_collection_poster()`, pre-existing).
2. The "ArtistName - Song" title prefix showed inconsistent capitalization
   across videos of the *same* artist ("Death Cab For Cutie" vs "Death Cab
   for Cutie") because that prefix comes straight from whatever casing the
   original YouTube uploader happened to use, per video — `_clean_video_title()`
   never normalized it.

### Root cause of why #2 was actually visible despite title cleanup already existing

`plex_clean_video_titles()` has existed since the earlier "automatic Plex
video-title cleanup" work (above), but `ArtworkWatcher._run()` only called
it when `new_folders` was non-empty — i.e. only when a *brand-new* artist
folder appeared. A new video added to an artist that **already** has a
folder (the common case — Death Cab already had 6 other videos) never
triggered it, so that video's title just never got cleaned or
casing-normalized at all until someone clicked "Clean Up Titles" manually.

**Fix**: moved the `plex_clean_video_titles(config)` call in
`ArtworkWatcher._run()` (`artwork_sync.py`) out of the `if new_folders:`
block so it runs on **every** poll cycle (still gated on
`plex_collection_sync_on_artwork`), not just when a new folder is detected.
Safe to do unconditionally because it was already idempotent (skips
titles with `title.locked`).

### Fix for #2 (casing): `_normalize_artist_prefix()`

New in `artwork_sync.py`:
- `_canonical_artist_names(config)` — returns every artist's name in its
  folder-derived casing (via the existing `folder_to_artist()`), sorted
  longest-first so e.g. a hypothetical "Foo Fighters Tribute" folder
  wouldn't get matched by a shorter unrelated prefix first.
- `_normalize_artist_prefix(title, canonical_names)` — if `title` starts
  (case-insensitively) with `"<name> -"` for some canonical name but not in
  that exact casing, rewrites just the prefix to the canonical casing,
  leaving the song title untouched.
- Wired into `plex_clean_video_titles()`: `cleaned =
  _normalize_artist_prefix(_clean_video_title(raw_title), canonical_names)`.
  Still uses the same `title.locked` skip / idempotency as before — this is
  an additional transform in the same pass, not a new pass.

### Fix for #1 (posters): per-video title-card generation

New in `artwork_sync.py`, gated on the `Pillow` package (added to
`requirements.txt`) being importable (`_PIL_AVAILABLE`), so a missing/failed
Pillow install degrades to "no title cards generated" rather than crashing
anything else:

- `generate_title_card(artist_name, song_title, artist_path, output_path)` —
  renders a 1000×1500 (2:3, matching Plex's poster grid) JPEG: the artist's
  own `fanart.jpg`/`background.jpg`/`folder.jpg`/`poster.jpg` (first one
  found), blurred + darkened as a backdrop (falls back to a plain dark
  gradient if the artist has no artwork yet), with the song title in a large
  bold font and the artist name in a smaller caps font along the bottom,
  behind a gradient scrim for legibility. Uses `fonts-dejavu-core` fonts at
  fixed filesystem paths (added to the `Dockerfile`'s apt-get list),
  falling back to Pillow's tiny bundled bitmap font if that package isn't
  present (looks worse, doesn't crash).
- `_plex_upload_poster(config, rating_key, image_path)` — generic
  `POST /library/metadata/<ratingKey>/posters`. This Plex endpoint accepts
  any metadata item's ratingKey, not just collections, so this single
  function now backs both the pre-existing `plex_upload_collection_poster()`
  (refactored to a thin wrapper around it) and the new per-video upload.
- `_generate_title_cards_for_videos(config, artist_name, artist_path, videos, force)` —
  shared core: for each `{rating_key, title}` not already recorded in that
  artist's `title-cards.json` (or all of them if `force=True`), strips the
  `"ArtistName -"` prefix to get the song title, generates the card to a
  temp file (`.title_card_<ratingKey>.jpg` in the artist folder, deleted
  after upload), uploads it, and records `{title, generated_at}` in
  `title-cards.json` on success.
  - **Why a local JSON marker file instead of checking Plex state**: unlike
    `title.locked` (a real field Plex exposes and `plex_clean_video_titles()`
    already checks), Plex's API doesn't expose an equivalent "was this
    poster manually/API-set" flag we can query cheaply. Tracking it
    ourselves, the same pattern `artist-metadata.json`/`has_metadata()`
    already establish for artwork state, is what makes re-running this
    every poll cycle cheap instead of re-generating+re-uploading an
    unchanged image for every video, every 2 minutes, forever.
- `plex_generate_title_cards(config, artist_name, artist_path, ...)` — single-
  artist entry point (used by the manual "Generate Title Cards" dashboard
  button's per-request path... actually wired to the bulk endpoint below;
  kept as a public single-artist function for any future per-artist UI
  action), calls `plex_find_videos_by_artist()` (one full-library GET).
- `plex_generate_title_cards_for_all(config, root_path, force=False)` — the
  actual periodic/bulk path. Fetches the whole library **once** and filters
  per-artist in Python, rather than calling `plex_find_videos_by_artist()`
  (a full-library GET *each*) once per artist folder — matters once there
  are more than a couple of artists, since this runs every poll cycle now
  (see the #2 fix above).

**Wiring**: `ArtworkWatcher._run()` also calls
`plex_generate_title_cards_for_all(config, self.root_path)` every poll
cycle (same `plex_collection_sync_on_artwork` gate, same block as the title
cleanup call above it). Manual trigger: `POST /api/plex/title-cards/generate`
(`app.py`, optional `{"force": true}` body to regenerate everything) and a
**"🖼️ Generate Title Cards"** button next to "🧹 Clean Up Titles" in
`templates/dashboard.html` (`generatePlexTitleCards()`).

**Verified live (2026-07-21)**: `docker-compose up -d --build` succeeded
(`Pillow` + `fonts-dejavu-core` installed cleanly), and — because the
"every poll cycle" fix above means this now runs immediately on container
start, not just on new-folder detection — the very first `ArtworkWatcher`
poll after the rebuild generated title cards for all 9 existing artists
with no manual trigger needed at all. Confirmed via the real running
container/Plex server:
- `docker exec vidshelf python3 -c "from PIL import Image"` succeeds;
  `fc-list | grep -i dejavu` shows all 8 DejaVu font files present.
- Every video's Plex `thumb` field carries a fresh version-hash
  (`/library/metadata/<key>/thumb/<epoch>`), confirming the poster upload
  was actually accepted, not just that the API call returned 200 — same
  verification approach as the artwork-swap fix earlier in this file.
- Downloaded a live generated poster (`Death Cab for cutie - "I Will Follow
  You into the Dark"`) and visually inspected it: legible wrapped title,
  artist name in caps, blurred/darkened fanart backdrop, readable gradient
  scrim — looks like a real poster, not a placeholder.
- A manual `POST /api/plex/title-cards/generate` immediately afterward
  correctly reported `generated: 0` for every artist (all ratingKeys already
  in that artist's `title-cards.json`) — confirms idempotency.
- Death Cab's 7 titles all now read `Death Cab for cutie - ...` with
  identical casing (previously mixed "For"/"for") — confirms
  `_normalize_artist_prefix()` works against real data. The
  screenshot-visible "stray quote" turned out to be a real stylistic
  full-width quote character in that song's actual title (`＂Black Sun＂`),
  not a cleaning bug — once the surrounding whitespace was normalized it
  renders correctly.
- No leftover `.title_card_<ratingKey>.jpg` temp files remained in any
  artist folder afterward, and no errors/warnings appeared in
  `docker logs` or `artwork-sync.log` across two full rebuild/restart
  cycles.

**If title cards stop generating / regress**: check, in order — (1) is
`plex_collection_sync_on_artwork` still `true` in `config.json` (same flag
gates this and title cleanup and collection sync), (2) did the image
rebuild actually happen after this change (`docker exec vidshelf python3
-c "from PIL import Image"` — `ImportError` means the old image is still
running), (3) `docker exec vidshelf fc-list | grep -i dejavu` — confirms
the font package actually landed in the image, (4) check
`<artist_folder>/title-cards.json` on the NAS — if a ratingKey is already a
key in it, that video will be skipped unless `force=True` is passed.

**If artist-name casing normalization doesn't seem to apply**: it only
rewrites the prefix if the artist already has a folder under
`artwork_sync.root_path` (that's where the canonical casing comes from) —
an artist with videos in Plex but no corresponding local folder (shouldn't
normally happen, but e.g. after manual file moves) won't get normalized.

## SECURITY AUDIT (2026-07-20, same session): full-project pass, fixed Critical/High/Medium/Low findings

A full-project security review (`app.py`, `downloader.py`, `artwork_sync.py`,
`artwork_swap.py`, `Dockerfile`, `docker-compose.yml`, `templates/`) was
requested after the title-card work above. Findings and fixes below, ordered
by severity; items intentionally **not** auto-fixed are called out with why,
per the earlier "RECURRING FAILURE MODE" note in this file about not
silently changing designed-public behavior.

### Critical

**C1 — Werkzeug debug mode enabled (`app.run(..., debug=True)`).** Flask's
debug mode enables the interactive in-browser debugger on any unhandled
exception, which allows arbitrary Python execution from a browser that
reaches it — not something that should ever be on by default on a service
bound to `0.0.0.0`. **Fix**: `app.py`'s `if __name__ == '__main__':` block
now reads `FLASK_DEBUG` (default off): `debug_mode =
os.environ.get('FLASK_DEBUG', '').lower() == 'true'`. Set `FLASK_DEBUG=true`
locally if you need the debugger; never set it in `docker-compose.yml`.

**C2 — Hardcoded Flask `secret_key` committed to source as a fixed string
literal.** This key signs session
cookies; anyone who has ever seen this repo (or its git history) could forge
a valid `session['username'] = 'admin'` cookie against any deployment still
using the literal string, bypassing login entirely. **Fix**:
`_get_or_create_secret_key()` in `app.py` — uses a `SECRET_KEY` env var if
set, otherwise generates one with `secrets.token_hex(32)` and persists it
into `config.json`'s `_secret_key` key (leading underscore = internal, not a
user-facing setting) so it survives restarts without ever falling back to a
known value. `docker-compose.yml`'s `environment:` now passes through
`SECRET_KEY` from `.env` (optional — see `.env.example`).

### High

**H1 — Hardcoded default credentials (`admin`/`adminadmin`), and password
changes never actually persisted.** `USERS = {'admin': 'adminadmin'}` was a
literal in source, and `/api/password` only mutated that in-memory dict —
every container restart silently reverted any password change back to
`adminadmin`, so in practice the password could never actually be changed
long-term. **Fix**: `_get_or_create_admin_credentials()` in `app.py` seeds
`config.json`'s `_auth` key (`{username, password_hash}`, hashed via
`werkzeug.security.generate_password_hash`) from `ADMIN_USERNAME`/
`ADMIN_PASSWORD` env vars on first run, or generates a random password and
prints it once (`[SECURITY] ...`) if neither is set. `/api/password` now
writes the new hash back into `config.json` so it survives restarts.
`docker-compose.yml` passes through `ADMIN_USERNAME`/`ADMIN_PASSWORD`.
**If you're locked out**: delete the `_auth` key from `config.json` and
restart — a fresh password will be generated and printed to `docker logs`
(or set `ADMIN_PASSWORD` in `.env` first for a predictable one).

**H2 — `/api/artwork/swap_noauth` is an unauthenticated SSRF vector.** This
endpoint (deliberately public — see the "Note on `_noauth` endpoints"
section above) takes a caller-supplied `new_image_url` and fetches it
server-side with no validation, then writes it as the artist's Plex
artwork. Without any host check, *any* unauthenticated caller could point
this at an internal address (the Plex server itself, cloud-metadata-style
endpoints, other LAN hosts) and the container would make that request.
**Fix**: added `_is_safe_download_url()` in `artwork_sync.py` — rejects any
URL whose scheme isn't `http`/`https` or whose host resolves to a
private/loopback/link-local/reserved/multicast address (via
`socket.getaddrinfo` + `ipaddress`). `download_image()` (the single
function both this endpoint and the *trusted* automatic artwork-sync path
funnel through) now checks every URL — including every redirect hop,
followed manually instead of via `requests`' `allow_redirects=True` — against
this before fetching.
- **Deliberately not fixed**: whether this endpoint should require auth at
  all. A previous session explicitly decided to keep `_noauth` endpoints
  public by design (see above) and explicitly flagged that as "a design
  decision for the user, not a regression to silently fix." This audit
  respects that and only closes the SSRF hole while leaving the
  no-login-required behavior in place — **see the catalog below** if you
  want to revisit whether this should require a session after all.
- **Known residual gap (documented, not fixed)**: the host-safety check
  resolves DNS once, up front. A malicious DNS server could return a public
  IP for that check and a private IP moments later for the actual
  connection (DNS rebinding). Fully closing that needs the resolved IP
  pinned for the actual request (a custom `requests` transport adapter),
  which wasn't implemented here — this stops ordinary SSRF attempts
  (`http://169.254.169.254/`, `http://localhost/`, RFC1918 ranges) without
  that added complexity.

### Medium

**M1 — No login throttling.** Combined with H1's previously-fixed default
credential, the login form had no brute-force protection at all. **Fix**:
simple in-memory lockout in `app.py` — `_LOGIN_FAILURES` (keyed by
`request.remote_addr`), locks an IP out for 5 minutes after 5 failed
attempts (`_login_is_locked`/`_record_login_failure`/
`_clear_login_failures`). Resets on container restart — acceptable for a
single-container, single-account app; the goal is raising the cost of
brute-forcing the login form, not building a distributed rate limiter.

**M2 — Non-constant-time credential comparisons.** Username was compared
with plain `==` (`secrets.compare_digest()` used now); passwords were
plaintext `==` comparisons against an in-memory dict rather than a hash
comparison. **Fix**: covered by the H1 rewrite —
`check_password_hash()`/`secrets.compare_digest()` throughout `login()` and
`/api/password`.

**M3 — Inconsistent URL validation let a caller-supplied URL reach yt-dlp's
generic extractor unvalidated.** `/api/channels/add` already restricted
channel URLs to `youtube.com`/`youtu.be` prefixes, but `/api/channel/videos`
(`?url=`) didn't apply the same check before handing the URL to yt-dlp —
yt-dlp supports hundreds of sites via a "generic" extractor, so this
effectively let the (single, trusted) admin account make the server issue
requests to arbitrary hosts. Low practical severity since only the trusted
admin account can reach it, but cheap to fix for defense-in-depth and to
close the gap between "an account compromise" and "an account compromise
that can also pivot the server into fetching arbitrary URLs." **Fix**:
extracted the prefix tuple to a shared `YOUTUBE_URL_PREFIXES` constant in
`app.py`, applied to both `/api/channels/add` (already had an equivalent
inline tuple) and `/api/channel/videos` (didn't check at all before).

**M4 — Missing baseline security response headers.** **Fix**: added an
`@app.after_request` hook in `app.py` setting `X-Content-Type-Options:
nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: same-origin` on every
response.

**M5 — Docker container runs as root with the `SYS_ADMIN` capability.**
**Not auto-fixed** — see the catalog below. `cap_add: SYS_ADMIN` in
`docker-compose.yml` is load-bearing for the CIFS volume mount (see the
CIFS RESOLVED section above, hard-won across several sessions); changing
the container's user without being able to rebuild and verify a real
download against the live NAS in this session risked silently re-breaking
that fix. Flagged for the user's decision, not changed.

### Low

**L1 — Session cookie hardening.** `SESSION_COOKIE_HTTPONLY` wasn't
explicitly set (Flask defaults this on already, but making it explicit
documents the intent) and `SESSION_COOKIE_SAMESITE` wasn't set at all.
**Fix**: `app.config['SESSION_COOKIE_HTTPONLY'] = True`,
`app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'` in `app.py`.
`SESSION_COOKIE_SECURE` is left **off by default** (env-gated via
`SESSION_COOKIE_SECURE=true`) since this is commonly reached over plain
HTTP on a home LAN — forcing `Secure` unconditionally would silently break
login for anyone not already behind HTTPS/a reverse proxy. Turn it on if
this is ever put behind TLS.

**L_config — internal keys (`_secret_key`, `_auth`) leaking through
`/api/config`.** Found while implementing C2/H1: `GET /api/config` returns
the *entire* `config.json` verbatim to the Settings page (used as a raw
config editor), which would have included the new `_secret_key` and
password hash. Worse, `POST /api/config` **replaces the whole file** with
whatever the client submits — since the Settings page's editor only
round-trips whatever GET returned, saving any settings change would have
silently dropped `_secret_key`/`_auth` from disk, invalidating every
session and regenerating (and re-printing) a brand new random admin
password on the next restart. **Fix**: `api_config()` in `app.py` now
strips `_secret_key`/`_auth` from the GET response, and POST preserves
whatever is currently on disk for both keys regardless of what the
submitted payload contains.

### Catalogued, not fixed (needs a decision, or is by-design)

- **Should `/api/artwork/search_noauth` / `swap_noauth` require a session?**
  Currently intentionally public (documented design decision, see above).
  The SSRF hole in the swap endpoint is now closed (H2), but an
  unauthenticated caller can still overwrite any artist's Plex
  collection artwork with any public image. If this container's port is
  ever reachable beyond a trusted LAN, that's a real (if low-stakes)
  defacement vector worth reconsidering.
- **CSRF tokens.** No `flask-wtf`/CSRF-token protection on state-changing
  POST endpoints. Practically mitigated today because every state-changing
  endpoint expects `Content-Type: application/json` and the app sets no
  permissive CORS headers, so a cross-origin page can't trigger these via a
  simple form post or a no-preflight fetch — but this is an implicit
  mitigation from the current shape of the API, not an explicit control. If
  CORS is ever added for a legitimate reason, revisit this at the same
  time.
- **`/api/browse-folder` lets the (single) authenticated admin browse the
  entire container filesystem.** By design — it backs the download-path
  folder picker. Not a bug, just worth knowing the admin account's reach
  when weighing H1/M1 above (a compromised admin session can enumerate any
  directory the container user can read).
- **DNS-rebinding TOCTOU gap in the new SSRF guard** (`_is_safe_download_url`
  in `artwork_sync.py`) — documented inline and above under H2. Would need a
  pinned-IP custom transport adapter to fully close; not implemented.
- **Docker container runs as root + `SYS_ADMIN`** (M5 above) — flagged, not
  changed, due to the CIFS mount's fragile history in this repo. If
  revisited, test a real end-to-end download against the live NAS
  afterward (same verification bar as the CIFS RESOLVED section), not just
  a container-starts check.

## FIXED (2026-07-21): duplicate Plex collections ("Nine Inch Nails" x3, "Soundgarden" x2, etc.)

The user spotted this visually in the Plex collections grid: the same
artist appearing as multiple separate collection tiles with different
(and each individually wrong) item counts — e.g. three "Nine Inch Nails"
tiles showing 9/1/7 items, two "Soundgarden" tiles showing 5/6.

### Root cause: check-then-create race in `plex_ensure_smart_collection()`

That function (`artwork_sync.py`) did: (1) GET the library's collections and
look for one whose title matches, (2) if none found, POST to create a new
smart collection. No synchronization existed between those two steps. Two
call paths can trigger this concurrently for the *same* artist:
- `api_music_videos_download()`'s per-download immediate sync (added in the
  "immediate collection sync on every download" work above) — each
  downloaded video spawns its own thread, and each thread calls
  `plex_sync_artist_collection()` → `plex_ensure_smart_collection()`
  independently right after its own download finishes.
- `ArtworkWatcher`'s background poll thread, which also calls
  `plex_sync_artist_collection()` for a newly-detected artist folder,
  running concurrently with any in-flight download threads for that same
  artist.

If two of these calls for the same artist both run their Step-1 "does it
exist?" check before either finishes Step 3's create, both see "no
collection yet" and both create one — classic TOCTOU race. The different,
individually-wrong childCounts on each duplicate happen because each
duplicate's smart-filter recount reflects whatever subset of matching
videos Plex had indexed at whatever moment each one last got its count
recomputed, not a live/shared value — they visibly diverge over time even
though all duplicates share the exact same filter.

**Fix**: added a module-level `_collection_creation_lock =
threading.Lock()` in `artwork_sync.py`, wrapping the entire check-then-create
body of `plex_ensure_smart_collection()`. A single global lock (not
per-artist) is sufficient — collection creation is infrequent and fast, so
briefly serializing unrelated artists' creation costs nothing measurable in
practice, and avoids the complexity of a per-artist lock registry.

### Cleanup: existing duplicates needed a one-time removal, the lock doesn't undo damage already done

New functions in `artwork_sync.py`:
- `plex_find_duplicate_collections(config)` — groups a library's
  collections by lowercased title, returns only groups with >1 entry.
- `plex_delete_collection(config, rating_key)` — `DELETE
  /library/collections/<ratingKey>`.
- `plex_dedupe_collections(config)` — for each duplicate group, keeps the
  entry with the highest `childCount` and deletes the rest. Safe to do
  blindly (no manual item-list reconciliation needed) because every
  duplicate in a group shares the *identical* smart filter — whichever one
  survives re-matches every video that belongs there on its own.

Exposed via `GET /api/plex/collections/duplicates` (report only) and
`POST /api/plex/collections/dedupe` (`app.py`), plus a **"🧬 Remove
Duplicate Collections"** dashboard button next to the other Plex-collection
tools.

**Verified live (2026-07-21)**: rebuilt, found the exact duplicates from the
screenshot still present (`GET /api/plex/collections/duplicates` → 3x
"Nine Inch Nails", 2x "Soundgarden"), ran the dedupe endpoint, confirmed
`duplicate_groups` is now empty and every artist has exactly one collection.

### Separate finding surfaced by this investigation, not fixed: "Nine Inch Nails" now correctly shows 0 items

After dedupe, the surviving "Nine Inch Nails" collection shows `childCount:
0` — triggering a library refresh doesn't change this, and it isn't stale
caching. Checked directly against the real library: there are only 2 actual
Nine Inch Nails videos in it, and **neither has an "ArtistName - Song"
separator in its title at all** — e.g. `Nine Inch Nails as Alive as You
Need Me to Be Official Music Video SnMyroAH0rg` (raw YouTube title, un-cleaned,
ID still attached, no dash after the artist name anywhere). The smart
collection's filter (`title contains "Nine Inch Nails -"`) correctly does
not match either one — this is the filter working as designed, not a bug in
it.

**Why this happens**: `downloader.py`'s `outtmpl`
(`'%(title)s-%(id)s.%(ext)s'`) uses the YouTube video's own title verbatim.
The entire "ArtistName - Song" convention this codebase's collections/
title-cleanup/title-cards all assume depends entirely on the original
YouTube uploader having used that exact convention in their video's title —
there's no enforcement or rewriting of the artist-prefix format at download
time. Most uploads do follow it (hence 10 of 11 artists' collections having
correct non-zero counts after the dedupe above); these 2 don't.

**Deliberately not auto-fixed**: rewriting these 2 titles to force an
"ArtistName - Song" shape would require guessing what the actual song title
is from a title with no separator to split on (`_clean_video_title()` and
`_normalize_artist_prefix()` both need a recognizable prefix/dash to work
from — there's nothing to normalize here, the information literally isn't
present in a parseable form). Manually retitling in Plex (or re-downloading
from a better-titled source, if one exists) is the correct fix, not a
heuristic guess. If more videos start showing up with 0-count collections
after this, check the raw title in Plex first — 9 times out of 10 this same
"no artist-prefix separator in the source YouTube title" cause is why.

## FIXED (2026-07-21): double toast on every single-video download click

The user spotted this visually too: clicking "Download" on a music-video
search result (or a channel video) showed two near-identical green toasts
back to back — "⏳ Starting download for X..." immediately, then "✅
Download started for X" a fraction of a second later. Unlike the earlier
duplicate-form-listener bug in `swap_art.html` (see the FIXED section
above), this wasn't two requests firing — it was one request, but the
handler function itself called `showToast()` twice: once before the
`fetch()`, once after it resolved.

The two toasts read almost identically because both `/api/download` and
`/api/music-videos/download` return near-instantly — they just spawn a
daemon thread and respond — so there was never a meaningful gap for the
"starting" toast to justify existing separately from the "started"
confirmation.

**Fix**: removed the pre-`fetch()` `showToast('Starting download for
...')` call from both `downloadVideo()` and `downloadMusicVideo()` in
`templates/dashboard.html`, leaving only the single toast that reports the
actual response (success or error). **Left `downloadAllChannelVideos()`'s
"⏳ Starting batch download..." toast alone** — that one has a real gap to
bridge: `/api/channels/download-all` fetches the channel's video list from
YouTube server-side before responding, which can take a perceptible moment,
so a distinct "started" vs. "in progress" pair of toasts is still
justified there.

**Verified live**: rebuilt, confirmed via `curl` that the served
`/dashboard` HTML no longer contains either single-download "Starting
download for..." string, while `downloadAllChannelVideos()`'s batch toast
is still present and untouched.

## FIXED (2026-07-21): "The Raconteurs" smart collection had 0 items — two stacked bugs, one code fix + one manual data fix

The user reported the collection "seemed to fail." Investigation via
`artwork-sync.log` and the live Plex API found **two independent causes**,
both hitting this one artist at once:

### Bug 1 (code, fixed + generalizes to any artist): en dash / em dash title separator

7 of this artist's 8 downloaded videos have titles using an en dash
(`–`, U+2013) instead of a plain hyphen between artist and song — e.g.
`The Raconteurs – Sunday Driver (Official Music Video)-kHpWUTCAR4I` (this
is how the uploader titled these on YouTube; not something this app
introduced). Every artist-prefix match in this codebase —
`plex_ensure_smart_collection()`'s filter, `plex_find_videos_by_artist()`,
`_normalize_artist_prefix()`, the title-card song-title split in
`_generate_title_cards_for_videos()` — looks for a literal `" - "`, so an
en/em-dash title silently matches nothing, with no error anywhere in the
pipeline to point at why.

**Fix**: added `_EN_EM_DASH_SEPARATOR_RE = re.compile(r'\s[–—]\s')` to
`artwork_sync.py`, applied first thing inside `_clean_video_title()`
(`title = _EN_EM_DASH_SEPARATOR_RE.sub(' - ', title)` before the existing
YouTube-ID/URL/boilerplate stripping). Since `plex_clean_video_titles()`
writes the cleaned title back to Plex and locks it, every downstream
consumer (`plex_find_videos_by_artist`, the smart-collection filter,
title-card generation) reads the *already-normalized* title back off Plex
— no other function needed its own dash-variant handling. This is a
general fix: any other artist whose uploads use "Artist – Song" instead of
"Artist - Song" was hitting the exact same silent 0-match failure and is
now fixed by the same code path, not just this one artist.

**Verified live**: rebuilt, ran title cleanup, confirmed all 8 titles now
read `The Raconteurs - <song>` (previously 7 of 8 had the en dash).

### Bug 2 (data, fixed manually with the user's explicit go-ahead): folder name typo

Separately, the artist folder itself was named `The_Racontuers` (missing an
"e") — a typo from whatever artist name was typed/selected at download
time — while every actual video title correctly says "The Raconteurs". This
meant the smart collection got created as `The Racontuers` (matching the
typo'd folder-derived name via `folder_to_artist()`), with a filter
(`title contains "The Racontuers -"`) that could never match the correctly-
spelled videos no matter what the dash fix did. Log evidence: repeated
`No artwork sources found for 'The Racontuers'` (TheAudioDB/Fanart.tv/
MusicBrainz/Wikipedia don't know a misspelled band name either) and
`Found 0 videos matching 'The Racontuers -' in Plex library` on every sync
attempt.

This is data (a real folder name on the NAS + a real Plex collection), not
a code bug, so it was **not auto-corrected** — confirmed with the user
first (which of: rename + recreate / just delete / leave it manually),
who chose rename + recreate. Fixed as:
1. `os.rename('/app/music_videos_final/The_Racontuers',
   '/app/music_videos_final/The_Raconteurs')` inside the container — a
   metadata-only rename, not a file copy, so none of the CIFS/`sendfile()`
   copy caveats in `CLAUDE.md` gotcha #2 apply.
2. Deleted the old mis-named, permanently-empty "The Racontuers" collection
   via `plex_delete_collection()` (the same function added for the
   duplicate-collection cleanup above).
3. Triggered `POST /api/artwork/sync` + `POST /api/plex/collections/sync`
   for `artist=The Raconteurs` — TheAudioDB immediately found real artwork
   for the correctly-spelled name, and a fresh smart collection was created
   matching all 8 videos.

**Verified live**: `The Raconteurs` collection now exists once, `smart=1`,
`childCount=8`, with a real downloaded poster/fanart.

**Also noticed, not fixed (separate, needs the user's own judgment)**: this
same folder also contains `The Dead Weather - Treat Me Like Your Mother
(Official Music Video)-M7QSkI6My1g.mkv` — a different Jack White side
project, filed into the Raconteurs folder by mistake at download time. This
doesn't affect the collection fix above (the smart filter is title-based,
so a correctly-titled Dead Weather video simply doesn't match the "The
Raconteurs -" filter either way, which is actually the *correct* outcome)
— but the file itself is sitting in the wrong artist's folder on the NAS.
Not moved, since this wasn't part of what the user asked to fix and moving
a NAS file the user might have organized deliberately without asking first
isn't a call to make unilaterally.

**If a "collection has 0 items" report comes in again**: check, in this
order — (1) does the raw title in Plex actually contain the artist name
immediately followed by `" - "` (an en/em dash, or a completely different
separator, will silently fail to match — grep the raw title for the dash
character), (2) does the folder name (`folder_to_artist()`'s output) really
match the spelling used in the actual video titles.

## ADDED (2026-07-21): convert downloaded videos to a Plex-direct-play-compatible format

The user asked to make sure downloaded videos are in a format most Plex
clients can play **without server-side transcoding**, without losing
quality — both fixing everything already downloaded and preventing the
problem for future downloads.

### Why this mattered

The old yt-dlp format selector (`'bestvideo+bestaudio/best'`,
`merge_output_format: 'mkv'`) just grabbed YouTube's highest-quality
available streams with no codec preference. YouTube's highest-quality
adaptive streams are very often **VP9 or AV1 video + Opus audio**, not
H.264/AAC — confirmed by probing this account's actual library:
`docker exec vidshelf python3 -c "import transcode; print(transcode.probe_media(path))"`
on a real downloaded Raconteurs video returned `{'video_codec': 'av1',
'audio_codec': 'opus'}`. AV1/VP9 hardware decode support is still
inconsistent across Plex clients (many smart TVs, older devices, some
mobile clients), and Opus audio direct-play support is narrower still —
so a library downloaded this way forces Plex Media Server to transcode on
playback for a large fraction of clients, even though the file "looks
fine" (good resolution/bitrate) on paper.

### Target format

**MP4 container, H.264 video, AAC audio** — the combination the widest
range of Plex clients (including older/embedded/smart-TV clients) can
direct-play with zero server-side transcoding.

### New module: `transcode.py`

- `probe_media(path)` — runs `ffprobe -show_streams` (JSON output),
  returns `{'container', 'video_codec', 'audio_codec'}` or `None` if the
  file can't be probed (left alone rather than guessed at).
- `needs_conversion(path)` — `True` unless the file is already MP4 +
  H.264 + (AAC or no audio track).
- `convert_to_plex_compatible(src_path, dest_path=None)` — the core
  converter. **Stream-copies whichever track is already compatible**
  instead of blindly re-encoding both: `-c:v copy` if video is already
  H.264, `-c:a copy` if audio is already AAC. Video only gets re-encoded
  (`-c:v libx264 -crf 17 -preset slow`) if it isn't H.264 already — CRF 17
  is a quality target (not a bitrate target), chosen to be visually
  indistinguishable from the source rather than a fixed bitrate that could
  visibly degrade on complex scenes. Audio only gets re-encoded
  (`-c:a aac -b:a 256k`) if it isn't AAC already. Writes to a same-directory
  `.converting.mp4` temp file first, then `os.replace()`s into place —
  never runs ffmpeg in-place (reading and writing the same path corrupts
  output).
- `convert_file_safely(path, scratch_dir)` — wraps the above for files that
  may live on the CIFS-mounted NAS: copies the source to local scratch
  storage first (manual buffered copy — never `shutil.copy2`/`copyfile`,
  see CLAUDE.md gotcha #2), converts locally, copies the result back, then
  removes the original. ffmpeg's own I/O should be fine directly against a
  CIFS mount (it doesn't use the `sendfile()` fast-path gotcha #2 warns
  about), but this is a bulk, unattended job touching potentially the whole
  library on a NAS this codebase has already had CIFS surprises with — local
  staging costs some extra I/O in exchange for not finding out the hard way.

### Wired into new downloads: `downloader.py`

`download_video()`'s yt-dlp format selector now prefers a native H.264+AAC
stream directly, avoiding any re-encode for the common case where YouTube
actually offers one:
```
'format': 'bestvideo[vcodec^=avc1]+bestaudio[acodec^=mp4a]/best[vcodec^=avc1]/bestvideo+bestaudio/best'
'merge_output_format': 'mp4'
```
Falls back to best-available (any codec) only if no native H.264/AAC
option exists (e.g. 4K content YouTube only serves in VP9/AV1, or older
uploads without a progressive H.264 option). After download and **before**
the move/copy to `plex_media_path` (which may be the CIFS-mounted NAS —
transcoding should happen against local storage, not over the network),
`download_video()` now checks `transcode.needs_conversion()` and runs
`transcode.convert_to_plex_compatible()` if needed, updating the
downloaded filename/extension if the container changed. Progress reporting
gained a `'converting'` status (new badge in `templates/dashboard.html`'s
`getStatusBadge()`, included in the "is this download still active"
check alongside `queued`/`downloading`).

The duplicated "find the file yt-dlp just wrote" logic that used to live
separately in both the paths-differ and same-path branches was
consolidated into one lookup, done once, right after download completes —
not a behavior change, just removed near-duplicate code while adding the
conversion step in the one place both branches now share.

### Batch-converting the existing library

New in `app.py`: `_gather_media_roots()` (music-video root + every
channel's resolved `plex_media_path` + `plex_base_path`, deduplicated),
`_scan_conversion_candidates()` (walks every root, returns paths needing
conversion — cheap, just `ffprobe` calls, no encoding), and
`_run_conversion_job()` (the actual background job, run in a daemon
thread via `POST /api/conversion/start`).

Endpoints:
- `POST /api/conversion/scan` — dry run, reports count + file list (capped
  at 50 in the response) without converting anything.
- `POST /api/conversion/start` — starts the background job; `409` if one's
  already running.
- `GET /api/conversion/status` — `{running, phase, total_files, scanned,
  converted, failed, current_file, errors, ...}`.

Dashboard: new "🎞️ Video Format Compatibility" section on the Settings page
(`templates/dashboard.html`), with Scan/Convert-All buttons and a live
progress bar that polls every 2s while a job is running, and resumes
polling on page (re)load if a job is still in progress
(`resumeConversionPollingIfRunning()`).

**Bug caught during testing, fixed before running the real batch job**:
`_run_conversion_job()` originally didn't set `_CONVERSION_STATE['running']
= True` until *after* the initial library scan (an `ffprobe` call per
existing file) finished — so a client polling `/api/conversion/status`
right after `/start` returns would see `running: false, total_files: 0`
and could easily misread "still scanning" as "job already finished".
Fixed by adding a `phase` field (`'idle' | 'scanning' | 'converting'`) and
setting `running: True, phase: 'scanning'` immediately when the background
thread starts, before the scan runs — the frontend now checks `phase`
explicitly instead of inferring state from `total_files`/`current_file`.

**Design note — Plex refresh frequency**: converting a file whose container
changes (`.mkv` → `.mp4`) is effectively a rename from Plex's point of
view. Verified live: after converting one file, Plex showed it as
**removed** (old item gone, including its locked title, custom poster, and
collection membership) until a library refresh, and then re-added as a
**new, uncleaned** item (raw filename title, default auto-thumbnail,
temporarily missing from its collection) until the existing title-cleanup /
title-card / collection-sync pipeline caught up. Since this is a
potentially many-hour job, refreshing only once at the very end would have
left every already-converted file looking "broken" in Plex for the
remainder of the run — so `_run_conversion_job()` calls
`trigger_plex_refresh()` after every successful conversion (matches this
codebase's existing pattern: `plex_sync_artist_collection()` already
triggers a refresh on every single download, not batched). Combined with
the earlier fix making `ArtworkWatcher` run title cleanup/title-card
generation every poll cycle (not just on new-folder detection), each
converted file self-heals back to a clean title/poster/collection
membership within one poll interval (≤120s) of Plex noticing the rename —
verified end-to-end on a real file (`The Raconteurs - Sunday Driver`):
converted → Plex refresh → showed up raw/uncleaned, collection childCount
dropped 8→7 → manual title-cleanup+collection-sync+title-card pass →
back to clean title, childCount 8, fresh title-card poster, all within
seconds once triggered.

**Trade-off worth knowing**: re-encoding AV1/VP9 to H.264 at a
visually-lossless CRF can produce a **larger** file than the source — AV1
in particular compresses meaningfully better than H.264 at equivalent
visual quality, so converting *for* compatibility trades some disk space
for direct-playability. Confirmed on the same test file: the AV1 source
was smaller than the resulting H.264 MP4. Not a bug — this is the actual
cost of the compatibility goal the user asked for, and disk space wasn't a
stated constraint (this NAS has multi-TB free per the CIFS mount section
above).

**Verified live (2026-07-21)**: rebuilt; confirmed `ffprobe`/`ffmpeg`
present and `transcode` importable in the container; probed a real library
file and got `av1`/`opus` as expected; ran a real end-to-end
`convert_file_safely()` call against a live NAS file (216s for one ~64MB
AV1/Opus MKV → a larger H.264/AAC MP4) and confirmed the output probes as
`mp4`/`h264`/`aac`; confirmed the self-healing behavior described above;
scanned the real library and found **117 of ~118 files** need conversion
(nearly the entire existing library came in as VP9/AV1); started the real
batch job in the background via `POST /api/conversion/start` after fixing
the scanning-phase status bug above.

**Scope/runtime note**: at roughly 3–4 minutes per file observed on this
hardware, converting the whole existing library is realistically a
multi-hour background job, not something to wait on synchronously. If this
session ended before it finished, check `GET /api/conversion/status` (or
the Settings page's Video Format Compatibility panel) for current
progress — the job is **not** resumable/persisted across a container
restart (see `_CONVERSION_STATE`'s docstring for why that trade-off was
made), so if the container restarted mid-job, re-run "Scan" to see what's
still left and click "Convert All" again; `needs_conversion()` is a
stateless check, so nothing already converted gets redone.

**Also noticed while scanning, not touched (separate from what was
asked)**: `Nine_Inch_Nails/David Bowie - I'm Afraid of Americans (Official
Video) [4K]-LT3cERVRoQo.mkv` is a Bowie song misfiled into the Nine Inch
Nails folder — the same kind of mistake as The Dead Weather video that
was found in The Raconteurs' folder earlier in this file (fixed then,
moved into its own "The Dead Weather" folder/collection at the user's
request). This one wasn't raised by the user this time, so it wasn't
moved — same treatment as before: flag it, don't act on NAS file
organization unilaterally.

**Follow-up (2026-07-21): this one turned out to be a real Bowie/NIN
collaboration, not a misfile.** The user clarified this specific video
*should* be in the Nine Inch Nails collection (Bowie's "I'm Afraid of
Americans (V1)" video/remix features Trent Reznor). Since the smart
collection matches on title (not folder location), simply having the file
in the right folder wasn't enough — its Plex title started with "David
Bowie -", so the "Nine Inch Nails -" filter never matched it. Fixed by
converting the file (see below) and using the existing
`plex_set_item_title()` to manually retitle it to `"Nine Inch Nails - I'm
Afraid of Americans (feat. David Bowie)"` with `title.locked=1` — the
existing per-poll title cleanup/normalization only *rewrites* casing on an
existing artist prefix, it doesn't invent a different artist attribution
for a video whose raw title never mentioned it, so this one specific item
needs a manual override rather than being something the automatic pipeline
would ever produce on its own. Verified: Nine Inch Nails collection
childCount went from 12 to 13 after the retitle.

## FIXED (2026-07-21): Docker Desktop crash during the bulk conversion — root cause was disk space, not the app

Partway through the batch conversion job above, the whole Docker Desktop
backend crashed — `docker ps` started returning `500 Internal Server
Error` from the Docker API itself, not an error from this project's
container. Diagnosis (not code-related, but recorded here since it
directly affected this project's data):

- `com.docker.service` (the Windows service Docker Desktop's engine depends
  on) had stopped.
- Root cause: `C:\Users\<user>\AppData\Local\Docker\wsl\disk\docker_data.vhdx`
  (Docker's WSL2 virtual disk) had grown to **122.8GB**, driving the
  Windows C: drive down to **~7GB free** (and further, down to ~5.78GB,
  over the course of this investigation) — critically low, enough to
  destabilize Docker Desktop's backend service. The likely trigger: this
  session ran `docker-compose up -d --build` many times (once per code
  change across the title-card, security-audit, and format-conversion
  work above), and Docker's build cache/old image layers don't get
  cleaned up automatically — confirmed via `docker system df` showing
  ~22GB of build cache and ~57GB of reclaimable image layers at the time.
  Also confirmed several *other*, unrelated Docker projects on this
  machine (plex-rewind, tdarr, portainer, bookorbit, netalertx, etc.)
  contributed to the same shared VHDX, so this wasn't 100% attributable to
  this project alone.
- **Important**: the vidshelf *container itself* never actually
  crashed or lost data — `docker ps` on the container was simply
  unreachable because the Docker Desktop API layer was down. Once the
  service was manually restarted (required the user — restarting a
  Windows service needs admin rights this session didn't have), the
  container came back up cleanly with all state intact (config, download
  history, Plex connection all preserved — everything that matters is on
  the D: drive bind mounts or the NAS, not in the VHDX).

**Fix (partial — the deeper piece needs the user's own follow-through)**:
1. `docker builder prune -af` + `docker image prune -af` + `docker
   container prune -f` + `docker network prune -f` — reclaimed ~42GB of
   *logical* Docker usage (build cache dropped from ~22GB to 0, images
   from 105GB to 27.85GB).
2. **This did NOT shrink the actual VHDX file on disk** — Windows/WSL2
   VHDX files are dynamically-expanding but never auto-shrink; freed
   internal blocks stay allocated to the file until it's explicitly
   compacted. Confirmed: VHDX size was unchanged (122.80GB) and C: free
   space was unchanged (5.78GB) even after the prune fully completed.
3. Compacting requires: `wsl --shutdown` (stops all WSL distros, including
   Docker's — briefly takes the container down) followed by a `diskpart`
   `compact vdisk` pass against the vhdx file, both from an **elevated**
   PowerShell — neither step is possible from this project's automation
   session (no admin rights). Provided the exact commands to the user;
   as of this writing it's unconfirmed whether they've been run (C: free
   space and the VHDX size were both still unchanged the last time this
   was checked).

**Knock-on effect observed, not yet independently confirmed as fully
resolved**: while C: was still critically low, a large/heavy video
conversion (the Björk/ROSALÍA collab, part of the batch-conversion cleanup
above) failed **twice** with `ffmpeg exited -9` (SIGKILL) after only
18-50 seconds of encoding — consistent with the Linux OOM killer acting
inside the WSL2 VM, plausibly because WSL2's swap file couldn't grow with
C: this low. **If other heavy encodes/conversions start failing the same
way (killed with exit -9 well before finishing, no clear ffmpeg error
message), check C: free space and the VHDX size first** before assuming
it's a `transcode.py`/ffmpeg bug — this exact symptom was caused by host
disk pressure, not the conversion code.

**If Docker Desktop's service crashes again**: check `Get-Service
com.docker.service`, then `Get-Item
"$env:LOCALAPPDATA\Docker\wsl\disk\docker_data.vhdx"` for its current
size and cross-reference against free space on C: — this is now the
first thing to check before assuming an app-level regression, the same
way `docker exec vidshelf df -h` is the first check for the CIFS-mount
class of bug documented earlier in this file.

## FIXED (2026-07-21): dashboard status-polling drowned out real log lines

While watching the conversion job, `docker logs` became almost unreadable
— the Settings page's conversion-progress panel polls
`GET /api/conversion/status` every 2 seconds while open (same pattern as
the pre-existing Downloads page polling `GET /api/downloads/progress`),
and Werkzeug's dev-server logs every single request at INFO level by
default. With a job actively running, this produced a near-continuous
wall of identical `"GET /api/conversion/status HTTP/1.1" 200 -` lines,
burying actually-useful log output (download errors, conversion
print-progress) in between them — this is what surfaced two real (and
unrelated) `[youtube] ... This video is not available` download failures
only after they'd already scrolled well up the log.

**Fix**: added a `logging.Filter` (`_SuppressNoisyPollingEndpoints` in
`app.py`) on the `werkzeug` logger that drops access-log lines for
`GET /api/conversion/status` and `GET /api/downloads/progress` specifically
— both are read-only, side-effect-free, timer-polled endpoints, not
something a human actually clicked, so there's no diagnostic value lost by
not logging them. Every other route (including errors, POSTed actions,
and yt-dlp's own print/log output) still appears normally.

**Verified live**: rebuilt, confirmed 5 consecutive polls to
`/api/conversion/status` produced zero new log lines in `docker logs`,
while a `POST /login` in between them logged as expected.

**If a future endpoint becomes similarly noisy** (e.g. a new
polling-based status panel), add its path to
`_SuppressNoisyPollingEndpoints._NOISY_PATHS` rather than reaching for a
blanket "disable access logging" — keeping everything else logged is what
makes `docker logs` useful for actually diagnosing something.

## FIXED (2026-07-21): music-video search creating a duplicate collection for an already-known artist

The user found this by searching "Weird Al Amish" (to narrow results down
to the "Amish Paradise" video) — it created a second "Weird Al Amish"
folder/collection instead of filing the video under the existing "Weird
Al".

**Root cause**: `templates/dashboard.html`'s Music Videos search box does
double duty — its text is both the YouTube search query (`POST
/api/music-videos/search`) *and*, verbatim, the `artist` field sent to
`POST /api/music-videos/download`, which becomes the folder name and
canonical Plex collection name. Typing a more specific search (artist +
song, a reasonable way to narrow results) makes for a perfectly good
search query but a bad artist identity — the whole string gets used as-is.

**Fix**: added `_resolve_existing_artist(query, root_path)` in `app.py`,
called at the top of `api_music_videos_download()`. If `query` exactly
matches, or starts with (on a word boundary), an already-known artist
folder's canonical name, it snaps to that existing name instead of the
raw search text. A genuinely new artist is still named from whatever was
supplied — this only prevents forking a near-duplicate for an artist
that's already being tracked.

**Data cleanup performed**: moved the misfiled Amish Paradise video from
`Weird_Al_Amish` into the existing `Weird_Al` folder, removed the
now-empty `Weird_Al_Amish` folder, and deleted the bogus "Weird Al Amish"
Plex collection.

**Separate, related finding — not fixed (needs the user's decision)**:
even after the merge, the "Weird Al" collection shows 0 items. All 5 of
his video titles use his stylized stage name with quote characters —
`＂Weird Al＂ Yankovic - ...` or `＂Weird＂ Al Yankovic - ...` (inconsistent
even within his own catalog) — neither of which starts with the plain
`Weird Al -` the collection's filter expects, since the folder's canonical
name is "Weird Al", not "Weird Al Yankovic". Fixing this would need
renaming the folder to `Weird_Al_Yankovic` and adding quote-character
normalization to title cleanup so both stylizations collapse to a
consistent prefix. Flagged for the user, not changed, same as the
Raconteurs folder-typo case earlier in this file — renaming an existing
folder is a judgment call, not something to do silently.

**Verified live**: rebuilt; confirmed `Weird Al`/`The Dead Weather`/`The
Raconteurs` (from earlier fixes) are unaffected; the code path for a
brand-new artist search (no existing folder match) still uses the
supplied name as before — only an existing-artist match now redirects.

## ADDED (2026-07-21): music-video search pagination + ranking improvements

The user asked for two things: paging (search only ever showed a small,
fixed set of results with no way to see more), and any other search
quality improvements worth making while in there.

### Pagination

Same pattern as the existing artwork-image search (`/api/artwork/search_noauth`,
`ARTWORK_SEARCH_PAGE_SIZE`): the full ranked result set is fetched from
YouTube once per artist query and cached (`_MUSIC_VIDEO_SEARCH_CACHE`,
10-minute TTL, keyed by lowercased artist string), then `POST
/api/music-videos/search` slices `MUSIC_VIDEO_SEARCH_PAGE_SIZE` (9) results
per `page` param instead of returning everything at once. Format-quality
enrichment (`get_video_formats_info()` — a full extra yt-dlp metadata call
per video) only runs on the page actually being returned, not the whole
cached set, and skips videos that already have `best_quality` cached from
an earlier page fetch (dicts in `page_videos` are the same objects held in
the cache, not copies).

Frontend (`templates/dashboard.html`): extracted the per-video card
markup into `renderMusicVideoCard()` (previously inlined in
`searchMusicVideos()`), added a "Load More Results" button
(`addMusicVideoLoadMoreButton()`/`removeMusicVideoLoadMoreButton()`) that
appends subsequent pages to the grid instead of replacing it — same
`fetchXPage(append)` shape as the existing swap-art image search
(`fetchSwapArtImagePage()`).

### Search breadth

`search_music_videos()` used two near-duplicate queries ("music video
official" vs "official music video" — near-identical word sets, heavily
overlapping YouTube result sets) at `ytsearch15` depth each. Replaced with
three differently-angled queries at greater depth: `"{artist} official
music video"` (ytsearch25), `"{artist} vevo"` (ytsearch20, targets
official-channel uploads specifically), and a bare `"{artist}"`
(ytsearch20, broadest net — catches uploads that don't literally say
"official"/"music video" in the title). Verified live: a "Weezer" search
went from the old ~15-30-result ceiling to **38 total unique results**
after dedup.

### Ranking quality fixes

In `rank_videos_by_quality()`:
- **Removed "topic" from the official-channel bonus patterns.** YouTube
  auto-generates "Artist - Topic" channels for audio-only uploads (a
  static image, no real video) — scoring these as "official" for a *music
  video* search ranked audio-only auto-uploads above genuine official
  video uploads whenever one appeared in results. This is a real quality
  bug independent of the pagination work, just found while in the same
  function.
- **Added a short-duration penalty** (`-15` for anything under 60s) —
  catches YouTube Shorts/teaser clips that otherwise looked like a strong
  match on title/channel signals alone but aren't actual music videos.
- **Added `'trailer'`/`'teaser'`** to the existing low-quality keyword
  penalty list (alongside `cover`/`karaoke`/`remix`/etc.).
- Moved `import math`/`import datetime` out of the per-video loop to
  module level — those ran on every single video scored, not a
  behavior change, just removed a pointless per-iteration re-import.

**Verified live**: rebuilt; `POST /api/music-videos/search` with
`{"artist": "Weezer", "page": 1}` returned `total: 38, has_more: true`;
`page: 2` returned a different, non-overlapping set of 9 videos without
re-querying YouTube (confirmed via the response `total` staying at 38
across both calls — only the format-enrichment step, not a new search,
accounted for the page's latency).

## FIXED (2026-07-21): "Weird Al" collection had 0 items — stylized quote characters in his own video titles

Follow-up to the duplicate-collection fix above — the user asked to also
fix why the (correctly merged) "Weird Al" collection still showed 0 items.

**Root cause**: all 5 of his videos are titled with quote characters
stylizing part of his own stage name — `＂Weird Al＂ Yankovic - Eat It` or
`＂Weird＂ Al Yankovic - Amish Paradise` (inconsistent even across his own
uploads — some quote "Weird Al", one quotes just "Weird"). Every
artist-prefix match in this codebase expects a plain `"ArtistName - "`,
so neither stylization ever matched — not a wrong artist name, just
punctuation actually embedded inside it.

**Fix**: added `_strip_artist_prefix_quotes()` to `artwork_sync.py`, wired
into `_clean_video_title()` right after the en/em-dash normalization added
earlier. Splits the title on the *first* `" - "` and strips full-width/curly
quote characters (`＂“”` — deliberately not ASCII `"`/`'`) only from the
portion before it, leaving anything after the separator untouched. This
matters: Death Cab's `"Death Cab for cutie - ＂Black Sun＂"` has its song
title legitimately stylized in quotes, and blanket-stripping quotes from
the whole title would have wrongly un-stylized that too. Verified the
logic in isolation before rebuilding:
```
'＂Weird Al＂ Yankovic - Eat It (Official 4K Video)' -> 'Weird Al Yankovic - Eat It (Official 4K Video)'
'＂Weird＂ Al Yankovic - Amish Paradise (...)'        -> 'Weird Al Yankovic - Amish Paradise (...)'
'Death Cab for cutie - ＂Black Sun＂'                  -> unchanged
```

**Also required** (the code fix alone doesn't retroactively fix already-cleaned
titles): renamed the `Weird_Al` folder to `Weird_Al_Yankovic` so the
folder-derived canonical name matches his full stage name used in his
video titles, not just "Weird Al". After rebuilding, the very next
`ArtworkWatcher` poll (runs every cycle, not just on new-folder detection —
see the earlier fix in this file) re-cleaned all 5 titles under the new
logic automatically — no manual per-item title fix was needed this time,
unlike the Bowie/NIN case earlier, because these titles weren't actually
locked yet when the fix landed.

Deleted the old empty "Weird Al" collection, ran a fresh
artwork+collection sync for "Weird Al Yankovic": real artwork found via
TheAudioDB, collection created with `childCount=5` (all videos).

**If a "collection has 0 items despite the artist folder existing"
report comes in again for a different artist**: check the raw title in
Plex for *any* non-ASCII punctuation immediately around the artist name,
not just the em/en-dash case already covered — this is the second
distinct "title has decorative punctuation the prefix-match doesn't
expect" bug found in as many days (Raconteurs' en-dash, now Weird Al's
quote stylization). Both got fixed the same way: normalize it in
`_clean_video_title()`, not by special-casing the artist.

## PREPARED (2026-07-23): audited and prepped for a public repo + rename

The user asked for a full project audit ahead of renaming this repo and
making it public. Findings and fixes below; the user will push the
resulting tree as a fresh initial commit to a new repo (git history of
this repo was explicitly not carried forward, so no history-rewriting was
needed for anything found here).

**Fixed**:
- **README was badly out of date** — described an early version of the
  app (said login was `admin`/`adminadmin`, listed "hardcoded default
  credentials" as a known limitation, didn't mention Plex OAuth,
  collections, artwork sync, title cards, or format conversion at all).
  Rewritten to match current reality, including a legal/ToS disclaimer
  section (personal-use framing, not affiliated with YouTube/Google/Plex)
  — standard practice for a public tool that downloads YouTube content,
  similar to yt-dlp's own disclaimer.
- **No LICENSE file existed** despite the README claiming MIT — added
  `LICENSE` (MIT, matching the README).
- **Real internal network details were in currently-tracked docs**:
  `CLAUDE.md`/`REFERENCE.md` had the actual NAS IP (`192.168.1.196`) and
  Plex server IP (`192.168.1.101` — was `.218`) hardcoded in prose/examples
  across ~7 locations. Replaced with placeholder addresses
  (`192.168.1.100`/`192.168.1.101`). Low actual risk (private/RFC1918,
  unreachable from the internet) but no reason to publish real home-network
  topology.
- **`docker-compose.yml` hardcoded the NAS device path** (`device:
  "//192.168.1.196/ppv/MusicVideos"`) directly — not just a privacy issue,
  this meant the file literally wouldn't work for anyone else without
  editing it. Moved to a `NAS_SMB_DEVICE` env var (same pattern as the
  existing `NAS_SMB_USER`/`NAS_SMB_PASS`), documented in `.env.example`.
  The current deployment's real value was preserved in the local
  (gitignored) `.env`.
- **`PLEX_CLIENT_ID` was a hardcoded UUID** in `artwork_sync.py`, shared by
  every clone of this codebase. Added `_get_or_create_plex_client_id()`
  (same pattern as `app.py`'s `_get_or_create_secret_key()`): prefers a
  `PLEX_CLIENT_ID` env var, otherwise generates and persists a random UUID
  into `config.json` on first run, so each deployment gets its own Plex
  OAuth identity. **Preserved continuity for this deployment**: pre-seeded
  the existing hardcoded UUID
  (`7d0dadb1-a111-402c-b1d6-d05b8e8dc5e2`) into this install's `config.json`
  as `_plex_client_id` before deploying the change, so the already-authorized
  Plex connection wasn't disrupted. `PLEX_PRODUCT` (the name shown on
  Plex's OAuth authorization screen) is now similarly overridable via env
  var, defaulting to `"vidshelf"`.
- **`test.txt`** (untracked scratch content, not covered by any
  `.gitignore` rule) — deleted; would have been picked up by a fresh
  `git add .` in the new repo otherwise.
- **`.claude/settings.local.json` was tracked** despite being per-developer
  local config by Claude Code's own naming convention (no shared
  `.claude/settings.json` exists in this repo — only the `.local.` variant
  was present, which shouldn't be). Untracked it (`git rm --cached`,
  file kept on disk) and added `.claude/settings.local.json` to
  `.gitignore`.

**Verified nothing broke**: rebuilt and did a **full `docker-compose down`
+ `up -d --build`** (not just a rebuild) since the `music_videos_final`
volume's `driver_opts.device` value changed — per this file's own CIFS
gotcha notes, a plain `up -d --build` doesn't reliably pick up a changed
named-volume definition. Confirmed via `docker exec vidshelf df -h` that
the mount is still the real NAS (`cifs`, 103T/4.1T), confirmed
`PLEX_CLIENT_ID` resolved to the exact same preserved UUID (not a fresh
random one), confirmed login still works, and confirmed
`GET /api/plex/collections/status` still returns real collection data —
i.e. the existing Plex connection survived the `PLEX_CLIENT_ID` change
unaffected.

**Confirmed clean (no changes needed)**: no real secrets (tokens,
passwords, API keys) found in any currently-tracked file; `.gitignore`
already correctly excludes every sensitive/generated file present in the
working tree (`.env`, `config.json`, `cookies.txt`, `active_downloads.json`,
`downloaded_videos.json`, `downloads/`, `.aider*`, `__pycache__/`) —
verified directly against `git status --porcelain --ignored`, not assumed;
`config.json.example`/`.env.example` were already clean, generic
placeholders suitable for public onboarding.

**Not fixed, flagged for the user's own follow-through**: the actual repo
rename itself (new name TBD — suggested a shortlist separately) and
updating the README's clone-URL placeholders
(`<your-username>/<new-repo-name>`) once the new repo exists.

## RENAMED (2026-07-23): youtubearr → Vidshelf

Chosen from a shortlist (deliberately avoiding an "-arr" suffix — mirroring
the Sonarr/Radarr naming convention closely enough to invite the same kind
of confusion an actual `*arr` project might reasonably want to avoid — and
avoiding "Plex" in the name for the same trademark-conflict reason).

The user confirmed the new public repo would start from a **fresh commit
history** — so unlike the CIFS/OAuth bugs elsewhere in this file, there was
no old-name residue in git history to worry about scrubbing; the only
thing that mattered was the *current* working tree being completely clean
of the old name before that first commit.

**Every git-tracked file renamed**, verified with a zero-result sweep
(`git ls-files -z | xargs -0 grep -lI "youtubearr"`) after the change —
not assumed clean from a partial search:
- `README.md`, `CLAUDE.md`, `REFERENCE.md` — titles and prose
- `artwork_sync.py` — `PLEX_PRODUCT` default (`'youtubearr'` →
  `'Vidshelf'`), a docstring, and the `User-Agent` header string sent to
  TheAudioDB/Fanart.tv/MusicBrainz/Wikimedia (`'youtubearr/1.0
  (artwork-sync)'` → `'vidshelf/1.0 (artwork-sync)'`)
- `templates/login.html` / `templates/dashboard.html` — page titles, logo
  text, help text, the Plex-authorization button copy
- `docker-compose.yml` — service name and `container_name` (`youtubearr` →
  `vidshelf`)
- `.env.example` — a stale comment that still said the `PLEX_PRODUCT`
  default was `"youtubearr"` after the code default had already changed

One deliberate exception, later reverted at the user's explicit
instruction: `REFERENCE.md`'s C2 finding (hardcoded Flask secret key, see
the security-audit section above) originally quoted the *exact* removed
string literal (`'youtubearr-secret-key-change-in-production'`) as
historical evidence. The user wants the new repo's first commit to read as
if the project was **always** called Vidshelf, with no trace it was ever
named anything else — so that quote was reworded to describe the literal
generically instead of reproducing the old name.

**Live container rename required care, not just a file edit**: changing
`docker-compose.yml`'s service name and `container_name` in one step means
`docker-compose down` no longer matches the *already-running* container
(compose resolves service names from the *current* file, and the running
container was started under the old name) — running it would silently do
nothing to the old container while the new service name sits unstarted,
and starting the new one first would then collide with the old container
still holding port 5000. Handled by stopping/removing the old container
directly by its literal Docker name first (`docker stop youtubearr &&
docker rm youtubearr`, not `docker-compose down`), *then* `docker-compose
up -d --build` to create the new `vidshelf` container.

**Verified live**: after the swap, `docker exec vidshelf df -h` confirmed
the CIFS mount was still the real NAS (unaffected by the container rename
— it's a named volume, not tied to the container's own name), login
worked, the dashboard served the new `Vidshelf` branding, and
`GET /api/plex/collections/status` returned real collection data —
i.e. the existing Plex OAuth connection and all synced collections
survived the rename intact.

**Cosmetic, not fixed**: the built image is still tagged
`youtubearr-vidshelf` (Docker Compose derives its default project-name
prefix from the *containing directory's* name — `D:\PlexProject\youtubearr`
in this checkout — not from the service name in the compose file). This
will resolve on its own once the new repo is cloned into a differently-named
directory; if it doesn't get a fresh checkout, add an explicit top-level
`name:` field to `docker-compose.yml` to control the project-name prefix
directly instead.

**If a future rename is ever needed again**: repeat this same sweep
(`git ls-files -z | xargs -0 grep -lI "<old-name>"`, case-insensitive)
rather than trusting an initial grep pass — the first attempt here missed
the `.env.example` comment and the docstring in `artwork_sync.py` until a
second full-repo sweep caught them.

## ADDED (2026-07-23): first-run experience for new users (dashboard checklist + login hint + a simpler Docker example)

Now that this is a public repo, first-time users get none of the
hand-holding a private/personal deployment implicitly had. Three small
additions to close that gap:

### Dashboard "Getting Started" checklist

New card in `templates/dashboard.html`'s `#page-dashboard`, above the
stats grid: three items (add a channel, download a video, optionally
connect Plex), each a clickable link to the relevant page
(`navigateTo('channels'|'music-videos'|'settings')`).

- `loadGettingStarted(channelCount, downloadsCount)` is called from the
  tail of the existing `loadDashboardStats()` (which already fetches
  channel/download counts for the stat cards — reused rather than adding a
  second round-trip), plus its own `fetch('/api/plex/config')` to check
  whether `token` is set.
- **Auto-hides once the two non-optional steps are done** (channel added
  + at least one download) — Plex stays optional by design (see README),
  so it doesn't gate auto-hiding on its own.
- **Manually dismissible** any time via a `✕` button, persisted in
  `localStorage` (`vidshelf_getting_started_dismissed`) — no backend
  state, so this is purely a per-browser preference, not tied to the
  account.

**Verified live**: confirmed the card's markup (`getting-started-card`,
`gs-step-channel`, `gs-step-download`, `gs-step-plex`) is served on
`/dashboard`. Auto-hide behavior wasn't separately visually verified in a
browser this session (this deployment already has channels and downloads,
so it auto-hides immediately, which is itself the expected/correct
behavior for an existing install) — if a fresh install ever shows the card
not appearing/disappearing correctly, check `loadGettingStarted()`'s early
`localStorage` check first (a stale `true` value from an unrelated earlier
dismiss would suppress it silently).

### Login page password hint

`templates/login.html` previously gave zero indication of *how* to get
the first-run admin password if `ADMIN_PASSWORD` wasn't set (see the
"replace hardcoded credentials" security fix earlier in this file) — a
new user would just see a blank login form with no clue to check
`docker compose logs`. Added a small permanent note below the login form
pointing at the `[SECURITY]`-prefixed log line and the relevant env vars.
Deliberately not conditional on anything (no way for the login page's
Flask route to know whether a password was auto-generated vs.
explicitly set without adding a new state check) — the note is accurate
and harmless to show either way.

### Simpler Docker Compose example in README

The repo's actual `docker-compose.yml` is tailored to this account's own
NAS/CIFS setup (`type: cifs` driver, `SYS_ADMIN` capability — see the
CIFS mount RESOLVED section above for why). A first-time user without a
NAS share would hit that complexity immediately with no simpler option
shown. Added a "Simple local-storage setup" section to `README.md`'s
Docker section with a minimal example swapping the `music_videos_final`
CIFS volume for a plain bind mount (`./music_videos:/app/music_videos_final`)
and no `cap_add`/`driver_opts`/`NAS_SMB_*` variables — those only matter
for the network-share case.

`ast.parse()` on all four touched Python files (`app.py`, `artwork_sync.py`,
`artwork_swap.py`, `downloader.py`) after every edit — syntax-valid.

**Verified live (2026-07-21)** against the real running container:
- `docker-compose up -d --build` recreated the container; `docker logs`
  showed `[SECURITY] No ADMIN_PASSWORD set — generated a random admin
  password on first run: '...'` (no `ADMIN_PASSWORD` was set in `.env`) and
  `Debug mode: off`.
- Logged in via `curl` with the generated password — `302` to `/dashboard`,
  session cookie set with `HttpOnly` + `SameSite=Lax`. Confirmed the new
  security headers (`X-Content-Type-Options`, `X-Frame-Options`,
  `Referrer-Policy`) are present on every response.
- 5 wrong-password attempts followed by a 6th attempt **using the correct
  password** was correctly rejected with "Too many failed login attempts" —
  confirms the lockout actually blocks valid credentials during the
  lockout window, not just invalid ones.
- `docker restart` (simulating any future restart) reused the persisted
  `_auth`/`_secret_key` from `config.json` with **no** new
  `[SECURITY] No ADMIN_PASSWORD set` line on the second boot — confirms
  credentials/secret key genuinely survive a restart now, unlike the old
  in-memory-only `USERS` dict.
- `GET /api/config` while authenticated returned real config content
  (`channels`, `plex`, `artwork_sync`, etc.) with **no** `_secret_key` or
  `_auth` keys present.
- `docker exec vidshelf cat /app/config.json` directly confirmed both
  `_secret_key` and `_auth.password_hash` are present on disk (as expected —
  only the API response is filtered, not the file itself).

## ADDED (2026-07-23): system health check, bounded download concurrency, Plex library confirmation

Three more first-run/reliability improvements, continuing the same theme.

### System Health panel (Settings page)

New checks, both read-only and side-effect-free:
- `transcode.check_dependencies()` — resolves `ffmpeg`/`ffprobe` the same
  way `_ffmpeg_bin()`/`_ffprobe_bin()` already do (respecting
  `FFMPEG_PATH`), confirms the binary actually **runs** (not just present
  via `shutil.which()` — a corrupt/non-executable binary would otherwise
  look identical to a healthy one), and parses the version string.
- `artwork_sync.check_title_card_dependencies()` — reports `_PIL_AVAILABLE`
  and whether any of the DejaVu font paths `_load_font()` already checks
  exist.

Exposed via `GET /api/system/health`, rendered as a simple ✅/❌ list on the
Settings page (`loadSystemHealth()`, called when the page loads). Most
useful for the **local (non-Docker) install path** — the Docker image
always bakes in ffmpeg/Pillow/fonts, so this mostly matters there if
`FFMPEG_PATH` is misconfigured; for a bare `pip install` setup, this turns
"why isn't conversion working" into a visible answer instead of a support
question.

**Verified live**: `GET /api/system/health` against the real container
returned `ffmpeg`/`ffprobe` v7.1.5 found at `/usr/bin/`, `pillow`/`fonts`
both `true` — matches what the earlier format-conversion work already
confirmed was installed.

### Bounded download concurrency

**Real gap, not theoretical**: `api_channels_download_all()` spawned one
raw `threading.Thread` per video with **no concurrency cap** — up to 20 at
once for a "Download All" click. Combined with automatic format
conversion (CPU/memory-heavy — see the `transcode.py` ADDED section
above), this is exactly what produced the real OOM-killed ffmpeg
conversions and contributed to the Docker Desktop crash documented
earlier in this file. Two download threads both encoding at once already
exceeded the container's memory limit once; 20 would guarantee it.

**Fix**: a single module-level `concurrent.futures.ThreadPoolExecutor`
(`_DOWNLOAD_EXECUTOR` in `app.py`, `max_workers` from
`MAX_CONCURRENT_DOWNLOADS` env var, default `2`) that every download entry
point submits to instead of spawning its own thread — `api_download()`,
`api_channels_download_all()`'s per-video loop, and
`api_music_videos_download()` all go through the same bounded pool now.
Excess submissions queue automatically (stdlib `ThreadPoolExecutor`
behavior — no custom semaphore logic needed).

**Also fixed as part of this**: pre-registering each download as
`'queued'` *before* submitting it to the pool, not after. Previously,
`_init_download()` (which sets the initial `'queued'` status) only ran
once `download_video()` itself started executing — with unbounded
threads that was immediate, so it was never noticed, but with a
2-worker cap, a video queued behind the first two wouldn't have appeared
in the progress UI **at all** until a worker actually picked it up
(`get_active_downloads()` only returns entries that have been
`_init_download()`-ed). Added `downloader.queue_download(video_id, title,
channel_url, final_path)` — generates the `download_id` and calls
`_init_download()` immediately, returning the ID to pass through to
`download_video(..., download_id=download_id)` (which already supported
an externally-supplied ID). All three download routes now call this
before `_DOWNLOAD_EXECUTOR.submit(...)`, so a 20-video "Download All"
shows all 20 as `queued` immediately, with 2 transitioning to
`downloading` at a time as the pool works through them.

Minor known side effect, not fixed: `download_video()` still
unconditionally re-calls `_init_download()` internally once it actually
starts (resetting `started_at` to that later time) — harmless
(`get_active_downloads()` sorting by `started_at` just reflects "when did
this actually start downloading" rather than "when was it queued", a
reasonable reading either way), not worth restructuring further for.

**Not stress-tested with real concurrent downloads** this session (would
have meant deliberately queuing multiple real videos against the live
NAS) — verified via container startup (no import/initialization errors
from the new `ThreadPoolExecutor` or the `queue_download` import) and
code review of all three call sites, not an actual multi-download queue
depth check. If downloads ever seem to silently stop progressing past 2
at a time with more expected to be running, check
`_DOWNLOAD_EXECUTOR._max_workers` / the `MAX_CONCURRENT_DOWNLOADS` env var
first — that's the cap working as designed, not a hang.

### Plex library confirmation step

**Real gap, already hit once**: `api_plex_discover_library()` guessed a
library from its **title** containing "music video" and saved whatever it
picked immediately, with no confirmation - exactly how the "Muisc Videos"
typo (see Bug C in the Plex OAuth/collections section above) silently
pointed collection sync at an unrelated library before. Re-running
discovery fresh against this account's real server *right now*, live,
during this session, picked **"4KMovies"** — a movies library — instead
of "Muisc Videos" (the actual, typo'd, currently-correctly-configured
library) confirming this isn't a hypothetical: the exact same class of
silent-wrong-guess is still one click away for any new user connecting
Plex for the first time.

**Fix**: new `plex_list_libraries(config)` in `artwork_sync.py` — lists
*every* library on the server (not just the auto-picked one), flagging
which one `plex_find_library_key()` would choose
(`is_auto_discovered: true`). Exposed via `GET /api/plex/libraries`
(read-only — confirmed live it doesn't touch `config.json`). The
Settings page's "Step 3" now calls this into a `<select>` dropdown
(auto-discovered entry pre-selected, labeled "— suggested") instead of
auto-discovering-and-saving in one step; saving is a separate explicit
action (`savePlexLibrary()` → existing `POST /api/plex/config`, which
already accepted `music_video_library_key` — no new save endpoint
needed).

`api_plex_discover_library()` (the old auto-save endpoint) is left in
place for API compatibility but the dashboard no longer calls it directly.

**Verified live against the real Plex server**: `GET
/api/plex/libraries` correctly listed all 10 libraries with
`"4KMovies"` flagged `is_auto_discovered: true` and `"Muisc Videos"`
(key 14, the actually-correct one) flagged `false` — confirming both that
the endpoint works and that this feature would have caught the exact
real misconfiguration this account already has a documented history of
hitting. Confirmed the saved `config.json` `music_video_library_key`
(still `"14"`) was unaffected by just listing.

## ADDED (2026-07-25): README screenshots

Added a "Screenshots" section to `README.md` (dashboard, channels,
downloads, music video finder, artists, swap-artwork, create-collection),
images checked into `screenshots/`. Excluded from the Docker image via
`.dockerignore` (`screenshots/`, `README.md`) — they're docs-only, no
reason to bloat the image.

**How the screenshots were captured** (no project-specific run skill
existed for this yet — `chromium-cli` wasn't installed, so this used
`npx playwright` directly against the already-running `vidshelf`
container): a small Playwright script (`chromium.launch()` headless,
1440×900 viewport) logged in via the real `/login` form, navigated the
sidebar's `data-page` links, and screenshotted each page. Two gotchas
worth remembering if this is redone:
- **Fixed `page.waitForTimeout(N)` after a nav click is unreliable** —
  the Channels page's `data-page="channels"` link resolves before its
  `fetch('/api/channels')` populates `.video-card` elements, and the
  Music Videos search takes several seconds (yt-dlp + ranking) before
  results replace the spinner. Wait on the actual DOM state instead:
  `page.waitForSelector('.video-card')` for Channels,
  `page.waitForFunction(...)` polling the search button's text for
  Music Videos, then `page.waitForLoadState('networkidle')` before the
  screenshot so YouTube thumbnail images finish loading (otherwise they
  screenshot as solid black boxes).
- **Relative screenshot paths resolve against the shell's cwd, not the
  script's directory** — running `node shoot.js` from a different
  directory than intended silently wrote a stray `shots/` folder into
  this repo's root instead of the scratchpad. Always confirm with `ls`
  after the first run, or use an absolute path in the script.

**Privacy pass before committing anything**: the Channels page shows
real monitored YouTube channel names/URLs and a real local download
path — genuinely identifying, unlike the Artists/Downloads pages which
only show public band/song names. Rather than pixel-editing the PNG
after the fact, the channel data was redacted **in the live DOM** via
`page.evaluate()` (replacing the `.video-card` name/URL/path text with
placeholders) immediately before that one screenshot — cleaner than
blurring since the layout/styling stays pixel-accurate.

**Also confirmed live (a genuine, if minor, security-audit win)**: the
user's first guess at admin credentials was the old pre-fix default
(`admin`/`adminadmin`, see the C2/H1 security audit above) — it was
correctly rejected with "Invalid username or password.", confirming
that fix is actually active on this running container and not just
documented.

**Artists page stands in for "what a Plex collection looks like"**: a
live Plex Web UI screenshot was deliberately not attempted — the
already-authorized OAuth token in `config.json` could have been used to
open an authenticated Plex Web session without a fresh login, but doing
so would expose whatever else is in the user's live Plex library beyond
just this app's collections, which is more exposure than the ask
warranted. The in-app Artists page (artist name + artwork + video
count) is the source of truth for what becomes a Plex collection here
anyway, so it was captioned as such instead.

## RELEASED (2026-07-25): v1.0.0 — first stable release

Preceded by a diff-scoped security review (`git diff origin/main...dev`,
the two commits ahead of `main` at the time: system health check +
bounded concurrency + Plex library confirmation, and the README
screenshots commit). Methodology: an independent sub-agent searched for
new vulnerabilities against the categories a security engineer would
check (injection, auth bypass, secrets, SSRF, XSS, unsafe
deserialization), explicitly scoped to what the diff *introduced* —
not the pre-existing, already-documented findings in the "SECURITY
AUDIT (2026-07-20)" section above. **Result: no findings met the
≥0.8-confidence bar.** Specifically verified clean: the two new routes
(`/api/system/health`, `/api/plex/libraries`) follow the same
session-check pattern as every other route; `download_id` is never
used as a filesystem path or subprocess argument; the new dashboard.html
JS routes every server string through the existing `escapeHtml()`
helper (and is actually *stricter* than the code it replaced — the old
`discoverPlexLibrary()` interpolated `data.message` into `innerHTML`
unescaped; that path no longer exists after this diff).

**Release mechanics** (see `CLAUDE.md`'s "Branching & release workflow"
— followed exactly, no deviations): bumped `VERSION` to `1.0.0` on
`dev` (was `0.1.0`), pushed; merged `dev` → `main` with an explicit
`--no-ff` merge commit (`557b6d3`) rather than a fast-forward, so
`main`'s log shows one merge commit per release instead of looking like
linear direct-to-main work; tagged that merge commit `v1.0.0`; pushed
`main` and the tag; published a GitHub Release from the tag via `gh
release create` with a summary of highlights + the security posture.
`dev` was left checked out afterward as the active branch, per the
"day-to-day work happens on dev" convention.

**If picking up new work after this point**: `main` is now at the
`v1.0.0` tag; `dev` is even with `main` (no unmerged commits either
direction) until the next feature/fix lands. Confirm with `git log
origin/main..dev --oneline` (should be empty right after a release) if
that's ever in doubt.

## REWROTE (2026-07-27): all commit authorship reassigned to the `andysom25` GitHub account — every SHA in this document before this section is dead

**The problem**: every commit in the repo was authored and committed as
`Andrew Someillan <andrew.someillan@ceretax.com>` — the *work* email.
GitHub has that address verified on a **different** account,
`fpasomeillan`, so despite the repo living at
`github.com/andysom25/Vidshelf`, GitHub attributed all 7 commits to
`fpasomeillan`. The commits looked correct locally (`git log` showed the
right human name); the mismatch was only visible on GitHub's side.
Diagnose this with:

```bash
gh api 'repos/andysom25/Vidshelf/commits?sha=dev&per_page=100' \
  --jq '.[] | "\(.sha[0:7])  \(.author.login // "*** UNATTRIBUTED ***")  \(.commit.author.email)"'
```

`.author.login` is GitHub's *resolved* account (email → verified-account
lookup), which is what the web UI shows — it is not the same thing as
`.commit.author.email`. Comparing only the local `git log` identity will
never surface this class of bug. Note the `?sha=<branch>` parameter: the
default lists the default branch only, so run it once per branch.

**The fix**: rewrote every commit as
`andysom25 <andrew.someillan@gmail.com>` — the personal address, which is
verified on the andysom25 account (confirmed empirically: all commits
report `author.login: andysom25` after the push).

**Where the identity lives**: this repo carries a `--local` override; the
machine's `--global` default is the *work* identity
(`Andrew Someillan <andrew.someillan@ceretax.com>`, which GitHub resolves
to `fpasomeillan`). That direction is deliberate and was corrected once
after an initial pass got it backwards: **work inherits by default,
personal is opt-in per repo.** A work commit accidentally landing under
the personal account is the worse of the two failure modes, so the
default is the conservative one. Do not globalize the personal identity.

Confirm which one a given repo will actually use with
`git var GIT_AUTHOR_IDENT` — it reports the *resolved* identity after
local/global precedence, unlike `git config --global user.email`, which
tells you nothing about what an individual repo overrides.

### Mechanics, and the two steps that are easy to get wrong

1. Rewrote all commits with `git filter-branch --env-filter` setting all
   four of `GIT_AUTHOR_NAME/EMAIL` and `GIT_COMMITTER_NAME/EMAIL`
   (setting only the author pair leaves the committer wrong, and GitHub
   surfaces both). Scoped `-- --branches --tags` — deliberately *not*
   `-- --all`, which also rewrites `refs/remotes/*` and desyncs the
   tracking refs from what the remote actually has. `git-filter-repo` is
   not installed on this machine; `filter-branch` is deprecated but fine
   at this scale.
2. **Recreated the annotated `v1.0.0` tag by hand.** `--tag-name-filter
   cat` re-points the tag at the rewritten commit but **preserves the
   original tagger identity**, so the tag object still read
   `andrew.someillan@ceretax.com` after filter-branch reported success.
   Fixed with `GIT_COMMITTER_DATE=... GIT_COMMITTER_NAME=...
   GIT_COMMITTER_EMAIL=... git tag -f -a v1.0.0 main -F <msgfile>`,
   passing the original tagger date (`2026-07-25T20:38:38-04:00`) so the
   tag doesn't silently jump forward to the rewrite date. Verify with
   `git for-each-ref refs/tags/v1.0.0 --format='%(taggername) %(taggeremail)'`
   — `git log` will not show you this.
3. Force-pushed `main` and `dev` (`--force-with-lease`) and the tag
   (`--force`).

**Verified**: all commits on both `dev` and `main` report
`author.login: andysom25`, with zero `UNATTRIBUTED` entries; the tag's
tagger field matches; `git diff <old-tip> <new-tip>` is empty (content
byte-identical, only metadata changed); the v1.0.0 GitHub Release
survived the tag force-update intact — it tracks the tag *name*, not the
old object, so it re-pointed automatically. Do **not** delete and
recreate the tag to "refresh" the release; that loses the release notes.

**Note on the noreply alternative**: the first pass of this rewrite used
`7364828+andysom25@users.noreply.github.com` (the `7364828` prefix is the
numeric user ID from `gh api users/andysom25 --jq .id`). That form is
guaranteed to resolve to the account and keeps a real address out of a
public repo's history, so it's the safer default if the personal address
is ever un-verified from the account. It was replaced with the plain
gmail address by preference. Both were confirmed to attribute correctly.
If you ever switch back, the whole procedure above applies unchanged —
only the email string differs.

### SHA mapping

Every SHA cited in the sections *above* this one refers to the original,
now-unreachable history:

| original | current | commit |
| --- | --- | --- |
| `104dade` | `e09138c` | Initial commit |
| `8c2421b` | `8501ac4` | first-run onboarding |
| `b59d56d` | `39045bb` | health check / bounded concurrency / Plex confirmation |
| `f6c9a2c` | `685b606` | README screenshots |
| `09c528c` | `e3747be` | Bump version to 1.0.0 |
| `557b6d3` | `6f9d8d0` | Release v1.0.0 (merge, `main` tip, tagged) |
| `63bf77b` | `305883e` | Document the v1.0.0 release cut |
| — | `938dea9` | Document this rewrite (`dev` tip, created post-rewrite) |

**If this ever regresses** (new commits showing the wrong author): check
`git config user.email` *in that repo* first — a stale `--local` value
silently overrides the global default, and that's far more likely than
anything about the history above. Then re-run the `gh api ...
.author.login` one-liner to confirm what GitHub actually resolved.

**Note for anyone with an older clone**: the pre-rewrite history is gone
from the remote. A plain `git pull` on a stale clone will try to merge
the two disjoint histories and produce a duplicated log. Re-clone, or
`git fetch origin && git reset --hard origin/dev`.

## FIXED (2026-07-27): state files could lose data three different ways — lost updates, torn writes, and a fresh-install crash loop

Three bugs, one root cause: state was written with `open(path, 'w')` +
`json.dump()` onto files that were bind-mounted individually. Shipped as
v1.1.0 along with the first published Docker image.

### Bug 1 — lost updates in the download tracker (the one users would notice)

`mark_video_downloaded()` did an unlocked read-modify-write:

```python
tracker = load_downloaded_tracker()   # read
tracker[channel_url].append(video_id) # modify
save_downloaded_tracker(tracker)      # write
```

It runs on `_DOWNLOAD_EXECUTOR`, the bounded pool added for concurrent
downloads. Two downloads finishing close together both load the same tracker,
each appends only its own video, and the second write discards the first. The
dropped video then looks new on the next channel check and **gets downloaded
again** — which would have surfaced as "Vidshelf keeps re-downloading things I
already have", with essentially no chance of reproducing it on demand.

This is the same class of bug as the duplicate-collections race already
documented above — that one was fixed with a lock; this instance was missed.

Reproduced with the pre-fix code, 12 threads × 40 updates: **8 of 480 entries
survived.** At the default `MAX_CONCURRENT_DOWNLOADS=2` the real-world rate is
far lower, but it is not zero. `tests/test_state.py` is the regression guard.

### Bug 2 — torn writes on any interruption

`open(path, 'w')` truncates *immediately*, so a crash or `docker compose down`
between truncate and final byte leaves a truncated or empty file. For
`config.json` that's every channel, the Plex token, and the secret key that
signs sessions.

### Bug 3 — v1.0.0 could not be installed by following its own README

The severe one. `app.secret_key = _get_or_create_secret_key()` runs at
**import time** (app.py), and `_read_raw_config()` caught only
`FileNotFoundError` and `json.JSONDecodeError`. Then:

1. `config.json` is gitignored, so a fresh clone doesn't have it.
2. `docker compose up` finds a missing bind-mount source and **creates it as a
   directory** — that's just what Docker does.
3. `open()` on a directory raises `IsADirectoryError`, an `OSError` that is
   *not* a `FileNotFoundError` — uncaught, at import.
4. Container crash-loops. Every new user, 100% of the time.

It went unnoticed because every development machine already had the three JSON
files from before they were gitignored.

### The fix: `state.py`, and why state had to move to a directory

New module owning where state lives and how it's written: atomic writes (temp
file in the same directory → `flush` → `fsync` → `os.replace`), one reentrant
lock per file, `update_json()` holding that lock across a whole
read-modify-write, and readers that tolerate a missing/corrupt/directory path
by returning `{}`.

**The trap**: adding atomic writes *without* moving state would have made
things worse. Docker bind-mounts a single file by **inode**, and `os.replace()`
swaps the inode — so the container would write to a new inode the host mount
doesn't follow, writes would silently stop reaching the host, and the next
restart would read stale state. Exactly the "looked fixed on paper" shape as
the CIFS bugs above. So the three file mounts collapsed into one directory
mount:

```yaml
- ./data:/app/data      # was: ./config.json, ./active_downloads.json, ./downloaded_videos.json
```

which also fixes bug 3 for free, since a directory is what Docker
auto-creates correctly.

**Migration runs at `state` import, deliberately** — not from a startup hook.
Both `app.py` (`app.secret_key`) and `artwork_sync.py` (`PLEX_CLIENT_ID`) read
config at *module* level, so there is no hook early enough. Migrating one
import too late would read an empty config, generate a fresh secret key, and
log every existing session out. It's idempotent, and never overwrites a file
already at the destination.

`artwork_sync.py` needed fixing separately: it hardcoded a bare
`'config.json'` relative path. Left alone, it would have created a *second*
config file in the working directory, so the Plex client ID persisted for
OAuth would never be the one `app.py` reads back and every restart would look
like a new device to Plex. Its other two JSON writes are per-artist sidecars
inside the media folders (on the NAS) — deliberately **not** converted, since
`os.replace` semantics on CIFS aren't reliable.

### Windows-only wrinkle found by the tests

`os.replace()` fails with `PermissionError [WinError 5]` if the destination is
open by *anyone*. POSIX doesn't care, so the container is unaffected, but
README option 3 supports running directly on Windows. Two mitigations:
`read_json()` takes the same per-file lock as `write_json()` (so the app's own
readers can't collide), and `_replace_with_retry()` absorbs short-lived
external holders — an editor, `tail`, an antivirus scanner.

Worth noting how this was found: the first version of the atomicity test
reported **PASS while the writer thread was dying on every iteration** — a
dead writer produces zero torn reads. Any concurrency test needs to assert
that the worker actually completed its work, not just that no corruption was
observed. The committed test asserts `len(writes_done) == 150`.

### How to verify

```bash
python tests/test_state.py          # 6/6, no dependencies needed
docker compose down && docker compose up -d --build
docker exec vidshelf ls -la /app/data    # config.json + both trackers
docker exec vidshelf df -h               # still check the CIFS mount is real
```

### What to check first if state ever looks wrong again

1. `docker exec vidshelf ls -la /app/data` — if it's empty but the app is
   running, the compose file probably still has the old per-file mounts.
2. Whether anything writes state without going through `state.py`. A bare
   `open('config.json', 'w')` anywhere is the bug returning; `grep -rn
   "open(.*config.json" *.py` should only match `state.py`.
3. `VIDSHELF_DATA_DIR` — if someone pointed it at a CIFS/SMB mount, atomic
   replace is unreliable there (see the CIFS sections above). It's documented
   in `.env.example` as unsupported for exactly that reason.

---

## ADDED (2026-07-27): CI + published multi-arch Docker image

`.github/workflows/ci.yml` — the repo's first CI. Two jobs:

- **test** (every push/PR to `main`/`dev`): runs `tests/test_state.py` and
  imports every module. The import check is deliberate: bug 3 above was an
  import-time failure, which no route-level test would have caught.
- **publish** (tags matching `v*` only, gated on `test` passing): builds
  `linux/amd64` + `linux/arm64` via QEMU/buildx and pushes to
  `ghcr.io/andysom25/vidshelf`, tagged `{version}`, `{major}.{minor}`,
  `{major}`, and `latest`.

arm64 is not optional for this audience — a large share of self-hosted Plex
runs on Synology or a Raspberry Pi, where an amd64-only image simply won't
start.

**Gotcha baked into the workflow**: the image name is hardcoded lowercase
rather than derived from `${{ github.repository }}`. The repo is
`andysom25/Vidshelf` with a capital V, and GHCR rejects uppercase image names
with an opaque `invalid reference format`.

Publishing only on tags matches the branching rules in `CLAUDE.md` — `main` is
releases-only, and an image built from an untagged commit has no meaningful
version to carry.

README's quick start now leads with `docker pull` instead of clone-and-build;
building from source moved to option 2.

## ADDED (2026-07-27): visible version badge + opt-out update check

Shipped as v1.2.0. Briefly tagged v1.1.1 first, then retagged: this is a
feature, and `CLAUDE.md`'s semver rule makes that a minor bump, not a patch.
The v1.1.1 tag and its GitHub release were deleted; its release *merge commit*
is still in `main`'s history (untagged), because rewriting a pushed `main`
to erase it would be far more disruptive than an orphaned merge commit.
The `1.1.1` container image tag also still exists in GHCR — deleting package
versions needs a token scope the local `gh` doesn't have. Neither is
referenced by anything.

### What already existed

`APP_VERSION` was already read from the `VERSION` file (app.py) and already
returned by the stats endpoint, and the Settings page's System Health panel
already rendered it. The problem was purely placement: it only appeared in a
panel nobody opens unless something is already broken, so "what version are
you on?" was still the first question on any bug report.

### The badge

Sidebar footer, under Sign Out — always visible on every page. Shows
`Vidshelf v1.2.0`, plus a red `v1.3.0 available` pill linking to the release
notes when behind. The pill is `display:none` unless an update actually
exists, so the normal state is one quiet grey line.

Populated by `loadVersionBadge()` from `/api/system/version` on page load.
Fire-and-forget: a failed fetch leaves the badge as-is rather than showing an
error, because a cosmetic version label is never worth an error banner.

### The update check (`updates.py`)

Queries `https://api.github.com/repos/andysom25/Vidshelf/releases/latest`.
Design constraints, all of which are load-bearing:

- **Server-side, not from the browser.** A `fetch()` from the dashboard would
  leak every user's IP to GitHub and break on networks that can't reach it.
- **Never blocks a page load.** `get_status()` always returns the cached
  answer immediately; if the cache is stale it starts a background thread and
  serves the old value meanwhile. Tested explicitly — the stale-cache test
  asserts `get_status()` returns in under 200ms against a fetch that sleeps
  300ms.
- **Cached 24h, persisted to `data/update_check.json`.** GitHub allows 60
  unauthenticated requests/hour/IP; one per day per install makes that
  irrelevant. Persisting matters because a container restart loop would
  otherwise become a request loop.
- **Failures are cached too**, including HTTP 403 rate-limiting, so a failing
  check backs off instead of retrying into the limit.
- **Notifies, never updates.** Deciding when to pull an image is the
  operator's call.
- **Opt-out toggle** in Settings → System Health, `update_check_enabled` in
  config, default on. Off means the app makes no outbound request of its own.

`/releases/latest` is used rather than `/releases` because GitHub already
excludes drafts and prereleases from it — a tagged beta can't be advertised
as an upgrade.

### Two bugs avoided, both covered by tests

**Lexicographic version comparison.** `'1.10.0' < '1.9.0'` as strings, so a
string compare silently stops advertising updates exactly when the minor
version goes double-digit. `parse_version()` returns an int tuple;
`test_numeric_not_lexicographic_comparison` pins it.

**A leaked in-flight flag.** `_refresh()` clears `_refreshing` in a `finally`.
Without that, one unexpected exception in the background thread would leave
the flag set and **no update check would ever run again for the life of the
process** — a permanently silent failure with no symptom.
`test_refresh_flag_is_released_even_if_fetch_raises` guards it.

Also handled: `APP_VERSION == 'unknown'` (VERSION file missing from the
image) never reports an update, since it's noise the user can't act on.

### How to verify

```bash
python tests/test_updates.py    # 9/9, no network touched (fetch is stubbed)

# against real GitHub, inside the container:
docker exec vidshelf python -c "
import updates
for cur in ['1.0.0', '1.2.0', 'unknown']:
    print(cur, updates.get_status(cur, enabled=True))"
```

Expect `1.0.0` to report an update, the current version not to, and
`unknown` not to. The cache lands at `data/update_check.json`.

### If it misbehaves

- Badge stuck / no update shown after a release: delete
  `data/update_check.json` to force a refetch — the 24h TTL is the usual
  explanation, not a bug.
- Never updates at all: check `update_check_enabled` in `data/config.json`,
  then that the container has outbound HTTPS.
- Wrong "update available" after a manual build: expected if `VERSION` on a
  dev build is behind the latest release. Comparison is against the release
  tag, not the image.

## CHANGED (2026-07-27): pinned dependencies, extracted the dashboard's CSS/JS, added route smoke tests

Shipped as v1.2.1. Maintenance only — no user-visible behaviour changes,
hence a patch rather than a minor.

### Dependencies are pinned exactly

`requirements.txt` was four `>=` ranges. That meant a user's
`docker compose up --build` could pull a different Flask or yt-dlp than
anything ever tested, and their bug report was unreproducible because there
was no way to know what they actually had. Now pinned `==`, including
transitives — Werkzeug especially, which has shipped breaking changes inside
a Flask major, so leaving it floating would have defeated the point.

The pins are the set verified running in the container, not whatever was
newest on PyPI.

**The tension this creates**: yt-dlp *needs* to stay fresh — YouTube changes
break extraction every few weeks, so an old pin is its own bug. Resolved with
`.github/workflows/bump-yt-dlp.yml`: weekly (and on demand via
`workflow_dispatch`, for when YouTube breaks mid-week), it finds the latest
yt-dlp on PyPI, applies the pin, installs it, runs all three test suites plus
an import check, and **only then** opens a PR against `dev`.

It opens a PR rather than pushing, deliberately: a yt-dlp release can itself
be broken, and auto-merging into the branch releases are cut from is how a
bad extractor reaches a published image. Note the tests do not exercise real
extraction against YouTube, so a suspicious release still wants one manual
download through the UI before merging.

### dashboard.html: 3,425 lines -> 611

The template was 161KB, but only ~600 lines of it were markup: 662 lines of
CSS and 2,149 lines of JS were inlined. Extracted to
`static/css/dashboard.css` and `static/js/dashboard.js`.

Two things that had to be checked first, either of which would have made this
a silent breakage:

1. **Jinja syntax inside the blocks.** A `{{ ... }}` in inlined JS stops being
   interpolated the moment it moves to a static file, and fails at runtime,
   not at build. Verified none of the five blocks contained any before
   moving.
2. **Execution order.** There were *four* consecutive `<script>` blocks, not
   one. Later blocks call into earlier ones, and some run `getElementById` at
   top level, so they need the DOM already parsed. They were contiguous with
   no markup between them and sat immediately before `</body>`, so
   concatenating them in order and loading one file from the same position
   preserves both the ordering and the DOM guarantee. The block boundaries are
   marked with comments in the output file.

On caching: Flask serves these with `Cache-Control: no-cache` plus an ETag, so
repeat loads revalidate and get a 304 rather than being cached outright. Still
a real saving over re-sending 160KB inside every page render, and it means an
upgrade can never serve a stale asset — which is worth more here than
aggressive caching, since there is no cache-busting in the asset URLs.

`app.py` (2,076 lines) and `artwork_sync.py` (2,032) were deliberately **not**
split in this release — see below.

### tests/test_routes.py — 7 tests over all 53 routes

Not functional tests; they assert what breaks silently during a refactor:

- every expected route is still registered, and the count hasn't collapsed
- no route 500s on a plain authenticated GET
- **every non-public route rejects an unauthenticated caller** — a standing
  security invariant, not just a refactor guard. Public by design:
  `login`, `logout`, `static`, and `favicon` (an inline SVG that browsers
  fetch on the login page before any session exists).
- the dashboard still renders *and* its static assets resolve. This one
  matters specifically because of the extraction above: a broken `url_for()`
  produces a blank-looking page that still returns HTTP 200, which no
  status-code check would catch.

Writing them surfaced two wrong assumptions in the tests themselves (not
bugs): `/api/downloads` doesn't exist — it's `/api/downloads/progress` — and
`/favicon.ico` is intentionally public.

### Why app.py wasn't split into blueprints

It's the obvious next step and it was explicitly deferred. 50 routes share
module-level state (`_DOWNLOAD_EXECUTOR`, `_CONVERSION_STATE`, the config
helpers), and rehoming those is the risky part — a route quietly failing to
register is invisible until someone clicks the button that needs it.

`tests/test_routes.py` is the prerequisite that makes that refactor safe, and
it now exists. The route-registration test is what would catch an endpoint
going missing during the move. Natural split, by current route counts:
plex (13), channels (5), artwork (5), system (4), downloads (4),
conversion (3), artists (3), music-videos (2).

### How to verify

```bash
python tests/test_state.py && python tests/test_updates.py && python tests/test_routes.py

docker compose up -d --build
docker exec vidshelf ls -la /app/static/css /app/static/js   # assets in the image
docker exec vidshelf pip freeze | grep -E 'Flask|yt-dlp'     # pins actually applied
curl -s localhost:5000/static/css/dashboard.css | wc -c      # ~14000, not 0
```

Note `curl -o /dev/null -w '%{size_download}'` reports 0 for these under Git
Bash on Windows even when the transfer is fine — pipe to `wc -c` instead
before concluding an asset is empty.

### If the dashboard renders unstyled or dead

1. `curl localhost:5000/static/css/dashboard.css` — a 404 means the `static/`
   directory didn't make it into the image; check `.dockerignore`.
2. Browser console for a JS error in `dashboard.js` — if it's a
   `{{ something }}` appearing literally, a Jinja expression got moved into
   the static file and needs to come back into the template (or be passed in
   via a `data-` attribute).
3. `python tests/test_routes.py` — `test_dashboard_renders_with_its_static_assets`
   covers exactly this.

## KNOWN COSMETIC ODDITY: three commit subjects start with a stray `@`

Three doc-only commits from 2026-07-27 read:

```
@ Document the commit-authorship rewrite to the andysom25 account
@ Correct the authorship write-up: gmail address, refreshed SHA map
@ Correct the identity-scoping note: work is the global default
```

**This is not corruption and nothing is missing.** The commit bodies are
intact; there is just an extra line containing a single `@` before the real
subject, which git folds into the subject when it renders. Some inner single
quotes were also stripped from those three bodies (`open(path, 'w')` reads as
`open(path, w)`).

Cause: the messages were passed using PowerShell here-string syntax
(`-m @'...'@`) to a tool running Git Bash. Bash parses that as three
concatenated tokens — a literal `@`, a single-quoted string, and another `@` —
rather than as a here-string, so the `@`s ended up inside the message and the
quotes were consumed as shell quoting. Use `git commit -F <file>` for
multi-line messages instead; it sidesteps shell quoting entirely.

**Deliberately not fixed.** All three commits are ancestors of the v1.1.0,
v1.2.0 and v1.2.1 tags, so correcting them would mean rewriting every commit
since, deleting and recreating all three tags, force-pushing `main` and `dev`,
re-pointing three published GitHub Releases, and breaking every existing
clone — to fix a cosmetic prefix on three documentation commits. Not worth it.

If history ever gets rewritten for some other reason, fold this in then:
`git filter-branch --msg-filter 'sed "/^@$/d"' <range>` removes the stray
lines.

## ADDED (2026-07-29): Artists page search, filters and sorting

Shipped as v1.3.0 (new feature -> minor, per `CLAUDE.md`'s semver rule).

The Artists page rendered every artist as one flat, always-alphabetical list.
With 23 artists locally that's already awkward; the list only grows.

### What was added

A filter bar above the list: a search box (with a clear button), three
selects, a Reset button, and a live "N of M artists" summary.

- **Search** — case-insensitive substring, matched against the display name
  *and* the folder name. Both, deliberately: `folder_to_artist()` rewrites
  folder names into display names, so someone who has been looking at the
  filesystem searches for what they saw there ("Bjork") and someone looking at
  the UI searches for what it shows ("Björk").
- **Artwork** — any / has artwork / missing artwork. The useful one for
  spotting gaps to fix with the artwork tools.
- **Videos** — any / has videos / empty folders. Empty folders are usually
  leftovers from a failed download.
- **Sort** — name A-Z, name Z-A, most videos, fewest videos.

### Client-side, on purpose

`/api/artists/summary` walks every artist directory and counts video files in
each — real I/O against what is usually a CIFS mount. So the list is fetched
once per page visit into `_artistsAll` and filtered in memory. Filtering
server-side would have meant re-walking the mount on every keystroke.

Consequences worth knowing:

- The search input is debounced 120ms. Not for the filtering, which is
  trivial, but for the DOM rebuild.
- Expanded rows survive re-rendering, via `_expandedArtists`. Only rows whose
  video list is already in `_artistVideosCache` are restored — re-fetching
  every open row on each keystroke would fire exactly the burst of directory
  walks this design avoids.
- `filterAndSortArtists()` must not sort its input in place. `_artistsAll` is
  the cached source of truth, and mutating it would make the unsorted order
  depend on whichever sort ran last. There is a test for this.

### Sorting ignores leading articles, matching Plex

"The Beatles" sorts under **B**, not T. This came out of a test expectation
rather than the original implementation: the first version sorted on the raw
name, and the disagreement turned out to be a real design question, not a bad
test. Plex files the same library that way, and this tool exists to feed Plex.
Verified against the real library: `The Dead Weather` now sorts between
`Bjork` and `Death Cab for cutie`; without it, that plus `The Killers` and
`The Raconteurs` clump uselessly at the end.

`/^(the|an|a)\s+/i` — the `\s+` is load-bearing. Without it "a-ha" would be
read as the article "a" and sort under "-ha". There's a test for that too.

Sorting is also `localeCompare` with `sensitivity: 'base'`, so accented names
sort where a reader expects instead of after Z by code point — a music library
is full of them.

Video-count sorts break ties by name so the order is stable between renders
rather than depending on the engine's sort.

### Two distinct empty states

"No artists yet" (library genuinely empty) and "No artists match these
filters" (library has content, filters exclude it all). The second offers a
Reset button, because the alternative is a user who thinks their library
vanished.

### Tests

`tests/test_artists_filter.js` — 23 assertions run under node against the
**real** `static/js/dashboard.js`, not a copy, so they can't drift from what
the browser loads. dashboard.js is a browser script with top-level DOM access,
so the test stubs a minimal `document`/`fetch` and ignores the expected throw
from the init code: JS hoists all function declarations before executing any
of the script, so every function is defined even though execution aborts. That
is what makes the pure helpers testable without restructuring working UI code.

Wired into CI as a separate "Run front-end tests" step (plus
`node --check`). No `setup-node` needed — `ubuntu-latest` already has node.

`tests/test_routes.py` gained
`test_artists_page_has_its_search_and_filter_controls`, which checks the
wiring specifically: the controls live in the template, their styling in
dashboard.css and their handlers in dashboard.js, so a rename in one and not
the others yields a filter bar that renders and silently does nothing. No
error, no bad status code, just a dead control.

### If the filters stop working

1. Browser console. A `renderArtistList is not defined` means the template
   references a handler the JS no longer has — `python tests/test_routes.py`
   catches that case.
2. Filtering the list but rows won't expand: check `_expandedArtists` and
   `_artistVideosCache` are still in sync in `toggleArtistRow()`.
3. Sort order looks wrong for a specific name: it's probably the article
   stripping doing its job. `node tests/test_artists_filter.js` documents the
   intended order.

## ADDED (2026-07-29): releases cut automatically when VERSION changes on main

Merging PR #1 exposed the gap this closes: the merge landed on `main` with
`VERSION` already at 1.3.0, CI passed — and nothing was released. No tag, no
GitHub release, no image. `main` sat with an untagged commit, breaking
`CLAUDE.md`'s own "every commit on main corresponds to a tagged release" rule,
and `:latest` still pointed at v1.2.1. Silent, because every check was green.

### The trap that dictates the design

**A tag pushed with `GITHUB_TOKEN` does not trigger other workflows.** GitHub
suppresses that to prevent recursive runs. So the obvious implementation — a
workflow that tags `main` and lets the existing tag-triggered publish job pick
it up — creates tags and releases while **never publishing an image again**.
Green ticks, no artifact. Exactly the failure shape the rest of this document
is full of.

So tagging *and* publishing happen in **one workflow run**. Nothing depends on
a cross-workflow trigger. The suppression then works in our favour: the tag
this run pushes doesn't start a second build of the same commit.

Second constraint: `docker/metadata-action`'s `type=semver` patterns read the
git **ref**, so they produce nothing on a branch push. Image tags are now
derived from the `VERSION` file via `type=raw` instead, which also makes
`VERSION` the single source of truth for both paths.

### How it works

`ci.yml` gained a `version` job that resolves three things:

| Situation | `publish` | `tag_needed` |
| --- | --- | --- |
| push to `main`, `v$VERSION` not tagged | true | true |
| push to `main`, `v$VERSION` already tagged | false | false |
| a `v*` tag pushed by hand | true | false |
| push to `dev`, or any PR | false | false |

Then `publish` (gated on `needs.version.outputs.publish`) builds and pushes the
multi-arch image, and only afterwards creates the tag and release.

**Ordering is deliberate**: image first, then tag. A failed build leaves no tag
and no release announcing an artifact that doesn't exist. Re-running after a
fix still works, because the `version` job decides from whether the tag exists,
not from anything about the previous run.

**Idempotent.** A docs-only merge that doesn't touch `VERSION` releases
nothing. Bumping `VERSION` twice in one PR still produces one release.

### Guards worth knowing about

- **VERSION must be semver.** A malformed value fails the job rather than
  producing a nonsense tag.
- **A hand-pushed tag must match VERSION.** `git tag v1.9.9` on a commit whose
  VERSION says 1.3.0 fails loudly, instead of publishing an image whose
  self-reported version disagrees with its tag.
- **`:latest` only moves for the newest release** (`is_newest`, computed with
  `sort -V` over all tags including the pending one). Re-publishing an older
  version can no longer drag `latest` backwards — which the previous
  unconditional `type=raw,value=latest` would have done.

The resolution logic was exercised against seven scenarios before committing
(already-tagged main push, untagged main push, an older untagged version, a dev
push, a matching tag push, a mismatched tag push, and a malformed VERSION).

### Release tags are attributed to github-actions[bot]

Deliberate: CI created them, not a person. Two `git config` lines in the "Tag
the release" step change that if releases should carry the andysom25 identity
instead — worth knowing given how much of this document is about commit
attribution.

### Related: bump-yt-dlp PRs show no CI checks

Same `GITHUB_TOKEN` rule. That workflow runs the full suite *before* opening
its PR, so the verification does happen — it just isn't displayed on the PR,
which reads as unverified. The PR body now explains this. Fixing it properly
needs a PAT stored as a secret; not worth a long-lived credential for a
cosmetic gap.

### Releasing from now on

Bump `VERSION` on `dev`, merge to `main`. That's it — the tag, the release and
the image follow. Hand-tagging still works and is still the way to publish a
commit whose VERSION is already tagged.

### Activating it (v1.3.1)

A workflow only takes effect once it is on the branch being pushed: GitHub runs
`ci.yml` as it exists on the ref receiving the push. So while this sat on `dev`
it did nothing, and a merge to `main` would still have used main's older
tag-only `ci.yml`. Easy to miss, because everything looks committed and green.

Activated by bumping `VERSION` to 1.3.1 and merging, so the new workflow's
first real run cut its own release — which also exercised the full auto-release
path (untagged VERSION on main -> tag + image + release) rather than only the
"already tagged, do nothing" branch a bump-free merge would have tested.

## CHANGED (2026-07-29): public-repo hardening, a production server, and two real fixes (v1.4.0)

Batch of work prompted by the repo being public. Grouped because most of it is
one-line-each; the parts with reasoning worth keeping are below.

### `POST /api/config` silently dropped internal keys

The endpoint replaces the whole config document and preserved a **hardcoded
pair** — `_secret_key` and `_auth`. That was correct when those were the only
internal keys, and silently wrong the moment `_plex_client_id` (v1.2.0) and
`update_check_enabled` (v1.2.0) appeared.

Reproducing the old merge against a realistic document loses three keys:
`_plex_client_id`, `update_check_enabled`, and any future underscore key.
Losing `_plex_client_id` is the damaging one — it's the identity Vidshelf
presents to Plex's OAuth flow, so every restart after a settings save would
look like a brand new device. Nothing raises an error; you'd notice as a
growing list of authorised devices in your Plex account, if ever.

Now **every** leading-underscore key is preserved, plus a small explicit list
for non-underscore internal settings (`update_check_enabled`). The underscore
prefix being the rule rather than an enumeration is the point: the next
internal key added is protected by default instead of by remembering to edit
this function. Callers can still *set* an internal key explicitly — the GET
response exposes `_plex_client_id`, so the editor has to be able to round-trip
it — only omitted keys are filled back in.

`tests/test_routes.py` has three guards for this, including one asserting an
explicitly-supplied internal value still wins.

### Fetches had no deadline, so a stall looked identical to a break

Reported as "the artists page just spins". It wasn't reproducible — the page
worked correctly in a real browser against the published image, and
`/api/artists/summary` returned in 0.6s. But the same symptom *was* reproduced
on the **Channels** page while capturing screenshots: `Loading channels…` sat
there indefinitely while a per-channel YouTube name lookup was slow.

The class of bug is real regardless of which page triggered it. Both endpoints
depend on something outside this app's control — a YouTube lookup, or a
directory walk across a network mount — and neither fetch had a timeout, so a
stall renders as a spinner that never resolves with no way to distinguish slow
from broken. The most likely trigger for the original report was a request left
hanging across one of several container restarts.

Added `fetchWithTimeout()` (25s, `AbortController`) plus `describeFetchError()`
so an abort reads as "Timed out after 25s" rather than a bare "aborted", and
both pages now render a **Retry** button instead of a dead panel. The
`clearTimeout` in a `finally` matters: without it a fast response leaves a
pending timer that fires an abort against an already-settled request.

### waitress instead of Werkzeug's development server

`app.run()` was serving the published image. Werkzeug's server is explicitly
not intended for unattended use, and this now ships as a container people run
for weeks.

waitress rather than gunicorn because it's pure-Python and runs on Windows,
which README option 3 depends on — gunicorn would have made the documented
local-install path Linux/macOS-only.

`threads=8`, not waitress's default of 4: the dashboard polls several endpoints
on a timer while downloads and the artwork watcher occupy their own background
threads, so 4 request threads is thin once a slow directory scan is in flight.
Configurable via `SERVER_THREADS`.

`FLASK_DEBUG=true` still routes to Werkzeug, because the reloader and
interactive debugger are the entire reason to ask for it. It now prints a
warning when it does.

Side benefit: `ident='Vidshelf'` replaces the `Server: Werkzeug/3.1.8
Python/3.12.13` header, which was advertising exact versions to anyone probing.

### HEALTHCHECK

Without one, `restart: unless-stopped` can't tell a wedged app from a working
one — the container stays "up" while serving nothing. Hits `/login` because
it's the only route that answers without a session, via `urllib` rather than
curl so the slim image doesn't need an extra package. Verified: the container
reports `(healthy)`.

### Supply chain: actions pinned to SHAs

All 12 `uses:` references across both workflows now pin a commit SHA with the
tag as a trailing comment. A moved tag on any of them would have executed
arbitrary code inside a job holding `contents: write` **and** `packages: write`
— i.e. it could publish a container image under this repo's name. A SHA can't
be moved.

That trades one risk for another: a pinned SHA can't receive security fixes on
its own. `.github/dependabot.yml` covers that, targeting `dev` (main is
releases-only) and **excluding yt-dlp**, which `bump-yt-dlp.yml` already
handles with a full test run before opening its PR. Two bots on the same line
would just conflict.

Dependabot's PRs *do* trigger CI, unlike PRs opened with `GITHUB_TOKEN` — so
they arrive with the suite already reported.

### Repository settings applied (not in git)

- Description and 10 topics set; the repo previously had neither, which made it
  effectively unlisted in GitHub search and topic pages.
- **Private vulnerability reporting enabled** — it was off, so the only way to
  report a hole in an app holding Plex tokens and NAS credentials was a public
  issue that discloses it. `SECURITY.md` documents scope, including what's
  deliberately out of scope (it's a single-admin LAN app; "exposed if you
  port-forward it without a proxy" isn't a vulnerability).
- Fork-PR workflow approval raised from first-time contributors to **all
  external contributors**.
- Dependabot alerts and security updates enabled.
- Empty wiki disabled.
- **`main` branch protection**: required check `Tests` (only — `Publish image`
  deliberately never runs on a PR, so requiring it would wait forever), 0
  required approvals (a solo maintainer can't approve their own PR), no
  force-push, no deletion, `enforce_admins: true`. Linear history and signed
  commits are deliberately **not** required: the first would block the `--no-ff`
  release merges, the second blocks every merge since commits aren't signed.

  Enforcement was verified empirically on a throwaway branch with identical
  rules rather than by trusting the config: force-push, deletion and a valid
  fast-forward push were all rejected server-side. Note `git push --dry-run` is
  **not** a valid test — it never sends a pack, so server-side rules never
  evaluate, and it happily reports success.

  `dev` is deliberately left unprotected. If that changes, do **not** add
  required status checks there: `bump-yt-dlp` opens its PRs with
  `GITHUB_TOKEN`, those never trigger workflows, and the PRs would become
  permanently unmergeable.

### Still outstanding

A `v*` tag ruleset. Restrict deletion and updates but **not creation** — the
release workflow creates tags, and restricting creation would break it.

### How to verify

```bash
python tests/test_state.py && python tests/test_updates.py && python tests/test_routes.py
node tests/test_artists_filter.js

docker compose up -d --build
docker inspect --format '{{.State.Health.Status}}' vidshelf   # healthy
docker logs vidshelf | grep waitress                          # confirms the server
curl -sD - -o /dev/null localhost:5000/login | grep -i server  # Server: Vidshelf
```

### Dependency bumps and why v1.4.1 exists (2026-07-29)

Dependabot opened six PRs within minutes of `dependabot.yml` landing. Five were
major action bumps, which looked alarming and turned out not to be:

| Action | Bump |
| --- | --- |
| `actions/setup-python` | 5.6.0 -> 7.0.0 |
| `docker/login-action` | 3.7.0 -> 4.6.0 |
| `docker/metadata-action` | 5.10.0 -> 6.2.0 |
| `docker/build-push-action` | 6.19.2 -> 7.3.0 |
| `peter-evans/create-pull-request` | 7.0.11 -> 8.1.1 |
| `certifi` | 2026.6.17 -> 2026.7.22 |

**Every one of those majors is the same change**: Node 24 as the default
runtime, requiring Actions Runner >= v2.327.1, plus an internal ESM refactor.
No input renames, no semantic changes. All jobs here are `ubuntu-latest`
(GitHub-hosted), so the runner requirement is satisfied automatically — it
would only matter on self-hosted runners.

Two specific fears were checked against real usage and dismissed:

- `metadata-action` v6 changed `#` handling in list inputs. Our `tags:` list
  contains no `#`; the explanatory comments sit *above* `tags:`, outside the
  list. `type=raw` and the `enable=` flag are untouched, so `:latest` still
  moves only when `is_newest` says so.
- `build-push-action` v7 removed `DOCKER_BUILD_NO_SUMMARY` and
  `DOCKER_BUILD_EXPORT_RETENTION_DAYS`, and dropped legacy export-build summary
  support. We set none of those. `cache-from`/`cache-to: type=gha` is not
  mentioned as changed.

**Merge order, and why it mattered.** Three of these live in the `publish` job,
which never runs on a pull request — so CI going green on those PRs proves
almost nothing. They were merged in ascending order of risk (certifi, then
setup-python which CI *does* exercise, then login/metadata/build-push, then
create-pull-request which only affects the weekly yt-dlp bumper), one at a time
because Dependabot rebases the remainder after each merge.

**v1.4.1 exists purely to exercise the publish job.** It carries no product
change. Without a release, three unreviewed majors would sit in the release
pipeline untested until the next release someone actually cared about. The
blast radius is bounded by the existing ordering — the image is pushed before
the tag is created, so a broken publish leaves no tag, no release, and a
re-run after a fix works.

Note for future bumps: `dependabot.yml` excludes yt-dlp on purpose, because
`bump-yt-dlp.yml` already proposes those with a full test run first. Two bots
on the same line would only conflict.

## ADDED (2026-07-29): unattended monitoring, notifications and retention (v1.5.0)

Closes the gap that made the README's "Sonarr/Radarr for YouTube" framing
inaccurate: **nothing ran on a schedule.** `download_mode` was read from exactly
one place — `/api/channels/download-all` — so a channel set to "New Only"
downloaded nothing until a human clicked a button. The only background thread in
the app was the artwork watcher.

Three features, shipped together because each one is unsafe without the others:
monitoring without notifications is automation you can't observe, and monitoring
without retention fills the disk.

### scheduler.py — ChannelMonitor

A daemon thread on an interval (default 60m, floored at 5m). Collaborators are
**injected as callables** rather than imported from app.py: app.py imports this
module, so importing back would be circular, and injection is what lets the
tests drive it with no Flask, no network and no yt-dlp.

Two behaviours that are deliberate and easy to get wrong:

**Already-downloaded videos are always skipped, regardless of mode.** The manual
endpoint honours `all` as "fetch up to 20 whether or not we have them" — a
reasonable thing to ask for by hand and a catastrophic thing to do hourly, since
it would re-download the same videos forever. On a timer, `all` and `new` behave
identically. `tests/test_scheduler.py::test_mode_all_still_skips_downloaded_on_a_timer`
is the guard.

**`manual` channels are never touched.** That mode means "I decide".

The thread always runs; whether a tick *does* anything is decided per-tick from
config, so toggling it in Settings takes effect without a restart. The wait is
interruptible (`threading.Event`) so a changed interval or "Check now" applies
immediately instead of up to an hour later. A per-channel failure is captured
into that channel's result rather than raised, so one dead channel can't stop
the others or kill the loop.

Started from `__main__` only, never at import — otherwise `python -c "import app"`
and the test client would spawn it.

### notify.py

Hand-rolled on `requests` rather than pulling in Apprise. Apprise gives ~80
targets for one line of config but adds five transitive pins
(requests-oauthlib, oauthlib, markdown, PyYAML, tzdata) to a project that keeps
its dependency surface small on purpose and runs its tests with none. Supports
Discord, Slack, ntfy, Gotify and a generic JSON webhook, with the shape detected
from the URL. If five targets stops being enough, Apprise is the right upgrade
and notify.py is the thing to delete.

**No SSRF guard here, deliberately.** artwork_sync.py guards its fetches because
those hosts arrive from a public search API. This URL is typed in by the admin
and usually points *into* their own LAN — an ntfy or Gotify container on the same
host. Refusing private addresses would break the primary use case. Different
threat model, different answer; recorded here so it doesn't look like an
oversight.

Two things the tests caught:

- **ntfy puts the title in an HTTP header**, which must be latin-1 encodable.
  `Björk — Jóga` raised `UnicodeEncodeError` inside requests. Titles are now
  ASCII-sanitised for that transport only.
- **Completion notifications are off by default.** A bulk download would emit
  dozens of messages, which is the fastest route to the user muting the channel
  and missing the failures that matter.

Every failure is swallowed and logged — a broken webhook must never stop a
download or wedge the scheduler. The config endpoint **never echoes the URL
back**, because it usually embeds a secret and the response would land in
browser history and any intermediate log.

### retention.py

The only code in Vidshelf that deletes media, so it's built to be boring:

- Off by default; `plan()` computes, `apply()` only ever acts on a plan.
- `keep_last` floored at 1 — no configuration empties a folder.
- Video extensions only, so artwork, `artist-metadata.json` and
  `title-cards.json` are never candidates and pruning can't break the Plex
  integration.
- A sweep over `SAFETY_MAX_DELETIONS` (200) refuses and asks.
- `apply()` refuses a plan carrying an error, so a caller can't execute a
  refused sweep by ignoring the field.
- The API requires `{"confirm": "DELETE"}`. Not a security control — the session
  already authenticates the admin — but friction on the one irreversible
  endpoint is worth it.

**It refuses to sweep a media root with no artist folders.** That guard exists
because of the decoy-volume incident in CLAUDE.md: a network path that silently
resolved to a small local directory instead of the NAS. Deleting based on what a
*wrong* mount contains is the one failure here that isn't recoverable. Verified
live — in a container with no NAS mounted, the plan endpoint refuses with a
pointer to `df -h` rather than reporting a clean sweep.

**Tracker entries are never removed.** `downloaded_videos.json` records "we
downloaded this", not "the file is present". Clearing entries would make the
scheduler re-download every pruned video on the next tick, prune it again, and
loop forever. Files go; history stays. `tests/test_invariants.py` asserts
retention.py contains no executable reference to the tracker at all.

The visible consequence is intended: a pruned video does not come back on its
own. Clearing download history is what makes it eligible again.

### Verified live in a container

| check | result |
| --- | --- |
| monitor thread starts, stays inert while disabled | `enabled=False, thread_alive=True, ticks=0` |
| interval clamped, not rejected | requested 1m -> 5m |
| retention refuses an unmounted-looking root | refused, with a `df -h` pointer |
| `POST /api/retention/apply` without confirm | HTTP 400 |
| webhook secret in the config response | not present; kind detected as `discord` |
| "Check now" with no channels | 200, empty results |

### Tests

`tests/test_scheduler.py` (16) and `tests/test_notify.py` (19), both
dependency-free and network-free. Route tests gained three: retention refusing
un-confirmed deletion, the notification URL not being echoed, and the monitor
interval being clamped.

### Test coverage: measured, then targeted (2026-07-29)

Measured with `coverage` (installed locally, deliberately **not** added to
requirements.txt — that stays runtime-only and the suites stay dependency-free).

Starting point was **28%**, which is a misleading number on its own. The useful
question isn't "what percentage is covered" but "can the failures this project
has actually suffered recur without CI noticing". For the worst of them the
answer was yes:

| Module | Before | After |
| --- | --- | --- |
| `notify.py` | 25% | **95%** |
| `transcode.py` | 30% | **50%** |
| `artwork_sync.py` | 11% | 15% |
| `app.py` | 30% | 33% |
| `state.py` / `updates.py` / `retention.py` / `scheduler.py` | 71-80% | unchanged |
| `downloader.py` | 23% | 23% |
| **Total** | **28%** | **33%** |

`downloader.py` staying at 23% is deliberate — see below.

#### The gap that mattered: an invariant, not coverage

`downloader.py`'s uncovered lines include the `copyfileobj` copy. All three copy
sites were **correct**, but nothing enforced it: someone "simplifying" one back
to `shutil.copy2` would break nothing in CI, because that bug only manifests
against a real CIFS mount. It cost two debugging sessions and appeared fixed
twice (CLAUDE.md gotcha #2).

Raising `downloader.py`'s line coverage would not have caught that. What catches
it is `tests/test_invariants.py`, which asserts on the **source text**:

- no `shutil.copy/copy2/copyfile` in executable code, and each copy site still
  contains a `copyfileobj`
- state files opened only by `state.py`
- `PYTHONUNBUFFERED=1` still in the Dockerfile
- compose mounts `./data`, not individual JSON files
- `app.run()` reachable only under the `debug_mode` branch
- GHCR image name lowercase and not derived from `${{ github.repository }}`
- `retention.py` contains no executable reference to the download tracker

Asserting on source text is unusual and the justification is narrow: these
failures only appear against a real CIFS mount, a real container restart, or a
real GHCR push, so no functional test reaches them. An invariant that fails in
CI beats a comment nobody reads at the moment they change the line. Each check
was verified to actually fire by feeding the checker a synthetic violation —
`shutil.copy2(a, b)` and a bare `open('config.json', 'w')` were both caught.

#### tests/test_media.py — the four measured gaps

`transcode.needs_conversion` (ffprobe replaced with canned JSON): AV1 and VP9
need conversion, `.webm`/`.mkv` do too even with compatible codecs, `.MP4`
uppercase does **not**, a silent video is left alone, and an unprobeable file is
left alone rather than fed into a re-encode on a guess.

`artwork_sync._is_safe_download_url` — a security control that had **zero
tests** at 11% coverage. Now covers non-http schemes, loopback (v4 and v6),
all three RFC1918 ranges, the `169.254.169.254` cloud-metadata address, a host
resolving to *both* a public and a private address (must reject), unresolvable
hosts, and a URL with no host. DNS is stubbed, so no network.

`_clean_video_title` / `folder_to_artist` — pure string functions whose edge
cases have already caused bugs (the stylized-quote incident). Covers YouTube-ID
stripping, "(Official … Video)" boilerplate, preserving meaningful
parentheticals like "(US Version)", en/em dash normalisation, and never
returning empty.

`app._sanitize_folder_name` — invalid-character stripping, separator collapsing,
and trailing dot/space removal (Windows and SMB reject those).

#### Two expectations that were wrong, and one thing deliberately not fixed

`_clean_video_title('---')` returns `'--'`. The contract guards *empty or
identical*, and `'--'` is neither, so that's correct behaviour and the test was
over-specified. It now asserts non-emptiness, which is the invariant that
matters.

`_sanitize_folder_name('///???')` returns `'_'`. Two different all-punctuation
names would therefore collide on one folder. **Left as-is on purpose**: real
artist names contain letters, and changing this function's output would orphan
every folder created by an earlier version — the app would create duplicates
alongside the originals. Folder-name stability is worth more than tidying a
degenerate case. The test documents that rather than asserting a fix.

#### Where to stop

`artwork_sync.py`'s remaining ~1,000 uncovered statements are mostly Plex and
artwork-provider HTTP calls, where mocking costs more than it protects. The pure
functions inside it are now covered; the I/O is better verified by an actual
sync against a real Plex server, which is what the manual Settings actions are
for.

## FIXED (2026-07-30): six gaps in the v1.5.0 automation, found by auditing it (v1.5.1)

Audited the three v1.5.0 features rather than waiting to hit their edges. Six
real gaps, three of them in code written the same day.

### Retention never ran automatically

`retention.sweep()` was called from exactly two places: the dry-run endpoint and
the apply endpoint. **Nothing in the scheduler called it.** So the justification
for shipping the three features together — "monitoring without retention fills
the disk" — was not actually delivered: monitoring ran unattended, pruning needed
a human to click Preview then Delete.

Now runs after a tick when `retention.auto_sweep` is set. Kept as a **second**
opt-in on top of `retention.enabled`, because an upgrade must never start
deleting media because a flag happened to already be true.

A failing sweep is caught and recorded rather than propagating — a retention
error must not lose the downloads a tick just queued. There's a test for that.

### Retention swept the wrong directory for channel downloads

`_media_root()` returned `artwork_sync.root_path` only. Channel videos land under
`plex_base_path` or a per-channel `plex_media_path`. So the feature that exists to
bound unattended *channel* monitoring never pruned what channel monitoring
downloads — it pruned music videos instead.

Now sweeps every root, via the pre-existing `_gather_media_roots()` (in app.py
since the initial commit, used by the conversion scan). Reused rather than
reimplemented, and it already filters to directories that exist, so roots the
container can't see — a UNC path on the host — never reach retention.

Per-root failures are now **skips, not aborts**: one unmounted share shouldn't
disable pruning everywhere. `plan()` only reports an overall error when *no* root
could be scanned. Roots are deduplicated, so overlapping config can't plan the
same file twice.

### A tick re-read the entire download history once per video

The worst of the three, and invisible from outside. `is_video_downloaded()` was
passed into the loop as a predicate, and each call re-read *and re-parsed* the
whole tracker file under a lock. Measured: **500 file reads for one 500-video
channel, per channel, per tick.**

The manual endpoint capped its listing at `videos[:20]`, so this never mattered
before something iterated a full listing.

Two fixes: the injected contract is now `list_downloaded(url) -> set`, read once
per channel; and `get_channel_videos()` output is bounded by `max_listing`
(default 50) instead of a channel's entire history.

### No brakes on unattended growth

The executor has 2 workers and an **unbounded queue**, so an hourly tick could
enqueue faster than it drained, forever. And nothing checked capacity — on a NAS
already at 97%, monitoring plus manual-only retention had no stopping condition
at all.

Three brakes, all conservative by default:

- `max_queue_depth` (20) — a deep queue skips the tick entirely, and doesn't even
  list channels, since each listing is a yt-dlp call.
- `min_free_gb` (0 = off) — per-channel destination check. Deliberately fails
  *open*: a destination that can't be stat'd (a UNC path invisible to the
  container) must not silently stop all downloading.
- `max_listing` (50) — above.

### Failing channels retried and notified every tick

A deleted or region-blocked channel errored on every pass, and every pass sent a
notification. Now exponential backoff — 1, 2, 4, then 8 ticks — cleared on the
first success.

### A crash-loop bug that only a real container run caught

Worth recording as a process point, not just a fix. `_settings()` changed from
returning a 3-tuple to a dict. Everything passed: all 118 tests, the import
check, `ast.parse`. The container then **crash-looped on startup**, because the
`__main__` block still unpacked three values.

Nothing in the suite executes `__main__` — the CI import check imports the module,
which by design does not run it. Building and starting the image is what found
it, which is exactly what CLAUDE.md's "Verifying fixes" section says to do and
why that instruction exists. The startup line now reads from the public
`status()`.

### A CSS bug that only looking at a screenshot caught

`.form-group input { width: 100% }` also matched checkboxes, stretching them to
the panel width and marooning each label on the far side of the row. The
notification event list had shipped in v1.5.0 looking broken.

Every automated check passed — the controls existed, were wired, and the route
tests confirmed all of it. It took *rendering the page and looking at it* to see
it. Fixed globally with `.form-group input[type="checkbox"] { width: auto }`.

### Release notes are now authored, not generated

Since automation took over in v1.4.0, `gh release create --generate-notes`
produced a single "Release vX.Y.Z … in #N" line. **v1.4.1 and v1.5.0 both shipped
with notes that said nothing.**

CI now prefers `release-notes/vX.Y.Z.md` via `--notes-file`, falling back to
generated notes with a workflow warning. `tests/test_invariants.py` asserts the
file exists for the current `VERSION`, is longer than a stub, and has section
headings — so the omission fails on `dev` instead of being discovered on a
published release. Both older releases were backfilled.

CLAUDE.md now records both standing rules: hand-written notes on every release,
and README updates on any release that changes user-facing behaviour — including
recapturing screenshots, which go stale silently with no test to catch them.
`settings.png` was recaptured here for exactly that reason.

## ADDED (2026-07-30): download control — cancel/retry, quality caps, cookies (v1.6.0)

v1.5.x added unattended automation; this adds the handles for it. Plus two
user-visible bugs found by reading the code rather than hitting them.

### Cancel and retry

There was no cancel or retry path anywhere. Tolerable when every download was a
deliberate click; poor once monitoring queues work on a timer and a deep queue
skips ticks.

**Cancellation is cooperative, not a kill.** yt-dlp has no cancel API; the
documented way to stop a download in flight is to **raise from a progress hook**,
which unwinds its internals cleanly and lets yt-dlp clean up its own `.part`
file. `request_cancel()` sets a flag; `_progress_hook` checks it on every
callback and raises `DownloadCancelled`. Latency is one progress callback — well
under a second for an active download.

Terminating the worker thread instead would risk a half-written file being moved
onto a network share, which is precisely the failure mode CLAUDE.md gotcha #2
exists because of.

A **queued** download has no worker to notice the flag, so `request_cancel()`
flips its status itself — otherwise the button would appear to do nothing until a
worker eventually picked it up.

**A cancellation is not a failure.** It records as `cancelled`, and all three
download workers plus the retry worker check `isinstance(exc, DownloadCancelled)`
before notifying, so stopping a download can't send a "download failed" alert for
something the user asked to stop. That guard is in four places because the
notification call is.

**Retry re-queues as a new download** rather than resurrecting the old entry, so
the record of the attempt that failed stays intact instead of being rewritten.

### Per-channel quality cap

The format selector was a hardcoded constant. Monitoring a 4K channel unattended
meant 4K files regardless — disk plus a CPU-heavy re-encode each.

`build_format_selector(max_height)` applies the cap to **every branch**, not just
the preferred one. That matters: the selector falls back through
`bestvideo[vcodec^=avc1]` → `best[vcodec^=avc1]` → `bestvideo+bestaudio` →
`best`, and without capping the fallbacks a channel publishing only AV1 at 2160p
would satisfy a later branch and silently ignore a 1080p request. There's a test
asserting every branch except the final bare `best` carries the cap.

Uncapped output is byte-identical to pre-v1.6.0 — pinned by a test, because a
change there would silently alter which stream every existing install picks.

Per channel rather than global: the reason to cap is usually one specific
channel, and a global cap to handle that would needlessly downgrade the rest.

### Cookies

`cookies.txt` has existed in this repo (gitignored) since long before anything
read it — `ydl_opts` never contained `cookiefile`, so age-restricted and
members-only videos failed with **no indication why**.

Now read from `data/cookies.txt` (preferred, since it's inside the mounted volume)
or a repo-root `cookies.txt` for continuity. Settings → System Health reports
whether one was found, so this specific class of silent misconfiguration is
answerable from the UI instead of by reading source.

The health row renderer needed extending: it keyed on `info.found` and painted
anything missing as a red ❌. An absent cookies file is *information*, not a
fault, so optional rows now render neutral (ℹ️) when missing.

### Bug: the dashboard showed the same number twice

```python
downloads_count = sum(len(vids) for vids in tracker.values())
videos_count    = sum(len(vids) for vids in tracker.values())
```

Identical expressions. "Videos Available" and "Downloads" were therefore always
equal — visible in the v1.3.0 screenshots as 24/24 — and one of four cards
conveyed nothing.

Now **New Available**: videos seen on monitored channels that aren't downloaded,
recorded per tick by the scheduler (`pending` per channel, summed by
`pending_count()`). It returns `None` until a check has run and the UI shows
`--`, because reporting `0` would assert "nothing new" when the truth is "nothing
has looked".

### Bug: the sidebar said "beta"

Untrue since v1.0.0, and the first thing a visitor read. Removed; the version was
already in the sidebar footer.

### Verified in a container

| check | result |
| --- | --- |
| cookies absent | reported, with instructions |
| cookies present | `available: true`, and `DEBUG: using cookies from ./data/cookies.txt` confirms yt-dlp received it |
| channel cap 1080 | resolved to 1080 |
| no channel cap | resolved to None (unchanged behaviour) |
| cancel queued | 200, status `cancelled` |
| cancel twice | 409 `Already cancelled` |
| retry | 200, genuinely re-queued and executed |

`tests/test_downloads.py` adds 10 tests over the format selector and the
cancellation flag; `test_routes.py` gained 5 for the endpoints, the stats fix and
the removed badge.

## SECURITY (2026-07-31): whole-project review before further work (v1.6.1)

Reviewed the entire project rather than a diff — `dev` and `main` were identical,
so a diff-scoped review had nothing to look at. Everything below was verified by
execution, not by reading.

### HIGH — stored XSS via video title, chaining to Plex token disclosure

`d.title` is set from yt-dlp's extracted YouTube title
(`real_title = info.get('title', title)` in downloader.py), persisted to
`active_downloads.json`, served by `/api/downloads/progress`, and interpolated
into `innerHTML` **unescaped**. Proven end-to-end: a title of
`<img src=x onerror=alert(1)>` reached the client verbatim, as did `d.error`.

The title is **not** operator-controlled — it comes from YouTube — so this is
attacker-influenced input, not self-XSS.

**The chain is what made it high.** `GET /api/config` returned `plex.token` in
plaintext, and so did `GET /api/plex/config`. Script in the admin session could
therefore read a bearer credential for the user's entire Plex account with one
fetch. Two things limited it: `/api/password` requires the current password, and
SameSite=Lax blocks cross-site CSRF.

Nine sites were unescaped (21 interpolations). Fixed in two layers:

1. **Escaped at every site.** `escapeHtml()` already existed and handles all five
   of ampersand, angle brackets and both quote characters, so it is correct for
   text and quoted-attribute contexts.
2. **Removed the prize.** Neither config endpoint returns the token now; both
   report a `token_set` boolean, which is all the UI ever did with it.

**The subtlety in (1):** five sites were inside inline onclick/onchange handlers.
`escapeHtml` is the *wrong tool* there — the HTML parser decodes the entity back
to a raw quote before the JS engine sees it, so a quote still breaks out of the
JS string. Those use `encodeURIComponent` at the call site with a matching
`decodeURIComponent` in the handler, since encodeURIComponent emits none of the
dangerous characters. Only reachable via the operator's own channel URL (the add
endpoint validates a YouTube *prefix* but not the remainder), so self-XSS — fixed
because a security release is the moment to do it.

**The interaction that needed care in (2):** the raw-config editor does GET then
POST. With the token stripped from the GET, a round-trip would POST a `plex`
object without it and silently disconnect Plex. The v1.4.0 merge preserves
top-level underscore keys, which cannot cover a nested one, so `plex.token` is
restored explicitly and the `token_set` marker is dropped before persisting.
`test_config_round_trip_does_not_disconnect_plex` performs the exact GET-then-POST
the editor does.

### MEDIUM — SYS_ADMIN granted for a mount the container never performs

`docker-compose.yml` granted `cap_add: SYS_ADMIN`, left over from an approach
where the container mounted the CIFS share itself. It doesn't: the `cifs` volume
driver mounts on the host and the container sees an ordinary bind.

**Tested rather than assumed** — a plain alpine container with zero added
capabilities listed all 28 artist directories and successfully wrote to the share.
So the capability bought nothing while granting close to a container-escape
primitive, which matters far more given the XSS above hands an attacker the admin
session.

Replaced with `cap_drop: ALL`. Verified on the live deployment after a full
recreate: CapAdd empty, CapDrop ALL, 28 dirs visible, write OK.

**Capability changes need `docker compose down && up`, not a restart** — a plain
`up -d` will not re-apply them.

The container still runs as **root** (the python:3.12-slim default). Left alone
here: changing USER interacts with file ownership on the CIFS mount (uid/gid 1000
in the volume options) and deserves its own change rather than riding along in a
security fix.

### LOW

- **config.json was 0644**, holding the Plex token, admin password hash and
  session signing key — and the data directory is bind-mounted, so any local user
  on the *host* could read all three. `state.write_json()` now chmods 0600.
  Best-effort with a swallowed OSError: SMB/CIFS and Windows bind mounts often
  reject chmod, and failing a state write over file permissions would be a much
  worse outcome. The mode must be set *after* `os.replace()`, because replace
  preserves the source file's mode and the temp file is created under the umask.
- **ffmpeg argument injection.** argv was already a list (no shell), and
  `src_path` directly follows `-i` so it is consumed as that option's value. The
  output is a trailing positional, and filenames derive from video titles, so a
  title beginning with a dash could be misread as an option. Added an
  end-of-options marker. **Verified ffmpeg accepts it** — byte-identical output
  with and without — because ffmpeg's parser is not GNU-style and assuming would
  have risked breaking all conversion.

### Reviewed and found sound

No `shell=True`, `os.system` or `eval` anywhere; all three subprocess calls use
argv lists. Path traversal via artist names is blocked — `artist_to_folder()`
replaces both path separators, and four traversal payloads were tested with none
escaping the media root. Login throttling (5 attempts / 300s per IP). HttpOnly
plus SameSite=Lax. Secret key generated and persisted, never a fixed default.
SSRF guard rejects loopback (v4 and v6), all three RFC1918 ranges, the
cloud-metadata address, and hosts resolving to both a public and a private
address. Notification URL withheld from API responses. Dependencies pinned with
Dependabot active. Secret scanning and push protection on. No credentials in git
history.

Only `/login` and `/favicon.ico` are reachable unauthenticated by GET; the two
`_noauth` artwork endpoints are deliberate and now documented precisely in
SECURITY.md rather than merely labelled deliberate.

### Three new invariants

`tests/test_invariants.py` gained checks that fail the build if any finding
regresses: unescaped server data reaching the DOM, the Plex token being returned
by either config endpoint, and SYS_ADMIN or privileged mode reappearing in
compose. **Each was verified to actually fire** by feeding the checker a synthetic
violation — a bare title interpolation, a compose file granting SYS_ADMIN, and an
`/api/config` returning the raw document.

### Still open, deliberately

`/api/artwork/swap_noauth` remains an unauthenticated write. Bounded (no
traversal, no folder creation, SSRF-guarded) and documented, but if the port is
ever exposed it is a defacement surface that also spends the Plex token. Requiring
auth would break whatever consumes it; worth revisiting with that consumer in
hand.

### Review pass on the v1.6.1 branch — what the first fix missed

The v1.6.1 changes above were reviewed before merging. The security reasoning
held, but the review found four problems, and fixing the first one properly then
surfaced a *second XSS vector of the same class* that neither the original pass
nor the review had spotted. Recorded in full because the failure mode is general:
**a regression guard written from the list of bugs you just fixed will only ever
catch those bugs.**

#### The guard was a naming test, not a safety test

The first `test_no_unescaped_server_data_in_the_dom` required a match on *both*
a receiver from a fixed list (`d`, `ch`, `v`, `a`, `c`, `r`, `p`, `s`, `data`,
`video`, `item`) *and* a field from a fixed list of twelve names. It reported a
clean pass — "unescaped server data remaining: none" — while:

- `ch.download_path` and `ch.plex_media_path` sat unescaped **in the same
  template literal** as five sites the same commit had just fixed. Neither field
  name was in the list.
- two `${errors}` interpolations sat unescaped immediately beside a newly-added
  `escapeHtml(r.artist)`. A bare local has no receiver to match.
- nine `'<div>' + e.message + '</div>'` concatenations reached `innerHTML`. The
  guard only ever looked at `${...}`, so **an entire sink syntax was invisible to
  it.**
- a rename to `dl.title` or `entry.error` would have walked straight through.
  Verified, not assumed: both were fed to the guard and both passed.

Rewritten to use name *patterns* on any receiver (`\w+_path`, `\w+_url`,
`\w+_id`, …) and to scan concatenation as well as interpolation. Expressions that
are genuinely safe by construction are now listed individually in
`REVIEWED_SAFE` with the reason — a short list is the point; a long one means the
check has stopped being one.

#### What the rewritten guard immediately found

Fourteen more unescaped sites, one of them a real vulnerability of the same class
as the original finding:

```
onclick="selectPlexServer('${srv.uri}', '${srv.name}')"
```

`srv.name` and `srv.uri` come from **plex.tv**, interpolated raw into an inline
`onclick` JS string. A Plex server named with a quote breaks out of the string.
This is precisely the case the v1.6.1 commit had already reasoned about correctly
for channel URLs — and it was sitting three hundred lines away, unfixed, because
the guard's field list did not contain `uri` and its receiver list did not
contain `srv`. Both arguments are now `encodeURIComponent`-encoded with matching
decodes in the handler.

Also escaped: `entry.name` and `entry.path` (folder browser), `data.auth_url`,
`srv.name` in its display span, `lib.title`, `r.label`, `r.why`, `f.artist`,
`data.errors.join()`, `v.id` (twice), `d.download_id`, and thirteen `e.message` /
`data.error` concatenations. Escape call sites went from 45 on `main` to 60 in the
first pass to **98** after the review.

#### Escaping into a text sink is a bug, not belt-and-braces

The first pass added `escapeHtml()` to a `showToast()` argument. `showToast()`
assigns `textContent`, which already makes markup inert — so the escape added
nothing and the entities became *visible*: **Guns N' Roses** reached the user as
`Guns N&#39; Roses`. Escaping is per-sink, and picking the wrong tool is possible
in both directions. `test_text_sinks_are_not_pre_escaped` now asserts the inverse
rule for `showToast` and `showConfirmModal`, and the DOM guard excludes those two
call sites structurally so the two checks cannot contradict each other.

#### `token_set` leaked into persisted state

`incoming_plex.pop('token_set', None)` was inside `if isinstance(incoming_plex,
dict) and current_token:`. On a **disconnected** install `current_token` is
falsy, so a raw-config round-trip persisted `"token_set": false` into
`config.json` and kept it there — a per-response presence flag masquerading as a
stored setting. The connected case passed throughout, which is exactly why it
needed its own test
(`test_config_round_trip_from_a_disconnected_install_persists_no_marker`). The
pop is now unconditional.

#### The one fix with no guard, and a guard that could not fail

`chmod 0600` had no test at all — the only one of the four fixes that could be
deleted silently. It cannot be asserted behaviourally, because `os.chmod` on
Windows only toggles the read-only bit, so a mode assertion would pass in CI and
fail on the development machine. It is now asserted at source level, like the
other invariants in that file.

The ordering half of that assertion was written as
`src.index('os.replace') < src.index('os.chmod(...)')` and was **vacuously
true**: `state.py`'s module docstring mentions `os.replace` four times before any
code does, and the real call is `_replace_with_retry(...)`, not `os.replace(...)`.
It could never have failed. Caught only by mutating the source to move the chmod
above the replace and observing that the test still passed — which is the whole
argument for proving a guard fires rather than observing it pass.

#### New invariant: encode and decode are a pair

`encodeURIComponent` at a call site with no matching `decodeURIComponent` in the
handler is a *silent functional break* — the fetch targets a channel URL or
download id that does not exist, so it 404s into a toast rather than erroring
visibly. No functional test in this repo clicks those buttons.

Two subtleties, both found by mutation rather than by reading:

- `downloadActions()` renders `onclick="${fn}(...)"`, so `cancelDownload` and
  `retryDownload` reach the DOM through a variable, not a literal. The first
  version of the pairing check reported a clean pass with **both** decodes
  deleted. It now resolves that indirection through the `btn(label, fn, danger)`
  helper's call sites.
- `selectPlexServer` takes *two* encoded arguments. A body that decodes only one
  still contains the word `decodeURIComponent`, so a presence check passes it.
  The check now counts encoded arguments per call site and requires at least as
  many decodes.

#### Verification

All six new or rewritten guards were proven to fail against a synthetic
violation — new receiver name, suffix-matched field, concatenation sink,
escaping into a text sink, chmod deleted, chmod moved before the replace — plus
four separate decode-removal mutations including the partial-decode case. A guard
that has only ever been observed passing has not been tested.

Re-verified in a container built from the branch and run with `--cap-drop ALL`:
`CapAdd=[]`, `CapDrop=[ALL]`, `config.json` mode `600`, `/dashboard` renders 200
with the JS wired up, neither config endpoint returns a stored
`SECRET-PLEX-TOKEN` while both report `token_set`, and a download record with a
`<img src=x onerror=…>` title is served raw by the API and escaped at every one
of its five render sites.
