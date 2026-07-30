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
