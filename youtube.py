"""Everything this app asks YouTube, via yt-dlp.

Extracted from app.py in v1.11.0. Metadata probes only: channel names, channel
listings, music-video search, and the quality label for one video. The actual
downloading lives in downloader.py and is deliberately kept apart, because the
two want opposite network settings.

That difference is the reason PROBE_TIMEOUTS is in this module and not shared with
downloader.py. yt-dlp's defaults -- no socket deadline, ten retries with
escalating backoff -- are right when the goal is to eventually get a file, and
wrong when a browser is waiting: v1.10.1 was one throttled probe holding an HTTP
response open until the browser gave up. Anything here that a request waits on
must stay bounded; anything in downloader.py may be patient.

tests/test_invariants.py enforces that every probe in this file builds its options
through _probe_opts(), and reads this module as part of the app rather than by
filename.
"""

import concurrent.futures
import datetime
import math
import threading
import time

import yt_dlp

_CHANNEL_NAME_CACHE = {}          # url -> (fetched_at, name)
_CHANNEL_NAME_TTL = 24 * 3600
_CHANNEL_NAME_LOCK = threading.Lock()


def get_channel_info(channel_url, force=False):
    """Lightweight fetch of just the channel display name.

    "Lightweight" is relative: this is a live yt-dlp extraction against YouTube
    and it was measured at **23 seconds** for a single channel. /api/channels
    calls it once per configured channel, so the Channels page cost 23s x N on
    every visit, and v1.9.0's 60-second dashboard refresh turned that into a
    continuous background load — with three channels the work per cycle exceeded
    the cycle itself, so it would never catch up and would hammer YouTube
    forever.

    Cached for a day. A channel's display name effectively never changes, and
    the cost of being a day stale is a slightly wrong label; the cost of not
    caching is rate-limiting that breaks actual downloads.
    """
    now = time.time()
    if not force:
        with _CHANNEL_NAME_LOCK:
            hit = _CHANNEL_NAME_CACHE.get(channel_url)
        if hit and (now - hit[0]) < _CHANNEL_NAME_TTL:
            return hit[1]

    name = _fetch_channel_name(channel_url)
    if name:
        # Only cache successes. A failure is usually transient (network, a
        # throttle), and caching it for a day would mean one bad moment
        # labelling a channel "Unknown Channel" until a restart.
        with _CHANNEL_NAME_LOCK:
            _CHANNEL_NAME_CACHE[channel_url] = (now, name)
    return name


# Bounds on every metadata-only yt-dlp call. These are NOT cosmetic.
#
# yt-dlp's defaults are tuned for "get the file eventually": no socket deadline
# at all, and 10 retries with escalating backoff. That is right for a download
# and wrong for anything a browser is waiting on, because a single throttled
# response stalls the HTTP request indefinitely. The music-video search made 9
# of these calls in series, so one slow probe took the whole response down and
# the UI reported "Failed to search: Failed to fetch" -- the browser's own
# message for a dead connection, naming nothing and pointing nowhere.
#
# Deliberately applied to metadata probes only. The download path in
# downloader.py keeps yt-dlp's patient defaults, because there a retry is the
# difference between getting the video and not.
PROBE_TIMEOUTS = {
    'socket_timeout': 15,
    'retries': 2,
    'extractor_retries': 1,
}


def _probe_opts(**extra):
    """yt-dlp options for a metadata-only call, with the timeouts applied.

    Everything that calls extract_info(download=False) should build its options
    through here, so a new probe cannot be added without bounds by forgetting to
    copy three keys. tests/test_invariants.py enforces that.
    """
    return {'quiet': True, 'no_warnings': True, **PROBE_TIMEOUTS, **extra}


def _fetch_channel_name(channel_url):
    try:
        ydl_opts = _probe_opts(extract_flat=True, dump_single_json=True)
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

    ydl_opts = _probe_opts(extract_flat=True, dump_single_json=True)
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
        ydl_opts = _probe_opts(extract_flat=True, dump_single_json=True)
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
        # Inert as things stand: the flat search this runs over does not return
        # upload_date (0 of 34 on a real query), so this bonus has never actually
        # been awarded. Left in place rather than deleted because it is correct
        # the moment the field is populated -- but do not count it as a live
        # ranking signal, and do not claim it in the README.
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
        ydl_opts = _probe_opts()
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f'https://www.youtube.com/watch?v={video_id}', download=False)
            formats = info.get('formats', [])
            
            # Extract the best available quality label
            max_height = 0
            best_quality = 'unknown'
            
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


# One page of search results is 9 videos; 6 at a time keeps the wall-clock close
# to a single probe without opening nine simultaneous connections to YouTube,
# which is the behaviour that got the dashboard throttled in v1.9.2.
PROBE_CONCURRENCY = 6
# Belt and braces over socket_timeout. yt-dlp can spend time outside a socket
# read (DNS, extractor parsing, its own sleeps between retries), so the only way
# to bound the endpoint is to stop waiting on the future as well.
PROBE_WALL_CLOCK_TIMEOUT = 25


def _enrich_video_qualities(page_videos):
    """Fill in 'best_quality' for a page of search results, in parallel.

    Never raises, and never leaves the caller without a response: a probe that
    fails or overruns yields 'unknown' for that one video. Returns the number
    that could not be resolved, for logging.
    """
    todo = [v for v in page_videos if 'best_quality' not in v]
    if not todo:
        return 0

    unresolved = 0
    # NOT `with ThreadPoolExecutor(...)`. The context manager exits via
    # shutdown(wait=True), which blocks until every probe finishes — and
    # Future.cancel() returns False for anything already running. Using it here
    # would make PROBE_WALL_CLOCK_TIMEOUT completely inert: the endpoint would
    # still hang for as long as the slowest probe, exactly the bug being fixed,
    # while looking like it had a timeout. shutdown(wait=False) returns
    # immediately and lets the orphaned threads finish into a result nobody
    # reads.
    pool = concurrent.futures.ThreadPoolExecutor(
        max_workers=min(PROBE_CONCURRENCY, len(todo)),
        thread_name_prefix='probe')
    try:
        futures = {pool.submit(get_video_formats_info, v['id']): v for v in todo}
        try:
            done, pending = concurrent.futures.wait(
                futures, timeout=PROBE_WALL_CLOCK_TIMEOUT)
        except Exception:       # noqa: BLE001 - wait() itself must not sink the request
            done, pending = set(), set(futures)
        for fut in done:
            v = futures[fut]
            try:
                v['best_quality'] = fut.result().get('best_quality', 'unknown')
            except Exception:   # noqa: BLE001
                v['best_quality'] = 'unknown'
                unresolved += 1
        for fut in pending:
            # Set it rather than leaving it absent. These dicts ARE the cached
            # objects, so an absent key means the next page request re-probes —
            # which is right. What must not happen is the request waiting on it.
            futures[fut]['best_quality'] = 'unknown'
            unresolved += 1
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    if unresolved:
        print(f'[search] {unresolved}/{len(todo)} quality probes did not resolve '
              f'within {PROBE_WALL_CLOCK_TIMEOUT}s; labelled unknown', flush=True)
    return unresolved
