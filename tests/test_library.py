"""The library scan behind the v1.9.0 dashboard.

    python tests/test_library.py

One walk of the media roots feeds every panel — totals, the added-over-time
chart, top artists, recently added, artwork health. That consolidation is the
point: the media root is normally a CIFS mount, so each traversal is the
expensive part, and a panel that fetches its own numbers costs another full
walk. The first version of the Plex-health panel did exactly that and made the
dashboard feel slow (0.75s on *every* request against 0.00s cached).

Dates come from st_mtime because the download tracker records video ids and no
timestamps at all. That is accurate for files Vidshelf wrote and wrong for
anything moved by hand on the NAS, which is why the UI says "added" rather than
"downloaded". Real download dates need the v2.0 data model.
"""

import os
import shutil
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_ORIGINAL_CWD = os.getcwd()
_WORK = tempfile.mkdtemp(prefix='vidshelf-library-test-')
os.environ['VIDSHELF_DATA_DIR'] = os.path.join(_WORK, 'data')
os.environ.setdefault('ADMIN_PASSWORD', 'library-test-password')

import app as app_module  # noqa: E402
# The library scan moved to library.py in v1.11.0. These tests patch and read
# module state (_LIBRARY_SCAN_CACHE, _invalidate_library_scan), and that only
# works against the module that owns it — app.py re-exports the names, but
# rebinding a re-export does not change what _library_scan itself sees. So they
# address library directly, and app_module stays only for CONFIG_FILE and the
# tracker write, which really are the app's.
import library as library_module  # noqa: E402
import state  # noqa: E402

_MEDIA = os.path.join(_WORK, 'media')


def _seed(files):
    """files: [(artist_folder, filename, kb, days_ago)]"""
    shutil.rmtree(_MEDIA, ignore_errors=True)
    now = time.time()
    for folder, name, kb, days_ago in files:
        d = os.path.join(_MEDIA, folder) if folder else _MEDIA
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, name)
        with open(path, 'wb') as fh:
            fh.write(b'\0' * (kb * 1024))
        stamp = now - (days_ago * 86400)
        os.utime(path, (stamp, stamp))
    state.write_json(app_module.CONFIG_FILE, {
        'artwork_sync': {'root_path': _MEDIA},
        'plex_base_path': _MEDIA,
        '_secret_key': 'k'}, indent=4)
    library_module._invalidate_library_scan()


DEFAULT = [
    ('Foo_Fighters', 'Foo Fighters - Everlong-aaaaaaaaaaa.mp4', 900, 5),
    ('Foo_Fighters', 'Foo Fighters - Run-bbbbbbbbbbb.mp4', 700, 40),
    ('Nirvana', 'Nirvana - Lithium-ccccccccccc.mp4', 500, 2),
    ('Weezer', 'Weezer - Buddy Holly-eeeeeeeeeee.mkv', 400, 1),
]


def test_totals_count_videos_only():
    """Artwork sits in the same folders and must not inflate the library size —
    it was the Disk Usage card being wrong that started all of this."""
    _seed(DEFAULT + [('Nirvana', 'folder.jpg', 64, 1),
                     ('Nirvana', 'artist-metadata.json', 4, 1)])
    scan = library_module._library_scan(force=True)
    assert scan['videos'] == 4, scan['videos']
    assert scan['bytes'] == (900 + 700 + 500 + 400) * 1024, scan['bytes']
    assert scan['artists'] == 3, scan['artists']


def test_added_30d_counts_only_the_last_30_days():
    _seed(DEFAULT)
    scan = library_module._library_scan(force=True)
    # 5, 2 and 1 days ago are in; 40 days ago is not.
    assert scan['added_30d'] == 3, scan['added_30d']


def test_month_series_is_dense_and_ends_now():
    """A month with no downloads must appear as a zero.

    Sorting the observed months alone would silently close the gap, and a quiet
    spell would read as continuous activity — a chart that lies rather than one
    that is merely sparse.
    """
    scan = library_module._library_scan(force=True)
    months = scan['months']
    assert len(months) == library_module.LIBRARY_HISTORY_MONTHS, len(months)
    keys = [m['month'] for m in months]
    assert keys == sorted(keys), 'series is not chronological'
    assert len(set(keys)) == len(keys), 'series has duplicate months'
    assert keys[-1] == time.strftime('%Y-%m'), 'series does not end at this month'
    # Consecutive: each step is exactly one calendar month.
    for earlier, later in zip(keys, keys[1:]):
        ey, em = (int(x) for x in earlier.split('-'))
        ly, lm = (int(x) for x in later.split('-'))
        assert (ly - ey) * 12 + (lm - em) == 1, f'gap between {earlier} and {later}'


