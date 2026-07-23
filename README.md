# Vidshelf

A self-hosted YouTube channel downloader and music-video finder that organizes everything into a Plex-ready library — think Sonarr/Radarr for YouTube content, with deep Plex integration built in.

**Vidshelf** lets you monitor YouTube channels, browse and download their videos, and search for official music videos by artist. Downloads are automatically converted to a format virtually every Plex client can play without server-side transcoding, and — if you connect a Plex server — the app keeps artist artwork, smart collections, clean titles, and designed poster art in sync for you automatically.

> **Note:** this project is a work in progress, still under active development, and does not yet have a stable release. Expect rough edges.

---

## ⚠️ Before you use this

This tool downloads video content from YouTube. It's built for personal use — archiving your own uploads, channels you have permission to save, Creative Commons–licensed content, or anything else you have the legal right to download. Downloading copyrighted material without permission may violate YouTube's Terms of Service and applicable copyright law in your jurisdiction. You're responsible for how you use it.

This project is not affiliated with, endorsed by, or sponsored by YouTube, Google, or Plex Inc.

---

## Features

### YouTube Channel Management
- **Add channels** by URL — automatically fetches the display name
- **Browse videos** — view the latest 50 videos from any channel
- **Download modes** — manual (pick videos), new (skip already-downloaded), all (download everything)
- **Bulk download** — download up to 20 videos in parallel with one click
- **Download history** — tracks downloaded video IDs per channel to avoid duplicates

### Music Video Finder
- **Search by artist** — enter an artist name (or artist + song to narrow results), get ranked music video results
- **Paginated results** — search results are cached server-side; click "Load More" to page through everything found instead of a fixed cutoff
- **Quality ranking** — results are scored on official-channel match, title quality keywords ("official", "music video", "HD", "4K", "lyric video"), view count, upload recency, and penalized for covers/karaoke/remixes/live versions/Shorts/trailers
- **Quality labels** — each result shows the best available resolution (4K, 1440p, 1080p, 720p)
- **Automatic artist matching** — searching "Artist + Song" to narrow results won't fork a duplicate artist folder/collection if that artist is already tracked

### Download Format & Quality
- Downloads the **best available quality** — 4K, 1080p60, 1440p, etc.
- **Automatic Plex-compatibility conversion** — prefers a native H.264/AAC stream at download time; if YouTube only offers something else (commonly VP9/AV1 + Opus for 4K or older uploads), converts to H.264/AAC/MP4 afterward so the widest range of Plex clients can direct-play it without server-side transcoding. Already-compatible tracks are stream-copied (no quality loss); only genuinely incompatible video gets re-encoded, at a high-quality setting chosen to be visually indistinguishable from the source.
- A batch conversion tool (Settings page) can fix an existing library that was downloaded before this feature existed.

