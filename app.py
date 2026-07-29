import os
import json
import threading
import concurrent.futures
import time
import math
import datetime
import secrets
import requests
import state
import updates
import notify
import retention
import scheduler
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, send_file
from werkzeug.security import generate_password_hash, check_password_hash
from downloader import download_video, get_active_downloads, queue_download
from artwork_sync import (
    ArtworkWatcher, sync_artist_artwork, sync_all_artists,
    trigger_plex_refresh, setup_logging, folder_to_artist,
    plex_sync_artist_collection, plex_find_library_key, plex_list_libraries,
    plex_oauth_start, plex_oauth_check_pin,
    plex_get_account_info, plex_get_servers,
    plex_clean_video_titles, search_artist_images,
    has_artwork, _clean_video_title,
    plex_generate_title_cards_for_all,
    plex_find_duplicate_collections, plex_dedupe_collections,
    check_title_card_dependencies,
)
# Import the new swap helper
from artwork_swap import plex_swap_collection_artwork
import transcode
import yt_dlp
import logging

_log = logging.getLogger('app')
try:
    from yt_dlp.version import __version__ as yt_dlp_version
except ImportError:
    yt_dlp_version = getattr(yt_dlp, '__version__', 'unknown')

try:
    with open('VERSION', 'r') as _f:
        APP_VERSION = _f.read().strip()
except FileNotFoundError:
    APP_VERSION = 'unknown'


class _SuppressNoisyPollingEndpoints(logging.Filter):
    """Werkzeug logs every request at INFO level, including the dashboard's
    status-polling endpoints — hit every ~2s while a download or conversion
    job is being watched — which drowns out everything actually worth
    seeing in `docker logs` (download errors, conversion progress prints,
    etc.) under a wall of identical 200-OK polling lines. These specific
    endpoints are read-only, side-effect-free, and polled on a timer, not
    something a human clicked — filtering them out of the access log loses
    nothing; every other route still logs normally."""
    _NOISY_PATHS = ('/api/conversion/status', '/api/downloads/progress')

    def filter(self, record):
        message = record.getMessage()
        return not any(f'"GET {path} ' in message for path in self._NOISY_PATHS)


logging.getLogger('werkzeug').addFilter(_SuppressNoisyPollingEndpoints())

# Cache of full (unpaginated) artwork-image search results, keyed by
# lowercased artist name, so "Load More" pages through results already
# fetched instead of re-hitting TheAudioDB/Fanart.tv/MusicBrainz/Wikimedia
# on every click. {artist_lower: (fetched_at, [urls])}
_ARTWORK_SEARCH_CACHE = {}
_ARTWORK_SEARCH_CACHE_TTL = 600  # seconds
ARTWORK_SEARCH_PAGE_SIZE = 5

# Same pattern for music-video search results, keyed by lowercased artist
# query: the full ranked result set is fetched once from YouTube and cached
# so "Load More" pages through it instead of re-searching on every click.
# {artist_lower: (fetched_at, [ranked_video_dicts])}
_MUSIC_VIDEO_SEARCH_CACHE = {}
_MUSIC_VIDEO_SEARCH_CACHE_TTL = 600  # seconds
MUSIC_VIDEO_SEARCH_PAGE_SIZE = 9

# Every download (single-click, bulk "download all", or music video) is
# submitted here instead of a raw threading.Thread — an unbounded thread
# per download meant "Download All" (up to 20 videos) could kick off 20
# concurrent downloads *and* 20 concurrent format-conversion encodes at
# once. That combination is what actually OOM-killed ffmpeg during the
# format-conversion work (see REFERENCE.md) - capping concurrency here
# fixes it at the source instead of hoping conversions don't overlap.
# Extra requests past the cap queue automatically; queue_download() (see
# downloader.py) pre-registers each one as 'queued' before submission so
# they all show up in the progress UI immediately, not just once a worker
# picks them up.
_DOWNLOAD_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=int(os.environ.get('MAX_CONCURRENT_DOWNLOADS', '2')),
    thread_name_prefix='download'
)

# State now lives in a mounted directory (./data) and is written atomically —
# see state.py for why single-file bind mounts made both of those impossible.
# Re-exported under the original names so the rest of this module reads
# unchanged.
CONFIG_FILE = state.CONFIG_FILE
TRACKER_FILE = state.TRACKER_FILE
ACTIVE_DOWNLOADS_FILE = state.ACTIVE_DOWNLOADS_FILE

for _migration in state.MIGRATIONS_PERFORMED:
    print(f"[state] Migrated legacy state file: {_migration}")

# Youtube-only allowlist for any endpoint that hands a caller-supplied URL to
# yt-dlp — yt-dlp supports hundreds of sites via a "generic" extractor, so an
# unvalidated URL here would let the (single, trusted) admin account make the
# server issue requests to arbitrary hosts. /api/channels/add already
# enforced this; /api/channel/videos didn't, which was an inconsistency, not
# a deliberate design choice.
YOUTUBE_URL_PREFIXES = ('https://www.youtube.com/', 'https://youtube.com/', 'https://youtu.be/')

# State for the batch "convert existing library to Plex-compatible format"
# job (see transcode.py). In-memory only (not persisted like
# active_downloads.json) — this is a one-shot maintenance job, not something
# that needs to survive a restart; a container restart mid-job just stops
# it, and re-running "Scan" shows whatever's still left since
# needs_conversion() is a stateless check re-run fresh each time.
_CONVERSION_STATE = {
    'running': False,
    'phase': 'idle',  # 'idle' | 'scanning' | 'converting'
    'started_at': None,
    'finished_at': None,
    'total_files': 0,
    'scanned': 0,
    'converted': 0,
    'failed': 0,
    'current_file': None,
    'errors': [],
}
_CONVERSION_LOCK = threading.Lock()
_CONVERSION_SCRATCH_DIR = os.path.join('.', 'downloads', '_conversion_scratch')

def load_config():
    return state.read_json(CONFIG_FILE)

def _read_raw_config():
    """Like load_config(), but never raises — used during startup, before
    the app (and thus any request context) exists."""
    return state.read_json(CONFIG_FILE)

def _write_raw_config(config):
    state.write_json(CONFIG_FILE, config, indent=4)

def _update_config(mutate):
    """Read-modify-write config.json under a lock, atomically.

    Prefer this over load_config() + _write_raw_config() anywhere the new
    value depends on the current one: Flask serves requests on threads, so
    two settings saves (or a save racing the Plex OAuth callback, which
    persists a token from a background poll) would otherwise each write a
    full document built from a stale read, and the loser's field silently
    disappears.
    """
    return state.update_json(CONFIG_FILE, mutate, indent=4)

def _get_or_create_secret_key():
    """Flask's secret key signs session cookies — anyone who knows it can
    forge a valid 'logged in as admin' session outright. This used to be a
    fixed string committed to source control, so anyone who'd ever seen this
    repo could forge a session against any deployment still using it.

    Prefer a SECRET_KEY env var; otherwise persist a freshly generated
    random key in config.json (under a leading-underscore key so it reads
    as internal state, not a user-facing setting) so sessions survive a
    container restart without ever falling back to a known value.
    """
    env_key = os.environ.get('SECRET_KEY')
    if env_key:
        return env_key
    config = _read_raw_config()
    existing = config.get('_secret_key')
    if existing:
        return existing
    new_key = secrets.token_hex(32)
    config['_secret_key'] = new_key
    try:
        _write_raw_config(config)
    except Exception:
        print("[SECURITY] Could not persist generated secret key to config.json — "
              "sessions will not survive a restart until this is writable.")
    return new_key

def _get_or_create_admin_credentials():
    """Load the admin username + password hash from config.json, seeding it
    on first run instead of a fixed 'admin'/'adminadmin' checked into
    source. Also fixes a related bug: the old /api/password handler only
    updated an in-memory dict, so any password change was silently reverted
    on the next restart — this persists changes to config.json instead.

    First-run behavior: ADMIN_USERNAME/ADMIN_PASSWORD env vars are used if
    set; otherwise a random password is generated and printed once so it can
    still be retrieved from `docker logs`.
    """
    config = _read_raw_config()
    creds = config.get('_auth', {})
    if creds.get('username') and creds.get('password_hash'):
        return creds['username'], creds['password_hash']

    # `or 'admin'` (not .get(..., 'admin')) because docker-compose passes
    # ADMIN_USERNAME='' — not an absent key — when the .env var is unset, and
    # os.environ.get() with a default only applies it for a missing key, not
    # an empty-string value.
    username = os.environ.get('ADMIN_USERNAME') or 'admin'
    password = os.environ.get('ADMIN_PASSWORD')
    generated = False
    if not password:
        password = secrets.token_urlsafe(12)
        generated = True
    password_hash = generate_password_hash(password)
    config['_auth'] = {'username': username, 'password_hash': password_hash}
    try:
        _write_raw_config(config)
    except Exception:
        print("[SECURITY] Could not persist generated admin credentials to config.json.")
    if generated:
        print(f"[SECURITY] No ADMIN_PASSWORD set — generated a random admin password on "
              f"first run: {password!r} (username: {username!r}). This is only printed "
              f"once; change it via the dashboard's Settings page, or set ADMIN_PASSWORD "
              f"and delete the '_auth' key from config.json to reset it.")
    return username, password_hash

app = Flask(__name__)
app.secret_key = _get_or_create_secret_key()
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
# Off by default since this is commonly reached over plain HTTP on a LAN;
# set SESSION_COOKIE_SECURE=true if this is ever put behind HTTPS/a reverse proxy.
app.config['SESSION_COOKIE_SECURE'] = os.environ.get('SESSION_COOKIE_SECURE', '').lower() == 'true'

_ADMIN_USERNAME, _ADMIN_PASSWORD_HASH = _get_or_create_admin_credentials()

# Simple in-memory login throttle, keyed by client IP. Resets on restart —
# acceptable here since this is a single-container, single-account app, not
# a distributed service; the goal is just to make the default/first-run
# credential meaningfully harder to brute-force, not to build a full
# rate-limiting subsystem.
_LOGIN_FAILURES = {}  # ip -> (fail_count, locked_until_epoch)
_LOGIN_MAX_ATTEMPTS = 5
_LOGIN_LOCKOUT_SECONDS = 300

def _login_is_locked(ip):
    count, locked_until = _LOGIN_FAILURES.get(ip, (0, 0))
    return count >= _LOGIN_MAX_ATTEMPTS and time.time() < locked_until

