"""
artwork_sync.py — Artist Artwork Downloader for Plex Music Video Libraries

Watches a root folder for new artist subdirectories, fetches artist artwork
from public APIs, and saves folder.jpg/poster.jpg/fanart.jpg so Plex can
display proper artist images instead of generic folder icons.

Integrated into Vidshelf as a background watcher thread + API endpoint.
"""

import os
import json
import logging
import re
import threading
import socket
import ipaddress
import uuid
import state
import titles

# Title and folder-name logic lives in titles.py so the *download* path can
# use it too. It was only ever reachable from here, which meant a title could
# only be cleaned after a file had already been written with a bad name — the
# ordering that made the "Artist - Song" incidents in REFERENCE.md unfixable.
# Re-exported under the old private names so nothing else had to move.
folder_to_artist = titles.folder_to_artist
artist_to_folder = titles.artist_to_folder
_clean_video_title = titles.clean_video_title
_normalize_artist_prefix = titles.normalize_artist_prefix
_strip_artist_prefix_quotes = titles._strip_artist_prefix_quotes
from datetime import datetime, timezone
from urllib.parse import quote, urlencode, urlparse, urljoin

import requests

try:
    from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageOps
    _PIL_AVAILABLE = True
except ImportError:
    _PIL_AVAILABLE = False

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

_log = logging.getLogger('artwork_sync')

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ARTWORK_FILES = ('folder.jpg', 'poster.jpg', 'fanart.jpg', 'background.jpg')
METADATA_FILE = 'artist-metadata.json'
LOG_FILE = 'artwork-sync.log'

# ---------------------------------------------------------------------------
# Artwork detection
# ---------------------------------------------------------------------------

def has_artwork(artist_path):
    """Check if an artist folder already has any artwork files."""
    if not os.path.isdir(artist_path):
        return False
    for fname in ARTWORK_FILES:
        if os.path.isfile(os.path.join(artist_path, fname)):
            return True
    return False


def has_metadata(artist_path):
    """Check if metadata file already exists."""
    return os.path.isfile(os.path.join(artist_path, METADATA_FILE))

# ---------------------------------------------------------------------------
# API Sources (tried in order)
# ---------------------------------------------------------------------------

def search_theaudiodb(artist_name):
    """Search TheAudioDB for artist artwork. Free, no API key needed for lookups."""
    url = f"https://www.theaudiodb.com/api/v1/json/2/search.php?s={quote(artist_name)}"
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        artists = data.get('artists', [])
        if not artists:
            return None
        artist = artists[0]
        result = {
            'source': 'theaudiodb',
            'source_url': url,
            'artist_name': artist.get('strArtist', artist_name),
        }
        # Artist artwork (square/cropped)
        for key, img_type in [('strArtistThumb', 'poster'), ('strArtistFanart', 'fanart'),
                               ('strArtistLogo', 'logo'), ('strArtistBanner', 'banner')]:
            val = artist.get(key)
            if val and val.strip():
                result[img_type] = val.strip()
        # Also check for "strArtistThumb" as folder image
        thumb = artist.get('strArtistThumb', '')
        if thumb and thumb.strip():
            result['folder'] = thumb.strip()
        # If no dedicated folder image, use poster as folder
        if 'folder' not in result and 'poster' in result:
            result['folder'] = result['poster']
        return result if ('folder' in result or 'poster' in result) else None
    except Exception as exc:
        _log.debug("TheAudioDB lookup failed for '%s': %s", artist_name, exc)
        return None


def search_musicbrainz(artist_name):
    """Search MusicBrainz for artist MBID, then try Cover Art Archive."""
    # Step 1: search for artist
    url = f"https://musicbrainz.org/ws/2/artist/?query={quote(artist_name)}&fmt=json&limit=1"
    try:
        resp = requests.get(url, timeout=15, headers={'User-Agent': 'vidshelf/1.0 (artwork-sync)'})
        resp.raise_for_status()
        data = resp.json()
        artists = data.get('artists', [])
        if not artists:
            return None
        mbid = artists[0].get('id')
        if not mbid:
            return None
        result = {
            'source': 'musicbrainz',
            'source_url': f"https://musicbrainz.org/artist/{mbid}",
            'artist_name': artists[0].get('name', artist_name),
        }
        # Step 2: try Cover Art Archive for artist images
        caa_url = f"https://coverartarchive.org/artist/{mbid}"
        try:
            caa_resp = requests.get(caa_url, timeout=15,
                                    headers={'User-Agent': 'vidshelf/1.0 (artwork-sync)'})
            if caa_resp.status_code == 200:
                caa_data = caa_resp.json()
                images = caa_data.get('images', [])
                for img in images:
                    img_url = img.get('image', '')
                    types = img.get('types', [])
                    if 'front' in types and img_url:
                        result['folder'] = img_url
                        if 'poster' not in result:
                            result['poster'] = img_url
                        break
        except Exception:
            pass
        # Step 3: try Wikimedia Commons via MusicBrainz
        wm_url = f"https://commons.wikimedia.org/w/api.php?action=query&list=search&srsearch={quote(artist_name + ' artist portrait')}&format=json&srlimit=1&srnamespace=6"
        try:
            wm_resp = requests.get(wm_url, timeout=15,
                                   headers={'User-Agent': 'vidshelf/1.0 (artwork-sync)'})
            if wm_resp.status_code == 200:
                wm_data = wm_resp.json()
                pages = wm_data.get('query', {}).get('search', [])
                if pages:
                    title = pages[0].get('title', '')
                    if title:
                        img_url = f"https://commons.wikimedia.org/wiki/Special:FilePath/{quote(title.replace('File:', ''))}"
                        if 'folder' not in result:
                            result['folder'] = img_url
                        if 'poster' not in result:
                            result['poster'] = img_url
        except Exception:
            pass
        return result if ('folder' in result or 'poster' in result) else None
    except Exception as exc:
        _log.debug("MusicBrainz lookup failed for '%s': %s", artist_name, exc)
        return None


def search_wikipedia(artist_name):
    """Search Wikipedia for an artist page and extract the infobox image."""
    url = f"https://en.wikipedia.org/w/api.php?action=query&prop=pageimages&titles={quote(artist_name)}&format=json&pithumbsize=500&redirects=1"
    try:
        resp = requests.get(url, timeout=15,
                            headers={'User-Agent': 'vidshelf/1.0 (artwork-sync)'})
        resp.raise_for_status()
        data = resp.json()
        pages = data.get('query', {}).get('pages', {})
        for page_id, page in pages.items():
            if page_id == '-1':
                continue
            thumbnail = page.get('thumbnail', {})
            source = thumbnail.get('source', '')
            if source:
                return {
                    'source': 'wikipedia',
                    'source_url': f"https://en.wikipedia.org/wiki/{quote(artist_name.replace(' ', '_'))}",
                    'artist_name': page.get('title', artist_name),
                    'folder': source,
                    'poster': source,
                }
        return None
    except Exception as exc:
        _log.debug("Wikipedia lookup failed for '%s': %s", artist_name, exc)
        return None


def search_fanarttv(artist_name, api_key):
    """Search Fanart.tv for artist artwork. Requires a free API key."""
    if not api_key:
        return None
    # First try MusicBrainz to get MBID
    url = f"https://musicbrainz.org/ws/2/artist/?query={quote(artist_name)}&fmt=json&limit=1"
    try:
        resp = requests.get(url, timeout=15, headers={'User-Agent': 'vidshelf/1.0 (artwork-sync)'})
        resp.raise_for_status()
        data = resp.json()
        artists = data.get('artists', [])
        if not artists:
            return None
        mbid = artists[0].get('id')
        if not mbid:
            return None
        # Now query Fanart.tv
        fa_url = f"https://webservice.fanart.tv/v3/music/{mbid}?api_key={api_key}"
        fa_resp = requests.get(fa_url, timeout=15)
        if fa_resp.status_code != 200:
            return None
        fa_data = fa_resp.json()
        result = {
            'source': 'fanarttv',
            'source_url': fa_url,
            'artist_name': artist_name,
        }
        # Artist thumb (best for folder/poster)
        artist_thumbs = fa_data.get('artistthumb', [])
        if artist_thumbs:
            best = artist_thumbs[0].get('url', '')
            if best:
                result['folder'] = best
                result['poster'] = best
        # Fanart / backgrounds
        artist_bg = fa_data.get('artistbackground', [])
        if artist_bg:
            best_bg = artist_bg[0].get('url', '')
            if best_bg:
                result['fanart'] = best_bg
                result['background'] = best_bg
        # Music logo
        music_logos = fa_data.get('musiclogo', [])
        if music_logos and 'folder' not in result:
            logo_url = music_logos[0].get('url', '')
            if logo_url:
                result['folder'] = logo_url
                result['poster'] = logo_url
        # Music banner as fallback
        if 'folder' not in result:
            banners = fa_data.get('musicbanner', [])
            if banners:
                banner_url = banners[0].get('url', '')
                if banner_url:
                    result['folder'] = banner_url
                    result['poster'] = banner_url
        return result if ('folder' in result or 'poster' in result) else None
    except Exception as exc:
        _log.debug("Fanart.tv lookup failed for '%s': %s", artist_name, exc)
        return None

