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
        # v1.5.0 — unattended monitoring, notifications, retention
        '/api/monitor/status', '/api/monitor/config', '/api/monitor/run',
        '/api/notifications/config', '/api/notifications/test',
        '/api/retention/config', '/api/retention/plan', '/api/retention/apply',
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


def test_v160_endpoints_are_registered_and_guarded():
    client = _client(authenticated=True)
    # Cancel/retry on an unknown id must report, not 500.
    assert client.post('/api/downloads/nope/cancel').status_code in (409, 404)
    assert client.post('/api/downloads/nope/retry').status_code == 404
    # Quality cap on an unknown channel.
    assert client.post('/api/channels/quality',
                       json={'url': 'https://nope', 'max_height': 1080}).status_code == 404
    assert client.post('/api/channels/quality',
                       json={'url': 'x', 'max_height': 'tall'}).status_code == 400


def test_v160_endpoints_reject_anonymous_callers():
    """These mutate state and one of them starts a download, so the auth check
    matters more than for a read-only route."""
    client = _client(authenticated=False)
    for path in ('/api/downloads/x/cancel', '/api/downloads/x/retry',
                 '/api/channels/quality'):
        assert client.post(path).status_code == 401, path


def test_stats_no_longer_reports_the_download_count_twice():
    """The v1.5.x bug: videos_count and downloads_count came from the identical
    expression, so two dashboard cards always showed the same number."""
    client = _client(authenticated=True)
    state.write_json(app_module.TRACKER_FILE,
                     {'https://c': ['a', 'b', 'c']}, indent=2)
    stats = client.get('/api/stats').get_json()
    assert stats['downloads_count'] == 3, stats
    # No check has run, so "new available" is unknown rather than 0 — claiming 0
    # would assert "nothing new" when the truth is "haven't looked".
    assert stats['new_available'] is None, stats


def test_downloads_page_offers_cancel_and_retry():
    client = _client(authenticated=True)
    js = client.get('/static/js/dashboard.js').get_data(as_text=True)
    for fn in ['function downloadActions', 'async function cancelDownload',
               'async function retryDownload']:
        assert fn in js, f'missing handler: {fn}'
    assert "'cancelled':" in js, 'no cancelled status badge'
    # And the channel quality selector.
    assert 'function changeChannelQuality' in js
    assert 'changeChannelQuality(' in js


def test_beta_badge_is_gone():
    """It said "beta" while shipping v1.5.1 with published images and CI."""
    html = _client(authenticated=True).get('/dashboard').get_data(as_text=True)
    assert '<span>beta</span>' not in html
    assert 'New Available' in html, 'stat card not relabelled'


def test_automation_settings_controls_are_present_and_wired():
    """The monitoring/retention panels span template, CSS and JS.

    A rename in one and not the others yields a control that renders and does
    nothing — no error, no bad status code. Covers the v1.5.1 brakes
    (listing cap, queue depth, free-space floor) and the automatic-prune opt-in.
    """
    client = _client(authenticated=True)
    html = client.get('/dashboard').get_data(as_text=True)
    for element_id in ['monitor-enabled', 'monitor-interval', 'monitor-max',
                       'monitor-listing', 'monitor-queue', 'monitor-freegb',
                       'retention-enabled', 'retention-keep', 'retention-auto',
                       'notify-enabled', 'notify-url']:
        assert f'id="{element_id}"' in html, f'missing control: {element_id}'

    js = client.get('/static/js/dashboard.js').get_data(as_text=True)
    for fn in ['function loadMonitorStatus', 'function saveMonitorConfig',
               'function runMonitorNow', 'function saveRetentionConfig',
               'function previewRetention', 'function applyRetention',
               'function saveNotifyConfig', 'function testNotify']:
        assert fn in js, f'handler referenced by the template is missing: {fn}'

    # The new knobs must actually be sent, not just rendered.
    for key in ['max_listing:', 'max_queue_depth:', 'min_free_gb:', 'auto_sweep:']:
        assert key in js, f'{key} never sent to the server'


def test_monitor_accepts_the_v151_brake_settings():
    client = _client(authenticated=True)
    resp = client.post('/api/monitor/config', json={
        'enabled': True, 'max_listing': 25,
        'max_queue_depth': 7, 'min_free_gb': 250})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    st = resp.get_json()['status']
    assert st['max_listing'] == 25
    assert st['max_queue_depth'] == 7
    assert st['min_free_gb'] == 250


def test_min_free_gb_zero_is_preserved_not_defaulted():
    """0 means "disable the check"; it must not be replaced by a default."""
    client = _client(authenticated=True)
    client.post('/api/monitor/config', json={'min_free_gb': 100})
    resp = client.post('/api/monitor/config', json={'min_free_gb': 0})
    assert resp.get_json()['status']['min_free_gb'] == 0


