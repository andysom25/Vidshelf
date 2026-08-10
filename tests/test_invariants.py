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

import glob
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The modules that together make up the web application. Everything that used to
# live in app.py is in here, so an invariant written against "the app" keeps
# holding after code moves between these files.
#
# WHY THIS EXISTS (v1.11.0). Ten invariants below used to read _read('app.py')
# directly. Splitting app.py into blueprints did not fail any of them — it
# *satisfied* them, silently, because the pattern each one forbids was no longer
# in the single file being read. Five of those ten guard bugs that actually
# shipped (the v1.6.1/v1.7.0 missing auth checks, v1.8.0's dropped cookies,
# v1.10.1's unbounded probe and its inert timeout). A refactor that disarms them
# without a single test going red is precisely the failure REFERENCE.md already
# records for the retention invariant: "defeated by any rename."
#
# Membership is asserted against the filesystem by
# test_the_app_source_list_covers_every_route_module, so a new blueprint cannot
# be added outside the guards' view.
APP_MODULES = ['app.py', 'config_store.py', 'webauth.py', 'tracker.py',
               'youtube.py', 'library.py']

_NON_APP_MODULES = [
    'downloader.py', 'transcode.py', 'artwork_swap.py', 'artwork_sync.py',
    'state.py', 'updates.py', 'scheduler.py', 'retention.py', 'notify.py',
    'titles.py',
]


def _route_modules():
    """Every blueprint module on disk, repo-relative and sorted."""
    found = glob.glob(os.path.join(ROOT, 'routes', '*.py'))
    return sorted(
        os.path.relpath(p, ROOT).replace(os.sep, '/')
        for p in found
        if os.path.basename(p) != '__init__.py'
    )


def _app_sources():
    """APP_MODULES plus every routes/*.py, skipping any that don't exist yet.

    Tolerant of absence on purpose: this list is the target layout, and the
    invariants must pass at every commit of the split rather than only at the
    end. What is NOT tolerated is a route module on disk that isn't covered —
    see test_the_app_source_list_covers_every_route_module.
    """
    names = [n for n in APP_MODULES if os.path.exists(os.path.join(ROOT, n))]
    return names + _route_modules()


def _read(name):
    with open(os.path.join(ROOT, name), encoding='utf-8') as fh:
        return fh.read()


def _read_app_sources():
    """The whole web application as one raw string.

    Replaces _read('app.py') in every invariant that is about the app rather than
    about one particular file.
    """
    return '\n'.join(_read(name) for name in _app_sources())


def _app_code_by_file():
    """[(name, code)] for each app module, comments and docstrings stripped.

    Use this, not _app_code(), for anything that slices a WINDOW of source —
    e.g. "the 2600 characters after `def _enrich_video_qualities`". In a
    concatenated string such a window can run off the end of one file and into
    the next, and then pass on the neighbouring file's text.

    A `# ==== name ====` separator does not solve that: _code_lines() splits each
    line on '#' and drops what's left when it's empty, so comment markers are
    gone by the time a window is taken. Keeping the files apart is the fix.
    """
    return [(name, '\n'.join(_code_lines(_read(name)))) for name in _app_sources()]


def _app_code():
    """Every app module's code as one string, comments and docstrings stripped.

    Fine for substring and count checks over the whole application. Not for
    window slicing — see _app_code_by_file().
    """
    return '\n'.join(code for _, code in _app_code_by_file())


def _find_in_app_code(needle):
    """(name, code, index) for the one app module containing needle.

    Returns (None, '', -1) if absent. Callers that slice a window use this so the
    window is bounded by a single file, which is what stops it running past the
    end of one module and asserting against the next one's text.
    """
    for name, code in _app_code_by_file():
        at = code.find(needle)
        if at != -1:
            return name, code, at
    return None, '', -1


# PY_FILES is every module in the project, and several invariants read all of
# them. Filtered by existence because APP_MODULES describes the target layout of
# the v1.11.0 split: the modules appear one commit at a time, and an invariant
# suite that crashes on a not-yet-created file is useless precisely during the
# refactor it exists to police.
PY_FILES = [n for n in APP_MODULES + _NON_APP_MODULES
            if os.path.exists(os.path.join(ROOT, n))] + _route_modules()


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


