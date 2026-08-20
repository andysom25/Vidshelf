"""Download-state bugs fixed in v1.8.1.

    python tests/test_download_state.py

The headline one had no visible symptom until it had already broken everything:
`shutil.copystat()` raised PermissionError on the CIFS mount, *after* the file
had been fully copied, so a finished download was reported as an error, its
local copy leaked, and it was never recorded — 2.6 GB of leaked temp files and
seven "failed" videos that were actually sitting correct and complete on the NAS.

The others are the same shape: state that only ever grew, or only ever leaked,
with nothing that would fail loudly.
"""

import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_ORIGINAL_CWD = os.getcwd()
_WORK = tempfile.mkdtemp(prefix='vidshelf-dlstate-test-')
os.environ['VIDSHELF_DATA_DIR'] = os.path.join(_WORK, 'data')
os.environ.setdefault('ADMIN_PASSWORD', 'dlstate-test-password')

import downloader  # noqa: E402
import state  # noqa: E402


def _reset():
    state.write_json(state.ACTIVE_DOWNLOADS_FILE, {})


def _entry(did, status, started_at=None):
    downloader._init_download(did, did.split('_')[0], 't', 'https://c',
                              final_path='/x')
    fields = {'status': status}
    if started_at is not None:
        fields['started_at'] = started_at
    downloader._update_progress(did, **fields)


# --------------------------------------------------------------------------
# copystat — the one that broke real downloads
# --------------------------------------------------------------------------

def test_copystat_failure_does_not_abort_a_completed_copy():
    """The file is already whole by the time copystat runs.

    The container runs as root while the CIFS mount forces uid=1000, and
    utime/chmod on a file you don't own needs CAP_FOWNER — which v1.6.1's
    `cap_drop: ALL` removed. Losing a timestamp is invisible to Plex; failing
    the download was not.
    """
    src = os.path.join(_WORK, 'src.mp4')
    dst = os.path.join(_WORK, 'dst.mp4')
    with open(src, 'wb') as fh:
        fh.write(b'video bytes')
    shutil.copyfile(src, dst)

    real = shutil.copystat

    def _boom(a, b, **kw):
        raise PermissionError(1, 'Operation not permitted')

    shutil.copystat = _boom
    try:
        downloader._copystat_best_effort(src, dst)   # must not raise
    finally:
        shutil.copystat = real

    assert os.path.isfile(dst), 'destination was disturbed by the failure'


def test_copystat_still_applied_when_the_filesystem_allows_it():
    """Best-effort must not mean never-tried — on a local disk it should work."""
    src = os.path.join(_WORK, 'stat-src.mp4')
    dst = os.path.join(_WORK, 'stat-dst.mp4')
    with open(src, 'wb') as fh:
        fh.write(b'x')
    shutil.copyfile(src, dst)
    old = (time.time() - 100000, time.time() - 100000)
    os.utime(src, old)
    downloader._copystat_best_effort(src, dst)
    assert abs(os.stat(dst).st_mtime - os.stat(src).st_mtime) < 2, \
        'timestamps were not copied on a filesystem that permits it'


# --------------------------------------------------------------------------
# Interrupted downloads -> the monitor deadlock
# --------------------------------------------------------------------------

def test_interrupted_downloads_are_closed_out_on_startup():
    """A restart used to leave these 'downloading' forever.

    That is what made the bug severe rather than cosmetic: the scheduler counts
    in-flight entries and skips a check once they reach max_queue_depth
    (default 20), so twenty interrupted downloads disabled channel monitoring
    permanently and silently.
    """
    _reset()
    for i, status in enumerate(['queued', 'downloading', 'converting']):
        _entry(f'v{i}_1', status)
    _entry('done_1', 'completed')
    _entry('err_1', 'error')

    interrupted, _ = downloader.reconcile_interrupted()
    assert interrupted == 3, interrupted

    data = state.read_json(state.ACTIVE_DOWNLOADS_FILE)
    assert not [e for e in data.values()
                if e['status'] in downloader.IN_FLIGHT_STATUSES], \
        'an in-flight entry survived reconciliation'
    assert data['done_1']['status'] == 'completed', 'terminal status was rewritten'
    assert data['err_1']['status'] == 'error', 'terminal status was rewritten'
    assert data['v0_1']['error'], 'interrupted entries should say why'


def test_reconcile_is_idempotent():
    _reset()
    _entry('a_1', 'downloading')
    assert downloader.reconcile_interrupted()[0] == 1
    assert downloader.reconcile_interrupted()[0] == 0, \
        'a second pass re-flagged already-closed entries'


def test_reconcile_survives_a_missing_or_empty_file():
    """Startup must not be blocked by housekeeping on a fresh install."""
    if os.path.exists(state.ACTIVE_DOWNLOADS_FILE):
        os.remove(state.ACTIVE_DOWNLOADS_FILE)
    assert downloader.reconcile_interrupted() == (0, 0)


