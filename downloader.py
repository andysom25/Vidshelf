import os
import shutil
import json
import threading
import time
import yt_dlp
import transcode
import state

DOWNLOAD_TRACKER_FILE = state.ACTIVE_DOWNLOADS_FILE
_lock = threading.Lock()

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
    download_id = f"{video_id}_{int(time.time())}"
    _init_download(download_id, video_id, title, channel_url, final_path=final_path)
    return download_id

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


def download_video(video_id, download_path, plex_media_path, title='Unknown', channel_url='', download_id=None, max_height=None, cookies_file=None):
    """Download a video with progress tracking. Runs synchronously but updates progress file."""
    if download_id is None:
        download_id = f"{video_id}_{int(time.time())}"

    # Initialize the download entry with final path tracking
    _init_download(download_id, video_id, title, channel_url, final_path=plex_media_path)

    # Use ffmpeg from PATH by default; override via env var if needed
    ffmpeg_bin = os.environ.get('FFMPEG_PATH')

    ydl_opts = {
        'outtmpl': os.path.join(download_path, '%(title)s-%(id)s.%(ext)s'),
        'format': build_format_selector(max_height),
        'merge_output_format': 'mp4',
        'quiet': False, # Set quiet to False for debugging
        'no_warnings': False, # Set no_warnings to False for debugging
        'progress_hooks': [_progress_hook(download_id)]
    }
    if ffmpeg_bin:
        ydl_opts['ffmpeg_location'] = ffmpeg_bin

    # Cookies unlock age-restricted and members-only content. The file existed
    # in this repo (gitignored) since long before this, but nothing ever passed
    # it to yt-dlp — so those downloads simply failed with no indication why.
    if cookies_file and os.path.isfile(cookies_file):
        ydl_opts['cookiefile'] = cookies_file
        print(f'DEBUG: using cookies from {cookies_file}')
    try:
        print(f"DEBUG: download_video called with download_path='{download_path}', plex_media_path='{plex_media_path}'")
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # Pre-extract to get the real title
            info = ydl.extract_info(f'https://www.youtube.com/watch?v={video_id}', download=False)
            real_title = info.get('title', title)
            _update_progress(download_id, title=real_title)
            # Now download
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
            shutil.copystat(src, dst)
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