def test_no_unescaped_server_data_in_the_dom():
    """v1.6.1 regression guard for stored XSS.

    `d.title` comes from yt-dlp's extracted YouTube title, so it is
    attacker-influenced: downloading a video titled `<img src=x onerror=...>`
    executed script in the admin's authenticated session, from which
    `GET /api/config` returned the Plex token. Nine sites interpolated server
    data into innerHTML without escaping.

    escapeHtml() escapes all five of & < > " ' so it is correct for text and
    quoted-attribute contexts. encodeURIComponent is accepted too, and is the
    right tool inside an inline handler — there, HTML entities are decoded by the
    parser before the JS engine sees them, so escaping alone would not prevent a
    quote breaking out of the JS string.

    The first version of this check required BOTH a receiver from a fixed list
    (`d`, `ch`, `v`, …) and a field from a fixed list of twelve names. That made
    it a naming test, not a safety test: it passed while `ch.download_path`,
    `ch.plex_media_path` and two `${errors}` sites sat unescaped in the same
    file, and a future rename to `dl.title` or `entry.error` would have walked
    straight through it. It also only looked at `${…}`, so nine
    `'…' + e.message + '…'` concatenations into innerHTML were invisible.

    So: any receiver, name patterns rather than exact names, and concatenation
    counts too. Expressions that genuinely cannot be escaped are listed
    individually in REVIEWED_SAFE with the reason, which is the part that has to
    stay short — a long exemption list means the check has stopped being one.
    """
    src = _read(os.path.join('static', 'js', 'dashboard.js'))

    # Names that carry free text, on any receiver. Suffix patterns (_path,
    # _name, _url, _title, _error, _id) matter as much as the bare names —
    # that is what `download_path` and `plex_media_path` needed.
    texty = re.compile(r'''
        (?: \b \w+ \. )?
        \b( title | url | urls | uri | uris | name | names | filename | filenames
          | error | errors | message | messages | artist | artists
          | channel | detail | details | path | paths | root | roots
          | folder | label | text | reason | description | query
          | video_id | id
          | \w+_path | \w+_name | \w+_url | \w+_title | \w+_error | \w+_id
          )\b
    ''', re.VERBOSE)

    # Text sinks are excluded structurally rather than per-expression, because
    # the correct treatment there is the opposite of escaping — see
    # test_text_sinks_are_not_pre_escaped. Blanking the spans keeps the offset
    # arithmetic (and so the reported line numbers) intact.
    def _blank(match):
        return ' ' * len(match.group(0))

    scan = re.sub(r'\.textContent\s*=\s*`[^`]*`', _blank, src)
    scan = re.sub(r'\b(?:showToast|showConfirmModal)\s*\(\s*`[^`]*`', _blank, scan)
    scan = re.sub(r"\b(?:showToast|showConfirmModal)\s*\(\s*'(?:[^'\\]|\\.)*'", _blank, scan)

    # Each of these is safe by construction, not by naming.
    REVIEWED_SAFE = {
        # Ternaries whose branches are both literals we wrote.
        "d.status === 'error' ? '#dc3545' : '#e94560'",
        "a.has_artwork ? '' : ' <span class=\\\"artist-row-count\\\">(no artwork)</span>'",
        # Pre-built HTML fragments; their own inputs are escaped where they are
        # assembled, and escaping them again would render the markup as text.
        "d.status === 'error' ? errorMsg : ''",
        'detail',
        'errors',
        # Numeric.
        '(p.roots || []).length',
        # `fn` and `label` are literals passed by downloadActions' own call
        # sites ('cancelDownload', '⃠ Cancel'), never server data.
        'fn',
        'label',
    }

    offenders = []

    def _check(expr, pos, why):
        expr = expr.strip()
        if 'escapeHtml' in expr or 'encodeURIComponent' in expr:
            return
        if expr in REVIEWED_SAFE:
            return
        if texty.search(expr):
            line = src[:pos].count('\n') + 1
            offenders.append(f'dashboard.js:{line}: {why} {expr[:70]}')

    # Template-literal interpolation.
    for match in re.finditer(r'\$\{([^{}]{1,200})\}', scan):
        _check(match.group(1), match.start(), '${...}')

    # String concatenation into a DOM sink -- `'<div>' + e.message + '</div>'`.
    for match in re.finditer(r"\+\s*([A-Za-z_$][\w.$]*(?:\([^()]*\))?)\s*\+", scan):
        _check(match.group(1), match.start(), 'concat')

    assert not offenders, (
        'Server-supplied data reaching the DOM without escaping. Wrap it in '
        'escapeHtml() — or encodeURIComponent() if it lands inside an inline '
        'onclick/onchange handler. If it is genuinely safe by construction, add '
        'it to REVIEWED_SAFE with the reason.\n  ' + '\n  '.join(offenders))


