import os
import shutil
import itertools
import json
import threading
import time
import yt_dlp
import transcode
import state
import titles

DOWNLOAD_TRACKER_FILE = state.ACTIVE_DOWNLOADS_FILE
_lock = threading.Lock()

# Statuses a download can still move on from. Anything else is finished, one way
# or another, and is only kept around for the history list.
IN_FLIGHT_STATUSES = ('queued', 'downloading', 'converting')
TERMINAL_STATUSES = ('completed', 'error', 'cancelled', 'interrupted')

# How much finished history to keep. Nothing ever pruned this file before
# v1.8.1, so it grew for the life of the install and was returned *in full* on
# a 2-second poll while the Downloads tab was open.
MAX_TERMINAL_ENTRIES = 200

_id_counter = itertools.count()

def _copystat_best_effort(src, dst):
    """Copy mtime/mode across, but never fail the download over it.

    CLAUDE.md always described this as "optional, cosmetic — drop it if the
    server rejects chmod". It rejects it, and until v1.8.1 the exception took
    the whole download down with it.

    Why the server rejects it: the container runs as **root (uid 0)** while the
    CIFS mount forces every file to **uid 1000** (`uid=1000,gid=1000` in the
    volume options). `utime()` and `chmod()` on a file you do not own require
    CAP_FOWNER — and v1.6.1's `cap_drop: ALL`, added because SYS_ADMIN was being
    granted for nothing, removed it. Proven rather than guessed: the same image
    against the same mount succeeds with `--cap-add FOWNER` and raises
    PermissionError without it.

    The copy itself has already completed by the time this runs, so the file on
    the NAS is whole and correct. Losing the source timestamp is invisible to
    Plex, which reads its own metadata. Failing here was strictly worse: it
    reported a finished download as an error, skipped the `os.remove(src)` on
    the next line so the local copy leaked, and left the video out of the
    tracker so it would be downloaded again.
    """
    try:
        shutil.copystat(src, dst)
    except OSError as exc:
        print(f'DEBUG: could not copy timestamps/mode to {dst} '
              f'(harmless, see _copystat_best_effort): {exc}')


def _load_active():
    return state.read_json(DOWNLOAD_TRACKER_FILE)

def _save_active(data):
    # Atomic — this is the hottest write in the app (the yt-dlp progress hook
    # fires several times a second per in-flight download), so it's the one
    # most likely to be interrupted mid-write by a container stop. A truncated
    # active_downloads.json used to mean the downloads UI came back empty.
    state.write_json(DOWNLOAD_TRACKER_FILE, data, indent=2)

def _init_download(download_id, video_id, title, channel_url, final_path=None):
    entry = {
        'download_id': download_id,
        'video_id': video_id,
        'title': title,
        'channel_url': channel_url,
        'status': 'queued',
        'progress': 0,
        'speed': None,
        'eta': None,
        'downloaded_bytes': 0,
        'total_bytes': 0,
        'error': None,
        'started_at': time.time(),
        'completed_at': None,
        'filename': None,
        'final_path': final_path,
        'moved_to_final': False,
        'final_file_exists': None
    }
    with _lock:
        data = _load_active()
        data[download_id] = entry
        _save_active(data)
    return entry

def queue_download(video_id, title, channel_url, final_path=None):
    """Pre-register a download as 'queued' before it's actually submitted
    to the bounded worker pool (see app.py's _DOWNLOAD_EXECUTOR), so bulk
    downloads all show up immediately in the progress UI instead of only
    appearing once a worker actually picks them up — with the pool capped
    at a small number of concurrent downloads, that could otherwise be a
    long wait for anything past the first few. Returns the download_id to
    pass through to download_video()."""
    # The counter is what makes this unique. Second resolution alone collided:
    # queueing the same video twice within one second produced the same id, and
    # the second _init_download silently overwrote the first's entry — which is
    # exactly what a retry does, since it reuses the failed download's video_id.
    download_id = f"{video_id}_{int(time.time())}_{next(_id_counter)}"
    _init_download(download_id, video_id, title, channel_url, final_path=final_path)
    return download_id


