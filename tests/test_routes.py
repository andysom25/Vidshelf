"""Route-level smoke tests: every registered route, exercised via Flask's
test client.

    python tests/test_routes.py

This is not a functional test suite — it does not assert that downloading a
video works. It asserts the things that break silently during a refactor:

- every route is still registered, and none disappeared
- no route 500s on a plain request
- every non-public route rejects an unauthenticated caller instead of
  leaking data or crashing
- the dashboard template still renders, with its static assets wired up

That last one matters because v1.2.1 moved 2,800 lines of CSS/JS out of the
template into static files. A typo in a url_for() there renders a blank page
that still returns HTTP 200, which no status-code check would catch.

These are also the prerequisite for splitting app.py into blueprints: a
50-route rewrite with nothing verifying the routes still exist is exactly how
an endpoint quietly goes missing.
"""

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_ORIGINAL_CWD = os.getcwd()
_WORK = tempfile.mkdtemp(prefix='vidshelf-routes-test-')
os.environ['VIDSHELF_DATA_DIR'] = os.path.join(_WORK, 'data')
os.environ.setdefault('ADMIN_PASSWORD', 'routes-test-password')

import app as app_module  # noqa: E402
import state  # noqa: E402

app_module.app.config['TESTING'] = True

# Reachable without a session. Everything else must reject anonymous callers.
# favicon belongs here deliberately: it serves a static inline SVG, and
# browsers request it on the login page before any session exists.
PUBLIC_ENDPOINTS = {'login', 'logout', 'static', 'favicon'}


def _client(authenticated):
    client = app_module.app.test_client()
    if authenticated:
        with client.session_transaction() as sess:
            sess['username'] = 'admin'
    return client


def _routes():
    """Every GET-able rule with no URL parameters — the ones a smoke test can
    call blind. Parameterised routes are covered by the registration test."""
    for rule in app_module.app.url_map.iter_rules():
        if rule.arguments:
            continue
        if 'GET' not in rule.methods:
            continue
        yield rule


def test_routes_are_registered():
    # A bare count would be brittle; what matters is that the known areas are
    # all still present after any restructuring.
    paths = {str(r) for r in app_module.app.url_map.iter_rules()}
    expected = [
        '/', '/login', '/logout', '/dashboard',
        '/api/stats', '/api/channels', '/api/download',
        '/api/downloads/progress', '/api/downloads/clear',
        '/api/system/health', '/api/system/version',
        '/api/plex/libraries',
    ]
    missing = [p for p in expected if p not in paths]
    assert not missing, f'routes disappeared: {missing}'
    # 53 at the time of writing; a floor rather than an exact count, so
    # adding routes doesn't fail the suite but losing a batch does.
    assert len(paths) >= 50, f'only {len(paths)} routes registered; expected 50+'


def test_no_route_500s_when_authenticated():
    client = _client(authenticated=True)
    failures = []
    for rule in _routes():
        if rule.endpoint in ('logout', 'static'):
            continue  # logout destroys the session the rest of the loop needs
        try:
            resp = client.get(str(rule))
        except Exception as exc:  # noqa: BLE001
            failures.append(f'{rule} raised {type(exc).__name__}: {exc}')
            continue
        if resp.status_code >= 500:
            failures.append(f'{rule} -> HTTP {resp.status_code}')
    assert not failures, 'routes erroring:\n  ' + '\n  '.join(failures)


def test_protected_routes_reject_anonymous_callers():
    client = _client(authenticated=False)
    leaked = []
    for rule in _routes():
        if rule.endpoint in PUBLIC_ENDPOINTS:
            continue
        resp = client.get(str(rule))
        # Either a redirect to login (pages) or 401/403 (APIs). A 200 means
        # the route is serving data to anyone who can reach the port.
        if resp.status_code == 200:
            leaked.append(f'{rule} -> HTTP 200 without a session')
        elif resp.status_code >= 500:
            leaked.append(f'{rule} -> HTTP {resp.status_code} without a session')
    assert not leaked, 'unauthenticated access problems:\n  ' + '\n  '.join(leaked)


def test_login_page_renders_anonymously():
    resp = _client(authenticated=False).get('/login')
    assert resp.status_code == 200
    assert b'password' in resp.get_data().lower()


def test_dashboard_renders_with_its_static_assets():
    """Guards the v1.2.1 CSS/JS extraction.

    A broken url_for() would still return 200 with a blank-looking page, so
    check the tags are actually emitted and the files actually resolve.
    """
    resp = _client(authenticated=True).get('/dashboard')
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    assert '/static/css/dashboard.css' in html, 'stylesheet link missing'
    assert '/static/js/dashboard.js' in html, 'script tag missing'
    # The markup itself must still be there, not just the asset tags.
    for marker in ['sidebar', 'page-dashboard', 'version-current']:
        assert marker in html, f'dashboard markup missing: {marker}'
    # Nothing should have been left inlined by the extraction.
    assert '<style' not in html, 'inline <style> left in the template'