def _lookup_mbid(artist_name):
    """Look up an artist's MusicBrainz ID. Separate from search_musicbrainz()
    so the manual multi-image search below can share one lookup across
    Fanart.tv and Cover Art Archive without touching the single-image
    automatic-sync path."""
    url = f"https://musicbrainz.org/ws/2/artist/?query={quote(artist_name)}&fmt=json&limit=1"
    try:
        resp = requests.get(url, timeout=15, headers={'User-Agent': 'vidshelf/1.0 (artwork-sync)'})
        resp.raise_for_status()
        artists = resp.json().get('artists', [])
        return artists[0].get('id') if artists else None
    except Exception as exc:
        _log.debug("MBID lookup failed for '%s': %s", artist_name, exc)
        return None


def _coverartarchive_images(mbid, limit=8):
    """Every image (not just the first 'front' one) Cover Art Archive has for an artist."""
    if not mbid:
        return []
    try:
        resp = requests.get(f"https://coverartarchive.org/artist/{mbid}", timeout=15,
                            headers={'User-Agent': 'vidshelf/1.0 (artwork-sync)'})
        if resp.status_code != 200:
            return []
        urls = []
        for img in resp.json().get('images', [])[:limit]:
            url = img.get('image')
            if url:
                urls.append(url)
        return urls
    except Exception as exc:
        _log.debug("Cover Art Archive multi-image lookup failed for mbid %s: %s", mbid, exc)
        return []


def _fanarttv_images(artist_name, api_key, mbid=None, limit_per_type=4):
    """Every image Fanart.tv has on file for an artist, not just index [0] of
    each type (which is all search_fanarttv() above returns, by design, for
    the single-best-image automatic sync path)."""
    if not api_key:
        return []
    mbid = mbid or _lookup_mbid(artist_name)
    if not mbid:
        return []
    try:
        resp = requests.get(f"https://webservice.fanart.tv/v3/music/{mbid}?api_key={api_key}", timeout=15)
        if resp.status_code != 200:
            return []
        data = resp.json()
        urls = []
        for field in ('artistthumb', 'artistbackground', 'hdmusiclogo', 'musiclogo', 'musicbanner'):
            for entry in data.get(field, [])[:limit_per_type]:
                url = entry.get('url')
                if url:
                    urls.append(url)
        return urls
    except Exception as exc:
        _log.debug("Fanart.tv multi-image lookup failed for '%s': %s", artist_name, exc)
        return []


_IMAGE_EXT_RE = re.compile(r'\.(jpe?g|png|gif|webp)$', re.IGNORECASE)


def _wikimedia_commons_images(artist_name, limit=6):
    """Broader Wikimedia Commons photo search (several results), vs. the
    single srlimit=1 lookup search_musicbrainz() uses as a last-resort fallback.

    Uses intitle:"<artist>" rather than a bare keyword search — a plain
    `srsearch=<artist> artist` full-text query matched the word "artist"
    anywhere in unrelated file descriptions (photos/scans of completely
    different people, even PDF/DjVu documents), not the actual artist. Also
    filters out non-image file types the raw search can still return.
    """
    srsearch = quote(f'intitle:"{artist_name}"')
    url = ("https://commons.wikimedia.org/w/api.php?action=query&list=search"
           f"&srsearch={srsearch}&format=json&srlimit={limit}&srnamespace=6")
    try:
        resp = requests.get(url, timeout=15, headers={'User-Agent': 'vidshelf/1.0 (artwork-sync)'})
        if resp.status_code != 200:
            return []
        urls = []
        for page in resp.json().get('query', {}).get('search', []):
            title = page.get('title', '')
            if title and _IMAGE_EXT_RE.search(title):
                urls.append(f"https://commons.wikimedia.org/wiki/Special:FilePath/{quote(title.replace('File:', ''))}")
        return urls
    except Exception as exc:
        _log.debug("Wikimedia Commons lookup failed for '%s': %s", artist_name, exc)
        return []


def search_artist_images(artist_name, api_key=''):
    """Collect candidate artwork image URLs for an artist from every real
    source used by sync_artist_artwork() (TheAudioDB, Fanart.tv, MusicBrainz/
    Cover Art Archive, Wikipedia/Wikimedia Commons), for use in the manual
    "Search Images" UI.

    Unlike sync_artist_artwork()'s single-best-image selection, this pulls
    *every* image each source has on file (all Fanart.tv array entries, all
    Cover Art Archive images, several Wikimedia Commons hits) so the UI has
    enough results to page through instead of the ~1-2 a single-best-per-source
    pick produces.

    Returns a list of unique image URLs, source-priority order preserved.
    """
    images = []
    seen = set()

    def _add(url):
        if url and url not in seen:
            seen.add(url)
            images.append(url)

    audiodb = search_theaudiodb(artist_name)
    if audiodb:
        for key in ('folder', 'poster', 'fanart', 'background', 'logo', 'banner'):
            _add(audiodb.get(key))

    mbid = _lookup_mbid(artist_name)

    for url in _fanarttv_images(artist_name, api_key, mbid=mbid):
        _add(url)

    for url in _coverartarchive_images(mbid):
        _add(url)

    wp = search_wikipedia(artist_name)
    if wp:
        _add(wp.get('folder'))

    for url in _wikimedia_commons_images(artist_name):
        _add(url)

    return images

# ---------------------------------------------------------------------------
# Image download
# ---------------------------------------------------------------------------