# --------------------------------------------------------------------------
# Unbounded history
# --------------------------------------------------------------------------

def test_finished_history_is_capped():
    """The whole file is served to the dashboard every 2 seconds, so unbounded
    growth costs bandwidth and render time, not just disk."""
    _reset()
    for i in range(downloader.MAX_TERMINAL_ENTRIES + 60):
        _entry(f'old{i}_1', 'completed', started_at=1000 + i)

    _, pruned = downloader.reconcile_interrupted()
    assert pruned == 60, pruned
    kept = state.read_json(state.ACTIVE_DOWNLOADS_FILE)
    assert len(kept) == downloader.MAX_TERMINAL_ENTRIES, len(kept)
    # Newest kept, oldest dropped.
    assert 'old259_1' in kept, 'the newest entry was pruned'
    assert 'old0_1' not in kept, 'the oldest entry survived'


def test_pruning_never_touches_in_flight_entries():
    """A live download must not be dropped just because history is long."""
    _reset()
    for i in range(downloader.MAX_TERMINAL_ENTRIES + 30):
        _entry(f'p{i}_1', 'completed', started_at=1000 + i)
    _entry('live_1', 'downloading', started_at=1)   # oldest of all

    downloader.reconcile_interrupted()
    kept = state.read_json(state.ACTIVE_DOWNLOADS_FILE)
    assert 'live_1' in kept, 'an in-flight entry was pruned as if it were history'


# --------------------------------------------------------------------------
# download_id collisions
# --------------------------------------------------------------------------

def test_two_downloads_of_one_video_in_the_same_second_get_distinct_ids():
    """The id was video_id + whole seconds. A retry reuses the failed
    download's video_id, so retrying promptly overwrote the original record."""
    _reset()
    ids = [downloader.queue_download('vid1', f'take {i}', 'https://c')
           for i in range(5)]
    assert len(set(ids)) == 5, ids
    assert len(state.read_json(state.ACTIVE_DOWNLOADS_FILE)) == 5


# --------------------------------------------------------------------------
# Staging sweep — the one that must never delete a library
# --------------------------------------------------------------------------

def _staging_fixture():
    """./downloads doubling as both staging and a channel's final destination —
    the default layout, and the one that makes a blind sweep dangerous."""
    base = tempfile.mkdtemp(prefix='sweep-', dir=_WORK)
    old = time.time() - 10 * 3600
    paths = {}

    def add(subdir, name, age=old):
        d = os.path.join(base, *subdir)
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, name)
        with open(p, 'wb') as fh:
            fh.write(b'x' * 1024)
        os.utime(p, (age, age))
        return p

    paths['downloads'] = os.path.join(base, 'downloads')
    paths['music'] = os.path.join(base, 'downloads', 'music_videos')
    os.makedirs(paths['music'], exist_ok=True)
    paths['library'] = add(['downloads'], 'Final Channel Video-abc12345678.mp4')
    paths['frag'] = add(['downloads'], 'Half Done-abc12345678.f251.webm')
    paths['part'] = add(['downloads'], 'Interrupted-xyz.mp4.part')
    paths['leak'] = add(['downloads', 'music_videos'], 'Artist - Song-vid1.mp4')
    paths['mfrag'] = add(['downloads', 'music_videos'], 'Artist - Song-vid1.f137.mp4')
    paths['fresh'] = add(['downloads', 'music_videos'], 'Downloading Now-vid2.mp4',
                         age=time.time())
    return paths


def test_sweep_never_removes_complete_files_from_an_unvouched_directory():
    """The regression test for a library I actually deleted.

    The first version of this sweep computed "pure staging" as "not among the
    destinations resolved from the current config". On an install whose
    plex_base_path had since been repointed at another share, ./downloads was not
    in that set — so four finished videos left there by an *earlier* config were
    classified as leftovers and removed. 640 MB, unrecoverable.

    The config says where files go now; it says nothing about where existing
    files came from. Orphans live precisely in the directory that is no longer a
    destination. So complete files are only removed from directories a caller
    explicitly vouches for, and everything else fails closed.
    """
    p = _staging_fixture()
    # ./downloads passed for intermediates only, and vouched for by nobody.
    downloader.sweep_staging([p['downloads']], pure_staging_dirs=[])
    assert os.path.exists(p['library']),         'a finished video was deleted from a directory nobody vouched for'
    assert not os.path.exists(p['frag']), 'a merge fragment survived'
    assert not os.path.exists(p['part']), 'a .part file survived'


def test_sweep_clears_complete_files_only_where_vouched_for():
    """./downloads/music_videos is staging by construction — the music route
    always copies out of it — so leaked complete copies there are removable."""
    p = _staging_fixture()
    downloader.sweep_staging([p['downloads']], pure_staging_dirs=[p['music']])
    assert not os.path.exists(p['leak']), 'a leaked complete copy survived'
    assert not os.path.exists(p['mfrag'])
    assert os.path.exists(p['library']), 'the sibling library file was touched'


