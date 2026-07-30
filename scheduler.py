"""Periodic channel monitoring — the thing that makes download modes mean something.

Before this existed, `download_mode` was read from exactly one place: the
`/api/channels/download-all` endpoint, which only fires when a human clicks a
button. A channel set to "New Only" therefore downloaded nothing until someone
pressed it. This runs the same decision on a timer.

Collaborators are injected rather than imported from app.py, for two reasons:
app.py imports this module (so importing it back would be circular), and it
makes the loop testable without Flask, a network, or a real yt-dlp call.

## Behaviours worth knowing

**Already-downloaded videos are always skipped, regardless of mode.** The manual
endpoint treats `all` as "fetch up to 20 whether or not we have them", which is
reasonable by hand and catastrophic every hour — it would re-download the same
videos forever. On a timer, `all` and `new` behave identically.

**`manual` channels are never touched.** That mode means "I decide".

**The downloaded set is fetched once per channel, not once per video.** v1.5.0
called an `is_downloaded(video_id, channel_url)` predicate inside the loop, and
each call re-read *and re-parsed* the whole tracker file under a lock. The manual
path capped its listing at 20 so nobody noticed; this loop iterates a channel's
entire listing, which measured 500 file reads for a 500-video channel — per
channel, per tick. Now: one read per channel.

**Three brakes on unattended growth**, all off or generous by default:
`max_listing` bounds how much of a channel is even considered, `max_queue_depth`
stops piling work onto a download queue that isn't draining, and `min_free_gb`
refuses to start downloads when the destination is nearly full. Without these,
an hourly job with a 2-worker pool and an unbounded queue grows without limit.

**Repeatedly failing channels back off exponentially** rather than erroring —
and notifying — every tick forever.

## Interaction with retention

Deleting a file does **not** remove its tracker entry, so a video pruned by
retention.py is not re-downloaded on the next tick. That is what keeps the two
features from forming a download/delete loop, and it's the reason retention must
never clear tracker entries.
"""

import threading
import time
import traceback

import notify

DEFAULT_INTERVAL_MINUTES = 60
MIN_INTERVAL_MINUTES = 5          # a floor, so a typo can't hammer YouTube
DEFAULT_MAX_PER_CHANNEL = 5       # queued per tick, so a new channel trickles in
DEFAULT_MAX_LISTING = 50          # how much of a channel's listing to consider
DEFAULT_MAX_QUEUE_DEPTH = 20      # don't pile onto a queue that isn't draining
DEFAULT_MIN_FREE_GB = 0           # 0 disables the free-space brake
MONITORED_MODES = ('new', 'all')

# Backoff for a channel that keeps failing: 1 tick, then 2, 4, 8, capped.
MAX_BACKOFF_TICKS = 8

GIB = 1024 ** 3


def _now():
    return time.time()


