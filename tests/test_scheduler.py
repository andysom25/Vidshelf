"""Tests for scheduler.py and retention.py — the unattended machinery.

    python tests/test_scheduler.py

No network, no Flask, no real yt-dlp: ChannelMonitor takes its collaborators as
callables precisely so this can drive it with fakes.

The two behaviours most worth pinning are the ones that would be expensive to
discover in production:

- A scheduled tick must never re-download something already downloaded. The
  manual endpoint treats mode 'all' as "fetch regardless of history", which is
  fine by hand and would re-download forever on a timer.
- Retention must refuse to sweep a media root that looks unmounted, rather than
  reporting a clean run. This project has a documented incident where a network
  path silently resolved to a small local decoy volume.
"""

import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import retention  # noqa: E402
import scheduler  # noqa: E402


# --------------------------------------------------------------------- helpers
def make_monitor(channels, videos_by_channel, downloaded=(), fail_on=()):
    """Build a ChannelMonitor over fakes. Returns (monitor, started_list)."""
    started = []
    downloaded = set(downloaded)

    def load_config():
        return {'channels': channels, 'channel_monitor': {'enabled': True,
                                                          'interval_minutes': 60,
                                                          'max_per_channel': 5}}

    def list_videos(url):
        if url in fail_on:
            raise RuntimeError('channel unreachable')
        return videos_by_channel.get(url, [])

    def is_downloaded(vid, url):
        return (vid, url) in downloaded

    def start_download(video, channel):
        started.append((video['id'], channel['url']))
        return f"dl-{video['id']}"

    mon = scheduler.ChannelMonitor(load_config, list_videos, is_downloaded, start_download)
    return mon, started


CH_NEW = {'url': 'https://youtube.com/@a', 'download_mode': 'new',
          'download_path': './d', 'plex_media_path': './p'}
CH_ALL = {'url': 'https://youtube.com/@b', 'download_mode': 'all',
          'download_path': './d', 'plex_media_path': './p'}
CH_MANUAL = {'url': 'https://youtube.com/@c', 'download_mode': 'manual',
             'download_path': './d', 'plex_media_path': './p'}


# ------------------------------------------------------------------ scheduler
def test_manual_channels_are_never_touched():
    mon, started = make_monitor(
        [CH_MANUAL], {CH_MANUAL['url']: [{'id': 'v1', 'title': 'One'}]})
    mon.run_once()
    assert started == [], 'a manual channel was downloaded by the scheduler'


def test_new_channel_downloads_only_undownloaded():
    mon, started = make_monitor(
        [CH_NEW],
        {CH_NEW['url']: [{'id': 'v1', 'title': '1'}, {'id': 'v2', 'title': '2'}]},
        downloaded=[('v1', CH_NEW['url'])])
    mon.run_once()
    assert started == [('v2', CH_NEW['url'])], started


def test_mode_all_still_skips_downloaded_on_a_timer():
    """The regression guard for an infinite re-download loop.

    The manual endpoint honours 'all' as "regardless of history". On a schedule
    that would re-fetch the same videos every tick forever.
    """
    mon, started = make_monitor(
        [CH_ALL],
        {CH_ALL['url']: [{'id': 'v1', 'title': '1'}, {'id': 'v2', 'title': '2'}]},
        downloaded=[('v1', CH_ALL['url']), ('v2', CH_ALL['url'])])
    mon.run_once()
    assert started == [], 'scheduler re-downloaded already-downloaded videos'


def test_max_per_channel_is_respected():
    videos = [{'id': f'v{i}', 'title': str(i)} for i in range(20)]
    mon, started = make_monitor([CH_NEW], {CH_NEW['url']: videos})
    mon.run_once()
    assert len(started) == 5, f'expected the default cap of 5, got {len(started)}'


def test_one_broken_channel_does_not_stop_the_others():
    mon, started = make_monitor(
        [CH_NEW, CH_ALL],
        {CH_ALL['url']: [{'id': 'ok', 'title': 'ok'}]},
        fail_on=[CH_NEW['url']])
    results = mon.run_once()
    assert started == [('ok', CH_ALL['url'])], started
    errored = [r for r in results if r['error']]
    assert len(errored) == 1 and 'unreachable' in errored[0]['error']


def test_interval_is_floored_not_rejected():
    mon, _ = make_monitor([], {})
    mon._load_config = lambda: {'channel_monitor': {'enabled': True, 'interval_minutes': 1}}
    _, interval, _ = mon._settings()
    assert interval == scheduler.MIN_INTERVAL_MINUTES, interval


def test_garbage_interval_falls_back_to_default():
    mon, _ = make_monitor([], {})
    mon._load_config = lambda: {'channel_monitor': {'enabled': True,
                                                    'interval_minutes': 'soon'}}
    _, interval, _ = mon._settings()
    assert interval == scheduler.DEFAULT_INTERVAL_MINUTES, interval


def test_status_reports_the_last_tick():
    mon, _ = make_monitor([CH_NEW], {CH_NEW['url']: [{'id': 'v1', 'title': '1'}]})
    mon.run_once()
    st = mon.status()
    assert st['checked_channels'] == 1
    assert st['started_downloads'] == 1
    assert st['total_ticks'] == 1
    assert st['last_run'] is not None