def test_sweep_leaves_recent_files_alone():
    """Belt-and-braces: the sweep only runs at startup, but a file being written
    right now must never be removed."""
    p = _staging_fixture()
    downloader.sweep_staging([p['downloads']], pure_staging_dirs=[p['music']])
    assert os.path.exists(p['fresh']), 'an in-progress download was swept'


def test_sweep_tolerates_a_missing_directory():
    assert downloader.sweep_staging(['/nonexistent/staging']) == (0, 0)


def test_intermediate_detection():
    for name in ('x.f251.webm', 'x.f137.mp4', 'a.f140.m4a', 'y.mp4.part',
                 'z.ytdl', 'q.temp'):
        assert downloader._is_intermediate(name), name
    for name in ('Artist - Song-vid1.mp4', 'video.mkv', 'f251.webm',
                 'The Strokes - 12_51-LPAVDHo1Elc.mp4'):
        assert not downloader._is_intermediate(name), name


def test_already_downloaded_is_asked_across_every_source():
    """Regression, v1.12.0.

    A YouTube video id is globally unique, so "do we already have this?" does not
    depend on which source it was filed under. Asking per-source got it wrong
    whenever the folder and the uploader spell the artist differently.

    The real case: five Matchbox Twenty videos are tracked under
    `music_video_Matchbox_20`, because the folder is `Matchbox_20`. A search for
    "Matchbox Twenty" resolves to `music_video_Matchbox_Twenty`, misses, and
    reported all five as not downloaded — so the Music Videos page offered a
    Download button for videos already on disk, and taking it would fork a second
    artist folder and Plex collection.
    """
    import tracker
    state.write_json(tracker.TRACKER_FILE, {
        'music_video_Matchbox_20': ['C-Naa1HXeDQ', 'HAkHqYlqops'],
        'https://www.youtube.com/@somechannel': ['CHANNELVID1'],
    }, indent=2)

    # The spelling mismatch that used to miss.
    for vid in ('C-Naa1HXeDQ', 'HAkHqYlqops'):
        assert tracker.is_video_downloaded(vid, 'music_video_Matchbox_Twenty') is False, (
            'the per-source check is expected to miss here — that is the bug')
        assert tracker.is_video_downloaded_anywhere(vid) is True, (
            f'{vid} is in the tracker under a different key and must count as '
            'downloaded; otherwise the UI offers a duplicate download')

    # A channel download counts too: the file is on disk either way.
    assert tracker.is_video_downloaded_anywhere('CHANNELVID1') is True

    # And something genuinely absent must still be absent.
    assert tracker.is_video_downloaded_anywhere('NEVERSEEN01') is False

    # The per-source check still works when the key does match — the music
    # download route relies on it to decide where to file a new download.
    assert tracker.is_video_downloaded('C-Naa1HXeDQ', 'music_video_Matchbox_20') is True


def test_a_repeat_download_is_filed_under_the_source_it_already_has():
    """Regression, v1.12.0 — the other half of the Matchbox case.

    _resolve_existing_artist snaps a search query to an existing artist FOLDER,
    which only works when both spell the name the same way. It cannot bridge
    "Matchbox Twenty" to the folder `Matchbox_20`, so downloading a video that is
    already in `Matchbox_20` after searching the band's own spelling created a
    second `Matchbox_Twenty` folder and a second Plex collection.

    The tracker already records where the video went, with no spelling involved.
    source_holding_video() reads that back so the repeat is filed alongside the
    original.
    """
    import tracker
    state.write_json(tracker.TRACKER_FILE, {
        'music_video_Matchbox_20': ['C-Naa1HXeDQ'],
        'https://www.youtube.com/@somechannel': ['CHANNELVID1'],
    }, indent=2)

    assert tracker.source_holding_video('C-Naa1HXeDQ') == 'music_video_Matchbox_20'
    assert tracker._artist_from_music_key('music_video_Matchbox_20') == 'Matchbox 20'

    # A channel-sourced video must NOT redirect the artist: filing a music
    # download under a channel URL would put it outside the artist library.
    assert tracker.source_holding_video('CHANNELVID1') == 'https://www.youtube.com/@somechannel'
    assert tracker._artist_from_music_key('https://www.youtube.com/@somechannel') is None

    # An unseen video leaves the caller's artist untouched.
    assert tracker.source_holding_video('NEVERSEEN01') is None


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    failures = 0
    for t in tests:
        try:
            t()
        except AssertionError as exc:
            failures += 1
            print(f'FAIL  {t.__name__}: {exc}')
        else:
            print(f'ok    {t.__name__}')
    print(f'\n{len(tests) - failures}/{len(tests)} passed')
    return 1 if failures else 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    finally:
        os.chdir(_ORIGINAL_CWD)
        shutil.rmtree(_WORK, ignore_errors=True)