class ChannelMonitor:
    """Background thread that polls monitored channels on an interval."""

    def __init__(self, load_config, list_videos, list_downloaded, start_download,
                 queue_depth=None, free_space=None, run_retention=None,
                 sleeper=None, clock=None):
        self._load_config = load_config
        self._list_videos = list_videos          # (url, limit) -> [ {id, title}, ... ]
        self._list_downloaded = list_downloaded  # (url) -> set of video ids
        self._start_download = start_download    # (video, channel_cfg) -> download_id
        self._queue_depth = queue_depth          # () -> int queued/in-flight
        self._free_space = free_space            # (path) -> bytes free, or None
        self._run_retention = run_retention      # () -> dict, or None
        self._sleep = sleeper or time.sleep
        self._clock = clock or _now

        self._thread = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.Lock()
        # url -> {'failures': int, 'skip_ticks': int}
        self._backoff = {}
        self._state = {
            'running': False,
            'last_run': None,
            'last_run_duration': None,
            'next_run': None,
            'checked_channels': 0,
            'started_downloads': 0,
            'last_results': [],
            'last_error': None,
            'last_skip_reason': None,
            'total_ticks': 0,
            'last_retention': None,
        }

    # ---------------------------------------------------------------- config
    def _settings(self):
        cfg = (self._load_config() or {}).get('channel_monitor') or {}

        def _int(key, default, minimum=1):
            try:
                return max(minimum, int(cfg.get(key, default)))
            except (TypeError, ValueError):
                return default

        # Clamped rather than rejected: a config typo should slow the loop down,
        # not stop monitoring or hammer YouTube every second.
        interval = _int('interval_minutes', DEFAULT_INTERVAL_MINUTES,
                        MIN_INTERVAL_MINUTES)
        return {
            'enabled': bool(cfg.get('enabled')),
            'interval_minutes': interval,
            'max_per_channel': _int('max_per_channel', DEFAULT_MAX_PER_CHANNEL),
            'max_listing': _int('max_listing', DEFAULT_MAX_LISTING),
            'max_queue_depth': _int('max_queue_depth', DEFAULT_MAX_QUEUE_DEPTH),
            'min_free_gb': _int('min_free_gb', DEFAULT_MIN_FREE_GB, 0),
        }

    def pending_count(self):
        """Videos seen on monitored channels that haven't been downloaded.

        Recorded per tick so the dashboard can show something meaningful instead
        of the download count twice. Returns None when no check has run yet —
        reporting 0 would claim "nothing new" when the truth is "not looked".
        """
        with self._lock:
            results = self._state.get('last_results') or []
            if not self._state.get('last_run'):
                return None
            return sum(r.get('pending', 0) for r in results)

    def status(self):
        with self._lock:
            state = dict(self._state)
            state['backoff'] = {u: dict(b) for u, b in self._backoff.items()
                                if b.get('skip_ticks')}
        state.update(self._settings())
        state['thread_alive'] = bool(self._thread and self._thread.is_alive())
        return state

    # ------------------------------------------------------------- brakes
    def _queue_is_full(self, settings):
        """True if the download queue is already deep enough to skip this tick.

        A 2-worker pool with an unbounded queue will happily accept more work
        than it can ever finish; without this an hourly tick keeps adding.
        """
        if not self._queue_depth:
            return False, 0
        try:
            depth = int(self._queue_depth())
        except Exception:  # noqa: BLE001
            return False, 0
        return depth >= settings['max_queue_depth'], depth

    def _space_is_low(self, path, settings):
        """True if `path` has less free space than the configured floor."""
        floor_gb = settings['min_free_gb']
        if not floor_gb or not self._free_space or not path:
            return False, None
        try:
            free = self._free_space(path)
        except Exception:  # noqa: BLE001
            return False, None
        if free is None:
            return False, None
        return free < floor_gb * GIB, free

    # ------------------------------------------------------------------ tick
    def run_once(self, ignore_backoff=False):
        """One monitoring pass. Returns the per-channel results.

        Exceptions from a single channel are captured into that channel's result
        rather than raised, so one dead channel can't stop the others or kill the
        loop.
        """
        config = self._load_config() or {}
        settings = self._settings()
        channels = [c for c in (config.get('channels') or [])
                    if c.get('download_mode') in MONITORED_MODES]

        started_at = self._clock()
        results = []
        total_started = 0
        skip_reason = None

        queue_full, depth = self._queue_is_full(settings)
        if queue_full:
            # Nothing is fetched at all — the point is to stop adding work, and
            # listing every channel costs a yt-dlp call each.
            skip_reason = (f'{depth} download(s) already queued '
                           f"(limit {settings['max_queue_depth']}) — skipped this check")
            channels = []

        for channel in channels:
            url = channel.get('url')
            if not url:
                continue

            entry = {'channel': url, 'found': 0, 'started': 0, 'pending': 0,
                     'error': None, 'skipped': None}

            if not ignore_backoff and self._should_skip_for_backoff(url):
                entry['skipped'] = 'backing off after repeated failures'
                results.append(entry)
                continue

            try:
                videos = self._list_videos(url, settings['max_listing']) or []
                entry['found'] = len(videos)

                # One tracker read per channel rather than one per video.
                already = self._list_downloaded(url) or set()
                # Everything in the (bounded) listing we don't already have —
                # what the dashboard's "New Available" card reports.
                entry['pending'] = sum(1 for v in videos
                                       if v.get('id') and v['id'] not in already)

                dest = channel.get('plex_media_path')
                low, free = self._space_is_low(dest, settings)
                if low:
                    entry['skipped'] = (
                        f'only {free / GIB:.1f} GB free at the destination '
                        f"(floor {settings['min_free_gb']} GB)")
                    results.append(entry)
                    continue

                for video in videos:
                    if entry['started'] >= settings['max_per_channel']:
                        break
                    vid = video.get('id')
                    if not vid or vid in already:
                        continue
                    self._start_download(video, channel)
                    entry['started'] += 1
                    total_started += 1
                self._record_success(url)
            except Exception as exc:  # noqa: BLE001
                entry['error'] = f'{type(exc).__name__}: {exc}'
                self._record_failure(url)
            results.append(entry)

        duration = self._clock() - started_at
        with self._lock:
            self._state.update({
                'last_run': started_at,
                'last_run_duration': round(duration, 2),
                'checked_channels': len(channels),
                'started_downloads': total_started,
                'last_results': results,
                'last_skip_reason': skip_reason,
                'total_ticks': self._state['total_ticks'] + 1,
            })

        retention_outcome = self._maybe_run_retention(config)
        self._notify(config, results, total_started, skip_reason, retention_outcome)
        return results

    # --------------------------------------------------------------- backoff
    def _should_skip_for_backoff(self, url):
        with self._lock:
            info = self._backoff.get(url)
            if not info or info.get('skip_ticks', 0) <= 0:
                return False
            info['skip_ticks'] -= 1
            return True

    def _record_failure(self, url):
        with self._lock:
            info = self._backoff.setdefault(url, {'failures': 0, 'skip_ticks': 0})
            info['failures'] += 1
            # 1, 2, 4, 8, 8, 8 … — a permanently dead channel settles into one
            # attempt every MAX_BACKOFF_TICKS ticks instead of every tick.
            info['skip_ticks'] = min(2 ** (info['failures'] - 1), MAX_BACKOFF_TICKS)

    def _record_success(self, url):
        with self._lock:
            self._backoff.pop(url, None)

    # -------------------------------------------------------------- retention
    def _maybe_run_retention(self, config):
        """Prune after a tick, if the operator opted in.

        v1.5.0 shipped retention as a manual button only, which meant unattended
        monitoring had no bound on disk use — the exact thing retention exists to
        prevent. Still opt-in (`retention.auto_sweep`), because this deletes
        media and nobody should discover that by surprise after an upgrade.
        """
        if not self._run_retention:
            return None
        ret_cfg = (config or {}).get('retention') or {}
        if not (ret_cfg.get('enabled') and ret_cfg.get('auto_sweep')):
            return None
        try:
            outcome = self._run_retention()
        except Exception as exc:  # noqa: BLE001
            outcome = {'error': f'{type(exc).__name__}: {exc}'}
        with self._lock:
            self._state['last_retention'] = outcome
        return outcome

    # ------------------------------------------------------------ notification
    def _notify(self, config, results, total_started, skip_reason, retention_outcome):
        """Summarise a tick — but only when there's something worth saying.

        A quiet tick every hour with nothing new is the normal case; notifying on
        it would train the user to ignore the channel.
        """
        errors = [r for r in results if r.get('error')]
        skips = [r for r in results if r.get('skipped')]
        pruned = (retention_outcome or {}).get('deleted') or []
        if not (total_started or errors or skip_reason or skips or pruned):
            return

        lines = []
        if skip_reason:
            lines.append(skip_reason)
        if total_started:
            lines.append(f'Queued {total_started} new video(s).')
        for r in results:
            if r.get('error'):
                lines.append(f"{r['channel']}: ERROR {r['error']}")
            elif r.get('skipped'):
                lines.append(f"{r['channel']}: skipped — {r['skipped']}")
            elif r.get('started'):
                lines.append(f"{r['channel']}: {r['started']} queued")
        if pruned:
            freed = (retention_outcome.get('freed_bytes') or 0) / GIB
            lines.append(f'Retention removed {len(pruned)} file(s), freed {freed:.2f} GB.')

        if errors:
            title = 'Vidshelf: channel check found problems'
        elif skip_reason or skips:
            title = 'Vidshelf: channel check skipped work'
        else:
            title = f'Vidshelf: queued {total_started} new video(s)'
        notify.send(config, notify.EVENT_SCHEDULER_SUMMARY, title, '\n'.join(lines))

    # ----------------------------------------------------------------- loop
    def _loop(self):
        # A first tick immediately on start would fire during container startup,
        # while mounts may still be settling. Wait one interval first.
        while not self._stop.is_set():
            settings = self._settings()
            sleep_for = settings['interval_minutes'] * 60
            with self._lock:
                self._state['running'] = True
                self._state['next_run'] = (self._clock() + sleep_for
                                           if settings['enabled'] else None)

            # Interruptible wait, so toggling the setting or asking for a manual
            # run takes effect now rather than up to an hour later.
            self._wake.wait(timeout=sleep_for)
            if self._wake.is_set():
                self._wake.clear()
            if self._stop.is_set():
                break

            if not self._settings()['enabled']:
                continue
            try:
                self.run_once()
            except Exception as exc:  # noqa: BLE001
                # run_once already captures per-channel errors, so reaching here
                # means something unexpected. Record it and keep looping — a
                # monitor that dies silently is worse than one reporting a bad
                # tick.
                with self._lock:
                    self._state['last_error'] = f'{type(exc).__name__}: {exc}'
                traceback.print_exc()

        with self._lock:
            self._state['running'] = False

    def start(self):
        if self._thread and self._thread.is_alive():
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name='channel-monitor',
                                        daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self._stop.set()
        self._wake.set()

    def trigger(self):
        """Ask the loop to run now (used by the 'Check now' button)."""
        self._wake.set()
