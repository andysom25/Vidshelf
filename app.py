import os
import json
import shutil
import threading
import concurrent.futures
import time
import math
import datetime
import secrets
import requests
import functools
import state
import config_store
import youtube
import library
import webauth
import updates
import notify
import retention
import titles
import scheduler
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, send_file
from werkzeug.security import generate_password_hash, check_password_hash
import downloader as downloader_module
from downloader import (download_video, get_active_downloads, queue_download,
                        request_cancel, DownloadCancelled, build_format_selector,
                        reconcile_interrupted)
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
# These two caches are mutated from waitress' 8 request threads and were
# never evicted: one entry per artist ever searched, held for the life of the
# process, each holding a full result list. _cache_put bounds them and takes a
# lock -- plain dict writes are atomic under the GIL, but read-modify-write
# eviction is not.
_SEARCH_CACHE_LOCK = threading.Lock()
_SEARCH_CACHE_MAX_ENTRIES = 128


def _cache_put(cache, key, value, ttl):
    """Store a (timestamp, value) entry, dropping expired and excess ones.

    Bounded by count as well as TTL: an expired entry is only overwritten if
    that exact key is searched again, so TTL alone never reclaims anything for
    a one-off search. Evicts oldest-first once over the cap.
    """
    now = time.time()
    with _SEARCH_CACHE_LOCK:
        cache[key] = (now, value)
        for stale in [k for k, (ts, _) in cache.items() if now - ts >= ttl]:
            del cache[stale]
        if len(cache) > _SEARCH_CACHE_MAX_ENTRIES:
            for oldest, _ in sorted(cache.items(), key=lambda kv: kv[1][0])[
                    :len(cache) - _SEARCH_CACHE_MAX_ENTRIES]:
                del cache[oldest]
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

# config.json I/O and the credentials it seeds live in config_store.py as of
# v1.11.0 — state still lives in a mounted directory (./data) and is still
# written atomically; see state.py for why single-file bind mounts made both of
# those impossible. Re-exported under the original names so the routes still in
# this module read unchanged; blueprints import from config_store directly.
CONFIG_FILE = config_store.CONFIG_FILE
TRACKER_FILE = config_store.TRACKER_FILE
ACTIVE_DOWNLOADS_FILE = config_store.ACTIVE_DOWNLOADS_FILE
load_config = config_store.load_config
_read_raw_config = config_store._read_raw_config
_write_raw_config = config_store._write_raw_config
_update_config = config_store._update_config

config_store.report_migrations()

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

app = Flask(__name__)
app.secret_key = config_store._get_or_create_secret_key()
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
# Off by default since this is commonly reached over plain HTTP on a LAN;
# set SESSION_COOKIE_SECURE=true if this is ever put behind HTTPS/a reverse proxy.
app.config['SESSION_COOKIE_SECURE'] = os.environ.get('SESSION_COOKIE_SECURE', '').lower() == 'true'

_ADMIN_USERNAME, _ADMIN_PASSWORD_HASH = config_store._get_or_create_admin_credentials()

# The login throttle, the session guard and the response headers live in
# webauth.py as of v1.11.0. Re-exported under their original names for the routes
# still in this module; blueprints import them from webauth directly.
_login_is_locked = webauth._login_is_locked
_record_login_failure = webauth._record_login_failure
_clear_login_failures = webauth._clear_login_failures
require_auth = webauth.require_auth

# Registered here rather than in webauth because an after_request hook belongs to
# an app, and webauth deliberately knows nothing about one — that is what lets
# every blueprint import require_auth without a cycle back through app.py.
app.after_request(webauth._set_security_headers)


def _cookies_file():
    """Path to a yt-dlp cookies file, or None.

    Lives in the data directory so it survives rebuilds and is covered by the
    same volume as config. A repo-root cookies.txt is also honoured, because one
    has existed there (gitignored) since long before anything read it.
    """
    for candidate in (os.path.join(state.DATA_DIR, 'cookies.txt'), './cookies.txt'):
        if os.path.isfile(candidate):
            return candidate
    return None


# Music-video downloads have no channel, so they're filed in the tracker under a
# synthetic key. Both directions live here rather than being spelled out at each
# site: the retry path needs to read the artist back out, and getting the
# transform subtly wrong there is invisible until a retry lands in the wrong
# folder. Lossy by construction — "A B" and "A_B" collapse to the same key — and
# left that way deliberately, since changing it would orphan existing history.
MUSIC_KEY_PREFIX = 'music_video_'


def _music_key_for_artist(artist):
    """Tracker key for an artist's music videos."""
    return f"{MUSIC_KEY_PREFIX}{artist.replace(' ', '_')}"


def _artist_from_music_key(channel_url):
    """Artist name from a synthetic music key, or None for a real channel URL."""
    if not channel_url or not channel_url.startswith(MUSIC_KEY_PREFIX):
        return None
    return channel_url[len(MUSIC_KEY_PREFIX):].replace('_', ' ').strip() or None


def _music_retry_destination(recorded_path, music_artist):
    """Where a retried music video should land.

    The path recorded on a download entry is only the artist folder once the job
    has actually *started*: the music route queues it with the music root, and
    download_video re-inits the entry with root/Artist when a worker picks it
    up. A download cancelled while still queued never got that far — so
    retrying one would drop the file loose in the root. Inside the library, but
    with no artist folder, so no artwork, no collection, and nothing on the
    Artists page.

    No-op for channel downloads, and idempotent: a path that already ends in the
    artist folder is returned unchanged, so the normal failed-mid-download case
    doesn't get a second folder nested inside the first.
    """
    if not music_artist:
        return recorded_path
    folder = _sanitize_folder_name(music_artist)
    if os.path.basename(os.path.normpath(recorded_path)) == folder:
        return recorded_path
    return os.path.join(recorded_path, folder)