def test_text_sinks_are_not_pre_escaped():
    """The mirror image of the check above, and a real v1.6.1 bug.

    showToast() and showConfirmModal() assign `textContent`, which neutralises
    markup on its own. Passing escapeHtml() output to them does not add safety —
    it renders the entities literally, so "Guns N' Roses" reached the user as
    "Guns N&#39; Roses". Escaping is per-sink, and these two are text sinks.
    """
    src = _read(os.path.join('static', 'js', 'dashboard.js'))
    offenders = []
    for match in re.finditer(r'\b(showToast|showConfirmModal)\s*\(', src):
        # Scan to the end of that statement -- far enough to cover the message
        # argument without needing a JS parser.
        chunk = src[match.end():match.end() + 400]
        chunk = chunk.split('\n\n')[0]
        if 'escapeHtml' in chunk.split(');')[0]:
            line = src[:match.start()].count('\n') + 1
            offenders.append(f'dashboard.js:{line}: {match.group(1)}(... escapeHtml ...)')

    assert not offenders, (
        'escapeHtml() passed to a textContent sink. textContent already makes '
        'markup inert; escaping first means the user sees &amp; and &#39; in '
        'artist names.\n  ' + '\n  '.join(offenders))


def test_encoded_handler_arguments_are_decoded_by_their_handler():
    """encodeURIComponent in an inline handler is half of a pair.

    Without the matching decodeURIComponent the value arrives percent-encoded,
    so the fetch targets a channel URL or download id that does not exist. That
    fails *quietly* — the request 200s against nothing, or 404s into a toast —
    which is a worse failure mode than the XSS it was added to prevent, and no
    functional test in this repo exercises those click paths.
    """
    src = _read(os.path.join('static', 'js', 'dashboard.js'))

    # onclick="someHandler('${encodeURIComponent(x)}'...  /  onchange="..."
    # Counted, not just detected: selectPlexServer takes two encoded arguments,
    # and a body that decodes only one of them still contains the word
    # decodeURIComponent. A presence check passes that; this does not.
    called = {}
    for match in re.finditer(
            r'on(?:click|change)="(\w+)\((?:[^"]*?\$\{encodeURIComponent\()[^"]*"', src):
        name = match.group(1)
        n = match.group(0).count('encodeURIComponent(')
        called[name] = max(called.get(name, 0), n)
    assert called, 'no encoded inline-handler call sites found — did the pattern change?'

    # downloadActions() builds its buttons through a `btn(label, fn, danger)`
    # helper, so the handler name reaches the DOM as `onclick="${fn}(...)"` — not
    # a literal, and invisible to the pattern above. That indirection is exactly
    # how cancelDownload/retryDownload slipped past the first version of this
    # check: it reported a clean pass with both decodes deleted.
    if re.search(r'on(?:click|change)="\$\{\w+\}\(\s*\'\$\{encodeURIComponent\(', src):
        indirect = set(re.findall(r"btn\(\s*'[^']*',\s*'(\w+)'", src))
        assert indirect, \
            'indirect handler call site found but its handler names could not be resolved'
        for name in indirect:
            called.setdefault(name, 1)

    offenders = []
    for name, encoded in sorted(called.items()):
        match = re.search(r'(?:async\s+)?function\s+' + name + r'\s*\([^)]*\)\s*\{', src)
        if not match:
            offenders.append(f'{name}: called with an encoded argument but not defined here')
            continue
        # The declaration through to the next top-level function is body enough.
        nxt = re.search(r'\n\s*(?:async\s+)?function\s', src[match.end():])
        body = src[match.end(): match.end() + (nxt.start() if nxt else 4000)]
        decoded = body.count('decodeURIComponent(')
        if decoded < encoded:
            line = src[:match.start()].count('\n') + 1
            offenders.append(
                f'dashboard.js:{line}: {name}() receives {encoded} encoded '
                f'argument(s) but decodes {decoded}')

    assert not offenders, (
        'encodeURIComponent at a call site without a matching decodeURIComponent '
        'in the handler. Both halves, for every argument, or neither.\n  '
        + '\n  '.join(offenders))


def test_every_download_call_site_passes_download_options():
    """v1.8.0 regression guard, and the bug that motivated the release.

    `_download_options()` supplies two things: the resolved quality cap and the
    cookies file. One of the five `download_video(...)` call sites was missing
    it — the music-video path — so every age-restricted music video failed with
    a bare yt-dlp error and no indication why, and the quality cap silently did
    not apply. It went unnoticed because the other four were correct and nothing
    compared them.

    Fails loudly rather than adding a default inside download_video(): a default
    there would resolve config deep in a worker thread and hide exactly this
    class of omission again.
    """
    src = _read_app_sources()
    # Each call spans several lines; look at a window after the opening paren.
    offenders = []
    for match in re.finditer(r'\bdownload_video\(', src):
        window = src[match.start(): match.start() + 400]
        # Cut the window at the end of the statement to avoid bleeding into the
        # next one and passing on its options.
        depth, end = 0, len(window)
        for idx, ch in enumerate(window):
            if ch == '(':
                depth += 1
            elif ch == ')':
                depth -= 1
                if depth == 0:
                    end = idx
                    break
        if '_download_options' not in window[:end]:
            line = src[:match.start()].count('\n') + 1
            offenders.append(f'app.py:{line}: download_video(...) without _download_options')

    assert not offenders, (
        'A download_video() call site is not passing _download_options(), so it '
        'gets no cookies (age-restricted videos fail) and no quality cap.\n  '
        + '\n  '.join(offenders))