def _is_safe_download_url(url):
    """SSRF guard: only allow http(s) URLs whose host resolves to a public
    address. `/api/artwork/swap_noauth` lets an *unauthenticated* caller
    supply an arbitrary image URL that this module fetches server-side —
    without this check, that endpoint could be used to probe or reach
    internal-network/loopback/link-local services (e.g. the Plex server
    itself, cloud metadata endpoints) via the container.

    Known residual gap: this resolves the hostname once, up front: a
    malicious DNS server could still return a public IP for this check and a
    private one for the actual connection a moment later (DNS rebinding).
    Closing that fully would mean pinning the resolved IP for the actual
    request too, which requests doesn't support without a custom transport
    adapter. Not implemented here — this stops the overwhelming majority of
    real-world SSRF attempts (localhost, RFC1918 ranges, link-local/cloud
    metadata IPs) without that added complexity.
    """
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https'):
            return False
        host = parsed.hostname
        if not host:
            return False
        for _family, _type, _proto, _canon, sockaddr in socket.getaddrinfo(host, None):
            ip = ipaddress.ip_address(sockaddr[0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                return False
        return True
    except Exception:
        return False


def download_image(url, dest_path, timeout=30, max_redirects=5):
    """Download an image from url to dest_path. Returns True on success.

    Redirects are followed manually (not via requests' allow_redirects=True)
    so every hop — including ones a remote server redirects to after the
    initial URL passed validation — gets the same _is_safe_download_url()
    check before being fetched.
    """
    for _ in range(max_redirects + 1):
        if not _is_safe_download_url(url):
            _log.warning("Refusing to download image from disallowed URL: %s", url)
            return False
        try:
            resp = requests.get(url, timeout=timeout, stream=True,
                                headers={'User-Agent': 'vidshelf/1.0 (artwork-sync)'},
                                allow_redirects=False)
        except Exception as exc:
            _log.debug("Failed to download image from %s: %s", url, exc)
            return False

        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get('Location')
            if not location:
                return False
            url = urljoin(url, location)
            continue

        if resp.status_code != 200:
            _log.debug("Failed to download image from %s: HTTP %d", url, resp.status_code)
            return False

        try:
            with open(dest_path, 'wb') as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)
            if os.path.getsize(dest_path) == 0:
                os.remove(dest_path)
                return False
            return True
        except Exception as exc:
            _log.debug("Failed to save image from %s: %s", url, exc)
            return False

    _log.warning("Too many redirects downloading image from %s", url)
    return False

# ---------------------------------------------------------------------------
# Main sync logic
# ---------------------------------------------------------------------------

def sync_artist_artwork(artist_path, config, force=False):
    """Sync artwork for a single artist folder.
    
    Args:
        artist_path: Full path to the artist folder.
        config: Artwork sync configuration dict.
        force: If True, overwrite existing artwork.
    
    Returns:
        dict with status info.
    """
    folder_name = os.path.basename(artist_path)
    artist_name = folder_to_artist(folder_name)
    
    result = {
        'artist': artist_name,
        'folder': folder_name,
        'path': artist_path,
        'success': False,
        'source': None,
        'source_url': None,
        'images_downloaded': [],
        'errors': [],
    }
    
    # Check if artwork already exists
    if not force and has_artwork(artist_path):
        _log.info("Skipping '%s' — artwork already exists", artist_name)
        result['success'] = True
        result['skipped'] = True
        return result
    
    # Try sources in order
    api_key = config.get('fanarttv_api_key', '')
    
    sources = []
    # TheAudioDB first (free, reliable)
    audiodb_result = search_theaudiodb(artist_name)
    if audiodb_result:
        sources.append(audiodb_result)
    
    # Fanart.tv if API key configured
    if api_key:
        fanart_result = search_fanarttv(artist_name, api_key)
        if fanart_result:
            sources.append(fanart_result)
    
    # MusicBrainz / Cover Art Archive
    mb_result = search_musicbrainz(artist_name)
    if mb_result:
        sources.append(mb_result)
    
    # Wikipedia as final fallback
    wp_result = search_wikipedia(artist_name)
    if wp_result:
        sources.append(wp_result)
    
    if not sources:
        _log.warning("No artwork sources found for '%s'", artist_name)
        result['errors'].append("No artwork sources returned results")
        return result
    
    # Use the best source (first one that gives us usable images)
    best_source = sources[0]
    result['source'] = best_source['source']
    result['source_url'] = best_source.get('source_url', '')
    
    # Download images
    images_to_download = []
    
    # folder.jpg — use 'folder' key, fall back to 'poster'
    folder_url = best_source.get('folder') or best_source.get('poster')
    if folder_url:
        images_to_download.append(('folder.jpg', folder_url))
    
    # poster.jpg — use 'poster' key, fall back to 'folder'
    poster_url = best_source.get('poster') or best_source.get('folder')
    if poster_url and poster_url != folder_url:
        images_to_download.append(('poster.jpg', poster_url))
    elif poster_url and not folder_url:
        images_to_download.append(('poster.jpg', poster_url))
    
    # fanart.jpg
    fanart_url = best_source.get('fanart')
    if fanart_url:
        images_to_download.append(('fanart.jpg', fanart_url))
    
    # background.jpg
    bg_url = best_source.get('background')
    if bg_url and bg_url != fanart_url:
        images_to_download.append(('background.jpg', bg_url))
    elif bg_url and not fanart_url:
        images_to_download.append(('background.jpg', bg_url))
    
    for fname, url in images_to_download:
        dest = os.path.join(artist_path, fname)
        if not force and os.path.isfile(dest):
            _log.debug("Skipping existing %s in '%s'", fname, artist_name)
            result['images_downloaded'].append({'file': fname, 'status': 'skipped'})
            continue
        _log.info("Downloading %s for '%s' from %s", fname, artist_name, url)
        ok = download_image(url, dest)
        if ok:
            result['images_downloaded'].append({'file': fname, 'status': 'downloaded', 'url': url})
        else:
            result['images_downloaded'].append({'file': fname, 'status': 'failed', 'url': url})
            result['errors'].append(f"Failed to download {fname}")
    
    # Write metadata file
    metadata = {
        'artist_name': artist_name,
        'folder_name': folder_name,
        'source': best_source['source'],
        'source_url': best_source.get('source_url', ''),
        'date_downloaded': datetime.now(timezone.utc).isoformat(),
        'images': result['images_downloaded'],
        'success': len([i for i in result['images_downloaded'] if i['status'] == 'downloaded']) > 0,
        'errors': result['errors'] if result['errors'] else None,
    }
    try:
        meta_path = os.path.join(artist_path, METADATA_FILE)
        with open(meta_path, 'w') as f:
            json.dump(metadata, f, indent=2)
    except Exception as exc:
        _log.error("Failed to write metadata for '%s': %s", artist_name, exc)
        result['errors'].append(f"Metadata write failed: {exc}")
    
    result['success'] = metadata['success']
    return result


def sync_all_artists(root_path, config, force=False):
    """Scan all artist folders under root_path and sync artwork for any that need it.
    
    Returns list of result dicts.
    """
    results = []
    if not os.path.isdir(root_path):
        _log.warning("Root path does not exist: %s", root_path)
        return results
    
    for entry in sorted(os.listdir(root_path)):
        entry_path = os.path.join(root_path, entry)
        if not os.path.isdir(entry_path):
            continue
        # Skip hidden/system folders
        if entry.startswith('.'):
            continue
        # Skip if it's not an artist folder (no video files)
        # We check: if it has .mkv/.mp4 files OR has no artwork yet, process it
        has_video = any(f.endswith(('.mkv', '.mp4', '.webm')) for f in os.listdir(entry_path))
        if not has_video and has_artwork(entry_path):
            # Folder with artwork but no videos yet — still process in case it's new
            pass
        
        _log.info("Processing artist folder: %s", entry)
        try:
            res = sync_artist_artwork(entry_path, config, force=force)
            results.append(res)
        except Exception as exc:
            _log.error("Error processing '%s': %s", entry, exc)
            results.append({
                'artist': folder_to_artist(entry),
                'folder': entry,
                'path': entry_path,
                'success': False,
                'errors': [str(exc)],
            })
    
    return results

# ---------------------------------------------------------------------------
# Folder watcher (polling-based)
# ---------------------------------------------------------------------------

class ArtworkWatcher:
    """Background thread that polls the root folder for new artist directories
    and triggers artwork sync (and, if enabled, Plex smart-collection sync)."""

    def __init__(self, root_path, load_config, interval=120):
        self.root_path = root_path
        self._load_config = load_config  # callable returning the full app config, read fresh each poll
        self.interval = interval
        self._known_folders = set()
        self._stop_event = threading.Event()
        self._thread = None

    def start(self):
        self._known_folders = self._get_current_folders()
        self._thread = threading.Thread(target=self._run, daemon=True, name='artwork-watcher')
        self._thread.start()
        _log.info("ArtworkWatcher started on %s (interval=%ds, %d existing folders)",
                  self.root_path, self.interval, len(self._known_folders))

    def stop(self):
        self._stop_event.set()

    def _get_current_folders(self):
        if not os.path.isdir(self.root_path):
            return set()
        return {entry for entry in os.listdir(self.root_path)
                if os.path.isdir(os.path.join(self.root_path, entry))
                and not entry.startswith('.')}

    def _run(self):
        while not self._stop_event.is_set():
            try:
                config = self._load_config()
                artwork_cfg = config.get('artwork_sync', {})
                current = self._get_current_folders()
                new_folders = current - self._known_folders
                if new_folders:
                    _log.info("Detected %d new artist folder(s): %s", len(new_folders), new_folders)
                    for folder in sorted(new_folders):
                        folder_path = os.path.join(self.root_path, folder)
                        if os.path.isdir(folder_path):
                            _log.info("Syncing artwork for new folder: %s", folder)
                            try:
                                sync_artist_artwork(folder_path, artwork_cfg)
                                if artwork_cfg.get('plex_collection_sync_on_artwork', False):
                                    plex_sync_artist_collection(config, folder_to_artist(folder), folder_path)
                            except Exception as exc:
                                _log.error("Artwork sync failed for '%s': %s", folder, exc)

                # Run every poll cycle, not just when a brand-new artist folder
                # shows up — an existing artist getting a *new* video was
                # previously invisible to this loop (new_folders stays empty),
                # so its title never got cleaned and it never got a title card
                # until the next manual button click. Both calls are
                # idempotent (title cleanup skips locked titles; title-card
                # generation skips ratingKeys already in title-cards.json), so
                # running them unconditionally is cheap even on a poll with no
                # new folders.
                if artwork_cfg.get('plex_collection_sync_on_artwork', False):
                    try:
                        plex_clean_video_titles(config)
                    except Exception as exc:
                        _log.error("Plex title cleanup failed: %s", exc)
                    try:
                        plex_generate_title_cards_for_all(config, self.root_path)
                    except Exception as exc:
                        _log.error("Plex title-card generation failed: %s", exc)

                self._known_folders = current
            except Exception as exc:
                _log.error("ArtworkWatcher error: %s", exc)
            self._stop_event.wait(self.interval)


# ---------------------------------------------------------------------------
# Title card generation — replaces Plex's auto-extracted video-frame
# thumbnail with a designed poster (artist name + song title over the
# artist's own fanart/folder art), the same way a real movie poster looks
# instead of a random mid-video screenshot.
# ---------------------------------------------------------------------------

TITLE_CARD_SIZE = (1000, 1500)  # 2:3 aspect ratio — matches Plex's poster grid
_TITLE_CARD_STATE_FILE = 'title-cards.json'

# Debian/Ubuntu's fonts-dejavu-core package (installed in the Dockerfile)
# provides these paths. Falls back to PIL's bundled bitmap font if the
# package isn't present (e.g. running outside the container), so this never
# hard-fails — it just looks worse until the image is rebuilt.
_BOLD_FONT_CANDIDATES = (
    '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
    '/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf',
)
_REGULAR_FONT_CANDIDATES = (
    '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
    '/usr/share/fonts/dejavu/DejaVuSans.ttf',
)


def check_title_card_dependencies():
    """Report on what title-card generation needs. Used by the Settings
    page's System Health panel — Pillow missing means no title cards at
    all (generate_title_card() just returns False); fonts missing means
    they'll render with Pillow's tiny bitmap default instead of DejaVu."""
    font_found = any(os.path.isfile(p) for p in _BOLD_FONT_CANDIDATES + _REGULAR_FONT_CANDIDATES)
    return {
        'pillow': {'found': _PIL_AVAILABLE},
        'fonts': {'found': font_found},
    }


def _load_font(size, bold=False):
    candidates = _BOLD_FONT_CANDIDATES if bold else _REGULAR_FONT_CANDIDATES
    for path in candidates:
        if os.path.isfile(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


def _text_width(draw, text, font):
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0]


def _line_height(draw, text, font):
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[3] - bbox[1]


def _wrap_text(draw, text, font, max_width, max_lines=3):
    """Greedy word-wrap to at most max_lines, ellipsizing the last line if
    the text still doesn't fit."""
    words = text.split()
    lines = []
    current = ''
    for word in words:
        trial = f'{current} {word}'.strip()
        if not current or _text_width(draw, trial, font) <= max_width:
            current = trial
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)

    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        while last and _text_width(draw, last + '…', font) > max_width:
            last = last[:-1].rstrip()
        lines[-1] = last + '…'
    return lines


