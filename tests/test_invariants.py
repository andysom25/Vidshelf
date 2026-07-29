"""Source-level invariants for bugs that cost days and would come back silently.

    python tests/test_invariants.py

These assert on the *source text*, which is unusual. The justification is
specific: the bugs below only manifest against a real CIFS-mounted NAS or a
real container restart, so no unit test, route test or CI run reproduces them.
Every one of them is documented in CLAUDE.md and REFERENCE.md because it was
expensive to diagnose — and every one is a single innocent-looking edit away
from returning.

A functional test would be better where one is possible. Where it isn't, an
invariant that fails loudly in CI beats a comment nobody reads at the moment
they're changing the line.
"""

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY_FILES = ['app.py', 'downloader.py', 'transcode.py', 'artwork_swap.py',
            'artwork_sync.py', 'state.py', 'updates.py', 'scheduler.py',
            'retention.py', 'notify.py']


def _read(name):
    with open(os.path.join(ROOT, name), encoding='utf-8') as fh:
        return fh.read()


def _code_lines(source):
    """Lines with comments and docstring bodies crudely stripped.

    The forbidden API names appear legitimately in explanatory comments all over
    this codebase, so matching raw text would fail on the very comments that
    document the rule.
    """
    out = []
    in_doc = False
    doc_delim = None
    for line in source.split('\n'):
        stripped = line.strip()
        if in_doc:
            if doc_delim in stripped:
                in_doc = False
            continue
        if stripped.startswith(('"""', "'''")):
            delim = stripped[:3]
            # A one-line docstring opens and closes on the same line.
            if stripped.count(delim) == 1:
                in_doc, doc_delim = True, delim
            continue
        code = line.split('#', 1)[0]
        if code.strip():
            out.append(code)
    return out


def test_no_shutil_copy_onto_network_mounts():
    """CLAUDE.md gotcha #2.

    shutil.copy/copy2/copyfile use os.sendfile() internally on Linux, which
    raises ENOSPC partway through against CIFS even with terabytes free — and
    only falls back to a read/write loop if the *first* sendfile call fails, so
    it looks fine until it isn't. This cost two debugging sessions and appeared
    fixed twice before it was.
    """
    forbidden = re.compile(r'shutil\.(copy2|copyfile|copy)\s*\(')
    offenders = []
    for name in PY_FILES:
        for i, line in enumerate(_code_lines(_read(name)), 1):
            if forbidden.search(line):
                offenders.append(f'{name}: {line.strip()}')
    assert not offenders, (
        'shutil.copy/copy2/copyfile found in executable code. Use\n'
        '    with open(src,"rb") as a, open(dst,"wb") as b: shutil.copyfileobj(a,b)\n'
        'See CLAUDE.md gotcha #2.\nOffenders:\n  ' + '\n  '.join(offenders))


def test_every_copy_site_uses_copyfileobj():
    """The positive half of the rule above — the safe pattern is still present.

    Guards against a copy site being deleted or refactored into something else
    entirely, which the negative test alone would happily pass.
    """
    expected = ['downloader.py', 'transcode.py', 'artwork_swap.py']
    for name in expected:
        src = _read(name)
        assert 'copyfileobj' in src, (
            f'{name} no longer contains a copyfileobj-based copy. If the copy '
            'moved, update this test; if it became shutil.copy, see CLAUDE.md #2.')


def test_all_json_state_goes_through_state_module():
    """v1.1.0 regression guard.

    A bare open(<state file>, 'w') reintroduces both the torn-write bug (an
    interrupted write truncates config.json, losing channels, the Plex token and
    the session secret) and the lost-update race in the download tracker. Only
    state.py is allowed to open these for writing.
    """
    pattern = re.compile(
        r"""open\(\s*['"]?[^'")]*(config\.json|downloaded_videos\.json|active_downloads\.json)""")
    offenders = []
    for name in PY_FILES:
        if name == 'state.py':
            continue
        for line in _code_lines(_read(name)):
            if pattern.search(line):
                offenders.append(f'{name}: {line.strip()}')
    assert not offenders, (
        'State files opened directly instead of via state.py '
        '(read_json/write_json/update_json):\n  ' + '\n  '.join(offenders))


