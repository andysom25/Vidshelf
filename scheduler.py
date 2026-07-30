"""Periodic channel monitoring — the thing that makes download modes mean something.

Before this existed, `download_mode` was read from exactly one place: the
`/api/channels/download-all` endpoint, which only fires when a human clicks a
button. A channel set to "New Only" therefore downloaded nothing until someone
pressed it. This runs the same decision on a timer.

Collaborators are injected rather than imported from app.py, for two reasons:
app.py imports this module (so importing it back would be circular), and it
makes the loop testable without Flask, a network, or a real yt-dlp call.

## Two behaviours worth knowing

**Already-downloaded videos are always skipped, regardless of mode.** The
manual endpoint treats `all` as "fetch up to 20 whether or not we have them",
which is a reasonable thing to ask for by hand and a catastrophic thing to do
every hour — it would re-download the same videos forever. On a timer, `all`
and `new` behave identically; `all` only differs when a human triggers it.

**`manual` channels are never touched.** That mode means "I decide", and a
scheduler overriding it would be the opposite of what it says.

## Interaction with retention

Deleting a file does **not** remove its tracker entry, so a video pruned by
retention.py is not re-downloaded on the next tick. That is what keeps the two
features from forming a download/delete loop, and it's the reason retention
must never clear tracker entries.
"""

import threading
import time
import traceback

import notify

DEFAULT_INTERVAL_MINUTES = 60
MIN_INTERVAL_MINUTES = 5          # a floor, so a typo can't hammer YouTube
DEFAULT_MAX_PER_CHANNEL = 5       # per tick, so a new channel trickles in
MONITORED_MODES = ('new', 'all')


def _now():
    return time.time()


class ChannelMonitor:
    """Background thread that polls monitored channels on an interval."""

    def __init__(self, load_config, list_videos, is_downloaded, start_download,
                 sleeper=None, clock=None):
        self._load_config = load_config
        self._list_videos = list_videos          # (channel_url) -> [ {id, title}, ... ]
        self._is_downloaded = is_downloaded      # (video_id, channel_url) -> bool
        self._start_download = start_download    # (video, channel_cfg) -> download_id
        self._sleep = sleeper or time.sleep
        self._clock = clock or _now

        self._thread = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._state = {
            'running': False,
            'last_run': None,
            'last_run_duration': None,
            'next_run': None,
            'checked_channels': 0,
            'started_downloads': 0,
            'last_results': [],     # [{channel, found, started, error}]
            'last_error': None,
            'total_ticks': 0,
        }

    # ---------------------------------------------------------------- config
    def _settings(self):
        cfg = (self._load_config() or {}).get('channel_monitor') or {}
        interval = cfg.get('interval_minutes', DEFAULT_INTERVAL_MINUTES)
        try:
            interval = int(interval)
        except (TypeError, ValueError):
            interval = DEFAULT_INTERVAL_MINUTES
        # Clamped rather than rejected: a config typo should slow the loop down,
        # not stop monitoring or spam YouTube every second.
        interval = max(MIN_INTERVAL_MINUTES, interval)

        limit = cfg.get('max_per_channel', DEFAULT_MAX_PER_CHANNEL)
        try:
            limit = max(1, int(limit))
        except (TypeError, ValueError):
            limit = DEFAULT_MAX_PER_CHANNEL

        return bool(cfg.get('enabled')), interval, limit

    def status(self):
        with self._lock:
            state = dict(self._state)
        enabled, interval, limit = self._settings()
        state['enabled'] = enabled
        state['interval_minutes'] = interval
        state['max_per_channel'] = limit
        state['thread_alive'] = bool(self._thread and self._thread.is_alive())
        return state

    # ------------------------------------------------------------------ tick
    def run_once(self):
        """One monitoring pass. Returns the per-channel results.

        Exceptions from a single channel are captured into that channel's
        result rather than raised, so one dead channel can't stop the others or
        kill the loop.
        """
        config = self._load_config() or {}
        _, _, limit = self._settings()
        channels = [c for c in (config.get('channels') or [])
                    if c.get('download_mode') in MONITORED_MODES]

        started_at = self._clock()
        results = []
        total_started = 0

        for channel in channels:
            url = channel.get('url')
            if not url:
                continue
            entry = {'channel': url, 'found': 0, 'started': 0, 'error': None}
            try:
                videos = self._list_videos(url) or []
                entry['found'] = len(videos)
                for video in videos:
                    if entry['started'] >= limit:
                        break
                    vid = video.get('id')
                    if not vid:
                        continue
                    # Always skip what we already have — see the module docstring
                    # on why 'all' does not mean 'all' on a timer.
                    if self._is_downloaded(vid, url):
                        continue
                    try:
                        self._start_download(video, channel)
                    except Exception as exc:  # noqa: BLE001
                        entry['error'] = f'{type(exc).__name__}: {exc}'
                        break
                    entry['started'] += 1
                    total_started += 1
            except Exception as exc:  # noqa: BLE001
                entry['error'] = f'{type(exc).__name__}: {exc}'
            results.append(entry)

        duration = self._clock() - started_at
        with self._lock:
            self._state.update({
                'last_run': started_at,
                'last_run_duration': round(duration, 2),
                'checked_channels': len(channels),
                'started_downloads': total_started,
                'last_results': results,
                'total_ticks': self._state['total_ticks'] + 1,
            })

        self._notify(config, results, total_started)
        return results

    def _notify(self, config, results, total_started):
        """Summarise a tick — but only when there's something worth saying.

        A quiet tick every hour with nothing new is the normal case; notifying
        on it would train the user to ignore the channel.
        """
        errors = [r for r in results if r.get('error')]
        if not total_started and not errors:
            return
        lines = []
        if total_started:
            lines.append(f'Queued {total_started} new video(s).')
        for r in results:
            if r.get('error'):
                lines.append(f"{r['channel']}: ERROR {r['error']}")
            elif r.get('started'):
                lines.append(f"{r['channel']}: {r['started']} queued")
        title = ('Vidshelf: channel check found problems' if errors
                 else f'Vidshelf: queued {total_started} new video(s)')
        notify.send(config, notify.EVENT_SCHEDULER_SUMMARY, title, '\n'.join(lines))

    # ----------------------------------------------------------------- loop
    def _loop(self):
        # A first tick immediately on start would fire during container startup,
        # while mounts may still be settling. Wait one interval first.
        while not self._stop.is_set():
            enabled, interval, _ = self._settings()
            sleep_for = interval * 60
            with self._lock:
                self._state['running'] = True
                self._state['next_run'] = self._clock() + sleep_for if enabled else None

            # Interruptible wait, so toggling the setting or asking for a manual
            # run takes effect now rather than up to an hour later.
            self._wake.wait(timeout=sleep_for)
            if self._wake.is_set():
                self._wake.clear()
            if self._stop.is_set():
                break

            enabled, _, _ = self._settings()
            if not enabled:
                continue
            try:
                self.run_once()
            except Exception as exc:  # noqa: BLE001
                # Belt and braces: run_once already contains per-channel errors,
                # so reaching here means something unexpected. Record it and keep
                # looping — a monitor that dies silently is worse than one that
                # reports a bad tick.
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