def _build_card_background(artist_path, size):
    """Use the artist's own fanart/folder art as a blurred, darkened
    backdrop so the card feels tied to the artist instead of being a
    generic template. Falls back to a plain dark gradient if no artwork
    exists yet for this artist."""
    for name in ('fanart.jpg', 'background.jpg', 'folder.jpg', 'poster.jpg'):
        path = os.path.join(artist_path, name)
        if not os.path.isfile(path):
            continue
        try:
            img = Image.open(path).convert('RGB')
            img = ImageOps.fit(img, size, Image.LANCZOS)
            img = img.filter(ImageFilter.GaussianBlur(8))
            overlay = Image.new('RGB', size, (10, 10, 16))
            return Image.blend(img, overlay, 0.6)
        except Exception as exc:
            _log.debug("Could not use %s as title-card background: %s", path, exc)
            continue
    return Image.new('RGB', size, (24, 24, 32))


def generate_title_card(artist_name, song_title, artist_path, output_path, size=TITLE_CARD_SIZE):
    """Render a poster-style JPEG (song title + artist name over the
    artist's art) to output_path. Returns True on success."""
    if not _PIL_AVAILABLE:
        return False
    try:
        img = _build_card_background(artist_path, size)
        draw = ImageDraw.Draw(img, 'RGBA')
        w, h = size

        # Dark gradient scrim over the bottom third so white text stays
        # legible regardless of what's in the background image there.
        scrim_height = int(h * 0.42)
        scrim = Image.new('RGBA', (w, scrim_height), (0, 0, 0, 0))
        scrim_draw = ImageDraw.Draw(scrim)
        for i in range(scrim_height):
            alpha = int(210 * (i / scrim_height))
            scrim_draw.line([(0, i), (w, i)], fill=(0, 0, 0, alpha))
        img.paste(scrim, (0, h - scrim_height), scrim)

        margin = int(w * 0.08)
        text_width = w - 2 * margin
        title_font = _load_font(int(w * 0.08), bold=True)
        artist_font = _load_font(int(w * 0.045), bold=False)
        line_gap = int(w * 0.02)

        title_lines = _wrap_text(draw, song_title, title_font, text_width, max_lines=3)
        artist_lines = _wrap_text(draw, artist_name.upper(), artist_font, text_width, max_lines=1)

        block_height = sum(_line_height(draw, l, title_font) + line_gap for l in title_lines)
        block_height += int(w * 0.03)
        block_height += sum(_line_height(draw, l, artist_font) + line_gap for l in artist_lines)

        y = h - margin - block_height
        for line in title_lines:
            draw.text((margin, y), line, font=title_font, fill=(255, 255, 255, 255))
            y += _line_height(draw, line, title_font) + line_gap
        y += int(w * 0.03)
        for line in artist_lines:
            draw.text((margin, y), line, font=artist_font, fill=(215, 215, 225, 255))
            y += _line_height(draw, line, artist_font) + line_gap

        img.convert('RGB').save(output_path, 'JPEG', quality=90)
        return True
    except Exception as exc:
        _log.warning("Failed to generate title card for '%s - %s': %s", artist_name, song_title, exc)
        return False