### Plex Integration (optional, connect via OAuth)
- **Automatic artist artwork** — pulls artist images from TheAudioDB, Fanart.tv, MusicBrainz, and Wikipedia/Wikimedia Commons for any newly-detected artist folder
- **Automatic smart collections** — one Plex collection per artist, backed by a saved search filter so newly-downloaded videos are picked up automatically, no manual "update collection" step
- **Automatic title cleanup** — strips the trailing YouTube ID and "(Official Video)"-style boilerplate from Plex's displayed titles, and normalizes artist-name casing/separators (handles inconsistent capitalization, en/em-dashes, and stylized quote characters some artists use in their own uploads) so collection filters reliably match
- **Automatic title-card posters** — generates a designed poster (artist name + song title over the artist's own art) for each video, replacing Plex's randomly-extracted video-frame thumbnail
- **Manual artwork swap** — search and preview alternate artist images and swap a collection's poster/art on demand
- **Duplicate-collection cleanup** — detects and merges same-artist collections that can occur from race conditions, keeping one and removing the rest

### Dashboard
- Single-page web dashboard with a dark theme, responsive down to mobile
- Real-time download and conversion progress (polling-based, no WebSockets needed)
- Artists page — browse tracked artists and their downloaded videos
- Settings page — raw config view, path editors, Plex connection management, password change, video-format-compatibility scan/convert tools

### Security
- No hardcoded credentials — a random admin password is generated on first run (or set your own via env vars) and printed once to the logs
- Session-signing key is randomly generated and persisted, not a fixed value baked into source
- Login throttling, security response headers, SSRF guarding on the public artwork-search endpoint, hardened session cookies

---

## Quick Start

### Option 1: Docker (Recommended)

```bash
# Clone the repo
git clone https://github.com/andysom25/Vidshelf.git
cd Vidshelf

# Optional: copy and edit the env template (admin credentials, NAS share
# for the music-video CIFS mount, Plex OAuth client identity, etc.)
cp .env.example .env

# Build and start
docker compose up -d

# Check the logs for your generated admin password if you didn't set one
docker compose logs | grep SECURITY

# Open http://localhost:5000
```

### Option 2: Local (Python)

```bash
# Clone the repo
git clone https://github.com/andysom25/Vidshelf.git
cd Vidshelf

# Install Python dependencies
pip install -r requirements.txt

# Install ffmpeg (required for downloading and format conversion)
# Windows: winget install "FFmpeg (Essentials Build)"
# macOS:   brew install ffmpeg
# Linux:   sudo apt install ffmpeg

# Start the server
python app.py

# Check the console output for your generated admin password if you
# didn't set ADMIN_PASSWORD
# Open http://localhost:5000
```

---

## Configuration

### Environment variables

All optional — see `.env.example` for the full template.

| Variable | Purpose |
|----------|---------|
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | Set your own admin login. If unset, a random password is generated on first run and printed once to the logs. |
| `SECRET_KEY` | Flask session-signing key. Auto-generated and persisted into `config.json` if unset. |
| `SESSION_COOKIE_SECURE` | Set `true` only if served over HTTPS (e.g. behind a reverse proxy). |
| `PLEX_CLIENT_ID` / `PLEX_PRODUCT` | This deployment's identity for Plex's OAuth flow. Auto-generated/persisted if unset. |
| `NAS_SMB_USER` / `NAS_SMB_PASS` / `NAS_SMB_DEVICE` | Only needed if you use the Docker Compose `cifs` volume for a network share — see the comments in `docker-compose.yml`. |
| `FFMPEG_PATH` | Override the ffmpeg/ffprobe binary location if not on `PATH`. |
| `MAX_CONCURRENT_DOWNLOADS` | Max downloads (and their format-conversion re-encodes) running at once, across all download types combined. Defaults to `2` — format conversion is CPU/memory-heavy, so raise this only if your hardware can handle more concurrent encodes. |

### `config.json`

Everything else lives in `config.json` (created automatically, gitignored — copy `config.json.example` if starting from scratch), and is fully editable from the Settings page:

```json
{
  "channels": [
    {
      "url": "https://www.youtube.com/@ChannelName",
      "download_path": "./downloads",
      "plex_media_path": "./downloads",
      "download_mode": "new"
    }
  ],
  "plex_base_path": "./downloads",
  "music_video_plex_path": "./downloads/music_videos",
  "artwork_sync": {
    "root_path": "/app/music_videos_final",
    "watch_interval": 120,
    "plex_collection_sync_on_artwork": true,
    "fanarttv_api_key": ""
  },
  "plex": {
    "server_url": "",
    "token": "",
    "music_video_library_key": ""
  }
}
```

### Download Modes

| Mode | Behavior |
|------|----------|
| `manual` | User clicks individual Download buttons |
| `new` | "Download All" skips already-downloaded videos |
| `all` | "Download All" downloads up to 20 videos regardless of history |

---

## Usage

### Managing Channels
1. **Add a channel** — Click "Add Channel" in the Channels page, paste the YouTube channel URL
2. **Browse videos** — Click on a channel card to load its latest 50 videos
3. **Download a video** — Click the download icon on any video
4. **Bulk download** — Use "Download All" to download all or only new videos

### Finding Music Videos
1. Navigate to **Music Videos** in the sidebar
2. Enter an artist name (e.g., "Foo Fighters") and click Search
3. Browse the ranked results — highest quality official videos appear first; click "Load More" for additional results
4. Click **Download** on any result to save it to the music video Plex path

### Connecting Plex (optional)
1. Go to **Settings** → Plex Integration → **Connect to Plex**
2. Authorize in the Plex tab that opens, then select your server and confirm the discovered music-video library
3. From then on, artwork, collections, title cleanup, and title-card posters stay in sync automatically as you download — the Settings page also has manual "Sync Collections", "Clean Up Titles", "Generate Title Cards", and "Remove Duplicate Collections" actions if you want to trigger something immediately

### Fixing an existing library's video format
1. Go to **Settings** → Video Format Compatibility → **Scan Library** to see what's not yet Plex-direct-play-compatible
2. Click **Convert All Non-Compatible Videos** to fix it — this can take a while for a large library (re-encoding is CPU-intensive), so it runs in the background with a progress indicator

---

## API Endpoints

<details>
<summary>Channel &amp; download management</summary>

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/channels` | GET | List all channels |
| `/api/channel/videos` | GET | `?url=` — List latest 50 videos |
| `/api/channels/add` | POST | Add a channel |
| `/api/channels/remove` | POST | Remove a channel |
| `/api/channels/mode` | POST | Update download mode (manual/new/all) |
| `/api/channels/download-all` | POST | Bulk download all/new videos |
| `/api/download` | POST | `{video_id, channel_url}` — Start a channel-video download |
| `/api/downloads/progress` | GET | Real-time download/conversion status |
| `/api/downloads/verify` | POST | Verify downloaded files exist at their final destination |
| `/api/downloads/clear` | POST | Clear download history |

</details>

<details>
<summary>Music videos</summary>

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/music-videos/search` | POST | `{artist, page}` — Paginated, ranked music video search |
| `/api/music-videos/download` | POST | `{video_id, title, artist}` — Download a music video |
| `/api/music-video-path` | GET/POST | Get/set the music video Plex path |
| `/api/artists` / `/api/artists/summary` / `/api/artists/videos` | GET | List tracked artists and their downloaded videos |

</details>

<details>
<summary>Plex integration</summary>

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/plex/oauth/start` / `/check` / `/servers` | POST | Plex OAuth login flow |
| `/api/plex/config` | GET/POST | Get/set Plex server URL, token, library key |
| `/api/plex/discover-library` | POST | Auto-discover the music-video library |
| `/api/plex/collections/sync` / `/status` | POST/GET | Sync or check Plex smart collections |
| `/api/plex/collections/duplicates` / `/dedupe` | GET/POST | Find/remove duplicate collections |
| `/api/plex/titles/clean` | POST | Clean up video titles in the library |
| `/api/plex/title-cards/generate` | POST | Generate designed poster art per video |
| `/api/artwork/sync` / `/status` | POST/GET | Sync/check artist artwork |
| `/api/artwork/search_noauth` / `/swap_noauth` | GET/POST | Search and swap artist artwork (unauthenticated by design — see `REFERENCE.md`) |

</details>

<details>
<summary>Format conversion</summary>

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/conversion/scan` | POST | Report which existing files need conversion |
| `/api/conversion/start` | POST | Start the batch conversion job |
| `/api/conversion/status` | GET | Poll conversion job progress |

</details>

<details>
<summary>System &amp; settings</summary>

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/stats` / `/api/system/info` | GET | Dashboard statistics and system info |
| `/api/config` | GET/POST | Raw config access (internal keys stripped) |
| `/api/password` | POST | Change admin password |
| `/api/plex-base-path` | GET/POST | Get/set Plex base path |
| `/api/browse-folder` | POST | Browse filesystem directories |

</details>

---

## Docker

### Volume Layout

| Host Path | Container Path | Purpose |
|-----------|---------------|---------|
| `./downloads/` | `/app/downloads` | Persistent downloads (survives rebuilds) |
| `./config.json` | `/app/config.json` | App configuration |
| `./downloaded_videos.json` | `/app/downloaded_videos.json` | Download history |
| `./active_downloads.json` | `/app/active_downloads.json` | Runtime progress |
| `music_videos_final` (named volume) | `/app/music_videos_final` | Music-video library — a native `cifs` mount if you're pointing at a network share (see `docker-compose.yml`); swap for a plain bind mount if you're storing locally instead |

### Simple local-storage setup

The `docker-compose.yml` in this repo is set up for a network share (a
native `cifs` volume, since Docker Desktop can't reliably bind-mount a
Windows drive letter or UNC path — see `REFERENCE.md` for why). If you
just want to store your music-video library on local disk instead of a
NAS, replace the `music_videos_final` volume with a plain bind mount:

```yaml
services:
  vidshelf:
    build: .
    container_name: vidshelf
    restart: unless-stopped
    ports:
      - "5000:5000"
    volumes:
      - ./downloads:/app/downloads
      - ./config.json:/app/config.json
      - ./active_downloads.json:/app/active_downloads.json
      - ./downloaded_videos.json:/app/downloaded_videos.json
      - ./music_videos:/app/music_videos_final   # local folder instead of a NAS share
    environment:
      - ADMIN_USERNAME=${ADMIN_USERNAME:-}
      - ADMIN_PASSWORD=${ADMIN_PASSWORD:-}
```

No `cap_add`/`driver_opts`/`NAS_SMB_*` variables needed for this version —
those only matter for the `cifs` network-share setup. See `.env.example`
for every environment variable this app recognizes.

### Commands

```bash
# Build and start
docker compose up -d

# View logs
docker compose logs -f

# Rebuild after code changes
docker compose up -d --build

# Stop
docker compose down
```

---

## Dependencies

- **Python 3.12** (see `Dockerfile`) — Flask, yt-dlp, Pillow
- **ffmpeg** — required for merging downloaded streams and for format conversion (installed automatically in Docker)

---

## Architecture

```
User Browser ──► Flask Server (app.py) ──► yt-dlp ──► YouTube
                     │                           │
                     │  downloader.py            │
                     │  (download queue,         │
                     │   progress tracking)       │
                     │        │                   │
                     │        ▼                   │
                     │  transcode.py              │
                     │  (Plex-compatibility        │
                     │   format conversion)        │
                     │        │                   │
                     ▼        ▼                   │
              Plex Media Directory                │
                     │                            │
                     ▼                            │
              artwork_sync.py ◄────────────────────┘
              (Plex OAuth, artist artwork,
               smart collections, title
               cleanup, title-card posters)
                     │
                     ▼
              Plex Media Server

config.json / downloaded_videos.json / active_downloads.json
  — file-based JSON storage, no database required
```

- **File-based JSON storage** — no database required
- **Threaded downloads** — background daemon threads, no Redis/Celery needed
- **Progress polling** — no WebSockets

---

## Known Limitations

- No download scheduling
- Music Video search relies on YouTube search (no dedicated data API)
- Plex library titles/collections assume an "Artist - Song" convention in the source video's title; a handful of stylistic variants are normalized automatically, but an artist whose uploads never include the artist name in a recognizable form won't be matched
- This is a personal project under active development, not a polished, versioned release

---

## License

MIT — see [LICENSE](LICENSE).

---

## Acknowledgements

Built with:
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) — YouTube download library
- [Flask](https://flask.palletsprojects.com/) — Python web framework
- [ffmpeg](https://ffmpeg.org/) — Multimedia processing
- [Pillow](https://python-pillow.org/) — Title-card poster generation
- [TheAudioDB](https://www.theaudiodb.com/), [Fanart.tv](https://fanart.tv/), [MusicBrainz](https://musicbrainz.org/), [Wikimedia Commons](https://commons.wikimedia.org/) — artist artwork sources