def _record_login_failure(ip):
    count, locked_until = _LOGIN_FAILURES.get(ip, (0, 0))
    count += 1
    if count >= _LOGIN_MAX_ATTEMPTS:
        locked_until = time.time() + _LOGIN_LOCKOUT_SECONDS
    _LOGIN_FAILURES[ip] = (count, locked_until)

def _clear_login_failures(ip):
    _LOGIN_FAILURES.pop(ip, None)

@app.after_request
def _set_security_headers(response):
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('X-Frame-Options', 'DENY')
    response.headers.setdefault('Referrer-Policy', 'same-origin')
    return response

def _notify_download(kind, title, channel_url, error=None):
    """Fire a download notification. Never raises — callers are worker threads.

    Reads config per call rather than caching it, so toggling notifications in
    Settings takes effect on the next download instead of the next restart.
    """
    try:
        config = load_config()
        if kind == 'failed':
            notify.send(config, notify.EVENT_DOWNLOAD_FAILED,
                        f'Vidshelf: download failed — {title}',
                        f'Channel: {channel_url}\nError: {error}')
        else:
            notify.send(config, notify.EVENT_DOWNLOAD_COMPLETE,
                        f'Vidshelf: downloaded — {title}',
                        f'Channel: {channel_url}')
    except Exception as exc:  # noqa: BLE001
        print(f'[notify] could not dispatch {kind} notification: {exc}')


def load_downloaded_tracker():
    return state.read_json(TRACKER_FILE)

def save_downloaded_tracker(tracker):
    state.write_json(TRACKER_FILE, tracker, indent=2)

def mark_video_downloaded(video_id, channel_url):
    """Record a video as downloaded.

    The read-modify-write has to happen under a single lock: this runs on the
    bounded download pool (_DOWNLOAD_EXECUTOR), so with concurrency > 1 two
    downloads finishing close together would both load the same tracker, each
    append only its own video, and whichever wrote second would drop the
    other's entry. The dropped video then looks new on the next channel check
    and gets downloaded all over again.
    """
    def _add(tracker):
        tracker.setdefault(channel_url, [])
        if video_id not in tracker[channel_url]:
            tracker[channel_url].append(video_id)

    state.update_json(TRACKER_FILE, _add, indent=2)

def is_video_downloaded(video_id, channel_url):
    tracker = load_downloaded_tracker()
    return video_id in tracker.get(channel_url, [])