def _download_options(channel_url=None):
    """Resolve per-download yt-dlp options: quality cap and cookies.

    A channel's own max_height wins; otherwise the global default applies. 0 or
    absent means no cap, which is the pre-v1.6.0 behaviour.
    """
    config = load_config()
    cap = config.get('max_height') or 0
    for ch in config.get('channels', []):
        if channel_url and ch.get('url') == channel_url:
            if ch.get('max_height'):
                cap = ch['max_height']
            break
    try:
        cap = int(cap)
    except (TypeError, ValueError):
        cap = 0
    return {'max_height': cap if cap > 0 else None, 'cookies_file': _cookies_file()}


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
    # A new file exists on disk now, so the cached library scan is stale.
    # Every successful download funnels through here, which makes it the one
    # place that reliably knows the library changed.
    _invalidate_library_scan()

def is_video_downloaded(video_id, channel_url):
    tracker = load_downloaded_tracker()
    return video_id in tracker.get(channel_url, [])

# yt-dlp metadata probes live in youtube.py as of v1.11.0. Re-exported under
# their original names for the routes still in this module; blueprints import
# from youtube directly. downloader.py is deliberately NOT part of this --
# see youtube.py for why probes are bounded and downloads are not.
PROBE_TIMEOUTS = youtube.PROBE_TIMEOUTS
PROBE_CONCURRENCY = youtube.PROBE_CONCURRENCY
PROBE_WALL_CLOCK_TIMEOUT = youtube.PROBE_WALL_CLOCK_TIMEOUT
_probe_opts = youtube._probe_opts
get_channel_info = youtube.get_channel_info
get_channel_videos = youtube.get_channel_videos
search_music_videos = youtube.search_music_videos
rank_videos_by_quality = youtube.rank_videos_by_quality
get_video_formats_info = youtube.get_video_formats_info
_enrich_video_qualities = youtube._enrich_video_qualities
_CHANNEL_NAME_CACHE = youtube._CHANNEL_NAME_CACHE



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
@require_auth
def api_channels():
    config = load_config()
    channels = []
    for ch in config.get('channels', []):
        display_name = get_channel_info(ch['url'])
        channels.append({
            'url': ch['url'],
            'display_name': display_name or 'Unknown Channel',
            'download_path': ch['download_path'],
            'plex_media_path': ch.get('plex_media_path', './downloads'),
            'download_mode': ch.get('download_mode', 'manual'),
            'max_height': ch.get('max_height', 0)
        })
    return jsonify({'channels': channels})

@app.route('/api/channel/videos')
@require_auth
def api_channel_videos():
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

# The library scan and the media-root resolution live in library.py as of
# v1.11.0. Re-exported for the routes still in this module; blueprints import
# from library directly. The two traps that bit before -- the non-reentrant
# scan lock and invalidation that only zeroed the timestamp -- are documented
# there, next to the code they apply to.
DEFAULT_MUSIC_ROOT = library.DEFAULT_MUSIC_ROOT
LIBRARY_HISTORY_MONTHS = library.LIBRARY_HISTORY_MONTHS
RECENTLY_ADDED_LIMIT = library.RECENTLY_ADDED_LIMIT
TOP_ARTISTS_LIMIT = library.TOP_ARTISTS_LIMIT
_music_root = library._music_root
_resolve_plex_path = library._resolve_plex_path
_sweep_staging_dirs = library._sweep_staging_dirs
_library_scan = library._library_scan
_maybe_rescan_async = library._maybe_rescan_async
_invalidate_library_scan = library._invalidate_library_scan
_is_under = library._is_under
_month_series = library._month_series
_library_size = library._library_size
_gather_media_roots = library._gather_media_roots



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
@require_auth
def api_download():
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
        # .get, not [] — a channel entry hand-edited through the raw-config
        # editor can legitimately lack these keys, and a bracket read turned
        # that into an unhandled KeyError and an HTTP 500. Every other reader
        # of download_path already defaulted; this one was the odd one out.
        if ch.get('url') == channel_url:
            download_path = ch.get('download_path', './downloads')
            plex_media_path = _resolve_plex_path(ch.get('plex_media_path', './downloads'))
            break

    download_id = queue_download(video_id, title, channel_url, final_path=plex_media_path)

    def _do_download():
        try:
            os.makedirs(plex_media_path, exist_ok=True)
            download_video(video_id, download_path, plex_media_path,
                          title=title, channel_url=channel_url, download_id=download_id,
                          **_download_options(channel_url))
            mark_video_downloaded(video_id, channel_url)
            _notify_download('complete', title, channel_url)
        except Exception as exc:  # noqa: BLE001
            # Still swallowed — a failed download must not take down the worker
            # thread — but no longer silently. Before notifications existed the
            # only trace of a failure was a line in `docker logs`, which nobody
            # reads until they notice something missing.
            # A cancellation is the user's own request, not a failure — it must
            # not send a "download failed" notification.
            if isinstance(exc, DownloadCancelled):
                print(f'[download] cancelled {video_id} ({title})')
            else:
                print(f'[download] FAILED {video_id} ({title}): {exc}')
                _notify_download('failed', title, channel_url, error=exc)

    _DOWNLOAD_EXECUTOR.submit(_do_download)
    return jsonify({'success': True, 'message': f'Download started for {video_id}', 'video_id': video_id})

