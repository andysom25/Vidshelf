"""Checks GitHub Releases for a newer Vidshelf and caches the answer.

Design constraints, since this is the one feature that makes a self-hosted
app phone home:

- **Server-side, not from the browser.** A fetch() from the dashboard would
  leak every user's IP to GitHub and break on networks that can't reach it.
- **Never blocks a page load.** get_status() always returns immediately from
  cache; if the cache is stale it kicks off a background refresh and serves
  the old answer meanwhile. A slow or unreachable GitHub must never make the
  dashboard hang.
- **Cached for a day, persisted to the data directory.** GitHub allows 60
  unauthenticated API requests per hour per IP; one per day per install
  leaves that irrelevant, and persisting means a restart loop doesn't turn
  into a request loop.
- **Notifies, never updates.** Deciding when to pull a new image is the
  operator's call.
- **Fails silently.** No network, DNS blocked, GitHub down, rate limited — a
  failed check reports "unknown" and the UI shows nothing. An update check is
  never worth an error banner.

/releases/latest is used rather than /releases because GitHub already
excludes drafts and prereleases from it, so a tagged beta can't be advertised
as an upgrade.
"""

import os
import re
import threading
import time

import requests

import state

REPO = 'andysom25/Vidshelf'
RELEASES_API = 'https://api.github.com/repos/{}/releases/latest'.format(REPO)
RELEASES_PAGE = 'https://github.com/{}/releases/latest'.format(REPO)

CACHE_TTL_SECONDS = 24 * 60 * 60
REQUEST_TIMEOUT_SECONDS = 5

_refresh_lock = threading.Lock()
_refreshing = False

_VERSION_RE = re.compile(r'^v?(\d+)\.(\d+)\.(\d+)')


def _cache_path():
    # Resolved per call rather than at import so VIDSHELF_DATA_DIR changes
    # (and the tests, which point it at a temp dir) are picked up.
    return os.path.join(state.DATA_DIR, 'update_check.json')


def parse_version(text):
    """'v1.2.3' / '1.2.3' -> (1, 2, 3). None if unparseable.

    Returns a tuple so comparison is numeric: plain string comparison would
    put '1.10.0' before '1.9.0', which is exactly the case where a user most
    needs to be told about an update.
    """
    if not text:
        return None
    match = _VERSION_RE.match(str(text).strip())
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def is_newer(candidate, current):
    parsed_candidate = parse_version(candidate)
    parsed_current = parse_version(current)
    if parsed_candidate is None or parsed_current is None:
        # 'unknown' current version (VERSION file missing from the image, say)
        # must not produce a spurious "update available".
        return False
    return parsed_candidate > parsed_current


def _fetch_latest():
    """Return {'latest': '1.2.0', 'url': ...} or {'error': '...'}."""
    try:
        response = requests.get(
            RELEASES_API,
            timeout=REQUEST_TIMEOUT_SECONDS,
            headers={
                'Accept': 'application/vnd.github+json',
                'User-Agent': 'Vidshelf-update-check',
            },
        )
        if response.status_code == 403:
            # Rate limited — cache it like any other answer so we back off
            # rather than retrying into the limit.
            return {'error': 'rate limited'}
        if response.status_code != 200:
            return {'error': 'http {}'.format(response.status_code)}
        payload = response.json()
        tag = payload.get('tag_name')
        if not tag:
            return {'error': 'no tag in response'}
        return {'latest': tag, 'url': payload.get('html_url') or RELEASES_PAGE}
    except (requests.RequestException, ValueError) as exc:
        return {'error': str(exc)[:200]}


def _refresh():
    global _refreshing
    try:
        result = _fetch_latest()
        result['checked_at'] = time.time()
        try:
            state.write_json(_cache_path(), result)
        except OSError:
            pass  # a read-only data dir shouldn't break the app
        return result
    finally:
        # Must be in a finally: if this thread dies for any reason and the
        # flag stays set, no update check ever runs again for the life of the
        # process — a permanently silent failure.
        with _refresh_lock:
            _refreshing = False


def _maybe_refresh_async():
    """Start a background refresh unless one is already running."""
    global _refreshing
    with _refresh_lock:
        if _refreshing:
            return
        _refreshing = True
    threading.Thread(target=_refresh, name='update-check', daemon=True).start()


def get_status(current_version, enabled=True):
    """Non-blocking. Returns what's known now; refreshes in the background.

    {'enabled', 'current', 'latest', 'update_available', 'url', 'checked_at'}
    """
    status = {
        'enabled': bool(enabled),
        'current': current_version,
        'latest': None,
        'update_available': False,
        'url': RELEASES_PAGE,
        'checked_at': None,
    }
    if not enabled:
        return status

    cached = state.read_json(_cache_path())
    checked_at = cached.get('checked_at') or 0
    if time.time() - checked_at > CACHE_TTL_SECONDS:
        _maybe_refresh_async()

    latest = cached.get('latest')
    if latest:
        status['latest'] = latest
        status['update_available'] = is_newer(latest, current_version)
        status['url'] = cached.get('url') or RELEASES_PAGE
    status['checked_at'] = cached.get('checked_at')
    return status
