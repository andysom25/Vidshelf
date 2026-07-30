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
def make_monitor(channels, videos_by_channel, downloaded=(), fail_on=(),
                 monitor_cfg=None, retention_cfg=None, queue_depth=None,
                 free_space=None, retention_fn=None):
    """Build a ChannelMonitor over fakes. Returns (monitor, started_list, counters)."""
    started = []
    downloaded = set(downloaded)
    counters = {'listing_calls': 0, 'downloaded_calls': 0, 'retention_calls': 0}

    def load_config():
        cfg = {'enabled': True, 'interval_minutes': 60, 'max_per_channel': 5}
        cfg.update(monitor_cfg or {})
        out = {'channels': channels, 'channel_monitor': cfg}
        if retention_cfg is not None:
            out['retention'] = retention_cfg
        return out

    def list_videos(url, limit):
        counters['listing_calls'] += 1
        if url in fail_on:
            raise RuntimeError('channel unreachable')
        vids = videos_by_channel.get(url, [])
        return vids[:limit] if limit else vids

    def list_downloaded(url):
        # Counted so a test can prove this is called once per channel rather
        # than once per video — the v1.5.0 performance bug.
        counters['downloaded_calls'] += 1
        return {vid for (vid, u) in downloaded if u == url}

    def start_download(video, channel):
        started.append((video['id'], channel['url']))
        return f"dl-{video['id']}"

    def _retention():
        counters['retention_calls'] += 1
        return (retention_fn or (lambda: {'deleted': [], 'freed_bytes': 0}))()

    mon = scheduler.ChannelMonitor(
        load_config, list_videos, list_downloaded, start_download,
        queue_depth=queue_depth, free_space=free_space,
        run_retention=_retention)
    return mon, started, counters


CH_NEW = {'url': 'https://youtube.com/@a', 'download_mode': 'new',
          'download_path': './d', 'plex_media_path': './p'}
CH_ALL = {'url': 'https://youtube.com/@b', 'download_mode': 'all',
          'download_path': './d', 'plex_media_path': './p'}
CH_MANUAL = {'url': 'https://youtube.com/@c', 'download_mode': 'manual',
             'download_path': './d', 'plex_media_path': './p'}


# ------------------------------------------------------------------ scheduler
def test_manual_channels_are_never_touched():
    mon, started, counters = make_monitor(
        [CH_MANUAL], {CH_MANUAL['url']: [{'id': 'v1', 'title': 'One'}]})
    mon.run_once()
    assert started == [], 'a manual channel was downloaded by the scheduler'


