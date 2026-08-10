"""Tests for v1.6.0: cancellation, quality caps and cookie handling.

    python tests/test_downloads.py

No network and no yt-dlp: the format selector and cancellation flag are pure
logic over the state files, which is where the risk actually is.
"""

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_WORK = tempfile.mkdtemp(prefix='vidshelf-dl-test-')
os.environ['VIDSHELF_DATA_DIR'] = os.path.join(_WORK, 'data')

import downloader  # noqa: E402
import state  # noqa: E402


def _reset():
    state.write_json(downloader.DOWNLOAD_TRACKER_FILE, {}, indent=2)


# ------------------------------------------------------- format selector
def test_no_cap_keeps_the_original_preference_order():
    """Uncapped output must stay byte-identical to pre-v1.6.0, or every existing
    install silently changes which stream it picks."""
    fmt = downloader.build_format_selector(None)
    assert fmt.startswith('bestvideo[vcodec^=avc1]+bestaudio[acodec^=mp4a]/'), fmt
    assert '[height<=' not in fmt, fmt


def test_zero_and_garbage_mean_no_cap():
    for value in (0, '0', None, '', 'best', -5):
        fmt = downloader.build_format_selector(value)
        assert '[height<=' not in fmt, f'{value!r} produced a cap: {fmt}'


def test_cap_is_applied_to_every_branch():
    """The fallbacks matter most. A channel offering only AV1 at 2160p would
    otherwise satisfy a later branch and quietly ignore a 1080p request."""
    fmt = downloader.build_format_selector(1080)
    branches = fmt.split('/')
    # The final bare 'best' is the last-resort escape hatch; every branch before
    # it must carry the cap.
    assert branches[-1] == 'best', branches
    for branch in branches[:-1]:
        assert '[height<=1080]' in branch, f'uncapped branch: {branch}'


def test_cap_accepts_a_numeric_string():
    assert '[height<=720]' in downloader.build_format_selector('720')


# ---------------------------------------------------------- cancellation
def test_cancelling_a_queued_download_marks_it_immediately():
    """A queued item has no worker to notice a flag, so the API flips it itself —
    otherwise the button would appear to do nothing."""
    _reset()
    did = downloader.queue_download('vid1', 'One', 'https://c')
    ok, detail = downloader.request_cancel(did)
    assert ok, detail
    entry = state.read_json(downloader.DOWNLOAD_TRACKER_FILE)[did]
    assert entry['status'] == 'cancelled', entry
    assert entry['completed_at'] is not None


def test_cancelling_a_running_download_only_sets_the_flag():
    """A running download must unwind through its own error path rather than
    being marked done behind its back."""
    _reset()
    did = downloader.queue_download('vid2', 'Two', 'https://c')
    downloader._update_progress(did, status='downloading')
    ok, _ = downloader.request_cancel(did)
    assert ok
    entry = state.read_json(downloader.DOWNLOAD_TRACKER_FILE)[did]
    assert entry['cancel_requested'] is True
    assert entry['status'] == 'downloading', 'status changed without the worker'
    assert downloader.is_cancelled(did) is True


def test_cannot_cancel_a_finished_download():
    _reset()
    for status in ('completed', 'error', 'cancelled'):
        did = downloader.queue_download(f'v-{status}', status, 'https://c')
        downloader._update_progress(did, status=status)
        ok, detail = downloader.request_cancel(did)
        assert ok is False, f'{status} was cancellable'
        assert status in detail, detail


def test_cancelling_an_unknown_download_is_reported_not_raised():
    _reset()
    ok, detail = downloader.request_cancel('does-not-exist')
    assert ok is False and 'Unknown' in detail


def test_progress_hook_raises_once_cancelled():
    """The mechanism itself: yt-dlp has no cancel API, so the hook raising is
    what actually stops a download."""
    _reset()
    did = downloader.queue_download('vid3', 'Three', 'https://c')
    downloader._update_progress(did, status='downloading')
    hook = downloader._progress_hook(did)

    # Before cancelling, the hook just records progress.
    hook({'status': 'downloading', 'downloaded_bytes': 10, 'total_bytes': 100})
    assert state.read_json(downloader.DOWNLOAD_TRACKER_FILE)[did]['progress'] == 10.0

    downloader.request_cancel(did)
    try:
        hook({'status': 'downloading', 'downloaded_bytes': 20, 'total_bytes': 100})
    except downloader.DownloadCancelled:
        pass
    else:
        raise AssertionError('hook did not raise after cancellation')


