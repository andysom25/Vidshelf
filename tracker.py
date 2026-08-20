"""What has already been downloaded, and the synthetic keys music videos use.

Extracted from app.py in v1.11.0.

downloaded_videos.json is the answer to "do we already have this?", and it holds
video ids and nothing else -- no titles, no timestamps, no paths. Retention sorts
by filesystem mtime *because* a download date was never recorded here. That
limitation is the motivation for the v2.0.0 data model; until then, this module is
the whole of it.

The read-modify-write in mark_video_downloaded() must stay under one lock. It runs
on the bounded download pool, so with concurrency > 1 two downloads finishing
together would each load the same tracker, append only their own video, and the
second writer would drop the first's entry -- which then looks new on the next
channel check and gets downloaded again.
"""

import os

import state
import titles
from config_store import TRACKER_FILE


def _invalidate_library_scan():
    """Tell library.py its cached scan is stale.

    Imported lazily, inside the call, purely to keep the dependency one-way at
    import time. tracker -> library is fine; the reverse never happens, and a
    module-level import here would make the pair harder to reason about than a
    single deferred lookup is worth.
    """
    import library
    library._invalidate_library_scan()

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
    # titles.artist_to_folder, not app._sanitize_folder_name. The two were
    # byte-identical implementations of the same rule -- artist_to_folder's own
    # docstring said "mirrors _sanitize_folder_name" -- and this is the one place
    # where a divergence between them would silently change WHERE a retried music
    # video lands. Checked before deduping rather than assumed: the two agreed on
    # all 3,029 inputs tried, including 3,000 randomised ones over the character
    # set that actually matters (spaces, dots, underscores, the Windows-invalid
    # set, tabs and newlines).
    folder = titles.artist_to_folder(music_artist)
    if os.path.basename(os.path.normpath(recorded_path)) == folder:
        return recorded_path
    return os.path.join(recorded_path, folder)


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

def is_video_downloaded_anywhere(video_id):
    """Do we already have this video, under ANY source?

    A YouTube video id is globally unique, so "have we downloaded this?" does not
    actually depend on which artist or channel we filed it under — and asking
    per-source gets the answer wrong whenever the folder and the uploader spell
    the artist differently.

    The case that motivated this: five Matchbox Twenty videos are tracked under
    `music_video_Matchbox_20`, because the folder is `Matchbox_20`. Searching for
    "Matchbox Twenty" — the spelling the band's own uploads use — resolves to
    `music_video_Matchbox_Twenty`, misses, and reports every one of them as not
    downloaded. Numerals versus words (`20`/`Twenty`, `3`/`Three`, `&`/`and`) are
    not something _resolve_existing_artist can close, and a substitution table
    would break artists whose names contain a real number (`Sum 41`, `Blink-182`,
    `Front 242`).

    Scanning every source sidesteps the question. The tracker is a few dozen ids
    per source and already in memory, so this is cheaper than the filesystem
    lookup the caller does to resolve the artist in the first place.

    A video that turns up under a different source than expected still means the
    file is on disk, which is what the caller is really asking.
    """
    tracker = load_downloaded_tracker()
    for ids in tracker.values():
        if video_id in ids:
            return True
    return False


def source_holding_video(video_id):
    """The tracker key this video is already filed under, or None.

    Used to stop a re-download forking a second artist folder. is_video_downloaded_anywhere()
    answers "do we have it"; this answers "where did we put it", which is what
    the download route needs to file a repeat under the same artist instead of
    inventing a near-duplicate from the search text.

    Returns the first match. A video under two sources is possible (a collab
    filed under both artists) and either answer is equally correct for this
    purpose — the point is to reuse an existing folder rather than create one.
    """
    tracker = load_downloaded_tracker()
    for key, ids in tracker.items():
        if video_id in ids:
            return key
    return None


def is_video_downloaded(video_id, channel_url):
    tracker = load_downloaded_tracker()
    return video_id in tracker.get(channel_url, [])
