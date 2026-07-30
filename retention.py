"""Prune old downloads so unattended monitoring can't fill the disk.

This is the only code in Vidshelf that deletes media, so it is built to be
boring and hard to misfire:

- **Off by default**, and automatic sweeping is a second, separate opt-in.
- **Plan first, always.** `plan()` computes what would go; `apply()` only ever
  acts on a plan handed to it. The API defaults to dry-run.
- **`keep_last` is floored at 1.** There is no configuration that deletes an
  artist's entire folder.
- **Only video files.** Artwork, `artist-metadata.json`, `title-cards.json` and
  anything else are never candidates, so pruning can't break the Plex
  integration.
- **Skips a suspect root.** A root that is missing, unreadable, or contains no
  artist folders at all is recorded as an error and skipped rather than swept.
  That guard exists because of a documented incident in this project: a network
  path that silently resolved to a small local decoy volume instead of the NAS
  (see CLAUDE.md). Deleting based on what a *wrong* mount contains is the one
  failure here that isn't recoverable.

## Every media root, not just the music-video one

v1.5.0 swept only `artwork_sync.root_path`, which meant the feature that exists
to bound unattended *channel* monitoring never pruned what channel monitoring
downloads — those land under `plex_base_path` or a per-channel
`plex_media_path`. Callers pass the full list; app.py already computes it in
`_gather_media_roots()` (used by the conversion scan since the initial commit),
and that helper filters to directories that actually exist, so roots the
container can't see never reach here.

A root that can't be scanned is skipped, not fatal — one unmounted share
shouldn't stop pruning a healthy one. `plan()` only reports an overall error
when *no* root could be scanned.

## Why the download tracker is never touched

`downloaded_videos.json` records "we have downloaded this", not "the file is
present". Removing a pruned video's entry would make the scheduler re-download
it on the next tick, delete it again on the next sweep, and loop forever —
burning bandwidth and thrashing the disk. Files go; tracker entries stay.

The visible consequence is intended: a pruned video will not come back on its
own. Clearing download history (Settings) is what makes it eligible again.
"""

import os
import time

import notify

VIDEO_EXTENSIONS = ('.mp4', '.mkv', '.webm', '.m4v', '.avi', '.mov')
DEFAULT_KEEP_LAST = 10
# A single sweep deleting more than this is far more likely to be a
# misconfiguration than an intention, so it stops and asks.
SAFETY_MAX_DELETIONS = 200
GIB = 1024 ** 3


def _settings(config):
    cfg = (config or {}).get('retention') or {}
    try:
        keep = int(cfg.get('keep_last_per_artist', DEFAULT_KEEP_LAST))
    except (TypeError, ValueError):
        keep = DEFAULT_KEEP_LAST
    # Floored at 1: never a configuration that empties a folder.
    keep = max(1, keep)
    return bool(cfg.get('enabled')), keep


def _media_roots(config, roots=None):
    """Every directory to sweep — see the module docstring."""
    if roots:
        # Deduplicate while preserving order, so overlapping config (a channel
        # path equal to plex_base_path) can't plan the same file twice.
        seen, out = set(), []
        for r in roots:
            if not r:
                continue
            key = os.path.normpath(r)
            if key not in seen:
                seen.add(key)
                out.append(key)
        return out
    artwork_cfg = (config or {}).get('artwork_sync') or {}
    return [artwork_cfg.get('root_path', '/app/music_videos_final')]


def _plan_one_root(root, keep):
    """Scan a single root. Returns (candidates, summary)."""
    summary = {'root': root, 'artists_scanned': 0, 'total_files': 0, 'error': None}
    candidates = []

    if not root or not os.path.isdir(root):
        summary['error'] = f'Not a directory: {root}'
        return candidates, summary

    try:
        entries = sorted(os.listdir(root))
    except OSError as exc:
        summary['error'] = f'Cannot read: {exc}'
        return candidates, summary

    artist_dirs = [e for e in entries
                   if not e.startswith('.') and os.path.isdir(os.path.join(root, e))]
    if not artist_dirs:
        # An empty root is far more likely to be an unmounted or wrongly-mounted
        # path than a genuinely empty library, and "nothing to delete" would hide
        # that.
        summary['error'] = ('No artist folders found — refusing to sweep this '
                            'root. This usually means the volume is not mounted '
                            'where it should be; check `docker exec vidshelf df -h`.')
        return candidates, summary

    for artist in artist_dirs:
        artist_path = os.path.join(root, artist)
        try:
            names = os.listdir(artist_path)
        except OSError:
            continue
        summary['artists_scanned'] += 1

        videos = []
        for name in names:
            if not name.lower().endswith(VIDEO_EXTENSIONS):
                continue
            full = os.path.join(artist_path, name)
            try:
                stat = os.stat(full)
            except OSError:
                continue
            videos.append({'path': full, 'name': name, 'artist': artist,
                           'root': root, 'size_bytes': stat.st_size,
                           'modified_at': stat.st_mtime})

        summary['total_files'] += len(videos)
        if len(videos) <= keep:
            continue

        # Newest first, so everything past `keep` is the oldest.
        videos.sort(key=lambda v: v['modified_at'], reverse=True)
        candidates.extend(videos[keep:])

    return candidates, summary


