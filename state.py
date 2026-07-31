"""Crash-safe, thread-safe persistence for Vidshelf's JSON state files.

Three files hold everything Vidshelf remembers across restarts: config.json
(channels, paths, Plex token, the session secret key), downloaded_videos.json
(what's already been fetched, per channel) and active_downloads.json (live
progress for the downloads UI). This module owns where they live and how they
get written. It exists to fix two distinct bugs, both introduced by writing
them the obvious way.

**Torn writes.** Every write site used to be `open(path, 'w')` followed by
`json.dump()`. `open(..., 'w')` truncates the file *immediately*, so a crash
or a `docker compose down` between the truncate and the last byte leaves a
truncated — or completely empty — file. For config.json that means losing
every configured channel, the Plex token, and the secret key that signs
session cookies. Writes here go to a temp file in the same directory, get
flushed and fsync'd, and are then `os.replace()`d over the target. A reader
(or a crash) sees either the whole old file or the whole new one, never a
half-written one.

**Lost updates.** `mark_video_downloaded()` in app.py did a read-modify-write
with no lock, on the bounded download thread pool. Two downloads finishing
close together would both load the same tracker, each append its own video,
and the second write would silently discard the first — so an already-
downloaded video looks new again and gets re-downloaded on the next channel
check. `update_json()` holds a per-file lock across the whole
read-modify-write so that can't interleave.

**Why state lives in a directory rather than as loose files.** `os.replace()`
swaps the inode, and Docker bind-mounts a *single file* by inode. Under the
pre-v1.1.0 compose layout — which mounted `./config.json:/app/config.json`
and the two trackers the same way — an atomic write inside the container
would land on a brand-new inode that the host's mount doesn't follow, so
writes would silently stop reaching the host and the next restart would read
stale state. Mounting the containing *directory* keeps replace safe.

That layout also broke fresh installs outright. The three files are
gitignored, so a new clone doesn't have them; Docker creates a missing
bind-mount source as a *directory*, which produced a `config.json` directory
inside the container, and `open()` on a directory raises `IsADirectoryError`
— an `OSError`, not the `FileNotFoundError` the old readers caught — so the
app crash-looped at import. A directory mount is exactly what Docker
auto-creates correctly, so `./data` fixes that case for free.
"""

import json
import logging
import os
import threading
import time

_log = logging.getLogger('state')

# Overridable so a local (non-Docker) run can keep state somewhere else; the
# Docker image leaves it at the default and mounts ./data over it.
DATA_DIR = os.environ.get('VIDSHELF_DATA_DIR') or os.path.join('.', 'data')

CONFIG_FILE = os.path.join(DATA_DIR, 'config.json')
TRACKER_FILE = os.path.join(DATA_DIR, 'downloaded_videos.json')
ACTIVE_DOWNLOADS_FILE = os.path.join(DATA_DIR, 'active_downloads.json')

# Where these sat before v1.1.0 — loose in the working directory, each one
# bind-mounted individually. migrate_legacy_state() relocates them on startup
# so upgrading is just `docker compose up -d --build` plus recreating the
# container; nobody has to move files by hand.
_LEGACY_LOCATIONS = (
    (os.path.join('.', 'config.json'), CONFIG_FILE),
    (os.path.join('.', 'downloaded_videos.json'), TRACKER_FILE),
    (os.path.join('.', 'active_downloads.json'), ACTIVE_DOWNLOADS_FILE),
)

# One lock per file rather than one global lock: config writes shouldn't
# serialise behind the progress-hook churn on active_downloads.json, which
# fires several times a second per in-flight download.
_locks = {}
_locks_guard = threading.Lock()


def _lock_for(path):
    key = os.path.abspath(path)
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            # Reentrant so a mutator passed to update_json() can call
            # read_json() on the same file without deadlocking itself.
            lock = threading.RLock()
            _locks[key] = lock
        return lock


def ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


def read_json(path, default=None):
    """Read JSON, returning `default` ({} if unset) for any unreadable file.

    IsADirectoryError is caught deliberately: under the old single-file mount
    layout Docker could leave a *directory* where a state file belongs, and
    the previous readers only caught FileNotFoundError, so that surfaced as an
    import-time crash loop instead of a first run with empty config.
    """
    if default is None:
        default = {}
    # Takes the same lock as write_json, for Windows' benefit: os.replace()
    # fails with PermissionError there if the destination is open by anyone,
    # so an unsynchronised reader holding the file would make concurrent
    # writes fail outright. (POSIX doesn't care — replace over an open file is
    # fine — but README option 2 supports running this directly on Windows.)
    with _lock_for(os.path.abspath(path)):
        try:
            with open(path, 'r') as f:
                return json.load(f)
        except (FileNotFoundError, IsADirectoryError, PermissionError, json.JSONDecodeError):
            return default


