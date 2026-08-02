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
            'retention.py', 'notify.py', 'titles.py']


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
    src = _read('app.py')
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

    app_src = _read('app.py')
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
    src = _read('app.py')
    inline = [ln for ln in src.split('\n') if "if 'username' not in session:" in ln]
    # One in dashboard(), one inside require_auth itself.
    assert len(inline) <= 2, (
        f'{len(inline)} hand-written session checks found; expected at most 2 '
        '(dashboard() and require_auth itself). New routes should use '
        '@require_auth.')
    assert 'def require_auth(view):' in src, 'the require_auth decorator is gone'
    assert '@functools.wraps(view)' in src, (
        'require_auth must use functools.wraps — Flask derives the endpoint name '
        'from __name__, and tests/test_routes.py keys its allowlist off those.')


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
    src = _read('app.py')
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