def test_there_is_one_music_root_setting():
    """v1.8.0 folded two config keys into one.

    `music_video_plex_path` was editable in Settings, persisted, seeded into
    config.json.example and documented in the README — and read by nothing in
    any download path, which hardcoded its destination instead. A setting that
    lies is worse than no setting.

    Guards both halves: the key must not come back, and the destination must not
    be hardcoded again.
    """
    # The *response* key `'music_video_plex_path'` and the request field
    # `data.get('music_video_plex_path')` are deliberately unchanged, so the
    # Settings page and any existing script keep working. What must not come
    # back is reading or writing it on a config document.
    config_access = re.compile(
        r"""(?:config|cfg|doc|merged|current)\s*(?:\[\s*|\.get\(\s*|\.pop\(\s*|
            \.setdefault\(\s*)['"]music_video_plex_path['"]""",
        re.VERBOSE)
    for name in ('app.py', 'artwork_sync.py', 'retention.py', 'scheduler.py'):
        for line in _code_lines(_read(name)):
            assert not config_access.search(line), (
                f'{name}: music_video_plex_path is being read from or written to '
                'config again — artwork_sync.root_path is the single source of '
                f'truth. {line.strip()}')

    app_src = _read_app_sources()
    literals = [ln.strip() for ln in _code_lines(app_src)
                if "'/app/music_videos_final'" in ln
                and 'DEFAULT_MUSIC_ROOT' not in ln]
    assert not literals, (
        'The music root is hardcoded again instead of going through '
        '_music_root(). That is how the download path came to ignore the '
        'configured value.\n  ' + '\n  '.join(literals))


def test_api_routes_are_guarded_by_the_decorator_not_by_hand():
    """v1.8.0 replaced 58 hand-written session checks with @require_auth.

    Two consecutive releases were, in part, "an endpoint was missing the
    check" — and v1.7.0 found one that ran its parameter validation first, so it
    answered anonymous probes with 400 and looked guarded. The decorator makes
    both impossible: you cannot forget half of it, and it cannot run after
    anything else.

    `dashboard()` is the one legitimate exception — it flashes and redirects to
    the login page rather than returning JSON.
    """
    src = _read_app_sources()
    inline = [ln for ln in src.split('\n') if "if 'username' not in session:" in ln]
    # One in dashboard(), one inside require_auth itself — wherever those two
    # now live. Counted across every app module, so relocating a route cannot
    # smuggle a hand-rolled check past this by leaving app.py.
    assert len(inline) <= 2, (
        f'{len(inline)} hand-written session checks found across '
        f'{len(_app_sources())} app modules; expected at most 2 (dashboard() and '
        'require_auth itself). New routes should use @require_auth.')
    assert 'def require_auth(view):' in src, 'the require_auth decorator is gone'
    assert '@functools.wraps(view)' in src, (
        'require_auth must use functools.wraps — Flask derives the endpoint name '
        'from __name__, and tests/test_routes.py keys its allowlist off those.')


def test_copystat_is_never_allowed_to_fail_a_transfer():
    """v1.8.1. CLAUDE.md always called copystat "optional, cosmetic — drop it if
    the server rejects chmod". transcode.py wrapped it in try/except for exactly
    that reason; downloader.py did not, and that asymmetry cost real downloads.

    The container runs as root while the CIFS mount forces uid=1000, and
    utime/chmod on a file you don't own needs CAP_FOWNER, which v1.6.1's
    `cap_drop: ALL` removed. copystat therefore raised PermissionError *after*
    the file was fully copied: the download was reported as failed, the
    `os.remove(src)` on the next line was skipped so local copies leaked (2.6 GB
    of them), and the video was never recorded, so it would download again.

    Nothing in CI has a CIFS mount, so this can only be a source-level rule:
    every copystat call must be guarded.
    """
    offenders = []
    for name in PY_FILES:
        # _code_lines strips comments *and* docstring bodies. Matching raw text
        # instead flagged this rule's own explanation of the bug, which is the
        # same trap the helper was written for in the first place.
        lines = _code_lines(_read(name))
        for i, line in enumerate(lines):
            if 'copystat' not in line:
                continue
            # The guarded helper itself, and any call to it, are the fix.
            if '_copystat_best_effort' in line:
                continue
            window = '\n'.join(lines[max(0, i - 4):i])
            if 'try:' in window:
                continue
            offenders.append(f'{name}: {line.strip()}')

    assert not offenders, (
        'Unguarded shutil.copystat(). It raises PermissionError on the CIFS '
        'mount (root vs uid=1000, no CAP_FOWNER) *after* the copy has already '
        'succeeded — failing a finished transfer over a cosmetic timestamp. '
        'Use downloader._copystat_best_effort() or wrap it in try/except.\n  '
        + '\n  '.join(offenders))


