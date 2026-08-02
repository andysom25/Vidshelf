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
