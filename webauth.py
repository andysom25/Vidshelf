"""Session guarding, login throttling and response headers.

Extracted from app.py in v1.11.0 so every blueprint can import @require_auth
without importing the application object. It uses Flask's request-context globals
(`session`, `jsonify`) but never the app itself, which is what keeps the
dependency graph acyclic: routes/* -> webauth -> flask.

The one thing in here that is registered ON the app — _set_security_headers, an
after_request hook — is exported as a plain function and wired up by app.py.
"""

import functools
import time

from flask import jsonify, session


def require_auth(view):
    """Reject anonymous callers before the view runs.

    This check used to be two hand-written lines at the top of 58 route
    bodies. Both v1.6.1 and v1.7.0 were, in part, "an endpoint was missing
    it" — and v1.7.0 found that /api/artwork/search_noauth had validated its
    query parameter *before* checking the session, so it answered anonymous
    probes with 400 and looked guarded to the route sweep while serving
    ?artist=... to anyone. A decorator makes both failures structural: you
    cannot forget half of it, and it cannot run after anything else.

    functools.wraps matters here beyond tidiness — Flask derives the
    endpoint name from __name__, and tests/test_routes.py keys its
    public-endpoint allowlist off those names. Since v1.11.0 those names carry a
    blueprint prefix (`channels.api_channels`), which wraps does not affect —
    but dropping wraps would collapse every guarded endpoint to
    `<blueprint>.wrapper` and break registration outright.
    """
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        if 'username' not in session:
            return jsonify({'error': 'Unauthorized'}), 401
        return view(*args, **kwargs)
    return wrapper


# Simple in-memory login throttle, keyed by client IP. Resets on restart —
# acceptable here since this is a single-container, single-account app, not
# a distributed service; the goal is just to make the default/first-run
# credential meaningfully harder to brute-force, not to build a full
# rate-limiting subsystem.
_LOGIN_FAILURES = {}  # ip -> (fail_count, locked_until_epoch)
_LOGIN_MAX_ATTEMPTS = 5
_LOGIN_LOCKOUT_SECONDS = 300
# Above this many tracked IPs, expired records are swept on the next failed
# login. Not a hard cap -- currently-locked IPs are never dropped, since
# forgetting one would hand an attacker a free reset.
_LOGIN_FAILURES_SOFT_CAP = 512


def _login_is_locked(ip):
    count, locked_until = _LOGIN_FAILURES.get(ip, (0, 0))
    return count >= _LOGIN_MAX_ATTEMPTS and time.time() < locked_until


def _record_login_failure(ip):
    now = time.time()
    # Evict records that can no longer lock anyone out. Without this the dict
    # kept one entry per source IP for the life of the process — unbounded
    # growth driven entirely by unauthenticated requests, which is a poor
    # property for the one endpoint that is reachable without a session.
    # Cheap: this runs only on a *failed* login, which is already rate-limited.
    if len(_LOGIN_FAILURES) > _LOGIN_FAILURES_SOFT_CAP:
        for stale_ip, (_, until) in list(_LOGIN_FAILURES.items()):
            if until < now and stale_ip != ip:
                del _LOGIN_FAILURES[stale_ip]

    count, locked_until = _LOGIN_FAILURES.get(ip, (0, 0))
    count += 1
    if count >= _LOGIN_MAX_ATTEMPTS:
        locked_until = now + _LOGIN_LOCKOUT_SECONDS
    _LOGIN_FAILURES[ip] = (count, locked_until)


def _clear_login_failures(ip):
    _LOGIN_FAILURES.pop(ip, None)


def _set_security_headers(response):
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('X-Frame-Options', 'DENY')
    response.headers.setdefault('Referrer-Policy', 'same-origin')

    # Rendered HTML must not be cached. It carried NO cache headers at all, so
    # browsers were free to reuse it indefinitely by heuristic — and after an
    # upgrade that meant the old markup with the new JavaScript. v1.12.0 added
    # sort/filter controls to the template, and on an updated container they
    # simply were not there: the page was the previous release's HTML, while the
    # sidebar happily reported the new version because that comes from an API
    # call the stale script still makes. It looks exactly like a broken release.
    #
    # Only HTML. Flask serves static files with Cache-Control: no-cache and an
    # ETag already, which is the right trade there: revalidate cheaply, and a
    # 304 costs nothing. Applying no-store to them would re-download ~170 KB of
    # JavaScript on every navigation for no benefit.
    if response.mimetype == 'text/html':
        response.headers['Cache-Control'] = 'no-store, must-revalidate'
    return response