def test_queue_depth_and_reconcile_share_one_status_list():
    """The monitor's queue brake counts in-flight downloads; reconcile clears
    them. If the two lists ever disagreed, a status counted by one and not
    cleared by the other would throttle channel monitoring forever — which is
    the v1.8.1 bug, in a new disguise."""
    app_src = _read_app_sources()
    assert 'downloader_module.IN_FLIGHT_STATUSES' in app_src, (
        '_monitor_queue_depth no longer uses downloader.IN_FLIGHT_STATUSES; it '
        'must not keep its own copy of the status list')
    dl_src = _read('downloader.py')
    assert 'IN_FLIGHT_STATUSES = ' in dl_src, 'IN_FLIGHT_STATUSES is gone'
    assert 'reconcile_interrupted' in dl_src, 'reconcile_interrupted is gone'
    assert 'reconcile_interrupted()' in app_src, (
        'nothing calls reconcile_interrupted() — interrupted downloads will '
        'accumulate and silently disable channel monitoring again')


def test_dashboard_helpers_are_not_defined_twice():
    """v1.8.2. dashboard.js is one flat global scope — four formerly-inline
    <script> blocks concatenated — so two functions with the same name silently
    shadow each other, later definition winning for *every* caller.

    Nearly shipped exactly that: a second `formatBytes` was added ~480 lines
    below a correct one that already existed, which would have quietly changed
    the output of five unrelated call sites. Nothing would have failed; the
    numbers would just have been different.
    """
    src = _read(os.path.join('static', 'js', 'dashboard.js'))
    names = re.findall(r'^\s*(?:async\s+)?function\s+(\w+)\s*\(', src, re.MULTILINE)
    dupes = sorted({n for n in names if names.count(n) > 1})
    assert not dupes, (
        'Duplicate top-level function names in dashboard.js. The file is a '
        'single global scope, so the later definition silently wins for every '
        f'caller of the earlier one: {", ".join(dupes)}')


def test_disk_usage_is_measured_from_the_media_roots():
    """v1.8.2 regression guard, for a number that was wrong in two ways at once.

    It walked `./downloads` (staging, not the library), and the JS then
    formatted the byte count as if it were kilobytes — so every reading was also
    1024x too large. The pair hid each other: staging is usually near-empty, and
    "0.0 KB" looks correct however you divide it.
    """
    src = _read_app_sources()
    assert "os.walk('./downloads')" not in src, (
        'disk usage is walking the staging directory again — it must measure '
        '_gather_media_roots(), which is the actual library')
    assert 'def _library_size(' in src, '_library_size() is gone'
    assert 'retention.VIDEO_EXTENSIONS' in src, (
        '_library_size must reuse retention.VIDEO_EXTENSIONS rather than '
        'defining a third copy of the extension list')

    js = _read(os.path.join('static', 'js', 'dashboard.js'))
    assert "disk.toFixed(1) + ' KB'" not in js, (
        'the inline byte formatter is back. It labels raw bytes as KB, so every '
        'reading is 1024x out; use formatBytes()')
    assert 'formatBytes(statsData.disk_usage)' in js, \
        'the Disk Usage card no longer goes through formatBytes()'


def test_the_sidebar_version_refreshes_with_the_dashboard():
    """v1.8.2. loadVersionBadge() ran once per page load and never again, so a
    tab left open across an upgrade reported the old version indefinitely — next
    to stats that were refreshing correctly — and the "update available" pill
    kept nagging after the update was installed."""
    js = _read(os.path.join('static', 'js', 'dashboard.js'))
    handler = js[js.index("if (page === 'dashboard')"):]
    handler = handler[:handler.index("if (page === 'swap-art')")]
    assert 'loadVersionBadge()' in handler, (
        'the dashboard page-switch handler no longer refreshes the version '
        'badge; a long-lived tab will show a stale version again')


