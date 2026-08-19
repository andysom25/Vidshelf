"""The music-video library: where it lives, and what is in it.

Extracted from app.py in v1.11.0. One cached filesystem walk
(_library_scan) feeds every panel on the dashboard, because the media root is
normally a CIFS mount and walking it per-panel was the reason the dashboard was
slow enough to look broken.

Two things in here are easy to break and were each a released bug:

  - _maybe_rescan_async() must be called from OUTSIDE _LIBRARY_SCAN_LOCK. The
    lock is not reentrant, so calling it while holding the lock deadlocks every
    request once the cache goes stale.
  - _invalidate_library_scan() must clear 'data', not just zero 'at'. Zeroing the
    timestamp alone leaves a stale entry servable, so the first read after a
    download returned pre-download counts.

Deliberately knows nothing about Flask, so blueprints can import it freely.
"""

import os
import threading
import time

import downloader as downloader_module
import retention
import titles
from artwork_sync import has_artwork
from config_store import load_config

DEFAULT_MUSIC_ROOT = '/app/music_videos_final'


def _music_root(config=None):
    """The one directory music videos live in.

    There used to be two settings for this. `artwork_sync.root_path` had twelve
    readers — the Artists page, artwork sync, retention, collections, title
    cards — and `music_video_plex_path` had none in any download path: it was
    editable in Settings, persisted, seeded into config.json.example and
    documented in the README, while the download route ignored it and hardcoded
    the value below. A setting that lies is worse than no setting, so v1.8.0
    made this the single source of truth and migrated the other key away
    (see state.migrate_music_video_path).
    """
    cfg = config if config is not None else load_config()
    root = (cfg.get('artwork_sync', {}) or {}).get('root_path') or DEFAULT_MUSIC_ROOT
    return root


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


def _sweep_staging_dirs():
    """Clean local staging leftovers at startup.

    Only ONE directory is vouched for as pure staging: `./downloads/music_videos`.
    The music-video route always writes there and always copies out to
    `<music root>/<Artist>/`, so nothing in it is ever a finished location. That
    is a property of the code, not of the current configuration.

    `./downloads` gets intermediates-only treatment, permanently. It is the
    historical default `plex_media_path`, which means it can hold finished media
    from *any* past configuration — and a config that no longer points at it is
    exactly when it holds orphans. Deriving safety from today's config deleted
    four finished videos on the first install this ran against; see
    sweep_staging's docstring.
    """
    config = load_config()

    intermediates_only = ['./downloads']
    for ch in config.get('channels', []):
        intermediates_only.append(ch.get('download_path', './downloads'))

    pure_staging = ['./downloads/music_videos']

    # A channel whose download path is not also its Plex path is staging by the
    # same argument as music_videos: the file is always copied out of it.
    for ch in config.get('channels', []):
        dl = ch.get('download_path', './downloads')
        dest = _resolve_plex_path(ch.get('plex_media_path', './downloads'))
        if os.path.normpath(os.path.abspath(dl)) != os.path.normpath(os.path.abspath(dest)):
            pure_staging.append(dl)
    # ...but never if some other channel treats it as a destination.
    finals = {os.path.normpath(os.path.abspath(p)) for p in _gather_media_roots(config)}
    finals.add(os.path.normpath(os.path.abspath(_music_root(config))))
    for ch in config.get('channels', []):
        finals.add(os.path.normpath(os.path.abspath(
            _resolve_plex_path(ch.get('plex_media_path', './downloads')))))
    pure_staging = [p for p in pure_staging
                    if os.path.normpath(os.path.abspath(p)) not in finals]

    removed, freed = downloader_module.sweep_staging(intermediates_only, pure_staging)
    if removed:
        print(f"[downloads] swept {removed} leftover file(s), "
              f"freed {freed / (1024 * 1024):.0f} MB")


_LIBRARY_SCAN_CACHE = {'at': 0.0, 'data': None}
_LIBRARY_SCAN_TTL = 300  # seconds
_LIBRARY_SCAN_LOCK = threading.Lock()

# How far back the "added over time" chart looks.
LIBRARY_HISTORY_MONTHS = 12
RECENTLY_ADDED_LIMIT = 10
TOP_ARTISTS_LIMIT = 8