# ------------------------------------------------------------------ retention
def _library(tmp, layout):
    """layout: {artist: n_videos}. Older files get older mtimes."""
    root = os.path.join(tmp, 'media')
    os.makedirs(root, exist_ok=True)
    now = time.time()
    for artist, count in layout.items():
        adir = os.path.join(root, artist)
        os.makedirs(adir, exist_ok=True)
        for i in range(count):
            p = os.path.join(adir, f'{artist} - song {i}.mp4')
            with open(p, 'wb') as fh:
                fh.write(b'x' * 1024)
            os.utime(p, (now - i * 86400, now - i * 86400))  # i=0 newest
        # A sidecar that must never be a deletion candidate.
        with open(os.path.join(adir, 'artist-metadata.json'), 'w') as fh:
            fh.write('{}')
        with open(os.path.join(adir, 'folder.jpg'), 'wb') as fh:
            fh.write(b'jpg')
    return root


def test_plan_keeps_newest_and_only_targets_videos():
    tmp = tempfile.mkdtemp()
    try:
        root = _library(tmp, {'Alpha': 5})
        cfg = {'retention': {'enabled': True, 'keep_last_per_artist': 2}}
        plan = retention.plan(cfg, root=root)
        assert plan['error'] is None, plan['error']
        assert plan['candidate_count'] == 3, plan['candidate_count']
        names = [c['name'] for c in plan['candidates']]
        # Newest two kept: song 0 and song 1.
        assert 'Alpha - song 0.mp4' not in names
        assert 'Alpha - song 1.mp4' not in names
        # Sidecars are never candidates.
        assert not any(n.endswith(('.json', '.jpg')) for n in names), names
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_keep_last_is_floored_at_one():
    tmp = tempfile.mkdtemp()
    try:
        root = _library(tmp, {'Alpha': 3})
        for bad in (0, -5):
            plan = retention.plan({'retention': {'keep_last_per_artist': bad}}, root=root)
            assert plan['keep_last_per_artist'] == 1, bad
            # One file always survives.
            assert plan['candidate_count'] == 2, plan['candidate_count']
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_refuses_a_root_with_no_artist_folders():
    """Guards the documented decoy-volume incident: an empty root almost
    certainly means the media volume isn't mounted, and reporting 'nothing to
    delete' would hide that."""
    tmp = tempfile.mkdtemp()
    try:
        empty = os.path.join(tmp, 'empty')
        os.makedirs(empty)
        plan = retention.plan({'retention': {'enabled': True}}, root=empty)
        assert plan['error'] and 'not mounted' in plan['error'], plan['error']
        assert plan['candidates'] == []
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_refuses_a_missing_root():
    plan = retention.plan({'retention': {'enabled': True}}, root='/definitely/not/here')
    assert plan['error'] and 'does not exist' in plan['error']


def test_apply_refuses_a_plan_that_errored():
    outcome = retention.apply({'error': 'volume not mounted', 'candidates':
                               [{'path': '/tmp/should-not-be-deleted', 'size_bytes': 1}]})
    assert outcome['error'] and 'Refusing' in outcome['error']
    assert outcome['deleted'] == []


def test_apply_deletes_only_the_planned_files():
    tmp = tempfile.mkdtemp()
    try:
        root = _library(tmp, {'Alpha': 4})
        cfg = {'retention': {'enabled': True, 'keep_last_per_artist': 2}}
        plan = retention.plan(cfg, root=root)
        outcome = retention.apply(plan)
        assert outcome['error'] is None
        assert len(outcome['deleted']) == 2, outcome
        remaining = sorted(n for n in os.listdir(os.path.join(root, 'Alpha'))
                           if n.endswith('.mp4'))
        assert remaining == ['Alpha - song 0.mp4', 'Alpha - song 1.mp4'], remaining
        # Sidecars untouched.
        assert os.path.exists(os.path.join(root, 'Alpha', 'artist-metadata.json'))
        assert os.path.exists(os.path.join(root, 'Alpha', 'folder.jpg'))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_sweep_dry_run_never_deletes():
    tmp = tempfile.mkdtemp()
    try:
        root = _library(tmp, {'Alpha': 5})
        before = len(os.listdir(os.path.join(root, 'Alpha')))
        result = retention.sweep({'retention': {'enabled': True,
                                                'keep_last_per_artist': 1}},
                                 root=root, dry_run=True)
        assert result['applied'] is None
        assert result['plan']['candidate_count'] == 4
        assert len(os.listdir(os.path.join(root, 'Alpha'))) == before
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_sweep_respects_the_disabled_flag():
    tmp = tempfile.mkdtemp()
    try:
        root = _library(tmp, {'Alpha': 5})
        result = retention.sweep({'retention': {'enabled': False,
                                                'keep_last_per_artist': 1}},
                                 root=root, dry_run=False)
        assert result['applied']['error'] and 'disabled' in result['applied']['error']
        assert len([n for n in os.listdir(os.path.join(root, 'Alpha'))
                    if n.endswith('.mp4')]) == 5
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


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
    sys.exit(main())