def test_top_artists_ranked_by_size():
    _seed(DEFAULT)
    scan = library_module._library_scan(force=True)
    names = [a['artist'] for a in scan['top_artists']]
    assert names[0] == 'Foo Fighters', names   # 1600 KB across two files
    assert set(names) == {'Foo Fighters', 'Nirvana', 'Weezer'}, names
    sizes = [a['bytes'] for a in scan['top_artists']]
    assert sizes == sorted(sizes, reverse=True), sizes
    # Folder names are converted back to display names.
    assert 'Foo_Fighters' not in names


def test_recent_is_newest_first_with_clean_titles():
    _seed(DEFAULT)
    scan = library_module._library_scan(force=True)
    stamps = [r['added_at'] for r in scan['recent']]
    assert stamps == sorted(stamps, reverse=True), 'recent is not newest-first'
    titles = [r['title'] for r in scan['recent']]
    # The trailing YouTube id is stripped for display.
    assert not any(t.endswith('aaaaaaaaaaa') for t in titles), titles
    assert 'Weezer - Buddy Holly' in titles, titles


def test_artwork_status_rides_along_on_the_same_scan():
    """The Plex-health panel used to call /api/artists/summary, which walks the
    media root again — 0.75s per request, every request, re-deriving numbers
    this scan had just produced. That duplicate traversal was the whole reason
    the dashboard felt slow."""
    _seed(DEFAULT + [('Foo_Fighters', 'folder.jpg', 32, 1)])
    scan = library_module._library_scan(force=True)
    # Nirvana and Weezer have no artwork; Foo Fighters does.
    assert scan['missing_artwork'] == 2, scan['missing_artwork']


def test_cache_is_used_and_invalidated_on_download():
    _seed(DEFAULT)
    first = library_module._library_scan(force=True)
    assert library_module._library_scan() is first, 'a fresh cache should be reused'

    # A completed download must make the next read see the new file. Without
    # this the five-minute TTL means you download something, look at the
    # dashboard, and it isn't there — which reads as a bug, not as caching.
    d = os.path.join(_MEDIA, 'Nirvana')
    with open(os.path.join(d, 'Nirvana - Breed-ddddddddddd.mp4'), 'wb') as fh:
        fh.write(b'\0' * 1024)
    app_module.mark_video_downloaded('ddddddddddd', 'music_video_Nirvana')
    assert library_module._library_scan()['videos'] == 5, 'cache survived a download'


def test_stale_cache_is_served_immediately_and_refreshed_behind_the_request():
    """Whoever arrives after the TTL lapses must not pay for the CIFS walk.

    Measured at 2.1s on a 197-video library, and it was always a *user* request
    that paid it. Mirrors updates.get_status(), which returns what it knows and
    refreshes in the background for the same reason.
    """
    _seed(DEFAULT)
    library_module._library_scan(force=True)
    library_module._LIBRARY_SCAN_CACHE['at'] = 0.0     # force stale

    result = {}

    def call():
        started = time.time()
        scan = library_module._library_scan()
        result['elapsed'] = time.time() - started
        result['videos'] = scan['videos']

    t = threading.Thread(target=call)
    t.start()
    t.join(timeout=5)
    assert not t.is_alive(), (
        'a stale read blocked — _maybe_rescan_async takes the same '
        'non-reentrant lock, so calling it under that lock deadlocks')
    assert result['videos'] == 4, result
    # Served from the stale copy, so effectively instant.
    assert result['elapsed'] < 1.0, result['elapsed']


def test_concurrent_stale_reads_start_only_one_rescan():
    """/api/stats and /api/library/stats are requested together on every
    dashboard load, and the 60s auto-refresh repeats that — without a guard a
    stale cache would kick off a pile of concurrent walks of the same tree."""
    _seed(DEFAULT)
    library_module._library_scan(force=True)
    library_module._LIBRARY_SCAN_CACHE['at'] = 0.0

    threads = [threading.Thread(target=library_module._library_scan) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert not any(t.is_alive() for t in threads), 'a concurrent stale read hung'
    time.sleep(0.5)   # let the single background rescan finish
    assert library_module._library_scan()['videos'] == 4


def test_scan_survives_an_empty_or_missing_root():
    """A fresh install has no media root yet; the dashboard must still render."""
    state.write_json(app_module.CONFIG_FILE, {
        'artwork_sync': {'root_path': os.path.join(_WORK, 'does-not-exist')},
        'plex_base_path': os.path.join(_WORK, 'does-not-exist'),
        '_secret_key': 'k'}, indent=4)
    library_module._invalidate_library_scan()
    scan = library_module._library_scan(force=True)
    assert scan['videos'] == 0 and scan['artists'] == 0, scan
    assert scan['bytes'] == 0
    assert len(scan['months']) == library_module.LIBRARY_HISTORY_MONTHS
    assert scan['top_artists'] == [] and scan['recent'] == []


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