@app.route('/api/downloads/progress')
@require_auth
def api_downloads_progress():
    return jsonify({'downloads': get_active_downloads()})

@app.route('/api/channels/add', methods=['POST'])
@require_auth
def api_channels_add():
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
@require_auth
def api_channels_remove():
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
@require_auth
def api_channels_mode():
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
@require_auth
def api_channels_download_all():
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
            download_video(vid, path, plex, title=title, channel_url=url,
                           download_id=download_id, **_download_options(url))
            mark_video_downloaded(vid, url)
            _notify_download('complete', title, url)
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, DownloadCancelled):
                print(f'[download] cancelled {vid} ({title})')
            else:
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
@require_auth
def api_stats():
    tracker = load_downloaded_tracker()
    downloads_count = sum(len(vids) for vids in tracker.values())

    # "Videos Available" used to be computed from the identical expression as
    # downloads_count, so the dashboard showed the same number in two cards and
    # one of them meant nothing. It now reports videos seen on your channels that
    # have NOT been downloaded — which is the number that tells you whether
    # there's anything to fetch. Populated by the channel monitor, so it reads
    # None until a check has run rather than pretending to be 0.
    new_available = _CHANNEL_MONITOR.pending_count()

    disk_usage, library_videos = _library_size()

    return jsonify({
        # Kept for backwards compatibility with any existing client; the
        # dashboard now uses new_available.
        'videos_count': new_available,
        'new_available': new_available,
        'downloads_count': downloads_count,
        'disk_usage': disk_usage,
        'library_videos': library_videos,
        # The dashboard hides the channel cards entirely when there are none,
        # so it needs to know that without a second request.
        'channels_count': len(load_config().get('channels', [])),
    })


@app.route('/api/library/stats')
@require_auth
def api_library_stats():
    """Everything the music-video dashboard renders, from one cached walk.

    `?refresh=1` forces a rescan, for the "just downloaded something and want to
    see it" case — otherwise the five-minute cache would make the dashboard look
    broken right after a download finishes.
    """
    force = request.args.get('refresh') in ('1', 'true', 'yes')
    return jsonify(_library_scan(force=force))

@app.route('/api/config', methods=['GET', 'POST'])
@require_auth
def api_config():
    if request.method == 'GET':
        # Strip internal-only keys (session-signing secret, password hash)
        # before handing config.json back to the frontend — this endpoint
        # feeds the Settings page's raw config editor, and there's no reason
        # for either value to round-trip through the browser at all.
        config = {k: v for k, v in load_config().items() if k not in ('_secret_key', '_auth')}
        # The Plex token is a bearer credential for the user's whole Plex
        # account. Nothing in the UI needs its value — only whether one is set —
        # and returning it made any script running in the admin session (e.g. via
        # a stored-XSS payload in a video title) able to exfiltrate it with one
        # fetch. Replaced with a boolean; POST preserves the stored value when
        # the key is absent, so the editor still round-trips safely.
        if isinstance(config.get('plex'), dict):
            plex = dict(config['plex'])
            plex['token_set'] = bool(plex.pop('token', None))
            config['plex'] = plex
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
            # Nested preservation for plex.token specifically. The GET above no
            # longer returns it, so a client round-tripping the document would
            # otherwise POST a `plex` object without it and silently disconnect
            # Plex. The top-level underscore rule can't cover a nested key.
            #
            # Dropping the marker is unconditional: gating it on current_token
            # meant a POST from a *disconnected* install persisted
            # "token_set": false into config.json, where it reads like a real
            # setting forever after. token_set is a presence flag computed per
            # response, never state.
            incoming_plex = merged.get('plex')
            if isinstance(incoming_plex, dict):
                incoming_plex.pop('token_set', None)   # never persist the marker
                current_token = (current.get('plex') or {}).get('token')
                if current_token:
                    incoming_plex.setdefault('token', current_token)
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
@require_auth
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
            _cache_put(_MUSIC_VIDEO_SEARCH_CACHE, cache_key, videos,
                       _MUSIC_VIDEO_SEARCH_CACHE_TTL)

        start = (page - 1) * MUSIC_VIDEO_SEARCH_PAGE_SIZE
        end = start + MUSIC_VIDEO_SEARCH_PAGE_SIZE
        page_videos = videos[start:end]

        # Enrich only this page — these dicts are the same objects held in
        # the cache, so a video that reappears (e.g. overlap between pages
        # after a re-search) doesn't get re-probed either.
        #
        # Concurrently, and with each probe's failure contained. This loop used
        # to be serial and unguarded, which made the whole endpoint as slow as
        # the sum of 9 network round-trips and as reliable as the worst one of
        # them: a single probe that hung took the entire HTTP response with it,
        # and the browser reported "Failed to fetch" — its message for a dead
        # connection, which names nothing and sends you looking in the wrong
        # place. Ask for a quality label, get 'unknown' if it doesn't arrive;
        # never trade the search results for it.
        _enrich_video_qualities(page_videos)

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
@require_auth
def api_music_videos_download():
    """Download a music video to the configured music video Plex path."""
    data = request.get_json()
    video_id = data.get('video_id', '')
    title = data.get('title', 'Music Video')
    artist = data.get('artist', 'Unknown Artist')

    if not video_id:
        return jsonify({'error': 'Missing video_id'}), 400

    # Download locally first (ext4 filesystem) to avoid CIFS/os.sendfile issues,
    # then download_video will copy the finished file to the final Y:\ mount.
    download_path = './downloads/music_videos'
    final_path = _music_root(load_config())

    # The search box doubles as a YouTube search query, so a more specific
    # search (artist + song, to narrow results) shouldn't fork a second
    # near-duplicate artist folder/collection if the artist is already
    # known — snap back to the existing artist's canonical name.
    artist = _resolve_existing_artist(artist, final_path)

    channel_url = _music_key_for_artist(artist)
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
            # Download locally, then download_video copies to artist_final_path.
            #
            # _download_options was missing here and only here — the other four
            # download_video call sites all passed it. That meant music videos
            # got no cookies, so every age-restricted one failed with a bare
            # yt-dlp error and no indication why, and the quality cap silently
            # didn't apply. The synthetic channel_url ("music_video_<Artist>")
            # matches no channel, so this correctly resolves to the global cap.
            download_video(video_id, download_path, artist_final_path,
                          title=title, channel_url=channel_url, download_id=download_id,
                          music_artist=artist,
                          **_download_options(channel_url))
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
@require_auth
def api_music_video_path():
    """Get or set the music video Plex path in config."""
    config = load_config()

    # Reads and writes artwork_sync.root_path now. The response key is
    # unchanged so the Settings page and any existing script keep working —
    # what changed is that editing this field finally affects where music
    # videos are downloaded, which it never did before v1.8.0.
    if request.method == 'GET':
        return jsonify({
            'music_video_plex_path': _music_root(config),
            'conflict': config.get('_music_path_conflict'),
        })

    elif request.method == 'POST':
        data = request.get_json(silent=True) or {}
        new_path = data.get('music_video_plex_path', '').strip()
        if not new_path:
            return jsonify({'error': 'Music video Plex path is required'}), 400

        def _merge(current):
            artwork = dict(current.get('artwork_sync') or {})
            artwork['root_path'] = new_path
            merged = dict(current)
            merged['artwork_sync'] = artwork
            # Setting the path explicitly resolves the migration warning.
            merged.pop('_music_path_conflict', None)
            return merged

        _update_config(_merge)
        return jsonify({'success': True,
                        'message': f'Music video path set to {new_path}'})