def test_is_cancelled_is_false_for_a_normal_download():
    _reset()
    did = downloader.queue_download('vid4', 'Four', 'https://c')
    assert downloader.is_cancelled(did) is False
    assert downloader.is_cancelled('nonexistent') is False


# ------------------------------------------- search probe bounds (v1.10.1)
#
# Imported inside the tests rather than at module scope, so a failed import here
# doesn't take the cancellation tests above down with it.

def _youtube_with_fake_probe(probe):
    """Substitute the quality probe and hand back the module that owns it.

    Patches `youtube`, not `app`. These probes moved to youtube.py in v1.11.0, and
    _enrich_video_qualities resolves get_video_formats_info against its OWN
    module globals — so patching app.get_video_formats_info silently stopped
    having any effect while the tests still read as though it did. All three of
    these failed on the extraction commit, which is the safety net working:
    patch the real owner, not a re-export that happens to share the name.
    """
    os.environ.setdefault('ADMIN_PASSWORD', 'test-only')
    import youtube
    youtube.get_video_formats_info = probe
    return youtube


def test_a_hanging_quality_probe_cannot_stall_the_search():
    """The v1.10.1 bug. One unbounded probe used to hold the whole HTTP response
    open until the browser gave up and reported "Failed to fetch".

    Proves the timeout is REAL, which is the part that is easy to get wrong:
    `with ThreadPoolExecutor(...)` exits via shutdown(wait=True) and blocks
    until every probe finishes, and Future.cancel() returns False once a task is
    running — so the obvious implementation has a timeout that does nothing at
    all while looking correct. This test fails against that version.
    """
    import time

    def probe(video_id):
        if video_id == 'HANGS':
            time.sleep(20)
            return {'best_quality': 'never'}
        return {'best_quality': '1080p'}

    yt = _youtube_with_fake_probe(probe)
    original = yt.PROBE_WALL_CLOCK_TIMEOUT
    yt.PROBE_WALL_CLOCK_TIMEOUT = 2
    try:
        videos = [{'id': 'HANGS'}, {'id': 'ok1'}, {'id': 'ok2'}]
        started = time.time()
        unresolved = yt._enrich_video_qualities(videos)
        elapsed = time.time() - started
    finally:
        yt.PROBE_WALL_CLOCK_TIMEOUT = original

    assert elapsed < 8, (
        f'took {elapsed:.1f}s for a 2s timeout — the hang was not contained, '
        'which means the executor is being waited on')
    assert unresolved == 1, unresolved
    assert videos[0]['best_quality'] == 'unknown', videos[0]
    # The healthy ones must still carry real labels; degrading everything
    # because one probe was slow would be its own bug.
    assert videos[1]['best_quality'] == '1080p', videos[1]
    assert videos[2]['best_quality'] == '1080p', videos[2]


def test_a_raising_quality_probe_does_not_fail_the_search():
    def probe(video_id):
        raise RuntimeError('YouTube said no')

    yt = _youtube_with_fake_probe(probe)
    videos = [{'id': 'a'}, {'id': 'b'}]
    unresolved = yt._enrich_video_qualities(videos)
    assert unresolved == 2, unresolved
    assert all(v['best_quality'] == 'unknown' for v in videos), videos


def test_already_enriched_videos_are_not_reprobed():
    """The page dicts are the cached objects, so re-probing them on every
    "Load More" is exactly the waste the cache exists to prevent."""
    calls = []

    def probe(video_id):
        calls.append(video_id)
        return {'best_quality': '720p'}

    yt = _youtube_with_fake_probe(probe)
    videos = [{'id': 'cached', 'best_quality': '4K'}, {'id': 'fresh'}]
    yt._enrich_video_qualities(videos)
    assert calls == ['fresh'], calls
    assert videos[0]['best_quality'] == '4K', 'an existing label was overwritten'


def test_every_metadata_probe_is_bounded():
    """yt-dlp defaults to no socket deadline and 10 retries with backoff. That is
    right for a download and wrong for anything a browser waits on."""
    yt = _youtube_with_fake_probe(lambda vid: {'best_quality': '1080p'})
    opts = yt._probe_opts()
    assert opts['socket_timeout'] > 0, opts
    assert 0 < opts['retries'] <= 3, opts
    assert 0 <= opts['extractor_retries'] <= 2, opts
    # Extra keys must not be able to drop the bounds.
    merged = yt._probe_opts(extract_flat=True, dump_single_json=True)
    for key in ('socket_timeout', 'retries', 'extractor_retries'):
        assert merged[key] == opts[key], f'{key} lost when extra options passed'


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
        code = main()
    finally:
        shutil.rmtree(_WORK, ignore_errors=True)
    sys.exit(code)
