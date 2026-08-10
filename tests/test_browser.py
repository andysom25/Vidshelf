"""Browser smoke tests — the bugs that only a rendered page reveals.

    python tests/test_browser.py                 # needs a running Vidshelf
    VIDSHELF_URL=http://127.0.0.1:5000 \
    VIDSHELF_PASSWORD=... python tests/test_browser.py

**Skips cleanly when Playwright isn't installed, or when no instance is
configured.** That is deliberate and matches how `coverage` is treated in this
project: useful locally, never a hard dependency, never in requirements.txt, and
CI stays dependency-free. A skipped run exits 0.

Why this file exists. Every other suite here asserts on source text or on JSON
from the test client, and three bugs shipped anyway because they lived in the gap
between "the data is right" and "the page is right":

- Top artists bars rendered at **0px**. The DOM was correct, the widths were
  correct, and `display: inline` silently ignored them. Nothing threw.
- The dashboard fetched `/api/channels` on a 60-second timer, which costs ~23s
  per channel against YouTube. Invisible unless you watch the network.
- Panels sat on "Loading…" when an asset failed to arrive, which reads exactly
  like an application bug and wasn't one.

None of those is reachable from Python. All three are trivial in a browser.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

URL = os.environ.get('VIDSHELF_URL', '').rstrip('/')
USERNAME = os.environ.get('VIDSHELF_USERNAME', 'admin')
PASSWORD = os.environ.get('VIDSHELF_PASSWORD', '')


def _skip(reason):
    print(f'SKIP  browser tests: {reason}')
    print('      (set VIDSHELF_URL and VIDSHELF_PASSWORD, and pip install playwright)')
    return 0


def _login(page):
    page.goto(URL + '/login', wait_until='domcontentloaded')
    page.fill('input[name="username"]', USERNAME)
    page.fill('input[name="password"]', PASSWORD)
    page.click('button[type="submit"], input[type="submit"]')
    page.wait_for_url('**/dashboard', timeout=20000)
    # Wait for DATA, not for an element to exist.
    #
    # The first version waited for '#top-artists .dash-row, #top-artists
    # .text-muted' — and .text-muted IS the "Loading…" placeholder, so it
    # matched immediately and every assertion ran against an unpopulated page.
    # On a cold cache that reported the application as broken when it was
    # perfectly healthy; once the cache warmed, the same test passed. A check
    # that depends on timing tells you about the timing.
    page.wait_for_function(
        """() => {
            const el = document.getElementById('stat-artists');
            const t = el ? el.textContent.trim() : '';
            return t && t !== '--';
        }""", timeout=45000)
    page.wait_for_timeout(800)   # let the remaining panels settle


# --------------------------------------------------------------------------
# The tests. Each takes an authenticated page on the dashboard.
# --------------------------------------------------------------------------

def check_bars_have_width(page, fail):
    """v1.9.1's bug: correct widths, ignored because the element was inline.

    Asserts rendered geometry, not the attribute — the attribute was right the
    whole time.
    """
    rows = page.evaluate("""() => [...document.querySelectorAll('#top-artists .dash-row')]
        .map(r => {
            const f = r.querySelector('.dash-row-fill');
            return { label: r.querySelector('.dash-row-label').textContent.trim(),
                     px: f ? Math.round(f.getBoundingClientRect().width) : -1 };
        })""")
    if not rows:
        return                      # empty library is a legitimate state
    for row in rows:
        if row['px'] <= 0:
            fail(f"top-artists bar for {row['label']!r} rendered at {row['px']}px "
                 "— a width on an inline element is ignored")
    # And they must differ, or they are not conveying anything.
    widths = {r['px'] for r in rows}
    if len(rows) > 2 and len(widths) == 1:
        fail(f'every top-artists bar is the same width ({widths.pop()}px) — '
             'the proportions are not being applied')


def check_stat_cards_are_populated(page, fail):
    """Cards stuck on '--' mean a failed fetch, which is what a hanging asset or
    a 500 looks like from the page's point of view."""
    for card in ('stat-artists', 'stat-library-videos', 'stat-disk', 'stat-added-30d'):
        text = page.evaluate(
            f"() => (document.getElementById({card!r})||{{}}).textContent || ''").strip()
        if not text or text == '--':
            fail(f'#{card} is still {text!r} — the dashboard did not populate')