# ---------- Folder Browser ----------

@app.route('/api/browse-folder', methods=['POST'])
@require_auth
def api_browse_folder():
    """List subdirectories of a given path for the folder browser modal."""
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
@require_auth
def api_artists():
    """Return a list of artists for which videos have been downloaded (based on artwork folder names)."""
    config = load_config()
    artwork_cfg = config.get('artwork_sync', {})
    root_path = _music_root(config)

    if not os.path.isdir(root_path):
        return jsonify({'error': f'Root path does not exist: {root_path}'}), 400

    artists = []
    for entry in sorted(os.listdir(root_path)):
        entry_path = os.path.join(root_path, entry)
        if os.path.isdir(entry_path) and not entry.startswith('.'):
            artists.append(folder_to_artist(entry))

    return jsonify({'artists': artists})


@app.route('/api/artists/summary')
@require_auth
def api_artists_summary():
    """Return each artist folder with its video count and artwork status, for the Artists page."""
    config = load_config()
    root_path = _music_root(config)

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
@require_auth
def api_artist_videos():
    """List the video files for one artist, for the Artists page's expandable detail view."""
    artist = request.args.get('artist', '').strip()
    if not artist:
        return jsonify({'error': 'Artist name is required'}), 400

    config = load_config()
    root_path = _music_root(config)
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
# `search_noauth` is kept as an alias so an existing bookmark or script gets a
# 401 rather than a 404 — the rename is cosmetic, the auth check is the change.
# It was never called by anything but this app's own dashboard (verified: the
# only two call sites were in static/js/dashboard.js, both inside an
# authenticated page), so requiring a session breaks no known consumer.
@app.route('/api/artwork/search', methods=['GET'])
@app.route('/api/artwork/search_noauth', methods=['GET'])
@require_auth
def api_artwork_search():
    """Search for artwork images for a given artist.

    Paginated: returns ARTWORK_SEARCH_PAGE_SIZE (5) images per call. The full
    result set is fetched once per artist and cached for
    _ARTWORK_SEARCH_CACHE_TTL seconds so clicking "Load More" (page=2, 3, ...)
    slices the cached list instead of re-querying every external API again.
    """
    # The auth check is @require_auth now. It used to be inline here, below a
    # comment insisting it stay above the parameter validation — v1.7.0 found
    # this route answering anonymous probes with 400 (because it validated
    # first) and so looking compliant to a sweep that only flagged 200s. The
    # decorator makes the ordering structural instead of a comment.
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
        _cache_put(_ARTWORK_SEARCH_CACHE, cache_key, images,
                   _ARTWORK_SEARCH_CACHE_TTL)

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
@require_auth
def api_artwork_current_image():
    """Serve an artist's current folder.jpg so the swap-art UI can preview
    the existing artwork before swapping it out."""
    artist = request.args.get('artist', '').strip()
    if not artist:
        return jsonify({'error': 'Artist name is required'}), 400
    config = load_config()
    root_path = _music_root(config)
    folder_name = _sanitize_folder_name(artist)
    image_path = os.path.join(root_path, folder_name, 'folder.jpg')
    if not os.path.isfile(image_path):
        return jsonify({'error': 'No artwork found for this artist'}), 404
    return send_file(image_path, mimetype='image/jpeg')