def plan(config, root=None, roots=None):
    """Work out what a sweep would delete across every root. Deletes nothing.

    `root` is accepted for the single-root case (tests, standalone use); `roots`
    takes a list and wins if both are given.
    """
    _, keep = _settings(config)
    target_roots = _media_roots(config, roots or ([root] if root else None))

    result = {
        'roots': [],
        'keep_last_per_artist': keep,
        'candidates': [],
        'total_files': 0,
        'total_bytes': 0,
        'artists_scanned': 0,
        'error': None,
        'planned_at': time.time(),
    }

    scanned_ok = 0
    for target in target_roots:
        candidates, summary = _plan_one_root(target, keep)
        result['roots'].append(summary)
        result['artists_scanned'] += summary['artists_scanned']
        result['total_files'] += summary['total_files']
        if summary['error'] is None:
            scanned_ok += 1
        for victim in candidates:
            result['candidates'].append(victim)
            result['total_bytes'] += victim['size_bytes']

    result['candidate_count'] = len(result['candidates'])

    if not target_roots:
        result['error'] = 'No media roots configured'
    elif scanned_ok == 0:
        # Every root refused or was unreadable — surface it rather than
        # reporting a clean sweep over nothing.
        details = '; '.join(f"{r['root']}: {r['error']}" for r in result['roots'])
        result['error'] = f'No media root could be scanned. {details}'
    elif result['candidate_count'] > SAFETY_MAX_DELETIONS:
        result['error'] = (
            f"Plan would delete {result['candidate_count']} files, above the "
            f'safety limit of {SAFETY_MAX_DELETIONS}. Raise keep_last_per_artist, '
            'or prune in smaller batches — this many at once is usually a '
            'misconfiguration rather than an intention.'
        )
    return result


def apply(plan_result, config=None):
    """Delete the files in a plan. Returns what actually happened.

    Refuses outright if the plan carries an error, so a caller can't accidentally
    execute a refused sweep by ignoring the field.
    """
    outcome = {'deleted': [], 'failed': [], 'freed_bytes': 0, 'error': None}

    if not plan_result:
        outcome['error'] = 'No plan supplied'
        return outcome
    if plan_result.get('error'):
        outcome['error'] = f"Refusing to apply a plan that reported: {plan_result['error']}"
        return outcome

    for victim in plan_result.get('candidates', []):
        path = victim.get('path')
        try:
            os.remove(path)
            outcome['deleted'].append(path)
            outcome['freed_bytes'] += victim.get('size_bytes', 0)
        except OSError as exc:
            outcome['failed'].append({'path': path, 'error': str(exc)})

    if config is not None and (outcome['deleted'] or outcome['failed']):
        freed_gb = outcome['freed_bytes'] / GIB
        lines = [f"Deleted {len(outcome['deleted'])} file(s), freed {freed_gb:.2f} GB."]
        if outcome['failed']:
            lines.append(f"{len(outcome['failed'])} deletion(s) failed.")
        lines.append('Pruned videos are not re-downloaded — their download '
                     'history is kept on purpose.')
        notify.send(config, notify.EVENT_RETENTION,
                    'Vidshelf: retention sweep completed', '\n'.join(lines))

    return outcome


def sweep(config, root=None, roots=None, dry_run=True):
    """plan() and, unless dry_run, apply(). The one entry point callers need."""
    enabled, _ = _settings(config)
    plan_result = plan(config, root=root, roots=roots)
    plan_result['enabled'] = enabled
    plan_result['dry_run'] = dry_run

    if dry_run:
        return {'plan': plan_result, 'applied': None}
    if not enabled:
        return {'plan': plan_result,
                'applied': {'error': 'Retention is disabled in settings', 'deleted': []}}
    return {'plan': plan_result, 'applied': apply(plan_result, config=config)}
