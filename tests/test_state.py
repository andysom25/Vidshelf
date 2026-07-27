"""Tests for state.py — the crash-safety and concurrency guarantees.

Deliberately dependency-free (plain asserts, no pytest) so it runs anywhere
Vidshelf itself runs:

    python tests/test_state.py

Each test runs in a fresh temp directory, so it never touches real state.

What's being protected, and why it's worth a test: before v1.1.0,
mark_video_downloaded() did an unlocked read-modify-write on the download
tracker while running on the bounded download pool. Reproducing the old
behaviour with 12 threads x 40 updates loses ~470 of 480 entries — each lost
entry is a video that looks new again and gets re-downloaded. Test 5 is the
regression guard for exactly that.
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_ORIGINAL_CWD = os.getcwd()
_WORK = tempfile.mkdtemp(prefix='vidshelf-test-')
os.chdir(_WORK)

# Seed pre-v1.1.0 state before importing, since migration runs at import.
with open('config.json', 'w') as fh:
    json.dump({'channels': ['a'], '_secret_key': 'KEEP-ME'}, fh)
with open('downloaded_videos.json', 'w') as fh:
    json.dump({'chan': ['v1']}, fh)
os.makedirs('data', exist_ok=True)
with open(os.path.join('data', 'active_downloads.json'), 'w') as fh:
    json.dump({'already': 'here'}, fh)
with open('active_downloads.json', 'w') as fh:
    json.dump({'legacy': 'loser'}, fh)

os.environ.pop('VIDSHELF_DATA_DIR', None)
import state  # noqa: E402


def test_migration_moves_state_without_losing_anything():
    cfg = state.read_json(state.CONFIG_FILE)
    # The secret key matters most: lose it and every session is invalidated,
    # which is why migration has to happen before app.py reads it at import.
    assert cfg.get('_secret_key') == 'KEEP-ME', f'secret key lost: {cfg}'
    assert state.read_json(state.TRACKER_FILE) == {'chan': ['v1']}
    # A file already at the destination must win over the legacy copy.
    assert state.read_json(state.ACTIVE_DOWNLOADS_FILE) == {'already': 'here'}
    assert not os.path.exists('config.json'), 'legacy file left behind'


def test_migration_is_idempotent():
    assert state.migrate_legacy_state() == []


def test_directory_in_place_of_state_file_reads_empty():
    # Docker creates a missing single-file bind-mount source as a directory.
    # open() then raises IsADirectoryError (an OSError, not FileNotFoundError),
    # which used to crash-loop the app at import on every fresh install.
    os.makedirs(os.path.join('data', 'bogus.json'), exist_ok=True)
    assert state.read_json(os.path.join('data', 'bogus.json')) == {}


def test_readers_never_see_a_partial_write():
    target = os.path.join('data', 'atomic.json')
    big = {'k%d' % i: 'x' * 200 for i in range(400)}
    full = len(big) + 1
    state.write_json(target, {**big, 'n': -1})

    stop = threading.Event()
    torn, done, errors = [], [], []

    def reader():
        while not stop.is_set():
            got = state.read_json(target)
            if len(got) != full:
                torn.append(len(got))

    def writer():
        # A writer that dies early produces zero torn reads, which would look
        # like a pass — so its failure has to be asserted on explicitly.
        try:
            for n in range(150):
                big['n'] = n
                state.write_json(target, big)
                done.append(n)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    readers = [threading.Thread(target=reader) for _ in range(4)]
    for t in readers:
        t.start()
    w = threading.Thread(target=writer)
    w.start()
    w.join()
    stop.set()
    for t in readers:
        t.join()

    assert not errors, f'writer died: {errors[0]!r}'
    assert len(done) == 150, f'only {len(done)}/150 writes completed'
    assert not torn, f'{len(torn)} partial reads observed'


def test_writes_survive_an_unsynchronised_external_reader():
    # Someone tailing config.json, or an antivirus scanner. On Windows
    # os.replace() fails while the destination is open by anyone, which is
    # what _replace_with_retry() absorbs. A no-op on POSIX.
    target = os.path.join('data', 'external.json')
    state.write_json(target, {'n': -1})
    stop = threading.Event()
    errors = []

    def external_reader():
        while not stop.is_set():
            try:
                with open(target) as fh:
                    fh.read()
            except OSError:
                pass
            time.sleep(0.005)

    reader = threading.Thread(target=external_reader)
    reader.start()
    try:
        for n in range(60):
            state.write_json(target, {'n': n, 'pad': 'y' * 500})
    except BaseException as exc:  # noqa: BLE001
        errors.append(exc)
    finally:
        stop.set()
        reader.join()

    assert not errors, f'write failed against an external reader: {errors[0]!r}'


def test_concurrent_updates_do_not_lose_entries():
    """The v1.1.0 regression guard — see this module's docstring."""
    tracker = os.path.join('data', 'race.json')
    state.write_json(tracker, {})
    n_threads, per_thread = 12, 40

    def marker(tid):
        for i in range(per_thread):
            def add(data, tid=tid, i=i):
                data.setdefault('chan', [])
                data['chan'].append(f'{tid}-{i}')
            state.update_json(tracker, add)

    threads = [threading.Thread(target=marker, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    got = state.read_json(tracker)['chan']
    expected = n_threads * per_thread
    assert len(got) == expected, f'lost updates: {len(got)}/{expected} survived'
    assert len(set(got)) == expected, 'duplicate entries'


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