def test_retention_auto_sweep_is_a_separate_opt_in():
    """Enabling retention must not by itself start unattended deletion."""
    client = _client(authenticated=True)
    client.post('/api/retention/config', json={'enabled': True})
    cfg = state.read_json(app_module.CONFIG_FILE).get('retention', {})
    assert cfg.get('enabled') is True
    assert not cfg.get('auto_sweep'), 'auto_sweep turned on by enabling retention'

    client.post('/api/retention/config', json={'auto_sweep': True})
    cfg = state.read_json(app_module.CONFIG_FILE).get('retention', {})
    assert cfg.get('auto_sweep') is True


def test_retention_apply_refuses_without_explicit_confirmation():
    """The only irreversible endpoint in the app must not fire on a bare POST.

    The confirm token isn't a security control — the session already
    authenticates the admin — it's there so a stray request or a mis-wired
    button can't delete media.
    """
    client = _client(authenticated=True)
    assert client.post('/api/retention/apply', json={}).status_code == 400
    assert client.post('/api/retention/apply', json={'confirm': 'yes'}).status_code == 400
    assert client.post('/api/retention/apply', json={'confirm': 'delete'}).status_code == 400


def test_plex_token_is_not_returned_by_either_config_endpoint():
    """v1.6.1: the token was the prize for any script in the admin session."""
    client = _client(authenticated=True)
    secret = 'PLEX-TOKEN-SHOULD-NOT-LEAK'
    state.write_json(app_module.CONFIG_FILE,
                     {'plex': {'token': secret, 'server_url': 'http://plex.local:32400'},
                      '_secret_key': 'k'}, indent=4)

    for path in ('/api/config', '/api/plex/config'):
        body = client.get(path).get_data(as_text=True)
        assert secret not in body, f'{path} leaked the Plex token'
        assert 'token_set' in body, f'{path} does not report token_set'

    # Presence is still discoverable, which is all the UI needs.
    assert client.get('/api/plex/config').get_json()['token_set'] is True
    # And the non-secret fields still come through.
    assert client.get('/api/plex/config').get_json()['server_url'] == 'http://plex.local:32400'


def test_config_round_trip_does_not_disconnect_plex():
    """The GET no longer returns the token, so a client POSTing the document back
    would otherwise wipe it — silently disconnecting Plex."""
    client = _client(authenticated=True)
    state.write_json(app_module.CONFIG_FILE,
                     {'plex': {'token': 'KEEP-TOKEN', 'server_url': 'http://p:32400'},
                      '_secret_key': 'k'}, indent=4)

    # Exactly what the raw-config editor does: GET, then POST it back.
    document = client.get('/api/config').get_json()
    assert 'token' not in document['plex']
    assert client.post('/api/config', json=document).status_code == 200

    after = state.read_json(app_module.CONFIG_FILE)
    assert after['plex']['token'] == 'KEEP-TOKEN', 'round-trip wiped the Plex token'
    assert 'token_set' not in after['plex'], 'the presence marker was persisted'
    assert after['_secret_key'] == 'k'


def test_config_round_trip_from_a_disconnected_install_persists_no_marker():
    """The same round-trip with no Plex token stored.

    The first version of the merge dropped `token_set` only inside the branch
    that restored the token, so an install that had never connected Plex wrote
    `"token_set": false` into config.json and kept it forever — a computed
    presence flag masquerading as a stored setting. The connected case above
    passed throughout, which is why this needs its own test.
    """
    client = _client(authenticated=True)
    state.write_json(app_module.CONFIG_FILE,
                     {'plex': {'server_url': '', 'music_video_library_key': ''},
                      '_secret_key': 'k'}, indent=4)

    document = client.get('/api/config').get_json()
    assert document['plex']['token_set'] is False, 'GET should report absence explicitly'
    assert client.post('/api/config', json=document).status_code == 200

    after = state.read_json(app_module.CONFIG_FILE)
    assert 'token_set' not in after['plex'], \
        'the presence marker was persisted by a disconnected install'
    assert 'token' not in after['plex'], 'an empty token should not be invented'


def test_notification_config_never_echoes_the_url_back():
    """The webhook URL usually embeds a secret; the response must not carry it.

    It would otherwise land in browser history and any intermediate request log.
    """
    client = _client(authenticated=True)
    secret = 'https://discord.com/api/webhooks/123/SUPERSECRETTOKENVALUE'
    resp = client.post('/api/notifications/config',
                       json={'enabled': True, 'url': secret})
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'SUPERSECRETTOKENVALUE' not in body, 'full webhook URL echoed in the response'
    payload = resp.get_json()['notifications']
    assert payload['url_set'] is True
    assert payload['detected_kind'] == 'discord'


def test_monitor_settings_round_trip_and_floor_the_interval():
    client = _client(authenticated=True)
    resp = client.post('/api/monitor/config',
                       json={'enabled': True, 'interval_minutes': 1, 'max_per_channel': 3})
    assert resp.status_code == 200
    status = resp.get_json()['status']
    assert status['enabled'] is True
    # A too-short interval is clamped rather than accepted, so a typo can't
    # hammer YouTube every minute.
    assert status['interval_minutes'] >= 5, status['interval_minutes']
    assert status['max_per_channel'] == 3


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