def test_unbuffered_stdout_is_set_in_the_image():
    """CLAUDE.md gotcha #3.

    Without PYTHONUNBUFFERED, concurrent download threads' print() output
    reaches `docker logs` out of chronological order — a "completed" line can
    precede its own "started" line. That already caused one false report that a
    working download was broken.
    """
    dockerfile = _read('Dockerfile')
    assert 'PYTHONUNBUFFERED=1' in dockerfile, \
        'PYTHONUNBUFFERED=1 missing from the Dockerfile — see CLAUDE.md gotcha #3.'


def test_compose_mounts_the_data_directory_not_individual_files():
    """v1.1.0 regression guard.

    Docker bind-mounts a single file by inode. State writes are atomic (temp
    file + os.replace), which swaps the inode — so per-file mounts silently stop
    propagating writes to the host, and a fresh clone gets *directories* created
    where the gitignored files should be, which crash-looped every new install.
    """
    compose = _read('docker-compose.yml')
    bad = [ln.strip() for ln in compose.split('\n')
           if re.search(r'-\s*\./(config|downloaded_videos|active_downloads)\.json\s*:', ln)]
    assert not bad, ('docker-compose.yml bind-mounts individual state files. '
                     'Mount ./data:/app/data instead.\n  ' + '\n  '.join(bad))
    assert re.search(r'-\s*\./data:/app/data', compose), \
        'docker-compose.yml no longer mounts ./data:/app/data.'


def test_werkzeug_dev_server_is_not_the_default():
    """v1.4.0 guard.

    app.run() served the published image until v1.4.0. Werkzeug's server is
    explicitly not for unattended use; waitress is. FLASK_DEBUG may still opt
    into Werkzeug deliberately, so this only asserts waitress is the default
    path.
    """
    src = _read('app.py')
    assert 'from waitress import serve' in src, 'waitress is no longer used to serve'
    # app.run must be reachable only under the debug branch.
    for i, line in enumerate(src.split('\n')):
        if 'app.run(' in line and not line.strip().startswith('#'):
            context = '\n'.join(src.split('\n')[max(0, i - 12):i])
            assert 'debug_mode' in context, (
                f'app.run() on line {i + 1} is not guarded by the debug_mode branch')


def test_ghcr_image_name_is_lowercase():
    """The repo is andysom25/Vidshelf with a capital V, and GHCR rejects
    uppercase image names with an opaque 'invalid reference format'. Deriving the
    name from ${{ github.repository }} would reintroduce that."""
    ci = _read(os.path.join('.github', 'workflows', 'ci.yml'))
    match = re.search(r'IMAGE_NAME:\s*(\S+)', ci)
    assert match, 'IMAGE_NAME not found in ci.yml'
    name = match.group(1)
    assert name == name.lower(), f'IMAGE_NAME must be lowercase for GHCR: {name}'
    assert 'github.repository' not in name, \
        'IMAGE_NAME must not derive from github.repository — it capitalises the V.'


def test_retention_never_clears_the_download_tracker():
    """Scheduler/retention interaction guard.

    downloaded_videos.json records "we downloaded this", not "the file exists".
    If retention removed tracker entries, the scheduler would re-download every
    pruned video on the next tick, prune it again, and loop forever.
    """
    # Code only: the module docstring names the tracker precisely to explain why
    # it must not be touched, so matching raw text would flag the documentation.
    code = '\n'.join(_code_lines(_read('retention.py')))
    for forbidden in ('TRACKER_FILE', 'downloaded_videos', 'mark_video_downloaded'):
        assert forbidden not in code, (
            f'retention.py has executable code referencing {forbidden!r}. It must '
            'never touch the download tracker — see its module docstring for the '
            'download/delete loop this prevents.')


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    failures = 0
    for t in tests:
        try:
            t()
        except AssertionError as exc:
            failures += 1
            print(f'FAIL  {t.__name__}:\n      {exc}')
        else:
            print(f'ok    {t.__name__}')
    print(f'\n{len(tests) - failures}/{len(tests)} passed')
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
