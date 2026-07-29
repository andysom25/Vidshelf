"""Prune old downloads so unattended monitoring can't fill the disk.

This is the only code in Vidshelf that deletes media, so it is built to be
boring and hard to misfire:

- **Off by default.** Enabling it is a deliberate act.
- **Plan first, always.** `plan()` computes what would go; `apply()` only ever
  acts on a plan handed to it. The API defaults to dry-run.
- **`keep_last` is floored at 1.** There is no configuration that deletes an
  artist's entire folder.
- **Only video files.** Artwork, `artist-metadata.json`, `title-cards.json` and
  anything else are never candidates, so pruning can't break the Plex
  integration.
- **Refuses to run against a suspect root.** If the media root is missing, not
  a directory, or contains no artist folders at all, it aborts rather than
  reporting a clean sweep. That guard exists because of a documented incident
  in this project: a network path that silently resolved to a small local decoy
  volume instead of the NAS (see CLAUDE.md). Deleting based on what a *wrong*
  mount contains is the one failure here that isn't recoverable.

## Why the download tracker is never touched

`downloaded_videos.json` records "we have downloaded this", not "the file is
present". Removing a pruned video's entry would make the scheduler re-download
it on the next tick, delete it again the next sweep, and loop forever — burning
bandwidth and thrashing the disk. Files go; tracker entries stay.

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


def _settings(config):
    cfg = (config or {}).get('retention') or {}
    try:
        keep = int(cfg.get('keep_last_per_artist', DEFAULT_KEEP_LAST))
    except (TypeError, ValueError):
        keep = DEFAULT_KEEP_LAST
    # Floored at 1: never a configuration that empties a folder.
    keep = max(1, keep)
    return bool(cfg.get('enabled')), keep


def _media_root(config):
    artwork_cfg = (config or {}).get('artwork_sync') or {}
    return artwork_cfg.get('root_path', '/app/music_videos_final')


def plan(config, root=None):
    """Work out what a sweep would delete. Never deletes anything.

    Returns a dict with `candidates` (files that would go, newest-kept-first
    ordering already applied), plus counts and any `error` explaining a refusal.
    """
    root = root or _media_root(config)
    _, keep = _settings(config)

    result = {
        'root': root,
        'keep_last_per_artist': keep,
        'candidates': [],
        'total_files': 0,
        'total_bytes': 0,
        'artists_scanned': 0,
        'error': None,
        'planned_at': time.time(),
    }

    if not root or not os.path.isdir(root):
        result['error'] = f'Media root does not exist or is not a directory: {root}'
        return result

    try:
        entries = sorted(os.listdir(root))
    except OSError as exc:
        result['error'] = f'Cannot read media root: {exc}'
        return result

    artist_dirs = [e for e in entries
                   if not e.startswith('.') and os.path.isdir(os.path.join(root, e))]

    if not artist_dirs:
        # See the module docstring: an empty root is far more likely to be an
        # unmounted or wrongly-mounted path than a genuinely empty library, and
        # "nothing to delete" would hide that.
        result['error'] = (
            f'No artist folders found under {root}. Refusing to sweep — this '
            'usually means the media volume is not mounted where it should be. '
            'Check `docker exec vidshelf df -h`.'
        )
        return result

    for artist in artist_dirs:
        artist_path = os.path.join(root, artist)
        try:
            names = os.listdir(artist_path)
        except OSError:
            continue
        result['artists_scanned'] += 1

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
                           'size_bytes': stat.st_size, 'modified_at': stat.st_mtime})

        result['total_files'] += len(videos)
        if len(videos) <= keep:
            continue

        # Newest first, so everything past `keep` is the oldest.
        videos.sort(key=lambda v: v['modified_at'], reverse=True)
        for victim in videos[keep:]:
            result['candidates'].append(victim)
            result['total_bytes'] += victim['size_bytes']

    result['candidate_count'] = len(result['candidates'])
    if len(result['candidates']) > SAFETY_MAX_DELETIONS:
        result['error'] = (
            f"Plan would delete {len(result['candidates'])} files, above the "
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
        freed_gb = outcome['freed_bytes'] / (1024 ** 3)
        lines = [f"Deleted {len(outcome['deleted'])} file(s), freed {freed_gb:.2f} GB."]
        if outcome['failed']:
            lines.append(f"{len(outcome['failed'])} deletion(s) failed.")
        lines.append('Pruned videos are not re-downloaded — their download '
                     'history is kept on purpose.')
        notify.send(config, notify.EVENT_RETENTION,
                    'Vidshelf: retention sweep completed', '\n'.join(lines))

    return outcome


def sweep(config, root=None, dry_run=True):
    """plan() and, unless dry_run, apply(). The one entry point callers need."""
    enabled, _ = _settings(config)
    plan_result = plan(config, root=root)
    plan_result['enabled'] = enabled
    plan_result['dry_run'] = dry_run

    if dry_run:
        return {'plan': plan_result, 'applied': None}
    if not enabled:
        return {'plan': plan_result,
                'applied': {'error': 'Retention is disabled in settings', 'deleted': []}}
    return {'plan': plan_result, 'applied': apply(plan_result, config=config)}