def test_sized_bar_elements_are_not_inline():
    """v1.9.0. A width set on an inline element does nothing.

    The Top artists bars are `<span class="dash-row-fill">` with an inline
    `width: 73.6%`. Spans default to `display: inline`, where width and height
    are ignored — so every bar computed to 0px while carrying a perfectly
    correct percentage, and the panel rendered as a column of identical empty
    tracks with the right numbers beside them. No error, no failing test, and
    the data was fine. It was only visible in a screenshot.

    CLAUDE.md already records one CSS bug that only a screenshot caught (the
    checkbox width rule in Settings). This is the second, so it gets a rule:
    anything that has a width driven by data must say how it displays.
    """
    # Strip CSS comments before matching. Without this the rule passes on its
    # own documentation: the comment explaining why `display: block` matters
    # contains the literal text "display:block", so the check matched the
    # explanation instead of the declaration and stayed green with the
    # declaration deleted. Same shape as the ordering assertion in
    # test_state_files_are_written_owner_only that was vacuously true — a guard
    # is worthless until you have watched it fail.
    css = re.sub(r'/\*.*?\*/', '', _read(os.path.join('static', 'css', 'dashboard.css')),
                 flags=re.DOTALL)
    js = _read(os.path.join('static', 'js', 'dashboard.js'))

    # Classes whose width is set from data at render time.
    sized = set(re.findall(r"class=\"([a-z-]+)\" style=\"width:", js))
    sized |= set(re.findall(r"class='([a-z-]+)' style='width:", js))
    assert sized, 'no data-driven width elements found — did the markup change?'

    offenders = []
    for cls in sorted(sized):
        rule = re.search(r'\.' + re.escape(cls) + r'\s*\{([^}]*)\}', css)
        if not rule:
            offenders.append(f'.{cls}: has an inline width but no CSS rule at all')
            continue
        body = rule.group(1)
        if not re.search(r'display\s*:\s*(block|flex|inline-block|grid)', body):
            offenders.append(
                f'.{cls}: width is set from data but nothing makes it a block — '
                'if this is a <span> the width is silently ignored')

    assert not offenders, (
        'Data-driven width on an element that may render inline:\n  '
        + '\n  '.join(offenders))


def test_the_auto_refreshing_dashboard_never_calls_a_live_yt_dlp_endpoint():
    """v1.9.1. /api/channels resolves each channel's display name with a live
    yt-dlp extraction — measured at 23 seconds for a single channel.

    v1.9.0 added a 60-second dashboard auto-refresh that fetched it. On an
    install with channels that is continuous background load against YouTube,
    and with three channels the work per cycle exceeds the cycle, so it never
    catches up. The user who found the dashboard slow had zero channels, which
    is the only reason it went unnoticed.

    The dashboard needs the channel *count*, which /api/stats already reports.
    Nothing on a timer may call the expensive endpoint.
    """
    js = _read(os.path.join('static', 'js', 'dashboard.js'))

    # The body of loadDashboardStats() — the function the 60s timer calls.
    start = js.index('async function loadDashboardStats(')
    end = js.index('async function loadLibraryPanels(', start)
    body = js[start:end]

    assert "fetch('/api/channels')" not in body, (
        'loadDashboardStats fetches /api/channels, which does a live yt-dlp '
        'lookup per channel. It runs on a 60s timer. Use '
        'statsData.channels_count from /api/stats instead.')
    assert 'channels_count' in body, (
        'the dashboard no longer reads channels_count — it must not go back to '
        'counting via /api/channels')

    # And the lookup itself must stay cached, for the Channels page's sake.
    app_src = _read_app_sources()
    assert '_CHANNEL_NAME_CACHE' in app_src, (
        'the channel-name cache is gone; the Channels page will pay ~23s per '
        'channel on every visit again')


def test_state_files_are_written_owner_only():
    """config.json holds the Plex token, the admin password hash and the session
    signing key, in a bind-mounted directory — 0644 meant any local user on the
    host could read all three.

    Asserted at source level rather than by writing a file and stat-ing it,
    because os.chmod on Windows only toggles the read-only bit: a mode assertion
    would pass on Linux and fail on the machine this is usually developed on. Of
    the four v1.6.1 fixes this was the only one with no regression guard at all,
    which made it the only one that could be deleted silently.
    """
    src = _read('state.py')
    assert 'os.chmod(abs_path, 0o600)' in src, \
        'state.write_json no longer restricts the mode of the file it writes'
    # The chmod must follow the replace: os.replace preserves the source file's
    # mode, so chmod-ing the temp file first would be silently undone.
    #
    # Anchored on the real call and on comment-stripped source. Matching bare
    # 'os.replace' against the raw text looked equivalent and was not — the
    # module docstring mentions os.replace four times before any code does, so
    # the ordering assertion was vacuously true and could not fail.
    code = '\n'.join(_code_lines(src))
    replace_call = '_replace_with_retry(tmp_path, abs_path)'
    assert replace_call in code, \
        'state.write_json no longer replaces via %s — update this test' % replace_call
    assert code.index(replace_call) < code.index('os.chmod(abs_path, 0o600)'), \
        'chmod must come after the replace, which preserves the source mode'


