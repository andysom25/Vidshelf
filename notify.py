"""Outbound notifications for unattended events.

Deliberately hand-rolled on `requests` rather than pulling in Apprise. Apprise
would give ~80 targets for one line of config, but it adds five transitive pins
(requests-oauthlib, oauthlib, markdown, PyYAML, tzdata) to a project that keeps
its dependency surface small on purpose and runs its tests with none. The five
services below cover what self-hosters actually point this at; if that stops
being true, Apprise is the right upgrade and this module is the thing to delete.

**No SSRF guard here, on purpose.** artwork_sync.py guards its URL fetches
because those hosts come from a public search API. This URL is typed in by the
admin and almost always points *into* their own LAN — an ntfy or Gotify
container on the same host, a Home Assistant webhook. Refusing private
addresses would break the primary use case. The threat model differs: an admin
configuring their own egress target is not the same as an untrusted host
arriving from a third-party API.

Every failure is swallowed and logged. A broken notification URL must never
stop a download or wedge the scheduler.
"""

import json
import threading
import time
from urllib.parse import urlparse

import requests

TIMEOUT_SECONDS = 10

# Notification kinds, so config can enable them individually.
EVENT_DOWNLOAD_COMPLETE = 'download_complete'
EVENT_DOWNLOAD_FAILED = 'download_failed'
EVENT_SCHEDULER_SUMMARY = 'scheduler_summary'
EVENT_RETENTION = 'retention'
EVENT_TEST = 'test'

ALL_EVENTS = (
    EVENT_DOWNLOAD_COMPLETE,
    EVENT_DOWNLOAD_FAILED,
    EVENT_SCHEDULER_SUMMARY,
    EVENT_RETENTION,
)

# Events that are on when notifications are first enabled. Completions are off
# by default: a bulk download would otherwise emit dozens of messages, which is
# the fastest way to get someone to mute the channel entirely.
DEFAULT_EVENTS = (EVENT_DOWNLOAD_FAILED, EVENT_SCHEDULER_SUMMARY, EVENT_RETENTION)


def detect_kind(url):
    """Infer the payload shape from the URL, so the common cases need no config."""
    host = (urlparse(url).hostname or '').lower()
    path = (urlparse(url).path or '').lower()
    if 'discord.com' in host or 'discordapp.com' in host:
        return 'discord'
    if 'hooks.slack.com' in host:
        return 'slack'
    if 'ntfy' in host:
        return 'ntfy'
    # Gotify's endpoint is /message, usually with ?token=
    if path.rstrip('/').endswith('/message'):
        return 'gotify'
    return 'webhook'


def _build_request(kind, url, title, message, event):
    """Return (kwargs for requests.post) for the given target shape."""
    text = f'{title}\n{message}' if message else title

    if kind == 'discord':
        # Discord rejects an empty content and truncates at 2000 chars.
        return {'json': {'content': text[:1900]}}
    if kind == 'slack':
        return {'json': {'text': text[:3000]}}
    if kind == 'ntfy':
        # ntfy takes the body as plain text and the title as a header. Headers
        # must be latin-1 encodable, so strip anything that isn't.
        safe_title = title.encode('ascii', 'replace').decode('ascii')
        return {
            'data': message.encode('utf-8') if message else text.encode('utf-8'),
            'headers': {'Title': safe_title, 'Tags': 'vidshelf'},
        }
    if kind == 'gotify':
        return {'json': {'title': title, 'message': message or title}}
    # Generic: send everything and let the receiver decide.
    return {
        'json': {
            'source': 'vidshelf',
            'event': event,
            'title': title,
            'message': message,
            'timestamp': int(time.time()),
        }
    }


def send(config, event, title, message='', _async=True):
    """Fire a notification if config enables it for this event.

    Returns True if a send was attempted (or queued), False if notifications
    are disabled, unconfigured, or this event isn't selected. Never raises.
    """
    cfg = (config or {}).get('notifications') or {}
    if not cfg.get('enabled'):
        return False
    url = (cfg.get('url') or '').strip()
    if not url:
        return False
    if event != EVENT_TEST:
        enabled_events = cfg.get('events') or list(DEFAULT_EVENTS)
        if event not in enabled_events:
            return False

    kind = cfg.get('kind') or 'auto'
    if kind == 'auto':
        kind = detect_kind(url)

    def _do():
        try:
            kwargs = _build_request(kind, url, title, message, event)
            resp = requests.post(url, timeout=TIMEOUT_SECONDS, **kwargs)
            if resp.status_code >= 400:
                print(f'[notify] {kind} target returned HTTP {resp.status_code}')
        except Exception as exc:  # noqa: BLE001
            # Never let a notification failure propagate into a download or the
            # scheduler loop — that would turn a cosmetic problem into an outage.
            print(f'[notify] failed to send {event} via {kind}: {exc}')

    if _async:
        # Off the caller's thread: a hanging webhook shouldn't stall a download
        # worker or delay the next scheduler tick.
        threading.Thread(target=_do, name='notify', daemon=True).start()
        return True
    _do()
    return True


def send_test(config):
    """Synchronous send used by the Settings page's Test button.

    Returns (ok, detail) so the UI can show why it failed instead of claiming
    success and leaving the user to wonder.
    """
    cfg = (config or {}).get('notifications') or {}
    url = (cfg.get('url') or '').strip()
    if not url:
        return False, 'No notification URL configured'

    kind = cfg.get('kind') or 'auto'
    if kind == 'auto':
        kind = detect_kind(url)
    try:
        kwargs = _build_request(kind, url, 'Vidshelf test notification',
                                'If you can read this, notifications are working.',
                                EVENT_TEST)
        resp = requests.post(url, timeout=TIMEOUT_SECONDS, **kwargs)
        if resp.status_code >= 400:
            return False, f'{kind} target returned HTTP {resp.status_code}'
        return True, f'Sent via {kind} (HTTP {resp.status_code})'
    except Exception as exc:  # noqa: BLE001
        return False, f'{type(exc).__name__}: {exc}'