def reconcile_interrupted(now=None):
    """Close out downloads that a restart left mid-flight, and trim history.

    Nothing reconciled this file before v1.8.1, and the consequence was much
    worse than a stale row in the UI. `_monitor_queue_depth()` counts entries in
    IN_FLIGHT_STATUSES to decide whether the download queue is backed up, and
    the scheduler skips a check entirely when that count reaches
    `max_queue_depth` (default 20). Since an interrupted download stayed
    'downloading' forever, twenty container restarts mid-download were enough to
    disable channel monitoring **permanently and silently** — no error, no log
    line, checks simply stopped happening.

    Also prunes finished entries beyond MAX_TERMINAL_ENTRIES, newest first. The
    file is served whole to the dashboard every two seconds, so unbounded growth
    is a real cost, not just disk.

    Safe to call at startup only: it must not run while downloads are live, or
    it would mark a genuinely running download as interrupted.

    Returns (interrupted_count, pruned_count).
    """
    stamp = now if now is not None else time.time()
    interrupted = 0
    pruned = 0
    with _lock:
        data = _load_active()

        # Snapshot what was *already* finished before anything is re-flagged.
        # Pruning the post-interruption set instead would let an entry be marked
        # interrupted and deleted in the same pass — the user would never see
        # that the download had been cut off, only that it vanished. It also
        # meant that calling this while downloads were live (which the contract
        # forbids, but which is one mistake away) deleted them outright.
        already_finished = [(e.get('started_at') or 0, k) for k, e in data.items()
                            if e.get('status') in TERMINAL_STATUSES]

        for entry in data.values():
            if entry.get('status') in IN_FLIGHT_STATUSES:
                entry['status'] = 'interrupted'
                entry['error'] = 'Vidshelf restarted while this was in progress'
                entry['completed_at'] = stamp
                interrupted += 1

        finished = already_finished
        if len(finished) > MAX_TERMINAL_ENTRIES:
            finished.sort(reverse=True)          # newest first; drop the tail
            for _, key in finished[MAX_TERMINAL_ENTRIES:]:
                del data[key]
                pruned += 1

        if interrupted or pruned:
            _save_active(data)
    return interrupted, pruned

def _update_progress(download_id, **kwargs):
    with _lock:
        data = _load_active()
        if download_id in data:
            data[download_id].update(kwargs)
            _save_active(data)

class DownloadCancelled(Exception):
    """Raised from inside the yt-dlp progress hook to abort a running download.

    yt-dlp has no cancel API — the documented way to stop a download in flight
    is to raise from a progress hook, which unwinds its internals cleanly and
    leaves the partial .part file for yt-dlp itself to clean up. Cancellation is
    therefore cooperative: it takes effect at the next progress callback, which
    for an active download is well under a second.
    """


def request_cancel(download_id):
    """Mark a download cancelled. Returns False if it is already finished.

    Setting a flag rather than killing a thread: a half-written file moved onto
    a network share is exactly the kind of mess this codebase has been bitten by
    before, so the download unwinds through its own error path instead.
    """
    with _lock:
        data = _load_active()
        entry = data.get(download_id)
        if not entry:
            return False, 'Unknown download'
        if entry.get('status') in ('completed', 'error', 'cancelled'):
            return False, f"Already {entry.get('status')}"
        entry['cancel_requested'] = True
        # Reflected immediately so the UI acknowledges the click even while a
        # queued item waits for a worker.
        if entry.get('status') == 'queued':
            entry['status'] = 'cancelled'
            entry['error'] = 'Cancelled before it started'
            entry['completed_at'] = time.time()
        _save_active(data)
        return True, 'Cancellation requested'


def is_cancelled(download_id):
    with _lock:
        entry = _load_active().get(download_id) or {}
        return bool(entry.get('cancel_requested'))


def _progress_hook(download_id):
    def hook(d):
        # Checked on every callback so a cancel lands promptly rather than at
        # the end of the file.
        if is_cancelled(download_id):
            raise DownloadCancelled(download_id)
        if d['status'] == 'downloading':
            total = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
            downloaded = d.get('downloaded_bytes', 0)
            pct = (downloaded / total * 100) if total > 0 else 0
            speed = d.get('speed')
            eta = d.get('eta')
            _update_progress(
                download_id,
                status='downloading',
                progress=round(pct, 1),
                downloaded_bytes=downloaded,
                total_bytes=total,
                speed=speed,
                eta=eta
            )
        elif d['status'] == 'finished':
            filename = d.get('filename', '')
            _update_progress(
                download_id,
                status='completed',
                progress=100,
                filename=filename
            )
    return hook