def _load_title_card_state(artist_path):
    path = os.path.join(artist_path, _TITLE_CARD_STATE_FILE)
    try:
        with open(path, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_title_card_state(artist_path, state):
    path = os.path.join(artist_path, _TITLE_CARD_STATE_FILE)
    try:
        with open(path, 'w') as f:
            json.dump(state, f, indent=2)
    except Exception as exc:
        _log.warning("Failed to save title-card state for '%s': %s", artist_path, exc)


def _generate_title_cards_for_videos(config, artist_name, artist_path, videos, force=False):
    """Shared core: given a list of {'rating_key', 'title'} dicts already
    known to belong to this artist, generate+upload a title card for any not
    already recorded in this artist's title-cards.json (or all of them, if
    force=True).

    Idempotent via that local marker file — Plex's API doesn't expose a
    poster-lock flag we can query the way plex_clean_video_titles() checks
    title.locked, so "already done" is tracked ourselves instead of
    re-generating and re-uploading unchanged posters on every poll.
    """
    result = {'artist': artist_name, 'processed': 0, 'generated': 0, 'errors': []}
    if not _PIL_AVAILABLE:
        result['errors'].append('Pillow not installed — cannot generate title cards')
        return result

    state = _load_title_card_state(artist_path)
    prefix = f"{artist_name} -"

    for video in videos:
        rating_key = video.get('rating_key')
        if not rating_key:
            continue
        if not force and rating_key in state:
            continue
        result['processed'] += 1

        song_title = video.get('title', '')
        if song_title.lower().startswith(prefix.lower()):
            song_title = song_title[len(prefix):].strip()

        tmp_path = os.path.join(artist_path, f'.title_card_{rating_key}.jpg')
        try:
            if not generate_title_card(artist_name, song_title, artist_path, tmp_path):
                result['errors'].append(f"Generation failed for '{song_title}'")
                continue
            if _plex_upload_poster(config, rating_key, tmp_path):
                state[rating_key] = {
                    'title': song_title,
                    'generated_at': datetime.now(timezone.utc).isoformat(),
                }
                result['generated'] += 1
            else:
                result['errors'].append(f"Upload failed for '{song_title}'")
        finally:
            if os.path.isfile(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass

    _save_title_card_state(artist_path, state)
    return result


def plex_generate_title_cards(config, artist_name, artist_path, library_key=None, force=False):
    """Generate + upload title cards for one artist (used by the manual
    per-artist UI action). For the periodic all-artists sweep, see
    plex_generate_title_cards_for_all(), which fetches the library once
    instead of once per artist."""
    if library_key is None:
        library_key = config.get('plex', {}).get('music_video_library_key', '')
    if not library_key:
        library_key = plex_find_library_key(config)
    if not library_key:
        return {'artist': artist_name, 'processed': 0, 'generated': 0,
                'errors': ['Could not determine Plex library key']}

    videos = plex_find_videos_by_artist(config, artist_name, library_key)
    return _generate_title_cards_for_videos(config, artist_name, artist_path, videos, force=force)


def plex_generate_title_cards_for_all(config, root_path, force=False):
    """Generate + upload title cards for every artist folder under
    root_path, fetching the whole library exactly once and filtering
    per-artist in Python — avoids one full-library GET per artist that
    plex_generate_title_cards() would otherwise repeat on every poll."""
    results = []
    if not os.path.isdir(root_path):
        return results

    library_key = config.get('plex', {}).get('music_video_library_key', '')
    if not library_key:
        library_key = plex_find_library_key(config)
    if not library_key:
        return results

    base_url = _plex_url(config)
    if not base_url:
        return results
    headers = _plex_headers(config)
    try:
        resp = requests.get(f"{base_url}/library/sections/{library_key}/all",
                            headers=headers, timeout=30)
        resp.raise_for_status()
        items = resp.json().get('MediaContainer', {}).get('Metadata', [])
    except Exception as exc:
        _log.warning("Failed to list library items for title-card generation: %s", exc)
        return results

    for entry in sorted(os.listdir(root_path)):
        entry_path = os.path.join(root_path, entry)
        if not os.path.isdir(entry_path) or entry.startswith('.'):
            continue
        artist_name = folder_to_artist(entry)
        prefix_lower = f"{artist_name} -".lower()
        videos = [
            {'rating_key': str(item.get('ratingKey', '')), 'title': item.get('title', '')}
            for item in items
            if item.get('title', '').lower().startswith(prefix_lower)
        ]
        try:
            results.append(_generate_title_cards_for_videos(config, artist_name, entry_path, videos, force=force))
        except Exception as exc:
            _log.error("Title card generation failed for '%s': %s", artist_name, exc)
            results.append({'artist': artist_name, 'processed': 0, 'generated': 0, 'errors': [str(exc)]})

    return results


# ---------------------------------------------------------------------------
# Plex Authentication (OAuth)
# ---------------------------------------------------------------------------

def _get_or_create_plex_client_id():
    """Each deployment should identify itself to Plex with its own OAuth
    client ID rather than every clone of this repo sharing one hardcoded
    value baked into source. Prefers a PLEX_CLIENT_ID env var; otherwise
    persists a freshly generated UUID into config.json — same pattern as
    app.py's _get_or_create_secret_key(), so re-running this always finds
    the same ID again instead of generating a new one (and re-registering
    as a "new" app with Plex) on every restart."""
    env_value = os.environ.get('PLEX_CLIENT_ID')
    if env_value:
        return env_value
    # Must go through state.CONFIG_FILE, not a bare 'config.json': since
    # v1.1.0 config lives in the mounted data directory, and a relative path
    # here would quietly create a *second* config file in the working
    # directory — so the client ID persisted for Plex OAuth would never be the
    # one app.py reads back, and every restart would look like a new device to
    # Plex.
    existing = state.read_json(state.CONFIG_FILE).get('_plex_client_id')
    if existing:
        return existing
    new_id = str(uuid.uuid4())

    def _set(config):
        # Re-check inside the lock: this runs at import, but a concurrent
        # writer that already generated an ID should win over ours rather than
        # get overwritten.
        return config if config.get('_plex_client_id') else {**config, '_plex_client_id': new_id}

    try:
        return state.update_json(state.CONFIG_FILE, _set, indent=4)['_plex_client_id']
    except OSError:
        _log.warning("Could not persist generated Plex client ID to config.json")
        return new_id


PLEX_CLIENT_ID = _get_or_create_plex_client_id()
PLEX_PRODUCT = os.environ.get('PLEX_PRODUCT') or 'Vidshelf'

def _plex_oauth_headers():
    return {
        'Accept': 'application/json',
        'X-Plex-Product': PLEX_PRODUCT,
        'X-Plex-Client-Identifier': PLEX_CLIENT_ID,
        'X-Plex-Device-Name': 'Chrome',
        'X-Plex-Platform': 'Chrome',
        'X-Plex-Platform-Version': '1.0',
        'X-Plex-Device': 'Chrome',
        'X-Plex-Version': '1.0',
    }

def plex_oauth_start(config):
    """Initiate Plex OAuth flow: get a PIN and return the auth URL.

    If a valid Plex client identifier is not configured (placeholders are still present),
    the function will skip the OAuth request and return None values. This prevents
    the 403/1068 error when the client is not registered with Plex.
    """
    # Detect placeholder values – if they haven't been replaced, abort the OAuth flow.
    _log.debug(f"DEBUG: PLEX_CLIENT_ID: {PLEX_CLIENT_ID}, PLEX_PRODUCT: {PLEX_PRODUCT}")
    if PLEX_CLIENT_ID.startswith("YOUR_") or PLEX_PRODUCT.startswith("YOUR_"):
        _log.warning("Plex OAuth is disabled because PLEX_CLIENT_ID or PLEX_PRODUCT is not set.")
        return None, None, None

    try:
        headers = _plex_oauth_headers()
        _log.debug("Requesting Plex PIN from https://plex.tv/api/v2/pins with headers: %s", headers)
        # strong=true is required for the clientID+code URL flow below — without it
        # Plex issues a PIN meant for the plex.tv/link manual-entry flow instead, and
        # the auth page fails with "We were unable to complete this request."
        resp = requests.post("https://plex.tv/api/v2/pins", headers=headers,
                              params={'strong': 'true'}, timeout=10)
        resp.raise_for_status()  # Raise HTTPError for bad responses (4xx or 5xx)
        data = resp.json()

        pin_id = data.get('id')
        code = data.get('code')

        if not pin_id or not code:
            _log.error("Plex PIN request failed: Missing 'id' or 'code' in response. Response: %s", data)
            return None, None, None

        # Matches python-plexapi's MyPlexPinLogin.oauthUrl(): "auth/#!?", not the
        # bare "auth#?" — app.plex.tv's router only picks up the params from this
        # exact hash-bang form.
        url_params = {
            'clientID': PLEX_CLIENT_ID,
            'code': code,
            'context[device][product]': PLEX_PRODUCT,
            'context[device][version]': headers['X-Plex-Version'],
            'context[device][platform]': headers['X-Plex-Platform'],
            'context[device][platformVersion]': headers['X-Plex-Platform-Version'],
            'context[device][device]': headers['X-Plex-Device'],
            'context[device][deviceName]': headers['X-Plex-Device-Name'],
        }
        full_auth_url = f"https://app.plex.tv/auth/#!?{urlencode(url_params)}"

        _log.info("Plex OAuth PIN requested. PIN: %s, Auth URL: %s", code, full_auth_url)
        _log.debug(f"DEBUG: plex_oauth_start returning: ({pin_id}, {code}, {full_auth_url})")
        return pin_id, code, full_auth_url
    except requests.exceptions.RequestException as req_exc:
        _log.error("Network or HTTP error during Plex OAuth initiation: %s. Response status: %s, Response text: %s",
                   req_exc, getattr(req_exc.response, 'status_code', 'N/A'), getattr(req_exc.response, 'text', 'N/A'))
        _log.debug("plex_oauth_start returning (None, None, None) after a RequestException")
        return None, None, None
    except Exception as exc:
        _log.error("An unexpected error occurred during Plex OAuth initiation: %s", exc)
        _log.debug("plex_oauth_start returning (None, None, None) after an unexpected exception")
        return None, None, None

def plex_oauth_check_pin(config, pin_id):
    """Check if the Plex PIN has been authenticated and retrieve the token."""
    try:
        headers = _plex_oauth_headers()
        resp = requests.get(f"https://plex.tv/api/v2/pins/{pin_id}", headers=headers, timeout=10)
        if resp.status_code == 404:
            _log.warning("Plex OAuth PIN %s not found or expired", pin_id)
            return False
        resp.raise_for_status()
        data = resp.json()

        auth_token = data.get('authToken')
        if auth_token:
            _log.info("Plex OAuth PIN %s authenticated. Token retrieved.", pin_id)
            return auth_token
        return None  # PIN still pending
    except Exception as exc:
        _log.error("Failed to check Plex PIN %s: %s", pin_id, exc)
        return False

def plex_get_account_info(config, token):
    """Get basic Plex account info using the auth token."""
    try:
        headers = {
            'X-Plex-Token': token,
            'Accept': 'application/json',
        }
        resp = requests.get("https://plex.tv/api/v2/user", headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return {
            'username': data.get('username'),
            'email': data.get('email'),
            'thumb': data.get('thumb'),
        }
    except Exception as exc:
        _log.warning("Failed to get Plex account info: %s", exc)
        return None

def plex_get_servers(config, token):
    """Get a list of Plex servers associated with the account."""
    try:
        headers = {
            'X-Plex-Token': token,
            'Accept': 'application/json',
            'X-Plex-Client-Identifier': PLEX_CLIENT_ID,
        }
        resp = requests.get("https://plex.tv/api/v2/resources", headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        
        servers = []
        for resource in data:
            if resource.get('provides', '') == 'server':
                connections = resource.get('connections', [])
                local_ip = None
                for conn in connections:
                    if conn.get('local', False):
                        local_ip = conn.get('uri')
                        break
                
                servers.append({
                    'name': resource.get('name'),
                    'clientIdentifier': resource.get('clientIdentifier'),
                    'uri': local_ip or (connections[0].get('uri') if connections else None),
                    'lastSeenAt': resource.get('lastSeenAt'),
                })
        return servers
    except Exception as exc:
        _log.warning("Failed to get Plex servers: %s", exc)
        return []

# ---------------------------------------------------------------------------
# Plex library refresh
# ---------------------------------------------------------------------------

def _plex_headers(config):
    """Build common Plex API headers."""
    plex_config = config.get('plex', {})
    token = plex_config.get('token', '')
    headers = {
        'X-Plex-Token': token,
        'Accept': 'application/json',
        'X-Plex-Product': PLEX_PRODUCT,
        'X-Plex-Client-Identifier': PLEX_CLIENT_ID,
        'X-Plex-Device-Name': 'Docker',
        'X-Plex-Platform': 'Docker',
        'X-Plex-Platform-Version': '1.0',
        'X-Plex-Device': 'Docker',
        'X-Plex-Provides': 'controller',
        'X-Plex-Version': '1.0',
    }
    return headers


def _plex_url(config, path=''):
    """Build a full Plex API URL."""
    plex_config = config.get('plex', {})
    server_url = plex_config.get('server_url', '').rstrip('/')
    if not server_url:
        return None
    return f"{server_url}{path}"


def plex_find_library_key(config):
    """Auto-discover the 'Other Videos' library key from Plex.
    
    Returns the library section key (string) or None if not found.
    """
    base_url = _plex_url(config)
    if not base_url:
        return None
    headers = _plex_headers(config)
    plex_config = config.get('plex', {})
    token = plex_config.get('token', '')
    
    try:
        # Attempt with headers first
        resp = requests.get(f"{base_url}/library/sections", headers=headers, timeout=10)
        if resp.status_code == 401:
            # Retry with token as query parameter
            resp = requests.get(f"{base_url}/library/sections?X-Plex-Token={token}", timeout=10)
        resp.raise_for_status()
        data = resp.json()
        directories = data.get('MediaContainer', {}).get('Directory', [])
        
        # Plex "Other Videos" libraries report as type='movie' in the API.
        # First, try to find a library whose title contains "music video" (case-insensitive).
        for d in directories:
            title = d.get('title', '')
            _type = d.get('type', '')
            key = str(d.get('key', ''))
            if key and 'music video' in title.lower():
                _log.info("Found music video library by title: '%s' (key=%s, type=%s)", title, key, _type)
                return key
        
        # Fallback: return the first library with type='movie' or type='video'
        for d in directories:
            _type = d.get('type', '')
            key = str(d.get('key', ''))
            if key and _type in ('movie', 'video'):
                _log.info("Found library: '%s' (key=%s, type=%s)", d.get('title', ''), key, _type)
                return key
        
        # Last resort: return the first library of any type
        for d in directories:
            key = str(d.get('key', ''))
            if key:
                _log.info("Fallback to first available library: '%s' (key=%s)", d.get('title', ''), key)
                return key
        
        _log.warning("No libraries found on Plex server")
        return None
    except Exception as exc:
        _log.warning("Failed to discover Plex library key: %s", exc)
        return None


def plex_list_libraries(config):
    """List every library on the connected Plex server, flagging which one
    auto-discovery (plex_find_library_key()) would pick.

    Auto-discovery guesses from the library *title* containing "music
    video" - a real account hit this exact failure mode with a library
    titled "Muisc Videos" (transposed typo), where discovery silently fell
    back to an unrelated movies library with no way to know it had guessed
    wrong short of manually cross-checking (see the Plex OAuth/collections
    bug stack in REFERENCE.md). Returning the full list lets the UI show a
    confirm/pick step instead of trusting the guess blindly.

    Returns a list of {'key', 'title', 'type', 'is_auto_discovered'} dicts,
    or [] on any failure.
    """
    base_url = _plex_url(config)
    if not base_url:
        return []
    headers = _plex_headers(config)
    plex_config = config.get('plex', {})
    token = plex_config.get('token', '')

    try:
        resp = requests.get(f"{base_url}/library/sections", headers=headers, timeout=10)
        if resp.status_code == 401:
            resp = requests.get(f"{base_url}/library/sections?X-Plex-Token={token}", timeout=10)
        resp.raise_for_status()
        directories = resp.json().get('MediaContainer', {}).get('Directory', [])
    except Exception as exc:
        _log.warning("Failed to list Plex libraries: %s", exc)
        return []

    auto_key = plex_find_library_key(config)
    libraries = []
    for d in directories:
        key = str(d.get('key', ''))
        if not key:
            continue
        libraries.append({
            'key': key,
            'title': d.get('title', ''),
            'type': d.get('type', ''),
            'is_auto_discovered': key == auto_key,
        })
    return libraries


def plex_set_item_title(config, library_key, rating_key, new_title):
    """Edit a library item's title and lock the field so future library
    rescans don't revert it. Returns True on success."""
    base_url = _plex_url(config)
    if not base_url:
        return False
    headers = _plex_headers(config)
    try:
        resp = requests.put(
            f"{base_url}/library/sections/{library_key}/all",
            headers=headers,
            params={
                'id': rating_key,
                'type': '1',
                'title.value': new_title,
                'title.locked': '1',
            },
            timeout=10
        )
        if resp.status_code in (200, 201):
            return True
        _log.warning("Failed to set title for item %s, status=%d: %s",
                     rating_key, resp.status_code, resp.text[:300])
        return False
    except Exception as exc:
        _log.warning("Failed to set title for item %s: %s", rating_key, exc)
        return False


def _canonical_artist_names(config):
    """Artist names in their folder-derived (canonical) capitalization.

    YouTube's own title casing is inconsistent per-video for the same artist
    (e.g. "Death Cab For Cutie" vs "Death Cab for Cutie" depending on which
    upload seeded it), so the "ArtistName -" prefix Plex displays can vary
    from one video to the next even though they're the same artist. Sorted
    longest-first so a longer artist name is matched before any shorter
    accidental substring overlap with another artist's name.
    """
    artwork_cfg = config.get('artwork_sync', {})
    root_path = artwork_cfg.get('root_path', '/app/music_videos_final')
    names = []
    if os.path.isdir(root_path):
        for entry in sorted(os.listdir(root_path)):
            if os.path.isdir(os.path.join(root_path, entry)) and not entry.startswith('.'):
                names.append(folder_to_artist(entry))
    names.sort(key=len, reverse=True)
    return names


def plex_clean_video_titles(config, library_key=None):
    """Scan the music-video library and clean up any item's title that isn't
    already locked (so re-running this is a no-op for anything already
    cleaned, and it never clobbers a title someone customized manually).

    Returns {'scanned': N, 'cleaned': N, 'examples': [{'before': ..., 'after': ...}, ...]}.
    """
    result = {'scanned': 0, 'cleaned': 0, 'examples': [], 'errors': []}
    canonical_names = _canonical_artist_names(config)

    if library_key is None:
        library_key = config.get('plex', {}).get('music_video_library_key', '')
    if not library_key:
        library_key = plex_find_library_key(config)
    if not library_key:
        result['errors'].append("Could not determine Plex library key")
        return result

    base_url = _plex_url(config)
    if not base_url:
        result['errors'].append("Plex not configured")
        return result
    headers = _plex_headers(config)

    try:
        resp = requests.get(f"{base_url}/library/sections/{library_key}/all",
                            headers=headers, timeout=30)
        resp.raise_for_status()
        items = resp.json().get('MediaContainer', {}).get('Metadata', [])
    except Exception as exc:
        _log.warning("Failed to list library items for title cleanup: %s", exc)
        result['errors'].append(str(exc))
        return result

    result['scanned'] = len(items)
    for item in items:
        raw_title = item.get('title', '')
        rating_key = str(item.get('ratingKey', ''))
        if not raw_title or not rating_key:
            continue

        title_locked = any(f.get('name') == 'title' and f.get('locked')
                           for f in item.get('Field', []))
        if title_locked:
            continue

        cleaned = _clean_video_title(raw_title)
        cleaned = _normalize_artist_prefix(cleaned, canonical_names)
        if cleaned == raw_title:
            continue

        if plex_set_item_title(config, library_key, rating_key, cleaned):
            result['cleaned'] += 1
            if len(result['examples']) < 10:
                result['examples'].append({'before': raw_title, 'after': cleaned})
        else:
            result['errors'].append(f"Failed to update title for '{raw_title}'")

    _log.info("Title cleanup: scanned %d, cleaned %d", result['scanned'], result['cleaned'])
    return result


def plex_find_videos_by_artist(config, artist_name, library_key=None):
    """Find all videos in the Plex library whose title starts with 'ArtistName -'.
    
    Returns list of dicts with 'rating_key' and 'title'.
    """
    if library_key is None:
        library_key = config.get('plex', {}).get('music_video_library_key', '')
    if not library_key:
        library_key = plex_find_library_key(config)
    if not library_key:
        _log.warning("Cannot find videos — no library key available")
        return []
    
    base_url = _plex_url(config)
    if not base_url:
        return []
    headers = _plex_headers(config)
    
    # Build the prefix to match: "ArtistName -"
    prefix = f"{artist_name} -"
    prefix_lower = prefix.lower()
    
    try:
        # Get all items from the library
        resp = requests.get(
            f"{base_url}/library/sections/{library_key}/all",
            headers=headers, timeout=30
        )
        resp.raise_for_status()
        data = resp.json()
        items = data.get('MediaContainer', {}).get('Metadata', [])
        
        matches = []
        for item in items:
            title = item.get('title', '')
            if title.lower().startswith(prefix_lower):
                matches.append({
                    'rating_key': str(item.get('ratingKey', '')),
                    'title': title,
                    'year': item.get('year'),
                })
        
        _log.info("Found %d videos matching '%s' in Plex library", len(matches), prefix)
        return matches
    except Exception as exc:
        _log.warning("Failed to search Plex library for '%s': %s", artist_name, exc)
        return []


def plex_get_machine_identifier(config):
    """Get the Plex server's machine identifier (needed to build smart-collection filter URIs)."""
    base_url = _plex_url(config)
    if not base_url:
        return None
    headers = _plex_headers(config)
    try:
        resp = requests.get(f"{base_url}/", headers=headers, timeout=10)
        resp.raise_for_status()
        return resp.json().get('MediaContainer', {}).get('machineIdentifier')
    except Exception as exc:
        _log.warning("Failed to get Plex machine identifier: %s", exc)
        return None


# Serializes the entire "does a collection for this artist already exist? if
# not, create one" sequence below. Without this, two near-simultaneous
# callers (e.g. two videos for the same new artist downloading at once —
# each spawns its own thread that calls this via plex_sync_artist_collection
# — or a download's immediate sync racing the ArtworkWatcher poll thread
# noticing the same brand-new folder) can both see "no collection yet" in
# Step 1 before either has finished Step 3, and both create one. This is
# exactly what produced multiple "Nine Inch Nails"/"Soundgarden" collections
# with different (stale) childCounts in practice — see the SECURITY/BUGFIX
# section in REFERENCE.md. A single global lock (not per-artist) is
# sufficient: collection creation is infrequent and fast, so briefly
# serializing across different artists too costs nothing measurable.
_collection_creation_lock = threading.Lock()


def plex_ensure_smart_collection(config, artist_name):
    """Create a Plex *smart* collection for the artist if one doesn't already exist.

    Smart collections are backed by a saved library filter (title contains
    "ArtistName -") rather than a fixed list of items, so Plex automatically
    adds any future matching videos — no manual "update collection" step needed.

    Returns the collection rating key (string) or None on failure.
    """
    base_url = _plex_url(config)
    if not base_url:
        return None
    headers = _plex_headers(config)
    library_key = config.get('plex', {}).get('music_video_library_key', '')
    if not library_key:
        library_key = plex_find_library_key(config)
    if not library_key:
        return None

    collection_title = artist_name

    with _collection_creation_lock:
        # Step 1: Check if a collection with this title already exists
        try:
            resp = requests.get(
                f"{base_url}/library/sections/{library_key}/collections",
                headers=headers, timeout=10
            )
            resp.raise_for_status()
            data = resp.json()
            collections = data.get('MediaContainer', {}).get('Metadata', [])
            for col in collections:
                if col.get('title', '').lower() == collection_title.lower():
                    _log.debug("Smart collection '%s' already exists (key=%s)",
                               collection_title, col.get('ratingKey', ''))
                    return str(col.get('ratingKey', ''))
        except Exception as exc:
            _log.debug("Failed to list collections: %s", exc)

        # Step 2: Build the smart-filter search URI: title contains "ArtistName -"
        machine_id = plex_get_machine_identifier(config)
        if not machine_id:
            return None
        filter_title = f"{artist_name} -"
        search_uri = (
            f"server://{machine_id}/com.plexapp.plugins.library"
            f"/library/sections/{library_key}/all?type=1&title={quote(filter_title)}"
        )

        # Step 3: Create the smart collection
        _log.info("Creating smart collection '%s' (filter: title contains '%s')",
                   collection_title, filter_title)
        try:
            resp = requests.post(
                f"{base_url}/library/collections",
                headers=headers,
                params={
                    'uri': search_uri,
                    'type': '1',
                    'title': collection_title,
                    'smart': '1',
                    'sectionId': library_key,
                },
                timeout=10
            )
            if resp.status_code in (200, 201):
                try:
                    data = resp.json()
                    collections = data.get('MediaContainer', {}).get('Metadata', [])
                    if collections:
                        new_key = str(collections[0].get('ratingKey', ''))
                        _log.info("Smart collection '%s' created (key=%s)", collection_title, new_key)
                        return new_key
                except Exception:
                    pass
                _log.info("Smart collection '%s' created successfully", collection_title)
                return collection_title  # Return name as fallback
            else:
                _log.warning("Failed to create smart collection, status=%d: %s",
                             resp.status_code, resp.text[:300])
                return None
        except Exception as exc:
            _log.warning("Failed to create smart collection '%s': %s", collection_title, exc)
            return None


def plex_find_duplicate_collections(config, library_key=None):
    """Group this library's collections by lowercased title and return only
    the groups with more than one entry — the shape the race condition
    plex_ensure_smart_collection() used to have (fixed above, via
    _collection_creation_lock) leaves behind: several collections with the
    same title, the same smart filter, but different (stale) childCounts.

    Returns {title_lower: [{'rating_key', 'title', 'child_count', 'smart'}, ...]}.
    """
    if library_key is None:
        library_key = config.get('plex', {}).get('music_video_library_key', '')
    if not library_key:
        library_key = plex_find_library_key(config)
    if not library_key:
        return {}

    base_url = _plex_url(config)
    if not base_url:
        return {}
    headers = _plex_headers(config)
    try:
        resp = requests.get(f"{base_url}/library/sections/{library_key}/collections",
                            headers=headers, timeout=10)
        resp.raise_for_status()
        collections = resp.json().get('MediaContainer', {}).get('Metadata', [])
    except Exception as exc:
        _log.warning("Failed to list collections for duplicate check: %s", exc)
        return {}

    groups = {}
    for col in collections:
        title = col.get('title', '')
        groups.setdefault(title.lower(), []).append({
            'rating_key': str(col.get('ratingKey', '')),
            'title': title,
            'child_count': col.get('childCount', 0),
            'smart': col.get('smart', 0),
        })
    return {title: entries for title, entries in groups.items() if len(entries) > 1}


def plex_delete_collection(config, rating_key):
    """Delete a collection by ratingKey. Returns True on success."""
    base_url = _plex_url(config)
    if not base_url:
        return False
    headers = _plex_headers(config)
    try:
        resp = requests.delete(f"{base_url}/library/collections/{rating_key}",
                               headers=headers, timeout=10)
        return resp.status_code in (200, 204)
    except Exception as exc:
        _log.warning("Failed to delete collection %s: %s", rating_key, exc)
        return False


def plex_dedupe_collections(config, library_key=None):
    """Clean up duplicate same-title collections left behind by the
    check-then-create race plex_ensure_smart_collection() used to have.

    All duplicates share the identical smart filter (same artist, same
    "title contains" search), so — unlike a real merge — there's no item
    list to reconcile: keeping any one survivor and deleting the rest is
    safe, since the survivor's smart filter re-matches every video that
    belongs there regardless of which duplicate happened to survive. Keeps
    the entry with the highest childCount (most likely to already reflect a
    recent recount) and, if the plex_collection_sync_on_artwork flag is on,
    re-uploads that artist's poster/art afterward so the survivor doesn't
    end up with a blank thumb if the one kept was never the one that got
    the artwork upload.

    Returns {'groups_found': N, 'deleted': [...], 'errors': [...]}.
    """
    result = {'groups_found': 0, 'deleted': [], 'kept': [], 'errors': []}
    duplicate_groups = plex_find_duplicate_collections(config, library_key=library_key)
    result['groups_found'] = len(duplicate_groups)

    for title_lower, entries in duplicate_groups.items():
        entries_sorted = sorted(entries, key=lambda e: e['child_count'], reverse=True)
        survivor = entries_sorted[0]
        result['kept'].append({'title': survivor['title'], 'rating_key': survivor['rating_key']})
        for dupe in entries_sorted[1:]:
            if plex_delete_collection(config, dupe['rating_key']):
                result['deleted'].append({'title': dupe['title'], 'rating_key': dupe['rating_key']})
                _log.info("Deleted duplicate collection '%s' (key=%s, had %d items) — kept key=%s (%d items)",
                          dupe['title'], dupe['rating_key'], dupe['child_count'],
                          survivor['rating_key'], survivor['child_count'])
            else:
                result['errors'].append(f"Failed to delete duplicate '{dupe['title']}' (key={dupe['rating_key']})")

    return result


def _plex_upload_poster(config, rating_key, image_path):
    """POST an image to /library/metadata/<ratingKey>/posters. This endpoint
    is generic to any Plex metadata item — collections and individual
    library items (e.g. a single video) alike — so this one implementation
    backs both plex_upload_collection_poster() and the per-video title-card
    upload in _generate_title_cards_for_videos()."""
    if not os.path.isfile(image_path):
        _log.warning("Poster image not found: %s", image_path)
        return False

    base_url = _plex_url(config)
    if not base_url:
        return False
    headers = _plex_headers(config)

    try:
        with open(image_path, 'rb') as f:
            img_data = f.read()
        resp = requests.post(
            f"{base_url}/library/metadata/{rating_key}/posters",
            headers=headers,
            files={'file': ('poster.jpg', img_data, 'image/jpeg')},
            timeout=30
        )
        if resp.status_code in (200, 201):
            return True
        _log.warning("Failed to upload poster for item %s, status=%d", rating_key, resp.status_code)
        return False
    except Exception as exc:
        _log.warning("Failed to upload poster for item %s: %s", rating_key, exc)
        return False


def plex_upload_collection_poster(config, collection_key, image_path):
    """Upload an image as the collection's poster/thumbnail.

    Args:
        config: Full app config dict.
        collection_key: The collection's rating key or title.
        image_path: Local path to the image file (folder.jpg).

    Returns: True on success.
    """
    ok = _plex_upload_poster(config, collection_key, image_path)
    if ok:
        _log.info("Collection poster uploaded successfully (key=%s)", collection_key)
    return ok


def plex_upload_collection_art(config, collection_key, image_path):
    """Upload an image as the collection's background art.
    
    Args:
        config: Full app config dict.
        collection_key: The collection's rating key or title.
        image_path: Local path to the image file (fanart.jpg or background.jpg).
    
    Returns: True on success.
    """
    if not os.path.isfile(image_path):
        _log.debug("Background art not found: %s", image_path)
        return False
    
    base_url = _plex_url(config)
    if not base_url:
        return False
    headers = _plex_headers(config)
    
    try:
        with open(image_path, 'rb') as f:
            img_data = f.read()
        
        # Plex API: POST to /library/metadata/<key>/arts with multipart upload
        resp = requests.post(
            f"{base_url}/library/metadata/{collection_key}/arts",
            headers=headers,
            files={'file': ('fanart.jpg', img_data, 'image/jpeg')},
            timeout=30
        )
        if resp.status_code in (200, 201):
            _log.info("Collection background art uploaded successfully (key=%s)", collection_key)
            return True
        else:
            _log.warning("Failed to upload background art, status=%d", resp.status_code)
            return False
    except Exception as exc:
        _log.warning("Failed to upload collection background art: %s", exc)
        return False


def plex_sync_artist_collection(config, artist_name, artist_path):
    """Full Plex collection sync for an artist:
    1. Find all videos matching "ArtistName -" in the library
    2. Create/update a collection with those videos
    3. Upload folder.jpg as collection poster
    4. Upload fanart.jpg as collection background
    5. Trigger library refresh
    
    Returns dict with status info.
    """
    result = {
        'artist': artist_name,
        'collection_created': False,
        'videos_found': 0,
        'poster_uploaded': False,
        'background_uploaded': False,
        'errors': [],
    }
    
    plex_config = config.get('plex', {})
    if not plex_config.get('server_url') or not plex_config.get('token'):
        _log.debug("Plex collection sync skipped — server_url or token not configured")
        result['errors'].append("Plex not configured")
        return result
    
    # Auto-discover library key if not set
    library_key = plex_config.get('music_video_library_key', '')
    if not library_key:
        library_key = plex_find_library_key(config)
        if library_key:
            # Save it back for future use
            plex_config['music_video_library_key'] = library_key
    
    if not library_key:
        result['errors'].append("Could not determine Plex library key")
        return result
    
    # Step 1: Report how many videos currently match (informational only —
    # the smart collection below finds these on its own and stays current
    # as more videos are added later).
    videos = plex_find_videos_by_artist(config, artist_name, library_key)
    result['videos_found'] = len(videos)

    # Step 2: Ensure the smart collection exists
    collection_key = plex_ensure_smart_collection(config, artist_name)
    
    if collection_key:
        result['collection_created'] = True
        
        # Step 3: Upload poster
        poster_path = os.path.join(artist_path, 'folder.jpg')
        if os.path.isfile(poster_path):
            result['poster_uploaded'] = plex_upload_collection_poster(config, collection_key, poster_path)
        
        # Step 4: Upload background
        for bg_name in ('fanart.jpg', 'background.jpg'):
            bg_path = os.path.join(artist_path, bg_name)
            if os.path.isfile(bg_path):
                result['background_uploaded'] = plex_upload_collection_art(config, collection_key, bg_path)
                if result['background_uploaded']:
                    break
    
    # Step 5: Trigger refresh
    trigger_plex_refresh(config)
    
    return result


def trigger_plex_refresh(config):
    """Tell Plex to scan the music videos library for new artwork."""
    plex_config = config.get('plex', {})
    server_url = plex_config.get('server_url', '').rstrip('/')
    token = plex_config.get('token', '')
    library_key = plex_config.get('music_video_library_key', '')
    
    if not server_url or not token:
        _log.debug("Plex refresh skipped — server_url or token not configured")
        return False
    
    # Auto-discover library key if not set
    if not library_key:
        library_key = plex_find_library_key(config)
        if library_key:
            plex_config['music_video_library_key'] = library_key
    
    # If no specific library key, try to trigger a general scan
    if library_key:
        url = f"{server_url}/library/sections/{library_key}/refresh?X-Plex-Token={token}"
    else:
        url = f"{server_url}/library/sections/all/refresh?X-Plex-Token={token}"
    
    try:
        resp = requests.get(url, timeout=10)
        if resp.status_code in (200, 202):
            _log.info("Plex library refresh triggered successfully")
            return True
        else:
            _log.warning("Plex refresh returned status %d", resp.status_code)
            return False
    except Exception as exc:
        _log.warning("Plex refresh failed: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Log file setup
# ---------------------------------------------------------------------------

def setup_logging(root_path):
    """Configure logging to both console and the artwork-sync.log file."""
    log_path = os.path.join(root_path, LOG_FILE) if root_path else LOG_FILE
    
    # Ensure log directory exists
    log_dir = os.path.dirname(log_path)
    if log_dir and not os.path.exists(log_dir):
        try:
            os.makedirs(log_dir, exist_ok=True)
        except Exception:
            pass
    
    handler = logging.FileHandler(log_path, encoding='utf-8')
    handler.setFormatter(logging.Formatter(
        '%(asctime)s [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S'
    ))
    _log.addHandler(handler)
    _log.setLevel(logging.INFO)
    # Also log to the root logger (which goes to stdout/stderr in Docker)
    _log.info("Artwork sync logging initialized — log file: %s", log_path)
    return log_path