# Was unauthenticated. That made it a defacement surface for anyone who could
# reach the port — overwrite folder.jpg/poster.jpg in an existing artist folder,
# and spend the stored Plex token updating that collection's artwork. Documented
# as "deliberate, bounded" through v1.6.1 on the grounds that requiring auth
# would break its consumer; the consumer turned out to be this app's own
# dashboard, which is authenticated. See the `search` alias note above.
@app.route('/api/artwork/swap', methods=['POST'])
@app.route('/api/artwork/swap_noauth', methods=['POST'])
@require_auth
def api_artwork_swap():
    """Swap artwork for a Plex collection."""
    data = request.get_json(silent=True) or {}
    artist_name = data.get('artist_name', '').strip()
    new_image_url = data.get('new_image_url', '').strip()
    if not artist_name or not new_image_url:
        return jsonify({'error': 'artist_name and new_image_url are required'}), 400
    config = load_config()
    result = plex_swap_collection_artwork(config, artist_name, new_image_url)
    return jsonify(result)


@app.route('/api/plex/collections/create', methods=['POST'])
@require_auth
def api_plex_collection_create():
    """Create a Plex collection for a single artist on demand."""
    data = request.get_json()
    artist = data.get('artist', '').strip()
    if not artist:
        return jsonify({'error': 'Artist name is required'}), 400

    config = load_config()
    root_path = _music_root(config)

    artist_folder = _sanitize_folder_name(artist)
    artist_path = os.path.join(root_path, artist_folder)

    if not os.path.isdir(artist_path):
        return jsonify({'error': f'Artist folder not found: {artist_folder}'}), 404

    result = plex_sync_artist_collection(config, artist, artist_path)
    return jsonify({'result': result})



@app.route('/api/password', methods=['POST'])
@require_auth
def api_password():
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
@require_auth
def api_downloads_clear():
    """Clear the download history tracker and active download progress entries."""
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
@require_auth
def api_plex_base_path():
    """Get or set the global Plex media base path. Each channel's plex_media_path is relative to this."""
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
@require_auth
def api_system_info():
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
@require_auth
def api_system_health():
    """Dependency check for the Settings page's System Health panel.
    Most useful for the local (non-Docker) install path — the Docker image
    always bakes in ffmpeg/Pillow/fonts, but even there this catches a
    misconfigured FFMPEG_PATH."""
    binaries = transcode.check_dependencies()
    title_card_deps = check_title_card_dependencies()
    # Cookies are optional, but their absence used to be invisible: a
    # cookies.txt sat in the repo for months while nothing passed it to yt-dlp,
    # so age-restricted downloads failed with no indication why. Reporting it
    # here means "is it being used" is answerable without reading the source.
    cookies = _cookies_file()
    return jsonify({
        'ffmpeg': binaries['ffmpeg'],
        'ffprobe': binaries['ffprobe'],
        'pillow': title_card_deps['pillow'],
        'fonts': title_card_deps['fonts'],
        'cookies': {
            'available': bool(cookies),
            'path': cookies,
            'detail': (f'In use: {cookies}' if cookies else
                       'Not found — age-restricted and members-only videos will '
                       'fail. Drop a yt-dlp cookies.txt into the data directory '
                       'to enable them.'),
        },
    })


@app.route('/api/system/version')
@require_auth
def api_system_version():
    """Current version plus, if enabled, whether a newer release exists.

    Always returns immediately — updates.get_status() serves the cached
    answer and refreshes in the background, so a slow or unreachable GitHub
    can't stall the sidebar this feeds.
    """
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
                           title=title, channel_url=url, download_id=download_id,
                           **_download_options(url))
            mark_video_downloaded(vid, url)
            _notify_download('complete', title, url)
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, DownloadCancelled):
                print(f'[monitor] cancelled {vid} ({title})')
            else:
                print(f'[monitor] FAILED {vid} ({title}): {exc}')
                _notify_download('failed', title, url, error=exc)

    _DOWNLOAD_EXECUTOR.submit(_run)
    return download_id


def _monitor_list_videos(channel_url, limit):
    """Channel listing for the monitor, bounded.

    get_channel_videos() returns a channel's *entire* listing — fine for the
    manual endpoint, which slices [:20], but the monitor iterates everything.
    Slicing here bounds the work per tick; the fetch itself is still the
    expensive part, which is why max_listing exists at all.
    """
    videos = get_channel_videos(channel_url) or []
    return videos[:limit] if limit else videos


def _monitor_downloaded_ids(channel_url):
    """The set of already-downloaded ids for one channel — read ONCE per channel.

    v1.5.0 passed is_video_downloaded() into the loop, and each call re-read and
    re-parsed the whole tracker file under a lock: measured at 500 file reads for
    a 500-video channel, per channel, per tick.
    """
    return set(load_downloaded_tracker().get(channel_url, []))


def _monitor_queue_depth():
    """How many downloads are queued or running, so the monitor can stop piling on.

    The executor has 2 workers and an unbounded queue, so nothing else prevents
    an hourly tick from adding work faster than it drains.
    """
    try:
        active = get_active_downloads() or []
    except Exception:  # noqa: BLE001
        return 0
    # The status list lives in downloader so this and reconcile_interrupted()
    # can't drift apart — if they ever disagreed, a status counted here but not
    # cleared there would throttle the monitor forever.
    return sum(1 for d in active
               if d.get('status') in downloader_module.IN_FLIGHT_STATUSES)