def get_channel_info(channel_url):
    """Lightweight fetch of just the channel display name."""
    try:
        ydl_opts = {
            'quiet': True,
            'extract_flat': True,
            'dump_single_json': True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(channel_url, download=False)
            return info.get('channel') or info.get('uploader') or 'Unknown Channel'
    except Exception:
        return None

def get_channel_videos(channel_url):
    """Fetch latest videos from a channel with full metadata in a single call."""
    if '/@' in channel_url and not channel_url.endswith('/videos'):
        channel_url = channel_url.rstrip('/') + '/videos'
    elif '/channel/' in channel_url and not channel_url.endswith('/videos'):
        channel_url = channel_url.rstrip('/') + '/videos'

    ydl_opts = {
        'quiet': True,
        'extract_flat': True,
        'dump_single_json': True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info_dict = ydl.extract_info(channel_url, download=False)
        entries = info_dict.get('entries', [])
        if not entries:
            return []
        videos = []
        for entry in entries:
            if entry is None:
                continue
            video_id = entry.get('id')
            if not video_id or len(video_id) != 11:
                continue
            thumbnail = f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg"
            videos.append({
                'id': video_id,
                'title': entry.get('title') or 'Unknown Title',
                'duration': entry.get('duration'),
                'view_count': entry.get('view_count'),
                'upload_date': entry.get('upload_date'),
                'thumbnail': thumbnail,
                'channel': info_dict.get('channel') or entry.get('uploader') or info_dict.get('uploader'),
                'description': ''
            })
        return videos

# ---------- Music Video Search Helpers ----------

def search_music_videos(artist):
    """Search YouTube for music videos by an artist using yt-dlp search."""
    # Three differently-angled queries instead of two near-duplicates
    # ("music video official" vs "official music video" return almost
    # identical result sets) - a bare-artist query catches uploads that
    # don't literally say "official"/"music video" in the title, and a
    # vevo-scoped query specifically surfaces official-channel uploads.
    # Deeper per-query result counts than before (15 -> 20-25) since the
    # full set is now cached and paged through server-side (see
    # api_music_videos_search) instead of being the only chance to see
    # more than ~15-30 results.
    queries = [
        f"ytsearch25:{artist} official music video",
        f"ytsearch20:{artist} vevo",
        f"ytsearch20:{artist}",
    ]
    
    seen_ids = set()
    all_results = []
    
    for query in queries:
        ydl_opts = {
            'quiet': True,
            'extract_flat': True,
            'dump_single_json': True,
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                results = ydl.extract_info(query, download=False)
                entries = results.get('entries', [])
                for entry in entries:
                    if entry is None:
                        continue
                    video_id = entry.get('id')
                    if not video_id or len(video_id) != 11:
                        continue
                    if video_id in seen_ids:
                        continue
                    seen_ids.add(video_id)
                    
                    thumbnail = f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg"
                    uploader = entry.get('uploader') or entry.get('channel') or 'Unknown'
                    
                    all_results.append({
                        'id': video_id,
                        'title': entry.get('title') or 'Unknown Title',
                        'duration': entry.get('duration'),
                        'view_count': entry.get('view_count'),
                        'upload_date': entry.get('upload_date'),
                        'thumbnail': thumbnail,
                        'channel': uploader,
                        'description': entry.get('description', '')[:200] if entry.get('description') else ''
                    })
        except Exception:
            continue
    
    return rank_videos_by_quality(all_results, artist)


def rank_videos_by_quality(videos, artist):
    """Rank music video results by quality signals to find the best versions."""
    artist_lower = artist.lower().strip()
    artist_simple = artist_lower.replace(' ', '').replace('.', '').replace('&', 'and')
    
    # Common known official music video channels. Deliberately excludes
    # "topic" - YouTube auto-generates "Artist - Topic" channels for
    # audio-only uploads (a static image, no real video), so treating them
    # as "official" for a *music video* search ranked them above genuine
    # official video uploads whenever one happened to appear in results.
    official_channel_patterns = [
        artist_lower,
        artist_simple,
        f"{artist_simple}vevo",
        f"{artist_lower}vevo",
        "vevo",
        "official",
    ]
    
    for v in videos:
        score = 0
        title_lower = v.get('title', '').lower()
        channel_lower = v.get('channel', '').lower()
        channel_simple = channel_lower.replace(' ', '').replace('.', '').replace('&', 'and')
        
        # --- Official channel detection (highest weight) ---
        # Check if channel name contains artist name or VEVO/Topic patterns
        is_official_channel = False
        for pattern in official_channel_patterns:
            if pattern in channel_simple or pattern in channel_lower:
                is_official_channel = True
                break
        
        if is_official_channel:
            score += 50
        
        # Extra bonus for exact artist match in channel name
        if artist_simple == channel_simple or artist_lower == channel_lower:
            score += 20
        
        # --- Title quality signals ---
        official_keywords = ['official', 'music video', 'official video', 'official music video',
                           'hq', 'hd', '4k', 'lyric video', 'audio']
        for kw in official_keywords:
            if kw in title_lower:
                score += 5
        
        # Penalize cover/karaoke/remix/live (unless we want those)
        low_quality_keywords = ['cover', 'karaoke', 'remix', 'live', 'tutorial', 'how to play',
                                'reaction', 'guitar lesson', 'drum cover', 'acoustic cover',
                                'instrumental', 'nightcore', 'sped up', 'slowed',
                                'trailer', 'teaser']
        for kw in low_quality_keywords:
            if kw in title_lower:
                score -= 10

        # Penalize very short durations - almost certainly a YouTube Short
        # or a teaser clip, not an actual music video, regardless of what
        # the title/channel otherwise suggested.
        duration = v.get('duration') or 0
        if 0 < duration < 60:
            score -= 15

        # Artist name in title
        if artist_lower in title_lower or artist_simple in title_lower.replace(' ', ''):
            score += 10

        # --- View count score (logarithmic, up to 20 pts) ---
        views = v.get('view_count') or 0
        if views > 0:
            view_score = min(20, math.log10(views) * 3)
            score += view_score

        # --- Recency bonus (up to 10 pts) ---
        upload_date = v.get('upload_date') or ''
        if upload_date and len(upload_date) == 8:
            try:
                upload_dt = datetime.datetime.strptime(upload_date, '%Y%m%d')
                days_old = (datetime.datetime.now() - upload_dt).days
                if days_old < 30:
                    score += 10  # Very recent
                elif days_old < 365:
                    score += 5   # Within the year
                elif days_old < 1825:
                    score += 2   # Within 5 years
            except ValueError:
                pass
        
        v['score'] = round(score, 1)
    
    # Sort by score descending
    videos.sort(key=lambda x: x.get('score', 0), reverse=True)
    return videos


def get_video_formats_info(video_id):
    """Get available format qualities for a video to determine best quality available."""
    try:
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f'https://www.youtube.com/watch?v={video_id}', download=False)
            formats = info.get('formats', [])
            
            # Extract the best available quality label
            max_height = 0
            best_quality = 'unknown'
            has_audio = False
            
            for f in formats:
                height = f.get('height') or 0
                if height > max_height and f.get('vcodec') != 'none':
                    max_height = height
            
            if max_height >= 2160:
                best_quality = '4K'
            elif max_height >= 1440:
                best_quality = '1440p'
            elif max_height >= 1080:
                best_quality = '1080p'
            elif max_height >= 720:
                best_quality = '720p'
            elif max_height >= 480:
                best_quality = '480p'
            elif max_height > 0:
                best_quality = f'{max_height}p'
            
            return {
                'best_quality': best_quality,
                'max_height': max_height,
                'duration': info.get('duration'),
                'title': info.get('title'),
                'channel': info.get('channel') or info.get('uploader'),
                'view_count': info.get('view_count'),
            }
    except Exception:
        return {
            'best_quality': 'unknown',
            'max_height': 0,
            'duration': None,
            'title': None,
            'channel': None,
            'view_count': None,
        }


# ---------- Routes ----------

@app.route('/')
def index():
    if 'username' in session:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        ip = request.remote_addr or 'unknown'
        if _login_is_locked(ip):
            flash('Too many failed login attempts. Please try again in a few minutes.', 'error')
            return render_template('login.html')

        username = request.form.get('username', '')
        password = request.form.get('password', '')
        if (secrets.compare_digest(username, _ADMIN_USERNAME)
                and check_password_hash(_ADMIN_PASSWORD_HASH, password)):
            _clear_login_failures(ip)
            session['username'] = username
            flash('Login successful!', 'success')
            return redirect(url_for('dashboard'))
        else:
            _record_login_failure(ip)
            flash('Invalid username or password.', 'error')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.pop('username', None)
    flash('You have been logged out.', 'info')
    return redirect(url_for('login'))

@app.route('/dashboard')
def dashboard():
    if 'username' not in session:
        flash('Please log in first.', 'error')
        return redirect(url_for('login'))
    return render_template('dashboard.html', username=session['username'])

# ---------- API Routes ----------

@app.route('/api/channels')
def api_channels():
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    config = load_config()
    channels = []
    for ch in config.get('channels', []):
        display_name = get_channel_info(ch['url'])
        channels.append({
            'url': ch['url'],
            'display_name': display_name or 'Unknown Channel',
            'download_path': ch['download_path'],
            'plex_media_path': ch.get('plex_media_path', './downloads'),
            'download_mode': ch.get('download_mode', 'manual')
        })
    return jsonify({'channels': channels})

@app.route('/api/channel/videos')
def api_channel_videos():
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    channel_url = request.args.get('url', '')
    if not channel_url:
        return jsonify({'error': 'Missing channel URL'}), 400
    if not channel_url.startswith(YOUTUBE_URL_PREFIXES):
        return jsonify({'error': 'Invalid YouTube URL'}), 400
    try:
        videos = get_channel_videos(channel_url)
        return jsonify({'videos': videos[:50]})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

def _resolve_plex_path(per_channel_plex_path):
    """Resolve the actual Plex destination path.
    
    If the per-channel plex_media_path is relative (starts with '.' or no drive letter),
    join it with the global plex_base_path. Otherwise use it as-is (supports UNC/absolute).
    """
    config = load_config()
    base = config.get('plex_base_path', './downloads')
    path = (per_channel_plex_path or './downloads').strip()

    # If path is absolute (has drive letter like D:\ or starts with \\ for UNC), use as-is
    if os.path.isabs(path):
        return path
    # If it starts with ./ or is just a relative folder name, resolve relative to base
    resolved = os.path.normpath(os.path.join(base, path))
    return resolved


def _gather_media_roots(config):
    """Every directory this app might have downloaded videos into: the
    music-video root plus every configured channel's resolved
    plex_media_path plus the global plex_base_path, deduplicated."""
    roots = set()
    artwork_cfg = config.get('artwork_sync', {})
    music_root = artwork_cfg.get('root_path', '/app/music_videos_final')
    if os.path.isdir(music_root):
        roots.add(os.path.normpath(music_root))
    for ch in config.get('channels', []):
        resolved = _resolve_plex_path(ch.get('plex_media_path', './downloads'))
        if os.path.isdir(resolved):
            roots.add(os.path.normpath(resolved))
    base = config.get('plex_base_path', './downloads')
    if os.path.isdir(base):
        roots.add(os.path.normpath(base))
    return sorted(roots)


def _scan_conversion_candidates(config):
    """Walk every known media root and return the full paths of video files
    that aren't already Plex-direct-play-compatible."""
    candidates = []
    for root in _gather_media_roots(config):
        for dirpath, _dirnames, filenames in os.walk(root):
            for fname in sorted(filenames):
                if not fname.lower().endswith(transcode.VIDEO_EXTENSIONS):
                    continue
                full_path = os.path.join(dirpath, fname)
                if transcode.needs_conversion(full_path):
                    candidates.append(full_path)
    return candidates


def _run_conversion_job(config):
    """Background job: convert every non-compatible video file found under
    the app's known media roots. Runs in its own daemon thread — see
    /api/conversion/start.

    Marks itself 'running' before the (potentially slow — one ffprobe call
    per existing file) scan step, not just once conversion actually starts.
    Otherwise a client polling /api/conversion/status right after "start"
    returns would see running=False/total_files=0 and could easily mistake
    "still scanning" for "job already finished".
    """
    with _CONVERSION_LOCK:
        _CONVERSION_STATE.update({
            'running': True,
            'phase': 'scanning',
            'started_at': time.time(),
            'finished_at': None,
            'total_files': 0,
            'scanned': 0,
            'converted': 0,
            'failed': 0,
            'current_file': None,
            'errors': [],
        })

    candidates = _scan_conversion_candidates(config)

    with _CONVERSION_LOCK:
        _CONVERSION_STATE['phase'] = 'converting'
        _CONVERSION_STATE['total_files'] = len(candidates)

    for path in candidates:
        with _CONVERSION_LOCK:
            _CONVERSION_STATE['current_file'] = path
        try:
            result = transcode.convert_file_safely(path, _CONVERSION_SCRATCH_DIR)
        except Exception as exc:
            result = {'success': False, 'error': str(exc)}
        with _CONVERSION_LOCK:
            _CONVERSION_STATE['scanned'] += 1
            if result.get('success'):
                _CONVERSION_STATE['converted'] += 1
            else:
                _CONVERSION_STATE['failed'] += 1
                _CONVERSION_STATE['errors'].append(f"{path}: {result.get('error')}")

        if result.get('success'):
            # Plex needs a library scan to notice a file that got renamed
            # (extension change, e.g. .mkv -> .mp4) - without this,
            # converted items just look "missing" for however long is left
            # of what can be a many-hour job, not just at the very end.
            # Refreshing after every file matches this codebase's existing
            # pattern (plex_sync_artist_collection() already triggers a
            # refresh on every single download, not batched) - cheap to
            # call, and Plex coalesces rapid repeat requests on its own.
            try:
                trigger_plex_refresh(config)
            except Exception as exc:
                _log.warning("Post-conversion Plex refresh failed for %s: %s", path, exc)

    with _CONVERSION_LOCK:
        _CONVERSION_STATE['running'] = False
        _CONVERSION_STATE['phase'] = 'idle'
        _CONVERSION_STATE['current_file'] = None
        _CONVERSION_STATE['finished_at'] = time.time()


@app.route('/api/download', methods=['POST'])
def api_download():
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.get_json()
    video_id = data.get('video_id', '')
    channel_url = data.get('channel_url', '')
    title = data.get('title', 'Unknown')
    if not video_id:
        return jsonify({'error': 'Missing video_id'}), 400

    config = load_config()
    download_path = './downloads'
    plex_media_path = './downloads'

    for ch in config.get('channels', []):
        if ch['url'] == channel_url:
            download_path = ch['download_path']
            plex_media_path = _resolve_plex_path(ch.get('plex_media_path', './downloads'))
            break

    download_id = queue_download(video_id, title, channel_url, final_path=plex_media_path)

    def _do_download():
        try:
            os.makedirs(plex_media_path, exist_ok=True)
            download_video(video_id, download_path, plex_media_path,
                          title=title, channel_url=channel_url, download_id=download_id)
            mark_video_downloaded(video_id, channel_url)
            _notify_download('complete', title, channel_url)
        except Exception as exc:  # noqa: BLE001
            # Still swallowed — a failed download must not take down the worker
            # thread — but no longer silently. Before notifications existed the
            # only trace of a failure was a line in `docker logs`, which nobody
            # reads until they notice something missing.
            print(f'[download] FAILED {video_id} ({title}): {exc}')
            _notify_download('failed', title, channel_url, error=exc)

    _DOWNLOAD_EXECUTOR.submit(_do_download)
    return jsonify({'success': True, 'message': f'Download started for {video_id}', 'video_id': video_id})

@app.route('/api/downloads/progress')
def api_downloads_progress():
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    return jsonify({'downloads': get_active_downloads()})

@app.route('/api/channels/add', methods=['POST'])
def api_channels_add():
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.get_json()
    url = data.get('url', '').strip()
    download_path = data.get('download_path', './downloads').strip()
    plex_media_path = data.get('plex_media_path', './downloads').strip()

    if not url:
        return jsonify({'error': 'Channel URL is required'}), 400
    if not url.startswith(YOUTUBE_URL_PREFIXES):
        return jsonify({'error': 'Invalid YouTube URL'}), 400

    config = load_config()
    for ch in config.get('channels', []):
        if ch['url'] == url:
            return jsonify({'error': 'Channel already exists'}), 409

    if 'channels' not in config:
        config['channels'] = []
    download_mode = data.get('download_mode', 'manual')
    if download_mode not in ('all', 'new', 'manual'):
        download_mode = 'manual'

    config['channels'].append({
        'url': url,
        'download_path': download_path,
        'plex_media_path': plex_media_path,
        'download_mode': download_mode
    })
    _write_raw_config(config)
    return jsonify({'success': True, 'message': 'Channel added successfully'}), 201

@app.route('/api/channels/remove', methods=['POST'])
def api_channels_remove():
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.get_json()
    url = data.get('url', '').strip()

    if not url:
        return jsonify({'error': 'Channel URL is required'}), 400

    config = load_config()
    original_len = len(config.get('channels', []))
    config['channels'] = [ch for ch in config.get('channels', []) if ch['url'] != url]

    if len(config['channels']) == original_len:
        return jsonify({'error': 'Channel not found'}), 404

    _write_raw_config(config)
    return jsonify({'success': True, 'message': 'Channel removed successfully'})

@app.route('/api/channels/mode', methods=['POST'])
def api_channels_mode():
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.get_json()
    url = data.get('url', '').strip()
    mode = data.get('download_mode', 'manual')

    if not url:
        return jsonify({'error': 'Channel URL is required'}), 400
    if mode not in ('all', 'new', 'manual'):
        return jsonify({'error': 'Invalid mode. Use: all, new, or manual'}), 400

    config = load_config()
    for ch in config.get('channels', []):
        if ch['url'] == url:
            ch['download_mode'] = mode
            _write_raw_config(config)
            return jsonify({'success': True, 'message': f'Download mode set to {mode}'})

    return jsonify({'error': 'Channel not found'}), 404

@app.route('/api/channels/download-all', methods=['POST'])
def api_channels_download_all():
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.get_json()
    channel_url = data.get('url', '').strip()

    config = load_config()
    download_path = './downloads'
    plex_media_path = './downloads'
    mode = 'manual'

    for ch in config.get('channels', []):
        if ch['url'] == channel_url:
            download_path = ch['download_path']
            plex_media_path = _resolve_plex_path(ch.get('plex_media_path', './downloads'))
            mode = ch.get('download_mode', 'manual')
            break
    else:
        return jsonify({'error': 'Channel not found'}), 404

    try:
        videos = get_channel_videos(channel_url)
    except Exception as e:
        return jsonify({'error': f'Failed to fetch videos: {str(e)}'}), 500

    def _download_one(video, path, plex, url, download_id):
        vid = video['id']
        title = video.get('title', 'Unknown')
        try:
            download_video(vid, path, plex, title=title, channel_url=url, download_id=download_id)
            mark_video_downloaded(vid, url)
            _notify_download('complete', title, url)
        except Exception as exc:  # noqa: BLE001
            print(f'[download] FAILED {vid} ({title}): {exc}')
            _notify_download('failed', title, url, error=exc)

    started = []
    for video in videos[:20]:
        vid = video['id']
        if mode == 'new' and is_video_downloaded(vid, channel_url):
            continue

        title = video.get('title', 'Unknown')
        download_id = queue_download(vid, title, channel_url, final_path=plex_media_path)
        _DOWNLOAD_EXECUTOR.submit(_download_one, video, download_path, plex_media_path, channel_url, download_id)
        started.append(vid)

    return jsonify({
        'success': True,
        'message': f'Started downloading {len(started)} videos (check Downloads page for progress)',
        'started_count': len(started)
    })

@app.route('/api/stats')
def api_stats():
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    tracker = load_downloaded_tracker()
    downloads_count = sum(len(vids) for vids in tracker.values())
    videos_count = sum(len(vids) for vids in tracker.values())

    disk_usage = 0
    for dirpath, _, filenames in os.walk('./downloads'):
        for f in filenames:
            try:
                fp = os.path.join(dirpath, f)
                disk_usage += os.path.getsize(fp)
            except OSError:
                pass

    return jsonify({
        'videos_count': videos_count,
        'downloads_count': downloads_count,
        'disk_usage': disk_usage
    })

@app.route('/api/config', methods=['GET', 'POST'])
def api_config():
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    if request.method == 'GET':
        # Strip internal-only keys (session-signing secret, password hash)
        # before handing config.json back to the frontend — this endpoint
        # feeds the Settings page's raw config editor, and there's no reason
        # for either value to round-trip through the browser at all.
        config = {k: v for k, v in load_config().items() if k not in ('_secret_key', '_auth')}
        return jsonify(config)
    elif request.method == 'POST':
        # This endpoint replaces the whole document, and the Settings page's
        # editor only round-trips the keys the GET above returned — so a save
        # would otherwise drop anything it didn't know about.
        #
        # Every leading-underscore key is preserved, not a hardcoded list of
        # two. The original code named _secret_key and _auth explicitly, which
        # was correct when those were the only internal keys and silently wrong
        # the moment _plex_client_id and update_check_enabled appeared: losing
        # _plex_client_id makes every restart look like a new device to Plex,
        # and it fails without an error anyone would notice. Treating the
        # underscore prefix as the rule means the next internal key added is
        # protected by default rather than by remembering to edit this line.
        new_config = request.get_json()
        if not isinstance(new_config, dict):
            return jsonify({'error': 'Expected a JSON object'}), 400

        def _merge(current):
            preserved = {k: v for k, v in current.items() if k.startswith('_')}
            merged = dict(new_config)
            # A caller may legitimately set an internal key (the GET response
            # exposes _plex_client_id, so the editor can round-trip it) — only
            # fill in the ones it left out.
            for key, value in preserved.items():
                merged.setdefault(key, value)
            # Non-underscore settings that aren't user-facing config but are
            # written by other endpoints would otherwise be dropped too.
            for key in ('update_check_enabled',):
                if key in current:
                    merged.setdefault(key, current[key])
            return merged

        _update_config(_merge)
        return jsonify({'success': True, 'message': 'Configuration updated'})

def _sanitize_folder_name(name):
    """Sanitize a name for use as a folder name, removing invalid filesystem characters."""
    invalid_chars = '<>:"/\\|?*'
    for c in invalid_chars:
        name = name.replace(c, '_')
    # Remove leading/trailing spaces and dots (problematic on Windows)
    name = name.strip().strip('.')
    # Collapse multiple spaces/underscores
    import re
    name = re.sub(r'[_\s]+', '_', name)
    if not name:
        name = 'Unknown_Artist'
    return name


def _resolve_existing_artist(query, root_path):
    """The Music Videos search box doubles as both a YouTube search query
    and (via the "artist" field sent to /api/music-videos/download) the
    identity used to create/join a folder + Plex collection. Typing a more
    specific search — adding a song title to narrow results, e.g. "Weird Al
    Amish" to find the "Amish Paradise" video — is a perfectly good search
    query, but taking it at face value as the *artist* forks a second
    near-duplicate folder/collection ("Weird Al Amish") instead of joining
    the existing "Weird Al" one.

    If `query` exactly matches, or starts with, an already-known artist
    folder's name (case-insensitive, on a word boundary), snap to that
    artist's canonical (folder-derived) name instead. Returns `query`
    unchanged if no existing artist matches — a genuinely new artist is
    still named from whatever the caller supplied.
    """
    if not os.path.isdir(root_path):
        return query
    query_lower = query.lower()
    known = [folder_to_artist(entry) for entry in os.listdir(root_path)
             if os.path.isdir(os.path.join(root_path, entry)) and not entry.startswith('.')]
    # Longest first so a longer existing artist name wins over a shorter
    # one that happens to also be a prefix (e.g. prefer "Death Cab for
    # Cutie" over a hypothetical shorter unrelated overlap).
    for name in sorted(known, key=len, reverse=True):
        name_lower = name.lower()
        if query_lower == name_lower or query_lower.startswith(name_lower + ' '):
            return name
    return query


# ---------- Music Video API Endpoints ----------

@app.route('/favicon.ico')
def favicon():
    """Serve a favicon as SVG inline to avoid missing favicon errors."""
    svg = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
  <defs>
    <linearGradient id="g" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#e94560"/>
      <stop offset="100%" stop-color="#e97a45"/>
    </linearGradient>
  </defs>
  <rect width="64" height="64" rx="14" fill="url(#g)"/>
  <text x="32" y="44" font-family="Arial,sans-serif" font-size="36" font-weight="bold" fill="#fff" text-anchor="middle">Y</text>
</svg>'''
    from flask import Response
    return Response(svg, mimetype='image/svg+xml')


@app.route('/api/music-videos/search', methods=['POST'])
def api_music_videos_search():
    """Search for music videos by artist name, returns ranked results.

    Paginated + cached, same pattern as /api/artwork/search_noauth: the
    full ranked result set is fetched once per artist query and cached for
    _MUSIC_VIDEO_SEARCH_CACHE_TTL seconds, so clicking "Load More" slices
    the cached list instead of re-searching YouTube on every page. Only the
    page actually being returned gets the (slower — one extra yt-dlp call
    per video) format-quality enrichment, not the whole cached set up
    front.
    """
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.get_json()
    artist = data.get('artist', '').strip()
    if not artist:
        return jsonify({'error': 'Artist name is required'}), 400
    try:
        page = max(1, int(data.get('page', 1)))
    except (TypeError, ValueError):
        page = 1

    try:
        cache_key = artist.lower()
        cached = _MUSIC_VIDEO_SEARCH_CACHE.get(cache_key)
        if cached and (time.time() - cached[0]) < _MUSIC_VIDEO_SEARCH_CACHE_TTL:
            videos = cached[1]
        else:
            videos = search_music_videos(artist)
            _MUSIC_VIDEO_SEARCH_CACHE[cache_key] = (time.time(), videos)

        start = (page - 1) * MUSIC_VIDEO_SEARCH_PAGE_SIZE
        end = start + MUSIC_VIDEO_SEARCH_PAGE_SIZE
        page_videos = videos[start:end]

        # Enrich only this page — these dicts are the same objects held in
        # the cache, so a video that reappears (e.g. overlap between pages
        # after a re-search) doesn't get re-probed either.
        for v in page_videos:
            if 'best_quality' not in v:
                fmt_info = get_video_formats_info(v['id'])
                v['best_quality'] = fmt_info.get('best_quality', 'unknown')

        return jsonify({
            'videos': page_videos,
            'artist': artist,
            'page': page,
            'page_size': MUSIC_VIDEO_SEARCH_PAGE_SIZE,
            'total': len(videos),
            'has_more': end < len(videos),
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/music-videos/download', methods=['POST'])
def api_music_videos_download():
    """Download a music video to the configured music video Plex path."""
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.get_json()
    video_id = data.get('video_id', '')
    title = data.get('title', 'Music Video')
    artist = data.get('artist', 'Unknown Artist')

    if not video_id:
        return jsonify({'error': 'Missing video_id'}), 400

    # Download locally first (ext4 filesystem) to avoid CIFS/os.sendfile issues,
    # then download_video will copy the finished file to the final Y:\ mount.
    download_path = './downloads/music_videos'
    final_path = '/app/music_videos_final'

    # The search box doubles as a YouTube search query, so a more specific
    # search (artist + song, to narrow results) shouldn't fork a second
    # near-duplicate artist folder/collection if the artist is already
    # known — snap back to the existing artist's canonical name.
    artist = _resolve_existing_artist(artist, final_path)

    channel_url = f"music_video_{artist.replace(' ', '_')}"
    download_id = queue_download(video_id, title, channel_url, final_path=final_path)

    def _do_download():
        print(f"DEBUG: _do_download thread started for video_id={video_id}")
        try:
            os.makedirs(download_path, exist_ok=True)
            # Create artist-specific subfolder under the final path
            artist_folder = _sanitize_folder_name(artist)
            artist_final_path = os.path.join(final_path, artist_folder)
            os.makedirs(artist_final_path, exist_ok=True)
            print(f"DEBUG: Directories created/ensured: download_path='{download_path}', artist_final_path='{artist_final_path}'")
            # Download locally, then download_video copies to artist_final_path
            download_video(video_id, download_path, artist_final_path,
                          title=title, channel_url=channel_url, download_id=download_id)
            mark_video_downloaded(video_id, channel_url)
            print(f"DEBUG: Music video download completed successfully for video_id={video_id} into artist folder '{artist_folder}'")

            # Ensure artwork + a Plex smart collection exist for this artist
            # right away, rather than waiting for ArtworkWatcher's next poll
            # (which only ever notices brand-new folders, not new videos
            # added to an artist that already has a folder). Both calls are
            # idempotent - safe to run on every download.
            config = load_config()
            artwork_cfg = config.get('artwork_sync', {})
            if artwork_cfg.get('plex_collection_sync_on_artwork', False):
                try:
                    sync_artist_artwork(artist_final_path, artwork_cfg)
                    plex_sync_artist_collection(config, artist, artist_final_path)
                    print(f"DEBUG: Plex collection sync completed for artist '{artist}'")
                except Exception as exc:
                    print(f"[ERROR] Plex collection sync failed for artist '{artist}': {exc}")
        except Exception as exc:
            import traceback
            traceback.print_exc()
            print(f"[ERROR] Music video download failed for video_id={video_id}: {exc}")
    
    _DOWNLOAD_EXECUTOR.submit(_do_download)
    return jsonify({
        'success': True,
        'message': f'Download started for {title}',
        'video_id': video_id
    })


@app.route('/api/music-video-path', methods=['GET', 'POST'])
def api_music_video_path():
    """Get or set the music video Plex path in config."""
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    config = load_config()
    
    if request.method == 'GET':
        current_path = config.get('music_video_plex_path', './downloads/music_videos')
        return jsonify({'music_video_plex_path': current_path})
    
    elif request.method == 'POST':
        data = request.get_json()
        new_path = data.get('music_video_plex_path', '').strip()
        if not new_path:
            return jsonify({'error': 'Music video Plex path is required'}), 400
        config['music_video_plex_path'] = new_path
        _write_raw_config(config)
        return jsonify({'success': True, 'message': f'Music video Plex path set to {new_path}'})


# ---------- Folder Browser ----------

@app.route('/api/browse-folder', methods=['POST'])
def api_browse_folder():
    """List subdirectories of a given path for the folder browser modal."""
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.get_json()
    path = data.get('path', '').strip()

    # If no path provided, return all available drives (Windows) or root (/)
    if not path:
        if os.name == 'nt':
            import string
            drives = []
            for d in string.ascii_uppercase:
                drive = d + ':\\'
                if os.path.exists(drive):
                    try:
                        label = os.path.getvolumeinformation(drive)[0] if hasattr(os.path, 'getvolumeinformation') else ''
                        drives.append({
                            'name': f'{drive} ({label})' if label else drive,
                            'path': drive
                        })
                    except:
                        drives.append({'name': drive, 'path': drive})
            return jsonify({'entries': drives, 'current_path': '', 'parent_path': None})
        else:
            return jsonify({'entries': [{'name': '/', 'path': '/'}], 'current_path': '', 'parent_path': None})

    # Validate path
    if not os.path.isdir(path):
        return jsonify({'error': 'Path is not a directory or does not exist'}), 400

    try:
        entries = []
        for entry in sorted(os.listdir(path)):
            full_path = os.path.join(path, entry)
            if os.path.isdir(full_path) and not entry.startswith('.'):
                entries.append({'name': entry, 'path': full_path})
        parent = os.path.dirname(path) if os.path.dirname(path) != path else None
        return jsonify({
            'entries': entries,
            'current_path': path,
            'parent_path': parent
        })
    except PermissionError:
        return jsonify({'error': 'Permission denied'}), 403
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ---------- Settings Endpoints ----------

@app.route('/api/artists')
def api_artists():
    """Return a list of artists for which videos have been downloaded (based on artwork folder names)."""
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    config = load_config()
    artwork_cfg = config.get('artwork_sync', {})
    root_path = artwork_cfg.get('root_path', '/app/music_videos_final')

    if not os.path.isdir(root_path):
        return jsonify({'error': f'Root path does not exist: {root_path}'}), 400

    artists = []
    for entry in sorted(os.listdir(root_path)):
        entry_path = os.path.join(root_path, entry)
        if os.path.isdir(entry_path) and not entry.startswith('.'):
            artists.append(folder_to_artist(entry))

    return jsonify({'artists': artists})


@app.route('/api/artists/summary')
def api_artists_summary():
    """Return each artist folder with its video count and artwork status, for the Artists page."""
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    config = load_config()
    artwork_cfg = config.get('artwork_sync', {})
    root_path = artwork_cfg.get('root_path', '/app/music_videos_final')

    if not os.path.isdir(root_path):
        return jsonify({'error': f'Root path does not exist: {root_path}'}), 400

    artists = []
    for entry in sorted(os.listdir(root_path)):
        entry_path = os.path.join(root_path, entry)
        if not os.path.isdir(entry_path) or entry.startswith('.'):
            continue
        video_count = sum(
            1 for f in os.listdir(entry_path)
            if f.lower().endswith(('.mp4', '.mkv', '.webm'))
        )
        artists.append({
            'artist': folder_to_artist(entry),
            'folder': entry,
            'video_count': video_count,
            'has_artwork': has_artwork(entry_path),
        })

    return jsonify({'artists': artists})


@app.route('/api/artists/videos')
def api_artist_videos():
    """List the video files for one artist, for the Artists page's expandable detail view."""
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    artist = request.args.get('artist', '').strip()
    if not artist:
        return jsonify({'error': 'Artist name is required'}), 400

    config = load_config()
    artwork_cfg = config.get('artwork_sync', {})
    root_path = artwork_cfg.get('root_path', '/app/music_videos_final')
    artist_folder = _sanitize_folder_name(artist)
    artist_path = os.path.join(root_path, artist_folder)

    if not os.path.isdir(artist_path):
        return jsonify({'error': f'Artist folder not found: {artist_folder}'}), 404

    videos = []
    for entry in sorted(os.listdir(artist_path)):
        if not entry.lower().endswith(('.mp4', '.mkv', '.webm')):
            continue
        entry_path = os.path.join(artist_path, entry)
        if not os.path.isfile(entry_path):
            continue
        raw_title = os.path.splitext(entry)[0]
        stat = os.stat(entry_path)
        videos.append({
            'filename': entry,
            'title': _clean_video_title(raw_title),
            'size_bytes': stat.st_size,
            'modified_at': stat.st_mtime,
        })

    return jsonify({'artist': artist, 'videos': videos})

# Unauthenticated search endpoint
@app.route('/api/artwork/search_noauth', methods=['GET'])
def api_artwork_search_noauth():
    """Search for artwork images for a given artist without authentication.

    Paginated: returns ARTWORK_SEARCH_PAGE_SIZE (5) images per call. The full
    result set is fetched once per artist and cached for
    _ARTWORK_SEARCH_CACHE_TTL seconds so clicking "Load More" (page=2, 3, ...)
    slices the cached list instead of re-querying every external API again.
    """
    artist = request.args.get('artist', '').strip()
    if not artist:
        return jsonify({'error': 'Artist name is required'}), 400
    try:
        page = max(1, int(request.args.get('page', 1)))
    except ValueError:
        page = 1

    cache_key = artist.lower()
    cached = _ARTWORK_SEARCH_CACHE.get(cache_key)
    if cached and (time.time() - cached[0]) < _ARTWORK_SEARCH_CACHE_TTL:
        images = cached[1]
    else:
        config = load_config()
        api_key = config.get('artwork_sync', {}).get('fanarttv_api_key', '')
        images = search_artist_images(artist, api_key)
        _ARTWORK_SEARCH_CACHE[cache_key] = (time.time(), images)

    start = (page - 1) * ARTWORK_SEARCH_PAGE_SIZE
    end = start + ARTWORK_SEARCH_PAGE_SIZE
    page_images = images[start:end]
    return jsonify({
        'images': page_images,
        'page': page,
        'page_size': ARTWORK_SEARCH_PAGE_SIZE,
        'total': len(images),
        'has_more': end < len(images),
    })


@app.route('/api/artwork/current_image', methods=['GET'])
def api_artwork_current_image():
    """Serve an artist's current folder.jpg so the swap-art UI can preview
    the existing artwork before swapping it out."""
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    artist = request.args.get('artist', '').strip()
    if not artist:
        return jsonify({'error': 'Artist name is required'}), 400
    config = load_config()
    artwork_cfg = config.get('artwork_sync', {})
    root_path = artwork_cfg.get('root_path', '/app/music_videos_final')
    folder_name = _sanitize_folder_name(artist)
    image_path = os.path.join(root_path, folder_name, 'folder.jpg')
    if not os.path.isfile(image_path):
        return jsonify({'error': 'No artwork found for this artist'}), 404
    return send_file(image_path, mimetype='image/jpeg')

# Unauthenticated swap endpoint
@app.route('/api/artwork/swap_noauth', methods=['POST'])
def api_artwork_swap_noauth():
    """Swap artwork for a Plex collection without authentication."""
    data = request.get_json() or {}
    artist_name = data.get('artist_name', '').strip()
    new_image_url = data.get('new_image_url', '').strip()
    if not artist_name or not new_image_url:
        return jsonify({'error': 'artist_name and new_image_url are required'}), 400
    config = load_config()
    result = plex_swap_collection_artwork(config, artist_name, new_image_url)
    return jsonify(result)


@app.route('/api/plex/collections/create', methods=['POST'])
def api_plex_collection_create():
    """Create a Plex collection for a single artist on demand."""
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    data = request.get_json()
    artist = data.get('artist', '').strip()
    if not artist:
        return jsonify({'error': 'Artist name is required'}), 400

    config = load_config()
    artwork_cfg = config.get('artwork_sync', {})
    root_path = artwork_cfg.get('root_path', '/app/music_videos_final')

    artist_folder = _sanitize_folder_name(artist)
    artist_path = os.path.join(root_path, artist_folder)

    if not os.path.isdir(artist_path):
        return jsonify({'error': f'Artist folder not found: {artist_folder}'}), 404

    result = plex_sync_artist_collection(config, artist, artist_path)
    return jsonify({'result': result})



@app.route('/api/password', methods=['POST'])
def api_password():
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    global _ADMIN_PASSWORD_HASH
    data = request.get_json()
    current = data.get('current_password', '')
    new_pass = data.get('new_password', '')

    if not check_password_hash(_ADMIN_PASSWORD_HASH, current):
        return jsonify({'error': 'Current password is incorrect'}), 403
    if not new_pass or len(new_pass) < 6:
        return jsonify({'error': 'New password must be at least 6 characters'}), 400

    _ADMIN_PASSWORD_HASH = generate_password_hash(new_pass)
    config = load_config()
    config['_auth'] = {'username': _ADMIN_USERNAME, 'password_hash': _ADMIN_PASSWORD_HASH}
    _write_raw_config(config)
    return jsonify({'success': True, 'message': 'Password updated successfully'})

@app.route('/api/downloads/clear', methods=['POST'])
def api_downloads_clear():
    """Clear the download history tracker and active download progress entries."""
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    try:
        # Clear the download history tracker
        state.write_json(TRACKER_FILE, {}, indent=2)
        # Also clear the active downloads progress display
        try:
            state.write_json(ACTIVE_DOWNLOADS_FILE, {}, indent=2)
        except Exception:
            pass
        return jsonify({'success': True, 'message': 'Download history and progress cleared'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/plex-base-path', methods=['GET', 'POST'])
def api_plex_base_path():
    """Get or set the global Plex media base path. Each channel's plex_media_path is relative to this."""
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    config = load_config()
    if request.method == 'GET':
        return jsonify({'plex_base_path': config.get('plex_base_path', './downloads')})
    elif request.method == 'POST':
        data = request.get_json()
        new_base = data.get('plex_base_path', '').strip()
        if not new_base:
            return jsonify({'error': 'Plex base path is required'}), 400
        config['plex_base_path'] = new_base
        _write_raw_config(config)
        return jsonify({'success': True, 'message': f'Plex base path set to {new_base}'})

@app.route('/api/system/info')
def api_system_info():
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    import platform

    config = load_config()
    channels_count = len(config.get('channels', []))
    tracker = load_downloaded_tracker()
    downloads_count = sum(len(vids) for vids in tracker.values())

    import shutil
    try:
        total, used, free = shutil.disk_usage('./downloads')
    except Exception:
        total = used = free = 0

    return jsonify({
        'app_version': APP_VERSION,
        'python_version': platform.python_version(),
        'platform': platform.platform(),
        'yt_dlp_version': yt_dlp_version,
        'channels_count': channels_count,
        'downloads_count': downloads_count,
        'disk_total': total,
        'disk_used': used,
        'disk_free': free,
        'plex_base_path': config.get('plex_base_path', './downloads')
    })


@app.route('/api/system/health')
def api_system_health():
    """Dependency check for the Settings page's System Health panel.
    Most useful for the local (non-Docker) install path — the Docker image
    always bakes in ffmpeg/Pillow/fonts, but even there this catches a
    misconfigured FFMPEG_PATH."""
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    binaries = transcode.check_dependencies()
    title_card_deps = check_title_card_dependencies()
    return jsonify({
        'ffmpeg': binaries['ffmpeg'],
        'ffprobe': binaries['ffprobe'],
        'pillow': title_card_deps['pillow'],
        'fonts': title_card_deps['fonts'],
    })


@app.route('/api/system/version')
def api_system_version():
    """Current version plus, if enabled, whether a newer release exists.

    Always returns immediately — updates.get_status() serves the cached
    answer and refreshes in the background, so a slow or unreachable GitHub
    can't stall the sidebar this feeds.
    """
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    config = load_config()
    # Opt-out, defaulting to on: the check is a single request per day, but
    # it is an outbound call that reveals an install exists, so it has to be
    # switchable for anyone who'd rather it didn't happen.
    enabled = config.get('update_check_enabled', True)
    return jsonify(updates.get_status(APP_VERSION, enabled=enabled))


# --------------------------------------------------------------------------
# Channel monitor (scheduler.py) — the module is deliberately ignorant of Flask,
# so the collaborators it needs are wired up here.
# --------------------------------------------------------------------------

def _monitor_start_download(video, channel):
    """Queue one video on behalf of the scheduler.

    Mirrors what /api/channels/download-all does per video, including the
    notification hooks, so a scheduled download is indistinguishable from a
    manual one on the Downloads page.
    """
    url = channel.get('url')
    download_path = channel.get('download_path', './downloads')
    plex_media_path = _resolve_plex_path(channel.get('plex_media_path', './downloads'))
    vid = video['id']
    title = video.get('title', 'Unknown')
    download_id = queue_download(vid, title, url, final_path=plex_media_path)

    def _run():
        try:
            os.makedirs(plex_media_path, exist_ok=True)
            download_video(vid, download_path, plex_media_path,
                           title=title, channel_url=url, download_id=download_id)
            mark_video_downloaded(vid, url)
            _notify_download('complete', title, url)
        except Exception as exc:  # noqa: BLE001
            print(f'[monitor] FAILED {vid} ({title}): {exc}')
            _notify_download('failed', title, url, error=exc)

    _DOWNLOAD_EXECUTOR.submit(_run)
    return download_id


_CHANNEL_MONITOR = scheduler.ChannelMonitor(
    load_config=load_config,
    list_videos=get_channel_videos,
    is_downloaded=is_video_downloaded,
    start_download=_monitor_start_download,
)


@app.route('/api/monitor/status')
def api_monitor_status():
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    return jsonify(_CHANNEL_MONITOR.status())


@app.route('/api/monitor/config', methods=['POST'])
def api_monitor_config():
    """Enable/disable monitoring and set its interval."""
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.get_json(silent=True) or {}

    def _set(config):
        cfg = dict(config.get('channel_monitor') or {})
        if 'enabled' in data:
            cfg['enabled'] = bool(data['enabled'])
        if 'interval_minutes' in data:
            try:
                cfg['interval_minutes'] = max(scheduler.MIN_INTERVAL_MINUTES,
                                              int(data['interval_minutes']))
            except (TypeError, ValueError):
                return jsonify({'error': 'interval_minutes must be a number'}), 400
        if 'max_per_channel' in data:
            try:
                cfg['max_per_channel'] = max(1, int(data['max_per_channel']))
            except (TypeError, ValueError):
                pass
        config['channel_monitor'] = cfg

    _update_config(_set)
    # Wake the loop so a changed interval or a freshly-enabled monitor applies
    # now, instead of after the remainder of the current sleep.
    _CHANNEL_MONITOR.trigger()
    return jsonify({'success': True, 'status': _CHANNEL_MONITOR.status()})


@app.route('/api/monitor/run', methods=['POST'])
def api_monitor_run():
    """Run one monitoring pass immediately, regardless of the enabled flag.

    Synchronous on purpose: it's the "Check now" button, and the user is waiting
    to see what it found. Downloads themselves still go to the worker pool.
    """
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    try:
        results = _CHANNEL_MONITOR.run_once()
    except Exception as exc:  # noqa: BLE001
        return jsonify({'error': f'{type(exc).__name__}: {exc}'}), 500
    return jsonify({'success': True, 'results': results,
                    'status': _CHANNEL_MONITOR.status()})


# --------------------------------------------------------------------------
# Retention (retention.py)
# --------------------------------------------------------------------------

@app.route('/api/retention/config', methods=['POST'])
def api_retention_config():
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.get_json(silent=True) or {}

    def _set(config):
        cfg = dict(config.get('retention') or {})
        if 'enabled' in data:
            cfg['enabled'] = bool(data['enabled'])
        if 'keep_last_per_artist' in data:
            try:
                cfg['keep_last_per_artist'] = max(1, int(data['keep_last_per_artist']))
            except (TypeError, ValueError):
                pass
        config['retention'] = cfg

    _update_config(_set)
    return jsonify({'success': True, 'retention': load_config().get('retention', {})})


@app.route('/api/retention/plan')
def api_retention_plan():
    """Dry run — what a sweep would delete. Never deletes anything."""
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    result = retention.sweep(load_config(), dry_run=True)
    plan = result['plan']
    # Trim the payload: a large library could plan hundreds of deletions and the
    # UI only shows a sample alongside the totals.
    sample = plan['candidates'][:50]
    return jsonify({
        'root': plan['root'],
        'keep_last_per_artist': plan['keep_last_per_artist'],
        'enabled': plan.get('enabled'),
        'artists_scanned': plan['artists_scanned'],
        'total_files': plan['total_files'],
        'candidate_count': plan.get('candidate_count', 0),
        'total_bytes': plan['total_bytes'],
        'error': plan['error'],
        'sample': [{'artist': c['artist'], 'name': c['name'],
                    'size_bytes': c['size_bytes'], 'modified_at': c['modified_at']}
                   for c in sample],
        'sample_truncated': plan.get('candidate_count', 0) > len(sample),
    })


@app.route('/api/retention/apply', methods=['POST'])
def api_retention_apply():
    """Actually delete. Requires an explicit confirm, and honours the enabled flag.

    The confirm token is not security — the session already authenticates the
    admin — it's there so this can never be triggered by a stray request or a
    mis-wired button, which for the one destructive endpoint in the app is worth
    the friction.
    """
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.get_json(silent=True) or {}
    if data.get('confirm') != 'DELETE':
        return jsonify({'error': "Refusing to delete without {\"confirm\": \"DELETE\"}"}), 400

    result = retention.sweep(load_config(), dry_run=False)
    applied = result['applied'] or {}
    return jsonify({
        'success': not applied.get('error'),
        'error': applied.get('error'),
        'deleted_count': len(applied.get('deleted', [])),
        'failed': applied.get('failed', []),
        'freed_bytes': applied.get('freed_bytes', 0),
        'plan_error': result['plan'].get('error'),
    })


# --------------------------------------------------------------------------
# Notifications (notify.py)
# --------------------------------------------------------------------------

@app.route('/api/notifications/config', methods=['POST'])
def api_notifications_config():
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.get_json(silent=True) or {}

    def _set(config):
        cfg = dict(config.get('notifications') or {})
        if 'enabled' in data:
            cfg['enabled'] = bool(data['enabled'])
        if 'url' in data:
            cfg['url'] = str(data['url']).strip()
        if 'kind' in data:
            kind = str(data['kind']).strip() or 'auto'
            cfg['kind'] = kind
        if 'events' in data and isinstance(data['events'], list):
            cfg['events'] = [e for e in data['events'] if e in notify.ALL_EVENTS]
        cfg.setdefault('events', list(notify.DEFAULT_EVENTS))
        config['notifications'] = cfg

    _update_config(_set)
    cfg = load_config().get('notifications', {})
    # Never echo the URL back in full — it usually embeds a webhook secret, and
    # this response goes into browser history and any request log in between.
    url = cfg.get('url') or ''
    return jsonify({'success': True, 'notifications': {
        'enabled': cfg.get('enabled', False),
        'kind': cfg.get('kind', 'auto'),
        'events': cfg.get('events', list(notify.DEFAULT_EVENTS)),
        'url_set': bool(url),
        'url_hint': (url[:28] + '…') if len(url) > 28 else url,
        'detected_kind': notify.detect_kind(url) if url else None,
    }})


@app.route('/api/notifications/test', methods=['POST'])
def api_notifications_test():
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    ok, detail = notify.send_test(load_config())
    return jsonify({'success': ok, 'detail': detail})


@app.route('/api/system/update-check', methods=['POST'])
def api_system_update_check_toggle():
    """Turn the update check on or off."""
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.get_json(silent=True) or {}
    if 'enabled' not in data:
        return jsonify({'error': 'enabled is required'}), 400
    enabled = bool(data['enabled'])

    def _set(config):
        config['update_check_enabled'] = enabled

    _update_config(_set)
    return jsonify({'success': True, 'enabled': enabled})

# ---------- Download Verification ----------

@app.route('/api/downloads/verify', methods=['POST'])
def api_downloads_verify():
    """Verify that downloaded files exist at their final destination paths."""
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    downloads = get_active_downloads()
    results = []
    
    for d in downloads:
        filename = d.get('filename')
        final_path = d.get('final_path')
        video_id = d.get('video_id')
        
        entry = {
            'video_id': video_id,
            'title': d.get('title', 'Unknown'),
            'status': d.get('status'),
            'final_path': final_path,
            'filename': filename,
            'moved_to_final': d.get('moved_to_final', False),
            'final_file_exists': d.get('final_file_exists'),
            'verified_now': None,
        }
        
        # Re-verify if we have a filename and final path
        if filename and final_path and os.path.isdir(final_path):
            full_path = os.path.join(final_path, filename)
            entry['verified_now'] = os.path.isfile(full_path)
        elif filename and final_path:
            # Path might not exist (e.g. network drive not mounted)
            entry['verified_now'] = False
            entry['verify_error'] = 'Final path does not exist or is not accessible'
        else:
            entry['verified_now'] = None
            entry['verify_error'] = 'No filename or final path recorded'
        
        results.append(entry)
    
    return jsonify({'results': results, 'total': len(results)})


@app.route('/api/downloads/<download_id>/verify', methods=['GET'])
def api_download_verify_single(download_id):
    """Verify a single download's file at its final destination."""
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    downloads = get_active_downloads()
    target = None
    for d in downloads:
        if d.get('download_id') == download_id:
            target = d
            break
    
    if not target:
        return jsonify({'error': 'Download not found'}), 404
    
    filename = target.get('filename')
    final_path = target.get('final_path')
    
    result = {
        'video_id': target.get('video_id'),
        'title': target.get('title'),
        'status': target.get('status'),
        'final_path': final_path,
        'filename': filename,
        'moved_to_final': target.get('moved_to_final', False),
        'final_file_exists': target.get('final_file_exists'),
        'verified_now': None,
    }
    
    if filename and final_path and os.path.isdir(final_path):
        full_path = os.path.join(final_path, filename)
        result['verified_now'] = os.path.isfile(full_path)
    elif filename and final_path:
        result['verified_now'] = False
        result['verify_error'] = 'Final path does not exist or is not accessible'
    else:
        result['verified_now'] = None
        result['verify_error'] = 'No filename or final path recorded'
    
    return jsonify(result)


# ---------- Artwork Sync API Endpoints ----------

@app.route('/api/artwork/sync', methods=['POST'])
def api_artwork_sync():
    """Trigger artwork sync for all artist folders or a specific one."""
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.get_json() or {}
    force = data.get('force', False)
    artist = data.get('artist', '').strip()
    
    config = load_config()
    artwork_cfg = config.get('artwork_sync', {})
    root_path = artwork_cfg.get('root_path', '/app/music_videos_final')
    
    if artist:
        # Sync a specific artist folder
        artist_folder = _sanitize_folder_name(artist)
        artist_path = os.path.join(root_path, artist_folder)
        if not os.path.isdir(artist_path):
            return jsonify({'error': f'Artist folder not found: {artist_folder}'}), 404
        result = sync_artist_artwork(artist_path, artwork_cfg, force=force)
        # After artwork sync, trigger Plex collection sync if enabled
        if artwork_cfg.get('plex_collection_sync_on_artwork', False):
            plex_sync_result = plex_sync_artist_collection(config, artist, artist_path)
            result['plex_collection_sync'] = plex_sync_result
        # Optionally trigger Plex refresh
        if result.get('success') and artwork_cfg.get('plex_refresh_on_sync', False):
            trigger_plex_refresh(config)
        return jsonify({'result': result})
    else:
        # Sync all artist folders
        results = sync_all_artists(root_path, artwork_cfg, force=force)
        # After artwork sync, trigger Plex collection sync if enabled
        if artwork_cfg.get('plex_collection_sync_on_artwork', False):
            for res in results:
                artist_name = res.get('artist')
                artist_path = res.get('path')
                if artist_name and artist_path:
                    plex_sync_result = plex_sync_artist_collection(config, artist_name, artist_path)
                    res['plex_collection_sync'] = plex_sync_result
        # Optionally trigger Plex refresh
        if any(r.get('success') for r in results) and artwork_cfg.get('plex_refresh_on_sync', False):
            trigger_plex_refresh(config)
        return jsonify({'results': results, 'total': len(results)})


@app.route('/api/artwork/status', methods=['GET'])
def api_artwork_status():
    """Get artwork sync status — which folders have artwork and which don't."""
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    config = load_config()
    artwork_cfg = config.get('artwork_sync', {})
    root_path = artwork_cfg.get('root_path', '/app/music_videos_final')
    
    if not os.path.isdir(root_path):
        return jsonify({'error': 'Root path does not exist', 'root_path': root_path}), 400
    
    folders = []
    for entry in sorted(os.listdir(root_path)):
        entry_path = os.path.join(root_path, entry)
        if not os.path.isdir(entry_path) or entry.startswith('.'):
            continue
        from artwork_sync import has_metadata
        folders.append({
            'folder': entry,
            'artist': folder_to_artist(entry),
            'has_artwork': has_artwork(entry_path),
            'has_metadata': has_metadata(entry_path),
        })
    
    return jsonify({
        'root_path': root_path,
        'total_folders': len(folders),
        'with_artwork': sum(1 for f in folders if f['has_artwork']),
        'without_artwork': sum(1 for f in folders if not f['has_artwork']),
        'folders': folders,
    })


 


@app.route('/api/plex/collections/sync', methods=['POST'])
def api_plex_collections_sync():
    """Sync Plex collections for all artist folders or a specific one."""
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.get_json() or {}
    artist = data.get('artist', '').strip()
    config = load_config()
    artwork_cfg = config.get('artwork_sync', {})
    root_path = artwork_cfg.get('root_path', '/app/music_videos_final')
    if not os.path.isdir(root_path):
        return jsonify({'error': f'Root path does not exist: {root_path}'}), 400
    if artist:
        # Sync a specific artist
        artist_folder = _sanitize_folder_name(artist)
        artist_path = os.path.join(root_path, artist_folder)
        if not os.path.isdir(artist_path):
            return jsonify({'error': f'Artist folder not found: {artist_folder}'}), 404
        result = plex_sync_artist_collection(config, artist, artist_path)
        return jsonify({'result': result})
    else:
        # Sync all artist folders
        results = []
        for entry in sorted(os.listdir(root_path)):
            entry_path = os.path.join(root_path, entry)
            if not os.path.isdir(entry_path) or entry.startswith('.'):
                continue
            artist_name = folder_to_artist(entry)
            _log.info("Syncing Plex collection for '%s'", artist_name)
            try:
                res = plex_sync_artist_collection(config, artist_name, entry_path)
                results.append(res)
            except Exception as exc:
                _log.error("Plex collection sync failed for '%s': %s", artist_name, exc)
                results.append({
                    'artist': artist_name,
                    'collection_created': False,
                    'errors': [str(exc)],
                })
        return jsonify({'results': results, 'total': len(results)})


@app.route('/api/plex/collections/status', methods=['GET'])
def api_plex_collections_status():
    """Get Plex collection status — which artists have collections and which don't."""
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    config = load_config()
    plex_config = config.get('plex', {})
    if not plex_config.get('server_url') or not plex_config.get('token'):
        return jsonify({'error': 'Plex not configured'}), 400
    library_key = plex_config.get('music_video_library_key', '')
    if not library_key:
        library_key = plex_find_library_key(config)
    if not library_key:
        return jsonify({'error': 'Could not determine Plex library key'}), 400
    base_url = plex_config.get('server_url', '').rstrip('/')
    headers = {'X-Plex-Token': plex_config.get('token', ''), 'Accept': 'application/json'}
    try:
        resp = requests.get(f"{base_url}/library/sections/{library_key}/collections", headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        collections = data.get('MediaContainer', {}).get('Metadata', [])
        collection_names = set()
        collection_list = []
        for col in collections:
            title = col.get('title', '')
            key = str(col.get('ratingKey', ''))
            child_count = col.get('childCount', 0)
            collection_names.add(title.lower())
            collection_list.append({'title': title, 'key': key, 'child_count': child_count})
        artwork_cfg = config.get('artwork_sync', {})
        root_path = artwork_cfg.get('root_path', '/app/music_videos_final')
        folders = []
        if os.path.isdir(root_path):
            for entry in sorted(os.listdir(root_path)):
                entry_path = os.path.join(root_path, entry)
                if not os.path.isdir(entry_path) or entry.startswith('.'):
                    continue
                artist_name = folder_to_artist(entry)
                has_collection = artist_name.lower() in collection_names
                folders.append({'folder': entry, 'artist': artist_name, 'has_collection': has_collection})
        return jsonify({
            'library_key': library_key,
            'total_collections': len(collection_list),
            'collections': collection_list,
            'total_folders': len(folders),
            'with_collection': sum(1 for f in folders if f['has_collection']),
            'without_collection': sum(1 for f in folders if not f['has_collection']),
            'folders': folders,
        })
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500


@app.route('/api/plex/collections/duplicates', methods=['GET'])
def api_plex_collections_duplicates():
    """Report any same-title collections in the music-video library — a
    symptom of the check-then-create race in plex_ensure_smart_collection()
    that's now fixed with a lock, but pre-existing duplicates from before
    the fix don't clean themselves up."""
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    config = load_config()
    duplicates = plex_find_duplicate_collections(config)
    return jsonify({'duplicate_groups': duplicates, 'total_groups': len(duplicates)})


@app.route('/api/plex/collections/dedupe', methods=['POST'])
def api_plex_collections_dedupe():
    """Delete duplicate same-title collections, keeping the one with the
    highest childCount from each group. Safe because every duplicate in a
    group shares the identical smart filter — there's no item list to
    reconcile, just extra collection objects to remove."""
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    config = load_config()
    result = plex_dedupe_collections(config)
    return jsonify(result)


@app.route('/api/conversion/scan', methods=['POST'])
def api_conversion_scan():
    """Dry run: report which downloaded video files aren't already
    Plex-direct-play-compatible (MP4/H.264/AAC), without converting
    anything."""
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    config = load_config()
    candidates = _scan_conversion_candidates(config)
    return jsonify({
        'needs_conversion': len(candidates),
        'files': candidates[:50],
        'truncated': len(candidates) > 50,
    })


@app.route('/api/conversion/start', methods=['POST'])
def api_conversion_start():
    """Kick off the batch conversion job in a background thread. Refuses to
    start a second job if one is already running."""
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    with _CONVERSION_LOCK:
        if _CONVERSION_STATE['running']:
            return jsonify({'error': 'A conversion job is already running'}), 409
    config = load_config()
    thread = threading.Thread(target=_run_conversion_job, args=(config,), daemon=True)
    thread.start()
    return jsonify({'success': True, 'message': 'Conversion job started'})


@app.route('/api/conversion/status', methods=['GET'])
def api_conversion_status():
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    with _CONVERSION_LOCK:
        return jsonify(dict(_CONVERSION_STATE))


@app.route('/api/plex/titles/clean', methods=['POST'])
def api_plex_titles_clean():
    """Clean up video titles in the music-video library (strips YouTube ID,
    embedded promo URLs, and generic "Official Video" boilerplate)."""
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    config = load_config()
    result = plex_clean_video_titles(config)
    return jsonify(result)


@app.route('/api/plex/title-cards/generate', methods=['POST'])
def api_plex_title_cards_generate():
    """Generate + upload a designed title-card poster (artist + song title
    over the artist's own art) for every video in the music-video library
    that doesn't already have one, replacing Plex's auto-extracted
    video-frame thumbnail."""
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.get_json(silent=True) or {}
    force = data.get('force', False)
    config = load_config()
    artwork_cfg = config.get('artwork_sync', {})
    root_path = artwork_cfg.get('root_path', '/app/music_videos_final')
    results = plex_generate_title_cards_for_all(config, root_path, force=force)
    return jsonify({
        'results': results,
        'total_artists': len(results),
        'total_generated': sum(r.get('generated', 0) for r in results),
        'errors': [e for r in results for e in r.get('errors', [])],
    })


@app.route('/api/plex/config', methods=['GET', 'POST'])
def api_plex_config():
    """Get or update Plex configuration."""
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    config = load_config()
    
    if request.method == 'GET':
        return jsonify(config.get('plex', {}))
    
    elif request.method == 'POST':
        data = request.get_json()
        if 'plex' not in config:
            config['plex'] = {}
        for key in ('server_url', 'token', 'music_video_library_key'):
            if key in data:
                config['plex'][key] = data[key]
        _write_raw_config(config)
        return jsonify({'success': True, 'message': 'Plex configuration updated'})


@app.route('/api/plex/discover-library', methods=['POST'])
def api_plex_discover_library():
    """Auto-discover the music video library key from Plex.

    Kept for API compatibility, but the dashboard no longer calls this
    directly — auto-discovering and saving in one step with no way to see
    what was actually picked is exactly how a mistyped library title
    ("Muisc Videos") silently pointed collection sync at the wrong library
    before (see REFERENCE.md). The UI now uses GET /api/plex/libraries to
    show every library with the auto-discovered one pre-selected, and only
    saves once the user confirms via POST /api/plex/config."""
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    config = load_config()

    library_key = plex_find_library_key(config)
    if library_key:
        # Save it
        if 'plex' not in config:
            config['plex'] = {}
        config['plex']['music_video_library_key'] = library_key
        _write_raw_config(config)
        return jsonify({
            'success': True,
            'library_key': library_key,
            'message': f'Library key discovered and saved: {library_key}'
        })
    else:
        return jsonify({'error': 'Could not discover library key. Check server_url and token.'}), 400


@app.route('/api/plex/libraries', methods=['GET'])
def api_plex_libraries():
    """List every library on the connected Plex server, flagging which one
    auto-discovery would pick — lets the UI show a confirm/pick step
    instead of trusting the guess blindly."""
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    config = load_config()
    libraries = plex_list_libraries(config)
    return jsonify({'libraries': libraries})


@app.route('/api/plex/oauth/start', methods=['POST'])
def api_plex_oauth_start():
    """Initiate Plex OAuth flow."""
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    config = load_config()
    pin_id, code, auth_url = plex_oauth_start(config)
    
    if pin_id and code and auth_url:
        session['plex_oauth_pin_id'] = pin_id
        return jsonify({
            'success': True,
            'pin_id': pin_id,
            'code': code,
            'auth_url': auth_url,
            'message': 'Plex OAuth initiated. Please open the URL to authorize.'
        })
    elif pin_id is None and code is None and auth_url is None:
        # OAuth is disabled – inform the client clearly
        return jsonify({
            'success': False,
            'message': 'Plex OAuth is not configured. Set PLEX_CLIENT_ID and PLEX_PRODUCT in artwork_sync.py to enable Plex integration.'
        }), 400
    else:
        return jsonify({'error': 'Failed to initiate Plex OAuth'}), 500


@app.route('/api/plex/oauth/check', methods=['POST'])
def api_plex_oauth_check():
    """Check if Plex OAuth PIN has been authenticated."""
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    pin_id = session.get('plex_oauth_pin_id')
    if not pin_id:
        return jsonify({'error': 'No active Plex OAuth session'}), 400
    
    config = load_config()
    auth_token = plex_oauth_check_pin(config, pin_id)
    
    if auth_token is None:
        return jsonify({'status': 'pending', 'message': 'PIN not yet authorized'})
    elif auth_token is False:
        session.pop('plex_oauth_pin_id', None)
        return jsonify({'status': 'failed', 'message': 'PIN authorization failed or expired'}), 400
    elif auth_token:
        config = load_config()
        if 'plex' not in config:
            config['plex'] = {}
        config['plex']['token'] = auth_token
        _write_raw_config(config)
        
        account_info = plex_get_account_info(config, auth_token)
        servers = plex_get_servers(config, auth_token)
        
        session.pop('plex_oauth_pin_id', None)
        return jsonify({
            'status': 'success',
            'message': 'Plex authenticated successfully!',
            'token': auth_token,
            'account': account_info,
            'servers': servers,
        })
    else:
        return jsonify({'status': 'pending', 'message': 'PIN not yet authorized'})


@app.route('/api/plex/oauth/servers', methods=['POST'])
def api_plex_oauth_servers():
    """Get Plex servers associated with the authenticated account."""
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    config = load_config()
    token = config.get('plex', {}).get('token', '')
    if not token:
        return jsonify({'error': 'Plex not authenticated'}), 400
    
    servers = plex_get_servers(config, token)
    account_info = plex_get_account_info(config, token)
    
    return jsonify({
        'servers': servers,
        'account': account_info,
    })


# ---------- Start Background Watcher ----------

_artwork_watcher = None

def start_artwork_watcher():
    """Start the background artwork folder watcher."""
    global _artwork_watcher
    try:
        config = load_config()
        artwork_cfg = config.get('artwork_sync', {})
        root_path = artwork_cfg.get('root_path', '/app/music_videos_final')
        interval = artwork_cfg.get('watch_interval', 120)
        
        if not os.path.isdir(root_path):
            print(f"[artwork_sync] Root path does not exist yet: {root_path}. Will retry on next poll.")
        
        # Setup logging
        setup_logging(root_path)
        
        _artwork_watcher = ArtworkWatcher(root_path, load_config, interval=interval)
        _artwork_watcher.start()
        print(f"[artwork_sync] Background watcher started on {root_path} (interval={interval}s)")
    except Exception as e:
        print(f"[artwork_sync] Failed to start watcher: {e}")

if __name__ == '__main__':
    os.makedirs('./downloads', exist_ok=True)
    if os.path.exists(CONFIG_FILE):
        try:
            cfg = load_config()
            if 'plex_base_path' not in cfg:
                cfg['plex_base_path'] = './downloads'
            if 'music_video_plex_path' not in cfg:
                cfg['music_video_plex_path'] = './downloads/music_videos'
            _write_raw_config(cfg)
        except Exception:
            pass
    
    # Start the artwork background watcher
    start_artwork_watcher()

    # Start the channel monitor. The thread always runs; whether a tick does
    # anything is decided per-tick from config, so toggling it in Settings takes
    # effect without a restart. Started here rather than at import so it never
    # runs under the test client or a `python -c "import app"` check.
    _CHANNEL_MONITOR.start()
    _mon_enabled, _mon_interval, _ = _CHANNEL_MONITOR._settings()
    print(f'[monitor] channel monitor thread started '
          f'(enabled={_mon_enabled}, interval={_mon_interval}m)')

    # debug=True enables Werkzeug's interactive debugger, which lets anyone
    # who can trigger an unhandled exception execute arbitrary Python in the
    # browser — never want that reachable by default. Opt in explicitly for
    # local development with FLASK_DEBUG=true.
    debug_mode = os.environ.get('FLASK_DEBUG', '').lower() == 'true'
    port = int(os.environ.get('PORT', '5000'))

    if debug_mode:
        # Only path that uses Werkzeug's development server, and only because
        # the reloader and interactive debugger are the point of asking for it.
        print('[server] FLASK_DEBUG=true — using the Werkzeug development '
              'server with the interactive debugger. Do not expose this.')
        app.run(host='0.0.0.0', port=port, debug=True)
    else:
        # waitress rather than Werkzeug: this app ships as a container image
        # people run unattended, and Werkzeug's server is explicitly not for
        # that. waitress over gunicorn because it's pure-Python and works on
        # Windows, which the local-install path in the README depends on.
        #
        # Threads matter here: the dashboard polls several endpoints on a timer
        # while downloads and the artwork watcher run in their own background
        # threads. waitress defaults to 4 request threads, which is thin once a
        # slow directory walk is in flight and the UI is still polling.
        from waitress import serve
        threads = int(os.environ.get('SERVER_THREADS', '8'))
        print(f'[server] Vidshelf {APP_VERSION} listening on http://0.0.0.0:{port} '
              f'(waitress, {threads} threads)')
        serve(app, host='0.0.0.0', port=port, threads=threads, ident='Vidshelf')