def test_plex_token_is_never_returned_by_the_api():
    """The Plex token is a bearer credential for the user's whole Plex account.

    Both /api/config and /api/plex/config used to return it, so any script in the
    admin session could exfiltrate it with one fetch. The UI only ever needed to
    know whether one exists, which is what token_set reports.
    """
    src = _read_app_sources()
    # Both GET handlers must strip it and expose the boolean instead.
    assert src.count("plex['token_set']") >= 2 or src.count("['token_set']") >= 2, \
        'token_set marker missing — did a GET handler stop stripping the token?'
    assert "plex.pop('token', None)" in src, \
        'the Plex token is no longer being popped before serialisation'
    # And the POST merge must restore it, or round-tripping config disconnects Plex.
    assert "incoming_plex.setdefault('token', current_token)" in src, \
        'POST /api/config no longer preserves the stored Plex token'


def test_container_grants_no_extra_capabilities():
    """SYS_ADMIN was granted for a CIFS mount the container never performs — the
    `cifs` volume driver mounts on the host. Verified: a container with zero
    added capabilities both reads and writes the share. SYS_ADMIN is close to a
    container-escape primitive, so it must not come back."""
    compose = _read('docker-compose.yml')
    for line in _code_lines(compose):
        assert 'SYS_ADMIN' not in line, f'SYS_ADMIN granted again: {line.strip()}'
        assert 'privileged: true' not in line.lower(), f'privileged mode: {line.strip()}'
    assert 'cap_drop' in compose, 'cap_drop: ALL removed from docker-compose.yml'


def test_current_version_has_hand_written_release_notes():
    """CLAUDE.md requires release-notes/vX.Y.Z.md for the version in VERSION.

    CI falls back to --generate-notes when the file is absent, which produces a
    single pull-request link and nothing else. v1.4.1 and v1.5.0 both shipped
    that way. This makes the omission fail on `dev` rather than being discovered
    on the published release, where fixing it means editing the release by hand.
    """
    version = _read('VERSION').strip()
    notes = os.path.join(ROOT, 'release-notes', f'v{version}.md')
    assert os.path.isfile(notes), (
        f'VERSION is {version} but release-notes/v{version}.md is missing. '
        'Write it as part of the release PR — see CLAUDE.md.')
    body = open(notes, encoding='utf-8').read().strip()
    # A stub file would satisfy existence while defeating the point.
    assert len(body) > 200, f'release-notes/v{version}.md looks like a stub ({len(body)} chars)'
    assert '##' in body, (
        f'release-notes/v{version}.md has no section headings — notes should be '
        'grouped for someone upgrading, not a flat commit list.')


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


def test_every_metadata_probe_goes_through_probe_opts():
    """v1.10.1. yt-dlp defaults to no socket deadline and 10 retries with
    backoff, which is right for a download and wrong for anything a browser is
    waiting on: one throttled response held the music-video search open until
    the browser gave up with "Failed to fetch".

    The fix was a single `_probe_opts()` helper carrying the bounds. This is what
    stops the next probe from being added as a bare dict literal — a functional
    test cannot catch it, because an unbounded probe works fine right up until
    the day YouTube is slow.
    """
    code = _app_code()

    # Every YoutubeDL construction in the web app is a metadata probe. Downloads
    # live in downloader.py, which is deliberately NOT in APP_MODULES because it
    # keeps yt-dlp's patient defaults on purpose.
    constructions = code.count('yt_dlp.YoutubeDL(')
    assert constructions >= 4, (
        f'expected at least 4 yt_dlp.YoutubeDL( sites across the app, found '
        f'{constructions} — if probes moved out of APP_MODULES, this invariant '
        'is no longer watching them')

    # No bare options dict may be handed to YoutubeDL. Matching the literal
    # `ydl_opts = {` is the check: every probe should build its options via
    # _probe_opts(...) instead, so the bounds cannot be forgotten.
    offenders = [name for name, mod in _app_code_by_file() if 'ydl_opts = {' in mod]
    assert not offenders, (
        f'{", ".join(offenders)} builds a yt-dlp options dict literally. Use '
        '_probe_opts(...) so socket_timeout and the retry caps are always '
        'present — an unbounded probe can hang an HTTP response indefinitely.')

    helper_calls = code.count('_probe_opts(')
    assert helper_calls >= constructions, (
        f'{constructions} YoutubeDL sites but only {helper_calls} _probe_opts( '
        'references (one is the def) — a probe is not using the helper')

    for key in ('socket_timeout', 'retries', 'extractor_retries'):
        assert key in code, f'PROBE_TIMEOUTS lost its {key!r} bound'