def _monitor_free_space(path):
    """Free bytes at a download destination, or None if it can't be determined."""
    try:
        target = _resolve_plex_path(path)
        # Walk up to the nearest existing ancestor: the artist subfolder may not
        # exist yet, but its parent volume is what we care about.
        while target and not os.path.isdir(target):
            parent = os.path.dirname(target)
            if parent == target:
                return None
            target = parent
        if not target:
            return None
        return shutil.disk_usage(target).free
    except Exception:  # noqa: BLE001
        return None


def _monitor_run_retention():
    """Automatic post-tick sweep across every media root."""
    config = load_config()
    result = retention.sweep(config, roots=_gather_media_roots(config), dry_run=False)
    return result.get('applied') or {}


_CHANNEL_MONITOR = scheduler.ChannelMonitor(
    load_config=load_config,
    list_videos=_monitor_list_videos,
    list_downloaded=_monitor_downloaded_ids,
    start_download=_monitor_start_download,
    queue_depth=_monitor_queue_depth,
    free_space=_monitor_free_space,
    run_retention=_monitor_run_retention,
)


@app.route('/api/channels/quality', methods=['POST'])
@require_auth
def api_channels_quality():
    """Cap the resolution downloaded from one channel. 0 clears the cap.

    Per-channel rather than global because the reason to cap is usually one
    specific channel publishing 4K — capping everything to protect against that
    would needlessly downgrade the rest.
    """
    data = request.get_json(silent=True) or {}
    url = (data.get('url') or '').strip()
    try:
        height = max(0, int(data.get('max_height', 0)))
    except (TypeError, ValueError):
        return jsonify({'error': 'max_height must be a number'}), 400

    found = {'ok': False}

    def _set(config):
        for ch in config.get('channels', []):
            if ch.get('url') == url:
                if height:
                    ch['max_height'] = height
                else:
                    ch.pop('max_height', None)
                found['ok'] = True
                break

    _update_config(_set)
    if not found['ok']:
        return jsonify({'error': 'Channel not found'}), 404
    label = f'{height}p' if height else 'best available'
    return jsonify({'success': True, 'message': f'Quality cap set to {label}'})


@app.route('/api/downloads/<download_id>/cancel', methods=['POST'])
@require_auth
def api_download_cancel(download_id):
    """Cancel a queued or running download.

    Cooperative: a queued item flips to cancelled immediately, while a running
    one stops at its next progress callback — under a second in practice. Killing
    the worker thread instead would risk leaving a half-written file on a network
    share, which is a failure mode this project has paid for before.
    """
    ok, detail = request_cancel(download_id)
    return jsonify({'success': ok, 'detail': detail}), (200 if ok else 409)


@app.route('/api/downloads/<download_id>/retry', methods=['POST'])
@require_auth
def api_download_retry(download_id):
    """Re-queue a failed or cancelled download.

    Re-queues as a *new* download rather than resurrecting the old entry, so the
    Downloads list keeps an honest record of the attempt that failed instead of
    rewriting history.
    """
    entry = next((d for d in (get_active_downloads() or [])
                  if d.get('download_id') == download_id), None)
    if not entry:
        return jsonify({'error': 'Unknown download'}), 404
    if entry.get('status') not in ('error', 'cancelled'):
        return jsonify({'error': f"Can only retry a failed or cancelled download "
                                 f"(this one is {entry.get('status')})"}), 409

    video_id = entry.get('video_id')
    channel_url = entry.get('channel_url') or ''
    title = entry.get('title') or 'Unknown'

    config = load_config()
    download_path = './downloads'
    # Fall back to where the original attempt was actually headed, not to the
    # generic downloads folder. This loop matches on ch['url'], and a music
    # video's channel_url is the synthetic "music_video_<Artist>" key, which
    # matches no channel — so a retried music video used to land outside its
    # artist folder: no artwork, no collection, invisible to the Artists page.
    # The correct destination was recorded on the entry all along.
    plex_media_path = entry.get('final_path') or _resolve_plex_path('./downloads')
    for ch in config.get('channels', []):
        if ch['url'] == channel_url:
            # A real channel still wins, so retrying an old failure after
            # editing that channel's path uses the new one.
            download_path = ch.get('download_path', './downloads')
            plex_media_path = _resolve_plex_path(ch.get('plex_media_path', './downloads'))
            break

    # Recover the artist from the synthetic key so a retried music video is
    # named the same way the original attempt would have been.
    music_artist = _artist_from_music_key(channel_url)
    plex_media_path = _music_retry_destination(plex_media_path, music_artist)

    new_id = queue_download(video_id, title, channel_url, final_path=plex_media_path)

    def _run():
        try:
            os.makedirs(plex_media_path, exist_ok=True)
            download_video(video_id, download_path, plex_media_path, title=title,
                           channel_url=channel_url, download_id=new_id,
                           music_artist=music_artist,
                           **_download_options(channel_url))
            mark_video_downloaded(video_id, channel_url)
            _notify_download('complete', title, channel_url)
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, DownloadCancelled):
                print(f'[retry] cancelled {video_id} ({title})')
            else:
                print(f'[retry] FAILED {video_id} ({title}): {exc}')
                _notify_download('failed', title, channel_url, error=exc)

    _DOWNLOAD_EXECUTOR.submit(_run)
    return jsonify({'success': True, 'download_id': new_id,
                    'message': f'Retrying {title}'})


@app.route('/api/monitor/status')
@require_auth
def api_monitor_status():
    return jsonify(_CHANNEL_MONITOR.status())