def test_artists_page_has_its_search_and_filter_controls():
    """Guards the wiring, which lives across three files.

    The controls are markup in the template, styling in dashboard.css and
    handlers in dashboard.js. A rename in one and not the others produces a
    filter bar that renders but does nothing — no error, no failing status
    code, just a dead control. The JS logic itself is covered by
    tests/test_artists_filter.js.
    """
    html = _client(authenticated=True).get('/dashboard').get_data(as_text=True)

    for element_id in ['artist-search', 'artist-filter-artwork',
                       'artist-filter-videos', 'artist-sort',
                       'artist-filter-summary', 'artist-search-clear']:
        assert f'id="{element_id}"' in html, f'missing control: {element_id}'

    # Handlers must actually be attached, not just the inputs present.
    assert 'onArtistFilterInput()' in html, 'search input has no handler'
    assert html.count('onchange="renderArtistList()"') >= 3, 'selects not all wired'
    assert 'resetArtistFilters()' in html, 'reset button not wired'

    js = _client(authenticated=True).get('/static/js/dashboard.js').get_data(as_text=True)
    for fn in ['function renderArtistList', 'function filterAndSortArtists',
               'function onArtistFilterInput', 'function resetArtistFilters',
               'function clearArtistSearch']:
        assert fn in js, f'handler referenced by the template is missing: {fn}'

    css = _client(authenticated=True).get('/static/css/dashboard.css').get_data(as_text=True)
    assert '.artist-filters' in css, 'filter bar styling missing'


def test_static_assets_are_served_and_non_empty():
    client = _client(authenticated=True)
    for path, needle in [
        ('/static/css/dashboard.css', '.sidebar'),
        ('/static/js/dashboard.js', 'function'),
    ]:
        resp = client.get(path)
        assert resp.status_code == 200, f'{path} -> HTTP {resp.status_code}'
        body = resp.get_data(as_text=True)
        assert len(body) > 1000, f'{path} is suspiciously small ({len(body)} bytes)'
        assert needle in body, f'{path} does not contain expected content ({needle!r})'


def test_config_post_preserves_internal_keys():
    """POST /api/config replaces the whole document — it must not drop keys.

    The original code preserved a hardcoded pair (`_secret_key`, `_auth`),
    which was correct until `_plex_client_id` and `update_check_enabled`
    existed. Losing `_plex_client_id` makes every restart look like a new
    device to Plex, and nothing surfaces an error — so this is a regression
    guard for silent data loss, not for a crash.
    """
    client = _client(authenticated=True)

    seeded = {
        '_secret_key': 'KEEP-SECRET',
        '_auth': {'username': 'admin', 'password_hash': 'KEEP-HASH'},
        '_plex_client_id': 'KEEP-CLIENT-ID',
        '_future_internal_key': 'KEEP-ME-TOO',
        'update_check_enabled': False,
        'channels': [{'url': 'https://www.youtube.com/@example'}],
        'plex_base_path': './downloads',
    }
    state.write_json(app_module.CONFIG_FILE, seeded, indent=4)

    # A client that only round-trips the user-facing settings, as the Settings
    # page editor does.
    resp = client.post('/api/config', json={
        'channels': [],
        'plex_base_path': './elsewhere',
    })
    assert resp.status_code == 200, resp.get_data(as_text=True)

    after = state.read_json(app_module.CONFIG_FILE)
    assert after['_secret_key'] == 'KEEP-SECRET', 'session secret dropped'
    assert after['_auth']['password_hash'] == 'KEEP-HASH', 'admin credentials dropped'
    assert after['_plex_client_id'] == 'KEEP-CLIENT-ID', 'Plex client ID dropped'
    # Any future underscore key must survive without editing the endpoint.
    assert after['_future_internal_key'] == 'KEEP-ME-TOO', 'unknown internal key dropped'
    assert after['update_check_enabled'] is False, 'update-check preference dropped'
    # The values the caller *did* send must still be applied.
    assert after['channels'] == []
    assert after['plex_base_path'] == './elsewhere'


def test_config_post_allows_overriding_an_internal_key():
    """Preserving omitted keys must not mean ignoring supplied ones."""
    client = _client(authenticated=True)
    state.write_json(app_module.CONFIG_FILE,
                     {'_plex_client_id': 'OLD', '_secret_key': 'S'}, indent=4)
    resp = client.post('/api/config', json={'_plex_client_id': 'NEW'})
    assert resp.status_code == 200
    after = state.read_json(app_module.CONFIG_FILE)
    assert after['_plex_client_id'] == 'NEW', 'explicit value was ignored'
    assert after['_secret_key'] == 'S', 'omitted internal key was dropped'


def test_config_post_rejects_a_non_object_body():
    client = _client(authenticated=True)
    assert client.post('/api/config', json=['not', 'an', 'object']).status_code == 400


def test_unknown_route_404s_rather_than_500s():
    resp = _client(authenticated=True).get('/api/definitely-not-a-real-route')
    assert resp.status_code == 404


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