def _library_scan(force=False):
    """One walk of the media roots, feeding every dashboard panel.

    Deliberately a single scan rather than an endpoint per panel. The media root
    is normally a CIFS mount, so each traversal is the expensive part and doing
    it four times to fill four cards would be four times the cost for the same
    bytes. Everything the dashboard shows is derived from this one pass.

    On dates: st_mtime is the closest thing to a download date that exists
    today. The tracker records only video ids — no timestamps at all — so
    "added over time" and "recently added" come from the filesystem. That is
    accurate for files Vidshelf wrote and wrong for anything moved or re-copied
    on the NAS by hand, which is why the UI labels it "added" rather than
    "downloaded". Real download dates need the v2.0 data model.

    Cached for _LIBRARY_SCAN_TTL: the dashboard asks on every visit, and a
    stat() per file over SMB is cheap for hundreds of videos and decidedly not
    for tens of thousands.
    """
    now = time.time()
    serve_stale = False
    with _LIBRARY_SCAN_LOCK:
        cached = _LIBRARY_SCAN_CACHE['data']
        fresh = cached is not None and (now - _LIBRARY_SCAN_CACHE['at']) < _LIBRARY_SCAN_TTL
        if cached is not None and not force:
            if fresh:
                return cached
            serve_stale = True

    if serve_stale:
        # Stale, but usable. Serve it and refresh behind the request rather than
        # making someone wait for a CIFS walk — measured at 2.1s on a 197-video
        # library, and it is the *user* who pays it every time the TTL lapses.
        # Mirrors updates.get_status(), which returns what it knows and refreshes
        # in the background for exactly this reason.
        #
        # The numbers are minutes-stale at worst, and a completed download
        # invalidates the cache outright, so the case this covers is "nobody has
        # looked at the dashboard in a while" — where a slightly old count beats
        # a spinner.
        #
        # Deliberately called *outside* the lock above: _maybe_rescan_async
        # acquires the same non-reentrant lock, so calling it from within would
        # deadlock every request the moment the cache went stale.
        _maybe_rescan_async()
        return cached

    config = load_config()
    music_root = os.path.normpath(os.path.abspath(_music_root(config)))

    total_bytes = 0
    video_count = 0
    per_artist = {}          # artist -> {'videos': n, 'bytes': n}
    per_month = {}           # 'YYYY-MM' -> n
    recent = []              # (mtime, artist, title, bytes)

    for root in _gather_media_roots(config):
        root_abs = os.path.normpath(os.path.abspath(root))
        for dirpath, _, filenames in os.walk(root):
            for name in filenames:
                if not name.lower().endswith(retention.VIDEO_EXTENSIONS):
                    continue
                try:
                    stat = os.stat(os.path.join(dirpath, name))
                except OSError:
                    # A file can vanish mid-walk (retention sweep, a move on the
                    # NAS). Skip it rather than failing the whole dashboard.
                    continue

                total_bytes += stat.st_size
                video_count += 1

                # Artist is the folder directly under the music root. Files
                # elsewhere (channel destinations) still count toward totals but
                # have no artist to attribute them to.
                artist = None
                here = os.path.normpath(os.path.abspath(dirpath))
                if here != music_root and _is_under(here, music_root):
                    artist = titles.folder_to_artist(
                        os.path.basename(here.rstrip(os.sep)))
                elif here != root_abs:
                    artist = os.path.basename(here.rstrip(os.sep))

                if artist:
                    bucket = per_artist.setdefault(
                        artist, {'videos': 0, 'bytes': 0, 'dir': here})
                    bucket['videos'] += 1
                    bucket['bytes'] += stat.st_size

                month = time.strftime('%Y-%m', time.localtime(stat.st_mtime))
                per_month[month] = per_month.get(month, 0) + 1
                recent.append((stat.st_mtime, artist or '', name, stat.st_size))

    recent.sort(reverse=True)
    cutoff_30d = now - (30 * 86400)

    # Artwork status is folded in here rather than left to
    # /api/artists/summary. The dashboard's Plex-health panel originally called
    # that endpoint, which walks the media root all over again — measured at
    # 0.75s per call, every call, against 0.00s for this cache. That second
    # traversal was exactly the duplication a single scan exists to avoid, and
    # it was the whole reason the dashboard felt slow. One extra isdir/isfile
    # check per artist folder is nothing next to a second full walk.
    missing_artwork = 0
    for info in per_artist.values():
        info['has_artwork'] = has_artwork(info['dir'])
        if not info['has_artwork']:
            missing_artwork += 1

    data = {
        'artists': len(per_artist),
        'videos': video_count,
        'bytes': total_bytes,
        'added_30d': sum(1 for r in recent if r[0] >= cutoff_30d),
        'missing_artwork': missing_artwork,
        'months': _month_series(per_month, LIBRARY_HISTORY_MONTHS, now),
        'top_artists': sorted(
            ({'artist': a, 'videos': v['videos'], 'bytes': v['bytes']}
             for a, v in per_artist.items()),
            key=lambda a: (-a['bytes'], a['artist']))[:TOP_ARTISTS_LIMIT],
        'recent': [{'artist': a, 'title': titles.clean_video_title(
                        os.path.splitext(t)[0]),
                    'bytes': b, 'added_at': m}
                   for m, a, t, b in recent[:RECENTLY_ADDED_LIMIT]],
        # The UI says "added" rather than "downloaded", and shows this string in
        # a tooltip on the Added card, so the caveat travels with the number
        # instead of living only in a comment.
        'dates_from': 'file modification time',
    }

    with _LIBRARY_SCAN_LOCK:
        _LIBRARY_SCAN_CACHE.update({'at': now, 'data': data})
    return data