@app.route('/api/monitor/config', methods=['POST'])
@require_auth
def api_monitor_config():
    """Enable/disable monitoring and set its interval."""
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
        for key, minimum in (('max_per_channel', 1), ('max_listing', 1),
                             ('max_queue_depth', 1), ('min_free_gb', 0)):
            if key in data:
                try:
                    cfg[key] = max(minimum, int(data[key]))
                except (TypeError, ValueError):
                    pass
        config['channel_monitor'] = cfg

    _update_config(_set)
    # Wake the loop so a changed interval or a freshly-enabled monitor applies
    # now, instead of after the remainder of the current sleep.
    _CHANNEL_MONITOR.trigger()
    return jsonify({'success': True, 'status': _CHANNEL_MONITOR.status()})


@app.route('/api/monitor/run', methods=['POST'])
@require_auth
def api_monitor_run():
    """Run one monitoring pass immediately, regardless of the enabled flag.

    Synchronous on purpose: it's the "Check now" button, and the user is waiting
    to see what it found. Downloads themselves still go to the worker pool.
    """
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
@require_auth
def api_retention_config():
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
        # A second, separate opt-in: 'enabled' allows manual sweeps, 'auto_sweep'
        # lets the monitor prune unattended. Kept separate so upgrading can never
        # start deleting media by surprise.
        if 'auto_sweep' in data:
            cfg['auto_sweep'] = bool(data['auto_sweep'])
        config['retention'] = cfg

    _update_config(_set)
    return jsonify({'success': True, 'retention': load_config().get('retention', {})})


