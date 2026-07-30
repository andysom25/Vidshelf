"""Tests for notify.py — target detection, payload shapes, and gating.

    python tests/test_notify.py

No network: requests.post is replaced with a recorder. Sends are forced
synchronous (_async=False) so assertions don't race a daemon thread.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import notify  # noqa: E402


class FakePost:
    """Stands in for requests.post, recording calls."""

    def __init__(self, status=200, raise_exc=None):
        self.status = status
        self.raise_exc = raise_exc
        self.calls = []

    def __call__(self, url, timeout=None, **kwargs):
        self.calls.append({'url': url, 'timeout': timeout, **kwargs})
        if self.raise_exc:
            raise self.raise_exc
        return type('R', (), {'status_code': self.status})()


def with_fake_post(fn, fake):
    original = notify.requests.post
    notify.requests.post = fake
    try:
        return fn()
    finally:
        notify.requests.post = original


def cfg(**over):
    base = {'enabled': True, 'url': 'https://example.invalid/hook',
            'kind': 'auto', 'events': list(notify.ALL_EVENTS)}
    base.update(over)
    return {'notifications': base}


# ------------------------------------------------------------- detect_kind
def test_detect_kind_recognises_each_service():
    cases = {
        'https://discord.com/api/webhooks/1/abc': 'discord',
        'https://discordapp.com/api/webhooks/1/abc': 'discord',
        'https://hooks.slack.com/services/T/B/x': 'slack',
        'https://ntfy.sh/my-topic': 'ntfy',
        'https://ntfy.example.org/topic': 'ntfy',
        'https://gotify.example.org/message?token=abc': 'gotify',
        'https://example.org/some/endpoint': 'webhook',
    }
    for url, expected in cases.items():
        got = notify.detect_kind(url)
        assert got == expected, f'{url} -> {got}, expected {expected}'


# ---------------------------------------------------------- payload shapes
def test_discord_payload_uses_content_and_is_truncated():
    fake = FakePost()
    long_msg = 'x' * 5000
    with_fake_post(lambda: notify.send(
        cfg(kind='discord'), notify.EVENT_TEST, 'Title', long_msg, _async=False), fake)
    body = fake.calls[0]['json']
    assert set(body) == {'content'}, body
    # Discord rejects >2000 chars outright, so it must be clipped before sending.
    assert len(body['content']) <= 1900, len(body['content'])


def test_slack_payload_uses_text():
    fake = FakePost()
    with_fake_post(lambda: notify.send(
        cfg(kind='slack'), notify.EVENT_TEST, 'T', 'M', _async=False), fake)
    assert fake.calls[0]['json'] == {'text': 'T\nM'}


def test_ntfy_sends_body_as_data_with_a_title_header():
    fake = FakePost()
    with_fake_post(lambda: notify.send(
        cfg(kind='ntfy'), notify.EVENT_TEST, 'A title', 'The body', _async=False), fake)
    call = fake.calls[0]
    assert call['data'] == b'The body'
    assert call['headers']['Title'] == 'A title'


def test_ntfy_title_header_is_ascii_safe():
    """HTTP headers must be latin-1 encodable; artist names frequently aren't.

    A raw 'Björk' in a header raises UnicodeEncodeError inside requests, which
    would have surfaced as a failed notification for exactly the library this
    app is built for.
    """
    fake = FakePost()
    with_fake_post(lambda: notify.send(
        cfg(kind='ntfy'), notify.EVENT_TEST, 'Björk — Jóga', 'body', _async=False), fake)
    header = fake.calls[0]['headers']['Title']
    header.encode('latin-1')  # must not raise
    assert '?' in header, header


def test_gotify_payload_shape():
    fake = FakePost()
    with_fake_post(lambda: notify.send(
        cfg(kind='gotify'), notify.EVENT_TEST, 'T', 'M', _async=False), fake)
    assert fake.calls[0]['json'] == {'title': 'T', 'message': 'M'}


def test_generic_webhook_includes_event_metadata():
    fake = FakePost()
    with_fake_post(lambda: notify.send(
        cfg(kind='webhook'), notify.EVENT_DOWNLOAD_FAILED, 'T', 'M', _async=False), fake)
    body = fake.calls[0]['json']
    assert body['source'] == 'vidshelf'
    assert body['event'] == notify.EVENT_DOWNLOAD_FAILED
    assert body['title'] == 'T' and body['message'] == 'M'
    assert isinstance(body['timestamp'], int)


# ------------------------------------------------------------------ gating
def test_disabled_sends_nothing():
    fake = FakePost()
    sent = with_fake_post(lambda: notify.send(
        cfg(enabled=False), notify.EVENT_DOWNLOAD_FAILED, 'T', _async=False), fake)
    assert sent is False and fake.calls == []


def test_missing_url_sends_nothing():
    fake = FakePost()
    sent = with_fake_post(lambda: notify.send(
        cfg(url='  '), notify.EVENT_DOWNLOAD_FAILED, 'T', _async=False), fake)
    assert sent is False and fake.calls == []


def test_unselected_event_is_not_sent():
    fake = FakePost()
    conf = cfg(events=[notify.EVENT_DOWNLOAD_FAILED])
    sent = with_fake_post(lambda: notify.send(
        conf, notify.EVENT_DOWNLOAD_COMPLETE, 'T', _async=False), fake)
    assert sent is False and fake.calls == []


def test_selected_event_is_sent():
    fake = FakePost()
    conf = cfg(events=[notify.EVENT_DOWNLOAD_FAILED])
    sent = with_fake_post(lambda: notify.send(
        conf, notify.EVENT_DOWNLOAD_FAILED, 'T', _async=False), fake)
    assert sent is True and len(fake.calls) == 1


def test_completions_are_off_by_default():
    """A bulk download would otherwise emit dozens of messages, which is the
    fastest route to the user muting the channel."""
    assert notify.EVENT_DOWNLOAD_COMPLETE not in notify.DEFAULT_EVENTS
    assert notify.EVENT_DOWNLOAD_FAILED in notify.DEFAULT_EVENTS
    fake = FakePost()
    conf = {'notifications': {'enabled': True, 'url': 'https://x.invalid/h'}}
    sent = with_fake_post(lambda: notify.send(
        conf, notify.EVENT_DOWNLOAD_COMPLETE, 'T', _async=False), fake)
    assert sent is False, 'completion notified without being enabled'


def test_test_event_bypasses_event_selection():
    """The Settings 'Test' button must work regardless of which events are on."""
    fake = FakePost()
    conf = cfg(events=[])
    sent = with_fake_post(lambda: notify.send(
        conf, notify.EVENT_TEST, 'T', _async=False), fake)
    assert sent is True and len(fake.calls) == 1


# --------------------------------------------------------------- resilience
def test_a_raising_transport_never_propagates():
    """A broken webhook must not take down a download worker or the scheduler."""
    fake = FakePost(raise_exc=RuntimeError('connection refused'))
    sent = with_fake_post(lambda: notify.send(
        cfg(), notify.EVENT_DOWNLOAD_FAILED, 'T', _async=False), fake)
    assert sent is True  # attempted, and swallowed internally


def test_http_error_status_does_not_raise():
    fake = FakePost(status=500)
    with_fake_post(lambda: notify.send(
        cfg(), notify.EVENT_DOWNLOAD_FAILED, 'T', _async=False), fake)
    assert len(fake.calls) == 1


def test_send_test_reports_failure_detail():
    fake = FakePost(status=404)
    ok, detail = with_fake_post(lambda: notify.send_test(cfg()), fake)
    assert ok is False and '404' in detail, detail


def test_send_test_reports_success_detail():
    fake = FakePost(status=200)
    ok, detail = with_fake_post(lambda: notify.send_test(cfg(kind='ntfy')), fake)
    assert ok is True and 'ntfy' in detail, detail


def test_send_test_without_a_url_is_a_clear_error():
    ok, detail = notify.send_test({'notifications': {'enabled': True, 'url': ''}})
    assert ok is False and 'No notification URL' in detail


def test_send_handles_a_missing_notifications_block():
    fake = FakePost()
    for conf in ({}, None, {'notifications': None}):
        sent = with_fake_post(lambda: notify.send(
            conf, notify.EVENT_DOWNLOAD_FAILED, 'T', _async=False), fake)
        assert sent is False
    assert fake.calls == []


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    failures = 0
    for t in tests:
        try:
            t()
        except AssertionError as exc:
            failures += 1
            print(f'FAIL  {t.__name__}: {exc}')
        else:
            print(f'ok    {t.__name__}')
    print(f'\n{len(tests) - failures}/{len(tests)} passed')
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