_library_rescanning = False


def _maybe_rescan_async():
    """Refresh the library scan in the background, one at a time.

    The guard matters: /api/stats and /api/library/stats are requested together
    on every dashboard load, so without it a stale cache would start two
    concurrent CIFS walks for the same data — and the 60-second auto-refresh
    would keep doing it.
    """
    global _library_rescanning
    with _LIBRARY_SCAN_LOCK:
        if _library_rescanning:
            return
        _library_rescanning = True

    def _run():
        global _library_rescanning
        try:
            _library_scan(force=True)
        except Exception as exc:  # noqa: BLE001
            print(f'[library] background rescan failed: {exc}')
        finally:
            with _LIBRARY_SCAN_LOCK:
                _library_rescanning = False

    threading.Thread(target=_run, name='library-scan', daemon=True).start()


def _invalidate_library_scan():
    """Drop the cached scan so the next dashboard load reflects a new file.

    Called when a download completes. Without this the five-minute TTL means
    you download something, look at the dashboard, and it isn't there — which
    reads as a bug rather than as caching. Invalidating on the event is far
    better than shortening the TTL: it costs one rescan when something actually
    changed, instead of a CIFS walk every minute forever.

    Drops the *data*, not just the timestamp. Expiring the timestamp alone is
    not enough now that a stale entry is served while it refreshes: the next
    read would hand back the pre-download counts and only correct itself once
    the background scan landed, so the download still appeared to do nothing.
    With no cached value there is nothing stale to serve, so the next read
    blocks and returns the truth — which is the right trade for an event that
    happens once per completed download.
    """
    with _LIBRARY_SCAN_LOCK:
        _LIBRARY_SCAN_CACHE['at'] = 0.0
        _LIBRARY_SCAN_CACHE['data'] = None


def _is_under(child, parent):
    """True if child is inside parent, comparing whole path components."""
    try:
        return os.path.commonpath([child, parent]) == parent
    except ValueError:      # different drives on Windows
        return False


def _month_series(counts, months, now):
    """A dense, chronologically ordered month series ending at the current month.

    Dense on purpose: a month with no downloads has to appear as a zero, or the
    chart silently closes the gap and a quiet spell reads as continuous
    activity. Sorting the dict keys alone would do exactly that.
    """
    series = []
    year, month = time.localtime(now).tm_year, time.localtime(now).tm_mon
    for offset in range(months - 1, -1, -1):
        m = month - offset
        y = year
        while m <= 0:
            m += 12
            y -= 1
        key = '%04d-%02d' % (y, m)
        series.append({'month': key, 'count': counts.get(key, 0)})
    return series


def _library_size(force=False):
    """Back-compat shim: (bytes, videos) from the shared scan."""
    scan = _library_scan(force=force)
    return scan['bytes'], scan['videos']


def _gather_media_roots(config):
    """Every directory this app might have downloaded videos into: the
    music-video root plus every configured channel's resolved
    plex_media_path plus the global plex_base_path, deduplicated."""
    roots = set()
    music_root = _music_root(config)
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