@app.route('/api/retention/plan')
@require_auth
def api_retention_plan():
    """Dry run — what a sweep would delete. Never deletes anything."""
    cfg = load_config()
    result = retention.sweep(cfg, roots=_gather_media_roots(cfg), dry_run=True)
    plan = result['plan']
    # Trim the payload: a large library could plan hundreds of deletions and the
    # UI only shows a sample alongside the totals.
    sample = plan['candidates'][:50]
    return jsonify({
        'roots': plan['roots'],
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
@require_auth
def api_retention_apply():
    """Actually delete. Requires an explicit confirm, and honours the enabled flag.

    The confirm token is not security — the session already authenticates the
    admin — it's there so this can never be triggered by a stray request or a
    mis-wired button, which for the one destructive endpoint in the app is worth
    the friction.
    """
    data = request.get_json(silent=True) or {}
    if data.get('confirm') != 'DELETE':
        return jsonify({'error': "Refusing to delete without {\"confirm\": \"DELETE\"}"}), 400

    cfg = load_config()
    result = retention.sweep(cfg, roots=_gather_media_roots(cfg), dry_run=False)
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
@require_auth
def api_notifications_config():
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
@require_auth
def api_notifications_test():
    ok, detail = notify.send_test(load_config())
    return jsonify({'success': ok, 'detail': detail})


@app.route('/api/system/update-check', methods=['POST'])
@require_auth
def api_system_update_check_toggle():
    """Turn the update check on or off."""
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
@require_auth
def api_downloads_verify():
    """Verify that downloaded files exist at their final destination paths."""
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
@require_auth
def api_download_verify_single(download_id):
    """Verify a single download's file at its final destination."""
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
@require_auth
def api_artwork_sync():
    """Trigger artwork sync for all artist folders or a specific one."""
    data = request.get_json() or {}
    force = data.get('force', False)
    artist = data.get('artist', '').strip()
    
    config = load_config()
    artwork_cfg = config.get('artwork_sync', {})
    root_path = _music_root(config)
    
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
@require_auth
def api_artwork_status():
    """Get artwork sync status — which folders have artwork and which don't."""
    config = load_config()
    root_path = _music_root(config)
    
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
@require_auth
def api_plex_collections_sync():
    """Sync Plex collections for all artist folders or a specific one."""
    data = request.get_json() or {}
    artist = data.get('artist', '').strip()
    config = load_config()
    root_path = _music_root(config)
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
@require_auth
def api_plex_collections_status():
    """Get Plex collection status — which artists have collections and which don't."""
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
        root_path = _music_root(config)
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
@require_auth
def api_plex_collections_duplicates():
    """Report any same-title collections in the music-video library — a
    symptom of the check-then-create race in plex_ensure_smart_collection()
    that's now fixed with a lock, but pre-existing duplicates from before
    the fix don't clean themselves up."""
    config = load_config()
    duplicates = plex_find_duplicate_collections(config)
    return jsonify({'duplicate_groups': duplicates, 'total_groups': len(duplicates)})


@app.route('/api/plex/collections/dedupe', methods=['POST'])
@require_auth
def api_plex_collections_dedupe():
    """Delete duplicate same-title collections, keeping the one with the
    highest childCount from each group. Safe because every duplicate in a
    group shares the identical smart filter — there's no item list to
    reconcile, just extra collection objects to remove."""
    config = load_config()
    result = plex_dedupe_collections(config)
    return jsonify(result)


@app.route('/api/conversion/scan', methods=['POST'])
@require_auth
def api_conversion_scan():
    """Dry run: report which downloaded video files aren't already
    Plex-direct-play-compatible (MP4/H.264/AAC), without converting
    anything."""
    config = load_config()
    candidates = _scan_conversion_candidates(config)
    return jsonify({
        'needs_conversion': len(candidates),
        'files': candidates[:50],
        'truncated': len(candidates) > 50,
    })


@app.route('/api/conversion/start', methods=['POST'])
@require_auth
def api_conversion_start():
    """Kick off the batch conversion job in a background thread. Refuses to
    start a second job if one is already running."""
    with _CONVERSION_LOCK:
        if _CONVERSION_STATE['running']:
            return jsonify({'error': 'A conversion job is already running'}), 409
    config = load_config()
    thread = threading.Thread(target=_run_conversion_job, args=(config,), daemon=True)
    thread.start()
    return jsonify({'success': True, 'message': 'Conversion job started'})


@app.route('/api/conversion/status', methods=['GET'])
@require_auth
def api_conversion_status():
    with _CONVERSION_LOCK:
        return jsonify(dict(_CONVERSION_STATE))


@app.route('/api/plex/titles/clean', methods=['POST'])
@require_auth
def api_plex_titles_clean():
    """Clean up video titles in the music-video library (strips YouTube ID,
    embedded promo URLs, and generic "Official Video" boilerplate)."""
    config = load_config()
    result = plex_clean_video_titles(config)
    return jsonify(result)


@app.route('/api/plex/title-cards/generate', methods=['POST'])
@require_auth
def api_plex_title_cards_generate():
    """Generate + upload a designed title-card poster (artist + song title
    over the artist's own art) for every video in the music-video library
    that doesn't already have one, replacing Plex's auto-extracted
    video-frame thumbnail."""
    data = request.get_json(silent=True) or {}
    force = data.get('force', False)
    config = load_config()
    root_path = _music_root(config)
    results = plex_generate_title_cards_for_all(config, root_path, force=force)
    return jsonify({
        'results': results,
        'total_artists': len(results),
        'total_generated': sum(r.get('generated', 0) for r in results),
        'errors': [e for r in results for e in r.get('errors', [])],
    })


@app.route('/api/plex/config', methods=['GET', 'POST'])
@require_auth
def api_plex_config():
    """Get or update Plex configuration."""
    config = load_config()
    
    if request.method == 'GET':
        # Same reasoning as /api/config: the UI only ever tests presence, so the
        # token itself never needs to leave the server.
        plex = dict(config.get('plex', {}))
        plex['token_set'] = bool(plex.pop('token', None))
        return jsonify(plex)
    
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
@require_auth
def api_plex_discover_library():
    """Auto-discover the music video library key from Plex.

    Kept for API compatibility, but the dashboard no longer calls this
    directly — auto-discovering and saving in one step with no way to see
    what was actually picked is exactly how a mistyped library title
    ("Muisc Videos") silently pointed collection sync at the wrong library
    before (see REFERENCE.md). The UI now uses GET /api/plex/libraries to
    show every library with the auto-discovered one pre-selected, and only
    saves once the user confirms via POST /api/plex/config."""
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
@require_auth
def api_plex_libraries():
    """List every library on the connected Plex server, flagging which one
    auto-discovery would pick — lets the UI show a confirm/pick step
    instead of trusting the guess blindly."""
    config = load_config()
    libraries = plex_list_libraries(config)
    return jsonify({'libraries': libraries})


@app.route('/api/plex/oauth/start', methods=['POST'])
@require_auth
def api_plex_oauth_start():
    """Initiate Plex OAuth flow."""
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
@require_auth
def api_plex_oauth_check():
    """Check if Plex OAuth PIN has been authenticated."""
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
@require_auth
def api_plex_oauth_servers():
    """Get Plex servers associated with the authenticated account."""
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
        root_path = _music_root(config)
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
                _write_raw_config(cfg)
            # music_video_plex_path is deliberately NOT seeded any more — it was
            # removed in v1.8.0 and re-adding it here would resurrect the key
            # state.migrate_music_video_path() had just deleted, on every start.
        except Exception:
            pass
    
    # Close out anything a previous run left mid-flight, before the monitor
    # starts. Must happen here and not at import: at import time under the test
    # client, or a `python -c "import app"` check, there is no previous run to
    # reconcile — and it must happen before _CHANNEL_MONITOR.start(), because
    # stale in-flight rows are what used to make the queue-depth brake disable
    # monitoring permanently.
    try:
        _interrupted, _pruned = reconcile_interrupted()
        if _interrupted or _pruned:
            print(f"[downloads] reconciled {_interrupted} interrupted, "
                  f"pruned {_pruned} old history entries")
    except Exception as exc:  # noqa: BLE001
        # Never let housekeeping stop the app from starting.
        print(f"[downloads] could not reconcile download history: {exc}")

    try:
        _sweep_staging_dirs()
    except Exception as exc:  # noqa: BLE001
        print(f"[downloads] could not sweep staging directories: {exc}")

    # Start the artwork background watcher
    start_artwork_watcher()

    # Start the channel monitor. The thread always runs; whether a tick does
    # anything is decided per-tick from config, so toggling it in Settings takes
    # effect without a restart. Started here rather than at import so it never
    # runs under the test client or a `python -c "import app"` check.
    _CHANNEL_MONITOR.start()
    # status() rather than the private _settings(): this line unpacked
    # _settings() as a 3-tuple until v1.5.1 turned it into a dict, which
    # crash-looped the container on startup. Nothing caught it because no test
    # executes __main__ — only a real container run does.
    _mon = _CHANNEL_MONITOR.status()
    print(f"[monitor] channel monitor thread started "
          f"(enabled={_mon['enabled']}, interval={_mon['interval_minutes']}m, "
          f"listing={_mon['max_listing']}, queue_limit={_mon['max_queue_depth']}, "
          f"min_free_gb={_mon['min_free_gb']})")

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