def check_panels_are_not_stuck_loading(page, fail):
    for panel in ('added-chart', 'top-artists', 'recently-added', 'plex-health'):
        text = page.evaluate(
            f"() => (document.getElementById({panel!r})||{{}}).textContent || ''")
        if 'Loading' in text or 'Checking' in text:
            fail(f'#{panel} is still showing a placeholder after load')


def check_refresh_does_not_call_expensive_endpoints(page, fail):
    """v1.9.2's bug: the 60s refresh fetched /api/channels, which resolves each
    channel's name via a live yt-dlp call (~23s each).

    Rather than wait a minute, call the refresh directly and record what it
    requests. This is the assertion that no amount of reading the source
    guarantees, because the call site is three functions away from the timer.
    """
    seen = []
    page.on('request', lambda r: seen.append(r.url))
    page.evaluate('() => loadDashboardStats()')
    page.wait_for_timeout(2500)

    banned = [u for u in seen if '/api/channels' in u and '/videos' not in u]
    if banned:
        fail('a dashboard refresh fetched /api/channels, which does a live '
             f'yt-dlp lookup per channel: {banned[0]}')
    if not any('/api/stats' in u for u in seen):
        fail('a dashboard refresh did not fetch /api/stats at all')


def check_no_console_errors(page_errors, fail):
    for err in page_errors:
        fail(f'console error on the dashboard: {err}')


def check_nothing_overflows_horizontally(page, fail):
    """A panel wider than the viewport is a layout bug you only see by looking."""
    overflow = page.evaluate("""() => {
        const d = document.documentElement;
        return { scroll: d.scrollWidth, client: d.clientWidth };
    }""")
    if overflow['scroll'] > overflow['client'] + 2:
        fail(f"page scrolls horizontally ({overflow['scroll']}px content in "
             f"{overflow['client']}px viewport)")


def main():
    if not URL or not PASSWORD:
        return _skip('VIDSHELF_URL / VIDSHELF_PASSWORD not set')
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return _skip('playwright not installed')

    failures = []

    def fail(msg):
        failures.append(msg)

    with sync_playwright() as p:
        # Prefer Playwright's own browser, fall back to an installed Chrome.
        # `pip install playwright` does not download browsers — that needs a
        # separate `playwright install`, which most people won't have run. A
        # system Chrome is far more likely to be present, and for smoke tests
        # any modern engine will do.
        browser = None
        for launcher in (lambda: p.chromium.launch(),
                         lambda: p.chromium.launch(channel='chrome'),
                         lambda: p.chromium.launch(channel='msedge')):
            try:
                browser = launcher()
                break
            except Exception:   # noqa: BLE001
                continue
        if browser is None:
            return _skip('no browser available — try `playwright install chromium`')

        errors = []
        page = browser.new_page(viewport={'width': 1440, 'height': 900})
        page.on('pageerror', lambda e: errors.append(str(e)))
        try:
            _login(page)
        except Exception as exc:   # noqa: BLE001
            print(f'FAIL  could not reach the dashboard: {exc}')
            browser.close()
            return 1

        checks = [
            ('stat cards populated', lambda: check_stat_cards_are_populated(page, fail)),
            ('panels not stuck loading', lambda: check_panels_are_not_stuck_loading(page, fail)),
            ('top-artists bars have width', lambda: check_bars_have_width(page, fail)),
            ('no horizontal overflow', lambda: check_nothing_overflows_horizontally(page, fail)),
            ('refresh avoids yt-dlp endpoints',
             lambda: check_refresh_does_not_call_expensive_endpoints(page, fail)),
            ('no console errors', lambda: check_no_console_errors(errors, fail)),
        ]
        passed = 0
        for name, fn in checks:
            before = len(failures)
            try:
                fn()
            except Exception as exc:   # noqa: BLE001
                fail(f'{name} raised {type(exc).__name__}: {exc}')
            if len(failures) == before:
                passed += 1
            print(('ok    ' if len(failures) == before else 'FAIL  ') + name)

        browser.close()

    for msg in failures:
        print(f'      {msg}')
    # passed, not len(checks) - len(failures): one check can report several
    # failures, which made the old arithmetic print "-1/6".
    print(f'\n{passed}/{len(checks)} checks passed')
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
