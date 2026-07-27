"""Tests for updates.py — version comparison and cache behaviour.

Dependency-free like the state tests:

    python tests/test_updates.py

No network is touched: _fetch_latest is monkeypatched. A test suite that
reaches out to GitHub would be slow, flaky, and would fail in CI's sandbox.
"""

import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_ORIGINAL_CWD = os.getcwd()
_WORK = tempfile.mkdtemp(prefix='vidshelf-updates-test-')
os.environ['VIDSHELF_DATA_DIR'] = os.path.join(_WORK, 'data')

import state  # noqa: E402
import updates  # noqa: E402


def test_parse_version():
    assert updates.parse_version('1.2.3') == (1, 2, 3)
    assert updates.parse_version('v1.2.3') == (1, 2, 3)
    assert updates.parse_version('  v10.0.1  ') == (10, 0, 1)
    assert updates.parse_version('1.2.3-beta.1') == (1, 2, 3)
    assert updates.parse_version('unknown') is None
    assert updates.parse_version('') is None
    assert updates.parse_version(None) is None


def test_numeric_not_lexicographic_comparison():
    # The case that a naive string compare gets wrong — and precisely when a
    # user most needs to hear about an update.
    assert updates.is_newer('1.10.0', '1.9.0')
    assert not updates.is_newer('1.9.0', '1.10.0')
    assert updates.is_newer('2.0.0', '1.99.99')


def test_is_newer_basics():
    assert updates.is_newer('1.1.1', '1.1.0')
    assert not updates.is_newer('1.1.0', '1.1.0')
    assert not updates.is_newer('1.0.0', '1.1.0')


def test_unknown_current_version_never_claims_an_update():
    # VERSION missing from the image makes APP_VERSION 'unknown'. Showing
    # "update available" then would be noise the user can't act on.
    assert not updates.is_newer('1.2.0', 'unknown')
    assert not updates.is_newer('1.2.0', None)


def test_disabled_check_reports_nothing_and_makes_no_call():
    calls = []
    original = updates._fetch_latest
    updates._fetch_latest = lambda: calls.append(1) or {'latest': 'v9.9.9'}
    try:
        status = updates.get_status('1.1.0', enabled=False)
    finally:
        updates._fetch_latest = original
    assert status['enabled'] is False
    assert status['update_available'] is False
    assert status['latest'] is None
    assert not calls, 'a disabled check must not contact GitHub'


def test_fresh_cache_is_used_without_refetching():
    state.write_json(updates._cache_path(), {
        'latest': 'v2.0.0',
        'url': 'https://example.invalid/releases/v2.0.0',
        'checked_at': time.time(),
    })
    calls = []
    original = updates._fetch_latest
    updates._fetch_latest = lambda: calls.append(1) or {'latest': 'v3.0.0'}
    try:
        status = updates.get_status('1.1.0', enabled=True)
        time.sleep(0.2)  # a background refresh, if wrongly started, lands here
    finally:
        updates._fetch_latest = original

    assert status['update_available'] is True
    assert status['latest'] == 'v2.0.0'
    assert status['url'] == 'https://example.invalid/releases/v2.0.0'
    assert not calls, 'a fresh cache must not trigger a refetch'


def test_stale_cache_refreshes_in_background_without_blocking():
    state.write_json(updates._cache_path(), {
        'latest': 'v1.0.0',
        'checked_at': time.time() - (updates.CACHE_TTL_SECONDS + 60),
    })
    original = updates._fetch_latest

    def slow_fetch():
        time.sleep(0.3)
        return {'latest': 'v5.0.0', 'url': 'https://example.invalid/5'}

    updates._fetch_latest = slow_fetch
    try:
        started = time.time()
        status = updates.get_status('1.1.0', enabled=True)
        elapsed = time.time() - started
        # Must return the *stale* answer immediately rather than waiting.
        assert elapsed < 0.2, f'get_status blocked for {elapsed:.2f}s'
        assert status['latest'] == 'v1.0.0'

        time.sleep(0.6)  # let the background refresh finish
        refreshed = updates.get_status('1.1.0', enabled=True)
        assert refreshed['latest'] == 'v5.0.0', 'background refresh did not land'
    finally:
        updates._fetch_latest = original


def test_failed_fetch_is_cached_and_reports_nothing():
    original = updates._fetch_latest
    updates._fetch_latest = lambda: {'error': 'boom'}
    try:
        updates._refresh()
        status = updates.get_status('1.1.0', enabled=True)
    finally:
        updates._fetch_latest = original
    assert status['update_available'] is False
    assert status['latest'] is None


def test_refresh_flag_is_released_even_if_fetch_raises():
    # If the in-flight flag leaked on an exception, no update check would ever
    # run again for the life of the process.
    original = updates._fetch_latest

    def boom():
        raise RuntimeError('network exploded')

    updates._fetch_latest = boom
    try:
        try:
            updates._refresh()
        except RuntimeError:
            pass
    finally:
        updates._fetch_latest = original
    assert updates._refreshing is False, 'in-flight flag leaked; checks would stop forever'


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    failures = 0
    for test in tests:
        try:
            test()
        except AssertionError as exc:
            failures += 1
            print(f'FAIL  {test.__name__}: {exc}')
        else:
            print(f'ok    {test.__name__}')
    print(f'\n{len(tests) - failures}/{len(tests)} passed')
    return 1 if failures else 0


if __name__ == '__main__':
    try:
        code = main()
    finally:
        os.chdir(_ORIGINAL_CWD)
        shutil.rmtree(_WORK, ignore_errors=True)
    sys.exit(code)
