"""Reading and writing config.json, plus the two credentials it seeds.

Extracted from app.py in v1.11.0. Nothing here touches Flask, so blueprints can
import it without an import cycle back through the application object — that
constraint is the whole reason the extraction happens before the routes move.

Ordering note, carried over from app.py and still load-bearing:
_get_or_create_secret_key() is called at import time, before any request context
exists, because Flask needs `secret_key` set on the app object as it is created.
That is why _read_raw_config() exists as a separate never-raises function, and
why nothing in this module may depend on the app. See state.py's own note on the
same ordering.
"""

import os
import secrets

import state
from werkzeug.security import generate_password_hash

# State lives in a mounted directory (./data) and is written atomically — see
# state.py for why single-file bind mounts made both of those impossible.
# Re-exported under the original names so callers read unchanged.
CONFIG_FILE = state.CONFIG_FILE
TRACKER_FILE = state.TRACKER_FILE
ACTIVE_DOWNLOADS_FILE = state.ACTIVE_DOWNLOADS_FILE


def report_migrations():
    """Print whatever state.py migrated on import.

    A function rather than module-level side effect: importing a module should
    not print, and the app's startup path is the right place to decide when this
    is announced.
    """
    for migration in state.MIGRATIONS_PERFORMED:
        # Not all migrations relocate a file any more — v1.8.0 added a
        # config-key fold — so the message says what happened rather than
        # assuming.
        print(f"[state] Migration: {migration}")


def load_config():
    return state.read_json(CONFIG_FILE)


def _read_raw_config():
    """Like load_config(), but never raises — used during startup, before
    the app (and thus any request context) exists."""
    return state.read_json(CONFIG_FILE)


def _write_raw_config(config):
    state.write_json(CONFIG_FILE, config, indent=4)


def _update_config(mutate):
    """Read-modify-write config.json under a lock, atomically.

    Prefer this over load_config() + _write_raw_config() anywhere the new
    value depends on the current one: Flask serves requests on threads, so
    two settings saves (or a save racing the Plex OAuth callback, which
    persists a token from a background poll) would otherwise each write a
    full document built from a stale read, and the loser's field silently
    disappears.
    """
    return state.update_json(CONFIG_FILE, mutate, indent=4)


def _get_or_create_secret_key():
    """Flask's secret key signs session cookies — anyone who knows it can
    forge a valid 'logged in as admin' session outright. This used to be a
    fixed string committed to source control, so anyone who'd ever seen this
    repo could forge a session against any deployment still using it.

    Prefer a SECRET_KEY env var; otherwise persist a freshly generated
    random key in config.json (under a leading-underscore key so it reads
    as internal state, not a user-facing setting) so sessions survive a
    container restart without ever falling back to a known value.
    """
    env_key = os.environ.get('SECRET_KEY')
    if env_key:
        return env_key
    config = _read_raw_config()
    existing = config.get('_secret_key')
    if existing:
        return existing
    new_key = secrets.token_hex(32)
    config['_secret_key'] = new_key
    try:
        _write_raw_config(config)
    except Exception:
        print("[SECURITY] Could not persist generated secret key to config.json — "
              "sessions will not survive a restart until this is writable.")
    return new_key


def _get_or_create_admin_credentials():
    """Load the admin username + password hash from config.json, seeding it
    on first run instead of a fixed 'admin'/'adminadmin' checked into
    source. Also fixes a related bug: the old /api/password handler only
    updated an in-memory dict, so any password change was silently reverted
    on the next restart — this persists changes to config.json instead.

    First-run behavior: ADMIN_USERNAME/ADMIN_PASSWORD env vars are used if
    set; otherwise a random password is generated and printed once so it can
    still be retrieved from `docker logs`.
    """
    config = _read_raw_config()
    creds = config.get('_auth', {})
    if creds.get('username') and creds.get('password_hash'):
        return creds['username'], creds['password_hash']

    # `or 'admin'` (not .get(..., 'admin')) because docker-compose passes
    # ADMIN_USERNAME='' — not an absent key — when the .env var is unset, and
    # os.environ.get() with a default only applies it for a missing key, not
    # an empty-string value.
    username = os.environ.get('ADMIN_USERNAME') or 'admin'
    password = os.environ.get('ADMIN_PASSWORD')
    generated = False
    if not password:
        password = secrets.token_urlsafe(12)
        generated = True
    password_hash = generate_password_hash(password)
    config['_auth'] = {'username': username, 'password_hash': password_hash}
    try:
        _write_raw_config(config)
    except Exception:
        print("[SECURITY] Could not persist generated admin credentials to config.json.")
    if generated:
        print(f"[SECURITY] No ADMIN_PASSWORD set — generated a random admin password on "
              f"first run: {password!r} (username: {username!r}). This is only printed "
              f"once; change it via the dashboard's Settings page, or set ADMIN_PASSWORD "
              f"and delete the '_auth' key from config.json to reset it.")
    return username, password_hash