def _replace_with_retry(tmp_path, abs_path, attempts=10, delay=0.02):
    """os.replace(), tolerating Windows' transient PermissionError.

    On Windows a rename over an existing file fails if anything else has that
    file open — another process tailing config.json, an editor, or an
    antivirus scanner that grabbed it the instant we created it. Our own
    readers are serialised by the file lock, so what's left is external and
    short-lived; a brief retry clears it. On POSIX this loop never spins.
    """
    for remaining in range(attempts - 1, -1, -1):
        try:
            os.replace(tmp_path, abs_path)
            return
        except PermissionError:
            if remaining == 0:
                raise
            time.sleep(delay)


def write_json(path, data, indent=2):
    """Write JSON atomically — readers never observe a partial file."""
    abs_path = os.path.abspath(path)
    directory = os.path.dirname(abs_path) or '.'
    os.makedirs(directory, exist_ok=True)

    # The temp file must be in the same directory as the target: os.replace()
    # is only atomic within a single filesystem, and /tmp is frequently a
    # different one inside a container. The pid/thread suffix keeps two
    # writers from colliding on the temp name even though the lock below
    # already serialises same-path writes within one process.
    tmp_path = os.path.join(
        directory,
        '.{}.{}.{}.tmp'.format(os.path.basename(abs_path), os.getpid(), threading.get_ident()),
    )

    with _lock_for(abs_path):
        try:
            with open(tmp_path, 'w') as f:
                json.dump(data, f, indent=indent)
                f.flush()
                # Without fsync the replace can land before the data does, so
                # a hard power loss can leave an intact-looking but empty file.
                os.fsync(f.fileno())
            _replace_with_retry(tmp_path, abs_path)
        except BaseException:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            raise

        # Also fsync the directory so the rename itself is durable. Not
        # supported on Windows (and not needed there for our purposes), hence
        # the broad guard rather than a platform check.
        try:
            dir_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except (OSError, AttributeError):
            pass

        # Owner-only. config.json holds the Plex token, the admin password hash
        # and the session signing key, and the data directory is bind-mounted —
        # so 0644 meant any local user on the *host* could read all three. The
        # temp file is created by open() under the process umask, and
        # os.replace() preserves the source's mode, so the mode has to be set
        # here rather than relying on the umask.
        #
        # Best-effort: SMB/CIFS shares and Windows bind mounts frequently reject
        # or ignore chmod, and failing a state write over file permissions would
        # be a far worse outcome than a permissive mode. Logged rather than
        # swallowed silently, because SECURITY.md claims these files are
        # owner-only — if that quietly stops being true, there should be a
        # trace of it somewhere other than an ls.
        #
        # Applies to every state file, not just config.json: the tracker and
        # active_downloads.json don't hold credentials, but they're written by
        # this same function and there's no reason to widen them.
        try:
            os.chmod(abs_path, 0o600)
        except OSError as exc:
            _log.debug("could not chmod 0600 %s: %s", abs_path, exc)


def update_json(path, mutate, default=None, indent=2):
    """Read-modify-write under one lock, so concurrent updates can't clobber.

    `mutate` receives the loaded data and may either mutate it in place or
    return a replacement. Returns whatever was written, so callers can use the
    post-update state without re-reading it.
    """
    with _lock_for(os.path.abspath(path)):
        data = read_json(path, default)
        result = mutate(data)
        if result is not None:
            data = result
        write_json(path, data, indent=indent)
        return data


def migrate_legacy_state():
    """Move pre-v1.1.0 state files into DATA_DIR. Idempotent.

    Only moves a legacy file when there's no file already at the new location,
    so a half-finished migration (or a user who populated ./data by hand)
    never loses the newer copy. Returns the list of human-readable moves made,
    for the caller to log — a silent migration is impossible to debug later.
    """
    ensure_data_dir()
    moved = []
    for legacy_path, new_path in _LEGACY_LOCATIONS:
        # isfile, not exists: under the old layout Docker may have created a
        # *directory* at the legacy path, which is not state worth migrating.
        if not os.path.isfile(legacy_path) or os.path.exists(new_path):
            continue
        try:
            os.replace(legacy_path, new_path)
            moved.append('{} -> {}'.format(legacy_path, new_path))
        except OSError as exc:
            # A cross-device legacy bind mount can't be renamed; copy instead
            # and leave the original in place rather than failing startup.
            try:
                data = read_json(legacy_path, default=None)
                if data is not None:
                    write_json(new_path, data)
                    moved.append('{} -> {} (copied: {})'.format(legacy_path, new_path, exc))
            except OSError:
                pass
    return moved


# Run at import, deliberately. Both app.py and artwork_sync.py read config at
# *import* time (app.secret_key and PLEX_CLIENT_ID are module-level), so there
# is no "startup" hook early enough to migrate from — and on an upgraded
# install the only copy of the persisted secret key is still at the legacy
# path. Migrating even one import too late would read an empty config,
# generate a fresh secret key, and log every existing session out. Whichever
# module imports state first triggers this, so the ordering holds regardless
# of how imports get rearranged later. It's idempotent and a no-op once done.
MIGRATIONS_PERFORMED = migrate_legacy_state()