def test_new_channel_downloads_only_undownloaded():
    mon, started, counters = make_monitor(
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
    mon, started, counters = make_monitor(
        [CH_ALL],
        {CH_ALL['url']: [{'id': 'v1', 'title': '1'}, {'id': 'v2', 'title': '2'}]},
        downloaded=[('v1', CH_ALL['url']), ('v2', CH_ALL['url'])])
    mon.run_once()
    assert started == [], 'scheduler re-downloaded already-downloaded videos'


def test_max_per_channel_is_respected():
    videos = [{'id': f'v{i}', 'title': str(i)} for i in range(20)]
    mon, started, counters = make_monitor([CH_NEW], {CH_NEW['url']: videos})
    mon.run_once()
    assert len(started) == 5, f'expected the default cap of 5, got {len(started)}'


def test_one_broken_channel_does_not_stop_the_others():
    mon, started, counters = make_monitor(
        [CH_NEW, CH_ALL],
        {CH_ALL['url']: [{'id': 'ok', 'title': 'ok'}]},
        fail_on=[CH_NEW['url']])
    results = mon.run_once()
    assert started == [('ok', CH_ALL['url'])], started
    errored = [r for r in results if r['error']]
    assert len(errored) == 1 and 'unreachable' in errored[0]['error']


def test_interval_is_floored_not_rejected():
    mon, _, _ = make_monitor([], {})
    mon._load_config = lambda: {'channel_monitor': {'enabled': True, 'interval_minutes': 1}}
    interval = mon._settings()['interval_minutes']
    assert interval == scheduler.MIN_INTERVAL_MINUTES, interval


def test_garbage_interval_falls_back_to_default():
    mon, _, _ = make_monitor([], {})
    mon._load_config = lambda: {'channel_monitor': {'enabled': True,
                                                    'interval_minutes': 'soon'}}
    interval = mon._settings()['interval_minutes']
    assert interval == scheduler.DEFAULT_INTERVAL_MINUTES, interval


def test_status_reports_the_last_tick():
    mon, _, _ = make_monitor([CH_NEW], {CH_NEW['url']: [{'id': 'v1', 'title': '1'}]})
    mon.run_once()
    st = mon.status()
    assert st['checked_channels'] == 1
    assert st['started_downloads'] == 1
    assert st['total_ticks'] == 1
    assert st['last_run'] is not None


# ------------------------------------------------- v1.5.1 gap fixes
def test_downloaded_set_is_read_once_per_channel_not_per_video():
    """The v1.5.0 performance bug.

    is_video_downloaded() re-read and re-parsed the whole tracker file per call;
    measured at 500 file reads for a 500-video channel, per channel, per tick.
    """
    videos = [{'id': f'v{i}', 'title': str(i)} for i in range(200)]
    mon, started, counters = make_monitor(
        [CH_NEW], {CH_NEW['url']: videos},
        monitor_cfg={'max_listing': 200, 'max_per_channel': 1})
    mon.run_once()
    assert counters['downloaded_calls'] == 1, (
        f"tracker read {counters['downloaded_calls']} times for one channel")


def test_listing_is_bounded_by_max_listing():
    """get_channel_videos returns a channel's entire history; the monitor must
    not iterate all of it every tick."""
    videos = [{'id': f'v{i}', 'title': str(i)} for i in range(500)]
    mon, started, _ = make_monitor(
        [CH_NEW], {CH_NEW['url']: videos},
        monitor_cfg={'max_listing': 10, 'max_per_channel': 100})
    mon.run_once()
    assert len(started) == 10, f'expected the listing cap of 10, got {len(started)}'


def test_deep_download_queue_skips_the_whole_tick():
    """A 2-worker pool with an unbounded queue will accept more than it can
    finish; nothing else stops an hourly tick from adding to it."""
    mon, started, counters = make_monitor(
        [CH_NEW], {CH_NEW['url']: [{'id': 'v1', 'title': '1'}]},
        monitor_cfg={'max_queue_depth': 5},
        queue_depth=lambda: 5)
    mon.run_once()
    assert started == [], 'queued work despite a full queue'
    # Nothing should even be listed — each listing is a yt-dlp call.
    assert counters['listing_calls'] == 0, 'fetched listings despite skipping'
    assert 'already queued' in (mon.status()['last_skip_reason'] or '')


def test_shallow_queue_does_not_skip():
    mon, started, _ = make_monitor(
        [CH_NEW], {CH_NEW['url']: [{'id': 'v1', 'title': '1'}]},
        monitor_cfg={'max_queue_depth': 5},
        queue_depth=lambda: 4)
    mon.run_once()
    assert started == [('v1', CH_NEW['url'])]


def test_low_free_space_skips_that_channel():
    mon, started, _ = make_monitor(
        [CH_NEW], {CH_NEW['url']: [{'id': 'v1', 'title': '1'}]},
        monitor_cfg={'min_free_gb': 100},
        free_space=lambda p: 50 * (1024 ** 3))
    results = mon.run_once()
    assert started == [], 'downloaded with the disk nearly full'
    assert 'free' in (results[0]['skipped'] or ''), results


def test_free_space_check_is_off_when_floor_is_zero():
    """Default behaviour must not change for anyone who hasn't set a floor."""
    mon, started, _ = make_monitor(
        [CH_NEW], {CH_NEW['url']: [{'id': 'v1', 'title': '1'}]},
        monitor_cfg={'min_free_gb': 0},
        free_space=lambda p: 1)
    mon.run_once()
    assert started == [('v1', CH_NEW['url'])]


def test_unknown_free_space_does_not_block_downloads():
    """A destination we can't stat (a UNC path the container can't see) must not
    silently stop all downloading."""
    mon, started, _ = make_monitor(
        [CH_NEW], {CH_NEW['url']: [{'id': 'v1', 'title': '1'}]},
        monitor_cfg={'min_free_gb': 100},
        free_space=lambda p: None)
    mon.run_once()
    assert started == [('v1', CH_NEW['url'])]


def test_failing_channel_backs_off_exponentially():
    """A permanently dead channel must stop erroring — and notifying — every tick."""
    mon, _, counters = make_monitor([CH_NEW], {}, fail_on=[CH_NEW['url']])
    # Tick 1 attempts and fails.
    mon.run_once()
    assert counters['listing_calls'] == 1
    # Tick 2 is skipped by backoff.
    results = mon.run_once()
    assert counters['listing_calls'] == 1, 'retried immediately despite backoff'
    assert 'backing off' in (results[0]['skipped'] or '')


def test_backoff_clears_after_a_success():
    videos = {CH_NEW['url']: [{'id': 'v1', 'title': '1'}]}
    mon, started, counters = make_monitor([CH_NEW], videos, fail_on=[CH_NEW['url']])
    mon.run_once()                      # fail -> backoff
    mon._list_videos = lambda url, limit: videos[url]   # channel recovers
    mon.run_once(ignore_backoff=True)   # forced attempt succeeds
    assert started == [('v1', CH_NEW['url'])]
    assert mon.status()['backoff'] == {}, 'backoff not cleared after success'


def test_retention_runs_after_a_tick_only_when_opted_in():
    """v1.5.0 shipped retention as a manual button only, so unattended
    monitoring had no bound on disk use at all."""
    videos = {CH_NEW['url']: [{'id': 'v1', 'title': '1'}]}

    # enabled but auto_sweep off -> no sweep
    mon, _, counters = make_monitor([CH_NEW], videos,
                                    retention_cfg={'enabled': True, 'auto_sweep': False})
    mon.run_once()
    assert counters['retention_calls'] == 0

    # both on -> sweep
    mon, _, counters = make_monitor([CH_NEW], videos,
                                    retention_cfg={'enabled': True, 'auto_sweep': True})
    mon.run_once()
    assert counters['retention_calls'] == 1

    # auto_sweep on but retention disabled -> no sweep
    mon, _, counters = make_monitor([CH_NEW], videos,
                                   retention_cfg={'enabled': False, 'auto_sweep': True})
    mon.run_once()
    assert counters['retention_calls'] == 0


def test_a_failing_retention_sweep_does_not_break_the_tick():
    def boom():
        raise RuntimeError('volume vanished')
    mon, started, _ = make_monitor(
        [CH_NEW], {CH_NEW['url']: [{'id': 'v1', 'title': '1'}]},
        retention_cfg={'enabled': True, 'auto_sweep': True},
        retention_fn=boom)
    mon.run_once()
    assert started == [('v1', CH_NEW['url'])], 'downloads lost to a retention error'
    assert 'volume vanished' in str(mon.status()['last_retention'])


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
    """Asserts the behaviour (refusal) rather than the exact wording."""
    plan = retention.plan({'retention': {'enabled': True}}, root='/definitely/not/here')
    assert plan['error'], 'a missing root was not refused'
    assert plan['candidates'] == []
    assert plan['roots'][0]['error'], 'per-root error not recorded'


# ---------------------------------------- v1.5.1: every media root, not just one
def test_sweeps_every_supplied_root():
    """v1.5.0 swept only the music-video root, so channel downloads — which land
    under plex_base_path — were never pruned."""
    tmp = tempfile.mkdtemp()
    try:
        root_a = _library(tmp, {'Alpha': 4})
        root_b = os.path.join(tmp, 'media2')
        os.makedirs(root_b)
        os.makedirs(os.path.join(root_b, 'Beta'))
        now = time.time()
        for i in range(4):
            p = os.path.join(root_b, 'Beta', f'Beta - song {i}.mp4')
            with open(p, 'wb') as fh:
                fh.write(b'y' * 2048)
            os.utime(p, (now - i * 86400, now - i * 86400))

        cfg = {'retention': {'enabled': True, 'keep_last_per_artist': 2}}
        plan = retention.plan(cfg, roots=[root_a, root_b])
        assert plan['error'] is None, plan['error']
        # 2 from each root.
        assert plan['candidate_count'] == 4, plan['candidate_count']
        roots_hit = {c['root'] for c in plan['candidates']}
        assert roots_hit == {os.path.normpath(root_a), os.path.normpath(root_b)}, roots_hit
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_one_unmountable_root_does_not_stop_the_others():
    """One unmounted share must not block pruning a healthy one — otherwise a
    UNC path the container can't see disables retention entirely."""
    tmp = tempfile.mkdtemp()
    try:
        good = _library(tmp, {'Alpha': 4})
        empty = os.path.join(tmp, 'unmounted')
        os.makedirs(empty)
        cfg = {'retention': {'enabled': True, 'keep_last_per_artist': 1}}
        plan = retention.plan(cfg, roots=[empty, good])
        assert plan['error'] is None, plan['error']
        assert plan['candidate_count'] == 3, plan['candidate_count']
        errs = [r for r in plan['roots'] if r['error']]
        assert len(errs) == 1 and 'not mounted' in errs[0]['error']
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_all_roots_unscannable_is_an_overall_error():
    tmp = tempfile.mkdtemp()
    try:
        e1 = os.path.join(tmp, 'a'); os.makedirs(e1)
        e2 = os.path.join(tmp, 'b'); os.makedirs(e2)
        plan = retention.plan({'retention': {'enabled': True}}, roots=[e1, e2])
        assert plan['error'] and 'No media root could be scanned' in plan['error']
        assert plan['candidates'] == []
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_duplicate_roots_are_deduplicated():
    """Overlapping config (a channel path equal to plex_base_path) must not plan
    the same file for deletion twice."""
    tmp = tempfile.mkdtemp()
    try:
        root = _library(tmp, {'Alpha': 4})
        cfg = {'retention': {'enabled': True, 'keep_last_per_artist': 2}}
        plan = retention.plan(cfg, roots=[root, root, root + os.sep])
        assert plan['candidate_count'] == 2, plan['candidate_count']
        assert len(plan['roots']) == 1, plan['roots']
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


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