def build_format_selector(max_height=None):
    """yt-dlp format string, optionally capped at a vertical resolution.

    Prefers a native H.264 (avc1) + AAC (mp4a) stream — the format virtually
    every Plex client direct-plays with no server-side transcoding. Falls back to
    the best available (often VP9/AV1 + Opus for 4K or older uploads with no
    native H.264 option) only when no compatible stream exists; transcode.py
    fixes that up afterwards rather than silently shipping something
    incompatible.

    `max_height` caps every branch. Without it, monitoring a 4K channel
    unattended means 4K files whether or not that's wanted — which costs disk and
    a CPU-heavy re-encode per file. The cap is applied to the fallbacks too, so
    asking for 1080p can't be quietly overridden by a channel that only offers
    AV1 at 2160p.
    """
    try:
        cap = int(max_height) if max_height else 0
    except (TypeError, ValueError):
        cap = 0
    h = f'[height<={cap}]' if cap > 0 else ''
    return (
        f'bestvideo[vcodec^=avc1]{h}+bestaudio[acodec^=mp4a]/'
        f'best[vcodec^=avc1]{h}/'
        f'bestvideo{h}+bestaudio/'
        f'best{h}/best'
    )


def download_video(video_id, download_path, plex_media_path, title='Unknown', channel_url='', download_id=None, max_height=None, cookies_file=None, music_artist=None):
    """Download a video with progress tracking. Runs synchronously but updates progress file.

    music_artist, when given, switches on download-time naming: the file is
    written as "Artist - Song-<id>.ext" instead of whatever the uploader called
    it. Only the music-video path passes it, so channel downloads are byte-for-
    byte unaffected. See titles.build_music_video_title() for why this can be
    done here but not in the Plex-side cleanup.
    """
    if download_id is None:
        download_id = f"{video_id}_{int(time.time())}"

    # Initialize the download entry with final path tracking
    _init_download(download_id, video_id, title, channel_url, final_path=plex_media_path)

    # Use ffmpeg from PATH by default; override via env var if needed
    ffmpeg_bin = os.environ.get('FFMPEG_PATH')

    # Cookies unlock age-restricted and members-only content. The file existed
    # in this repo (gitignored) since long before this, but nothing ever passed
    # it to yt-dlp — so those downloads simply failed with no indication why.
    # They're needed on the *probe* too: without them an age-restricted video
    # won't even extract, so the probe would fail before the download ever ran.
    use_cookies = bool(cookies_file and os.path.isfile(cookies_file))

    def _base_opts():
        opts = {
            'format': build_format_selector(max_height),
            'merge_output_format': 'mp4',
            'quiet': False, # Set quiet to False for debugging
            'no_warnings': False, # Set no_warnings to False for debugging
        }
        if ffmpeg_bin:
            opts['ffmpeg_location'] = ffmpeg_bin
        if use_cookies:
            opts['cookiefile'] = cookies_file
        return opts

    if use_cookies:
        print(f'DEBUG: using cookies from {cookies_file}')
    try:
        print(f"DEBUG: download_video called with download_path='{download_path}', plex_media_path='{plex_media_path}'")

        # Probe first, on a throwaway instance. The output template has to be
        # decided *before* the downloading instance is constructed, and for a
        # music video that decision depends on metadata only the probe returns.
        # Mutating ydl.params on a live instance would be the shorter route and
        # is not supported — hence two instances.
        with yt_dlp.YoutubeDL({**_base_opts(), 'skip_download': True}) as probe:
            info = probe.extract_info(f'https://www.youtube.com/watch?v={video_id}',
                                      download=False)
        real_title = info.get('title', title)

        if music_artist:
            resolved = titles.build_music_video_title(music_artist, real_title, info)
            stem = titles.sanitize_filename(resolved, fallback=video_id)
            # Escape % so a title containing one isn't read as a yt-dlp field.
            # -%(id)s stays: the post-download file match below finds the output
            # by looking for video_id in the filename.
            name_template = stem.replace('%', '%%') + '-%(id)s.%(ext)s'
            print(f'DEBUG: music-video naming "{real_title}" -> "{stem}"')
            real_title = resolved
        else:
            name_template = '%(title)s-%(id)s.%(ext)s'

        _update_progress(download_id, title=real_title)

        ydl_opts = {
            **_base_opts(),
            'outtmpl': os.path.join(download_path, name_template),
            'progress_hooks': [_progress_hook(download_id)],
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([f'https://www.youtube.com/watch?v={video_id}'])

        # Find the file yt-dlp just wrote to download_path
        downloaded_file = None
        for filename in os.listdir(download_path):
            if video_id in filename and filename.endswith(('.mp4', '.mkv', '.webm')):
                downloaded_file = filename
                break

        if downloaded_file:
            local_path = os.path.join(download_path, downloaded_file)
            # Convert to a Plex-direct-play-compatible format (H.264/MP4/AAC)
            # BEFORE moving to plex_media_path, which may be a network mount
            # (see CLAUDE.md gotchas #1/#2) - transcoding is CPU/IO-heavy
            # work that should run against local storage, not over the wire.
            # Only re-encodes whichever track (if any) isn't already
            # compatible; a no-op for the common case where the format
            # selector above already got a native H.264/AAC stream.
            if transcode.needs_conversion(local_path):
                _update_progress(download_id, status='converting')
                print(f"DEBUG: {downloaded_file} isn't Plex-direct-play-compatible, converting...")
                conv_result = transcode.convert_to_plex_compatible(local_path)
                if conv_result['success']:
                    downloaded_file = os.path.basename(conv_result['output_path'])
                    print(f"DEBUG: Converted to Plex-compatible format: {downloaded_file}")
                else:
                    print(f"WARNING: Conversion failed for {downloaded_file}: {conv_result['error']} "
                          f"— shipping the original format instead")
                _update_progress(download_id, status='downloading', filename=downloaded_file)

        # After download (+ optional conversion), check if move/copy is needed
        if downloaded_file and os.path.normpath(download_path) != os.path.normpath(plex_media_path):
            print(f"DEBUG: Paths differ. Moving/copying from '{download_path}' to '{plex_media_path}'")
            src = os.path.join(download_path, downloaded_file)
            dst = os.path.join(plex_media_path, downloaded_file)

            # shutil.copy2 is NOT safe here: on Linux, shutil.copyfile() uses
            # os.sendfile() internally as a fast-path (since Python 3.8), which
            # is the exact syscall that fails with ENOSPC on this CIFS/Samba
            # mount. copy2 only falls back to a plain read/write loop if the
            # *first* sendfile() call fails - once a few chunks succeed, later
            # ENOSPC errors propagate straight through. Do a manual buffered
            # copy instead, which matches the plain open().write() that's
            # already confirmed to work on this mount.
            with open(src, 'rb') as fsrc, open(dst, 'wb') as fdst:
                shutil.copyfileobj(fsrc, fdst)
            _copystat_best_effort(src, dst)
            os.remove(src) # Remove from source after successful copy
            _update_progress(download_id, filename=downloaded_file)
            # Verify the file exists at the final destination
            final_exists = os.path.isfile(dst)
            _update_progress(
                download_id,
                moved_to_final=True,
                final_file_exists=final_exists
            )
            if final_exists:
                print(f"DEBUG: File successfully moved to final destination: {dst}")
            else:
                print(f"ERROR: File NOT found at final destination after copy: {dst}")
        elif downloaded_file:
            print(f"DEBUG: Download path and Plex media path are the same: '{download_path}'")
            _update_progress(
                download_id,
                filename=downloaded_file,
                moved_to_final=True,
                final_file_exists=True
            )
        else:
            print(f"DEBUG: Downloaded file not found in '{download_path}' for video_id {video_id}")
            _update_progress(
                download_id,
                moved_to_final=False,
                final_file_exists=False,
                error="Downloaded file not found in source path after download"
            )

        _update_progress(download_id, status='completed', progress=100, completed_at=time.time())
    except Exception as e:
        # A cancellation is not a failure: it must not be recorded as an error,
        # and above all must not fire a "download failed" notification for
        # something the user asked to stop.
        if isinstance(e, DownloadCancelled) or is_cancelled(download_id):
            _update_progress(download_id, status='cancelled',
                             error='Cancelled', completed_at=time.time())
            raise DownloadCancelled(download_id) from None
        _update_progress(download_id, status='error', error=str(e), completed_at=time.time())
        raise

def get_active_downloads():
    """Return all tracked downloads sorted by start time (newest first)."""
    with _lock:
        data = _load_active()
    downloads = list(data.values())
    downloads.sort(key=lambda x: x.get('started_at', 0), reverse=True)
    return downloads