def test_the_search_never_waits_on_its_own_thread_pool():
    """The subtle half of the same fix.

    `with ThreadPoolExecutor(...)` exits via shutdown(wait=True), which blocks
    until every submitted probe finishes, and Future.cancel() returns False once
    a task is running. Written that way, the wall-clock timeout is inert: the
    endpoint still hangs for as long as the slowest probe while looking as though
    it is bounded. tests/test_downloads.py proves the behaviour; this catches the
    regression at the point someone "tidies up" the executor into a with-block.
    """
    # Located per-file, not in a concatenation of every app module: the window
    # below is a fixed character count, and in a joined string it would happily
    # run off the end of this function's file and start asserting against the
    # next module's text.
    owner, code, start = _find_in_app_code('def _enrich_video_qualities')
    assert owner is not None, '_enrich_video_qualities is gone — has the search changed?'
    body = code[start:start + 2600]

    assert 'with concurrent.futures.ThreadPoolExecutor' not in body, (
        '_enrich_video_qualities uses ThreadPoolExecutor as a context manager. '
        'Its __exit__ calls shutdown(wait=True) and blocks on every probe, so '
        'the timeout stops working. Use an explicit '
        'shutdown(wait=False, cancel_futures=True).')
    assert 'wait=False' in body, (
        '_enrich_video_qualities must shut its pool down with wait=False, or a '
        'hanging probe still holds the response open')


def test_there_is_one_folder_name_sanitiser():
    """v1.11.0 dedupe.

    app._sanitize_folder_name and titles.artist_to_folder were two byte-identical
    implementations of the rule that decides every artist folder name on disk —
    artist_to_folder's docstring even said "mirrors _sanitize_folder_name". Either
    could be edited without the other, and the one place it would show up first is
    _music_retry_destination, i.e. silently in WHERE a retried music video lands.

    Verified equivalent over 3,029 inputs before collapsing them, then collapsed
    onto titles.artist_to_folder. This keeps the second copy from reappearing.
    """
    offenders = []
    for name, code in _app_code_by_file():
        if 'def _sanitize_folder_name(' in code:
            offenders.append(name)
    assert not offenders, (
        f'{", ".join(offenders)} defines _sanitize_folder_name again. '
        'titles.artist_to_folder is the single implementation — a second copy of '
        'this rule can drift, and it decides artist folder names on disk.')

    # And the survivor still exists, or the alias above points at nothing.
    assert 'def artist_to_folder(' in _read('titles.py'), \
        'titles.artist_to_folder is gone — it is the only folder-name sanitiser left'


def test_the_app_source_list_covers_every_route_module():
    """The guard on the guards (v1.11.0).

    Every invariant that says "the app must never do X" reads _app_sources(). If a
    module of the web app is outside that list, X becomes legal there — silently,
    with the whole suite green. That is exactly how splitting app.py would have
    disarmed ten invariants at once, five of them protecting bugs that shipped.

    Two halves:
      - every routes/*.py on disk is picked up (handled by globbing, asserted
        here so the glob itself can't quietly stop matching)
      - every module that registers a Flask route is inside _app_sources()
    """
    covered = set(_app_sources())

    on_disk = set(_route_modules())
    assert on_disk <= covered, (
        f'route modules not covered by _app_sources(): {sorted(on_disk - covered)}')

    # The real check: find anything that looks like a Flask view anywhere in the
    # repo and insist it is covered. A new package (say `api/v2/`) would be
    # invisible to the routes/*.py glob, and this is what notices.
    route_markers = ('@app.route(', '.route(', 'Blueprint(')
    uncovered = []
    for path in glob.glob(os.path.join(ROOT, '**', '*.py'), recursive=True):
        rel = os.path.relpath(path, ROOT).replace(os.sep, '/')
        if rel.startswith(('tests/', '.venv/', 'venv/')) or rel in covered:
            continue
        code = '\n'.join(_code_lines(_read(rel)))
        if any(marker in code for marker in route_markers):
            uncovered.append(rel)
    assert not uncovered, (
        f'{", ".join(uncovered)} registers Flask routes but is not in '
        'APP_MODULES / routes/. Every source-level invariant reads '
        '_app_sources(), so code outside it is unguarded. Add it there.')


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
