"""Unit tests for the pure logic in transcode.py, artwork_sync.py and app.py.

    python tests/test_media.py

These are the four highest-value coverage gaps identified after measuring:
`transcode.needs_conversion` (a wrong answer either ships incompatible files or
re-encodes a whole library), `artwork_sync._is_safe_download_url` (a security
control that had zero tests), `_clean_video_title` / `folder_to_artist` (pure
string functions with edge cases that have already caused bugs), and the path
helpers in app.py.

No ffmpeg, no network, no Plex: ffprobe is replaced with canned JSON and DNS
resolution with a stub.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# app.py reads/writes state at import, so keep it out of the real data dir.
import tempfile  # noqa: E402
os.environ.setdefault('VIDSHELF_DATA_DIR', tempfile.mkdtemp(prefix='vidshelf-media-test-'))

import artwork_sync  # noqa: E402
import transcode  # noqa: E402
import app as app_module  # noqa: E402


# ------------------------------------------------------- transcode: probing
class FakeProbe:
    """Stands in for subprocess.run, returning canned ffprobe output."""

    def __init__(self, streams=None, returncode=0, stdout=None, raise_exc=None):
        self.returncode = returncode
        self.raise_exc = raise_exc
        if stdout is not None:
            self._stdout = stdout
        else:
            self._stdout = json.dumps({'streams': streams or []})

    def __call__(self, *args, **kwargs):
        if self.raise_exc:
            raise self.raise_exc
        return type('R', (), {'returncode': self.returncode,
                              'stdout': self._stdout, 'stderr': ''})()


def with_probe(fake, fn):
    original = transcode.subprocess.run
    transcode.subprocess.run = fake
    try:
        return fn()
    finally:
        transcode.subprocess.run = original


def _streams(video=None, audio=None):
    out = []
    if video:
        out.append({'codec_type': 'video', 'codec_name': video})
    if audio:
        out.append({'codec_type': 'audio', 'codec_name': audio})
    return out


def test_h264_aac_mp4_needs_no_conversion():
    fake = FakeProbe(_streams('h264', 'aac'))
    assert with_probe(fake, lambda: transcode.needs_conversion('/x/a.mp4')) is False


def test_vp9_needs_conversion():
    fake = FakeProbe(_streams('vp9', 'aac'))
    assert with_probe(fake, lambda: transcode.needs_conversion('/x/a.mp4')) is True


def test_av1_needs_conversion():
    """4K YouTube uploads are frequently AV1, which most Plex clients can't
    direct-play — the whole reason this check exists."""
    fake = FakeProbe(_streams('av1', 'aac'))
    assert with_probe(fake, lambda: transcode.needs_conversion('/x/a.mp4')) is True


def test_opus_audio_needs_conversion():
    fake = FakeProbe(_streams('h264', 'opus'))
    assert with_probe(fake, lambda: transcode.needs_conversion('/x/a.mp4')) is True


def test_webm_container_needs_conversion_even_with_compatible_codecs():
    fake = FakeProbe(_streams('h264', 'aac'))
    assert with_probe(fake, lambda: transcode.needs_conversion('/x/a.webm')) is True


def test_mkv_container_needs_conversion():
    fake = FakeProbe(_streams('h264', 'aac'))
    assert with_probe(fake, lambda: transcode.needs_conversion('/x/a.mkv')) is True


def test_uppercase_extension_is_not_treated_as_a_different_container():
    """probe_media lowercases the extension; a file named .MP4 must not be
    needlessly re-encoded."""
    fake = FakeProbe(_streams('h264', 'aac'))
    assert with_probe(fake, lambda: transcode.needs_conversion('/x/A.MP4')) is False


def test_video_with_no_audio_track_is_left_alone():
    """A silent video is still direct-playable; treating a missing audio codec
    as incompatible would re-encode it pointlessly."""
    fake = FakeProbe(_streams('h264', None))
    assert with_probe(fake, lambda: transcode.needs_conversion('/x/a.mp4')) is False


def test_unprobeable_file_is_left_alone_rather_than_guessed():
    """The safe default: an unreadable or corrupt file must not be fed into a
    re-encode on the assumption that it needs one."""
    for fake in (FakeProbe(returncode=1),
                 FakeProbe(stdout='not json at all'),
                 FakeProbe(raise_exc=OSError('ffprobe missing'))):
        assert with_probe(fake, lambda: transcode.needs_conversion('/x/a.mp4')) is False


def test_probe_media_returns_none_on_failure():
    assert with_probe(FakeProbe(returncode=1),
                      lambda: transcode.probe_media('/x/a.mp4')) is None


def test_probe_media_picks_the_first_stream_of_each_type():
    streams = [
        {'codec_type': 'video', 'codec_name': 'h264'},
        {'codec_type': 'video', 'codec_name': 'mjpeg'},   # embedded cover art
        {'codec_type': 'audio', 'codec_name': 'aac'},
        {'codec_type': 'audio', 'codec_name': 'ac3'},     # secondary track
    ]
    info = with_probe(FakeProbe(streams), lambda: transcode.probe_media('/x/a.mp4'))
    assert info['video_codec'] == 'h264', info
    assert info['audio_codec'] == 'aac', info


# ------------------------------------------------------- SSRF guard
def with_resolver(mapping, fn):
    """Replace getaddrinfo so no DNS is used and results are deterministic."""
    original = artwork_sync.socket.getaddrinfo

    def fake(host, *a, **kw):
        if host not in mapping:
            raise OSError(f'no such host: {host}')
        return [(2, 1, 6, '', (ip, 0)) for ip in mapping[host]]

    artwork_sync.socket.getaddrinfo = fake
    try:
        return fn()
    finally:
        artwork_sync.socket.getaddrinfo = original


PUBLIC = {'cdn.example.org': ['93.184.216.34']}


def test_public_https_url_is_allowed():
    assert with_resolver(PUBLIC, lambda: artwork_sync._is_safe_download_url(
        'https://cdn.example.org/a.jpg')) is True


def test_non_http_schemes_are_rejected():
    for url in ('file:///etc/passwd', 'ftp://cdn.example.org/a.jpg',
                'gopher://cdn.example.org/', 'data:image/png;base64,AAAA', ''):
        assert with_resolver(PUBLIC, lambda u=url: artwork_sync._is_safe_download_url(u)) is False, url


def test_loopback_is_rejected():
    for ip in ('127.0.0.1', '127.1.2.3', '::1'):
        m = {'evil.example.org': [ip]}
        assert with_resolver(m, lambda: artwork_sync._is_safe_download_url(
            'http://evil.example.org/a.jpg')) is False, ip


def test_rfc1918_private_ranges_are_rejected():
    for ip in ('10.0.0.5', '172.16.4.4', '192.168.1.218'):
        m = {'evil.example.org': [ip]}
        assert with_resolver(m, lambda: artwork_sync._is_safe_download_url(
            'http://evil.example.org/a.jpg')) is False, ip


def test_cloud_metadata_endpoint_is_rejected():
    """169.254.169.254 is the canonical SSRF target — it's link-local."""
    m = {'metadata.example.org': ['169.254.169.254']}
    assert with_resolver(m, lambda: artwork_sync._is_safe_download_url(
        'http://metadata.example.org/latest/meta-data/')) is False


def test_a_host_resolving_to_both_public_and_private_is_rejected():
    """The guard must reject if *any* resolved address is internal — otherwise a
    multi-A-record host smuggles in a private target."""
    m = {'mixed.example.org': ['93.184.216.34', '192.168.1.10']}
    assert with_resolver(m, lambda: artwork_sync._is_safe_download_url(
        'http://mixed.example.org/a.jpg')) is False


def test_unresolvable_host_is_rejected():
    assert with_resolver({}, lambda: artwork_sync._is_safe_download_url(
        'http://nope.invalid/a.jpg')) is False


def test_url_without_a_host_is_rejected():
    assert with_resolver(PUBLIC, lambda: artwork_sync._is_safe_download_url(
        'http:///a.jpg')) is False


# ------------------------------------------------------- title / folder names
def test_folder_to_artist_replaces_underscores():
    assert artwork_sync.folder_to_artist('Foo_Fighters') == 'Foo Fighters'
    assert artwork_sync.folder_to_artist('The_Killers') == 'The Killers'


def test_folder_to_artist_collapses_and_trims():
    assert artwork_sync.folder_to_artist('  Sugar__Ray  ') == 'Sugar Ray'


def test_clean_video_title_strips_the_trailing_youtube_id():
    got = artwork_sync._clean_video_title(
        'Nirvana - Smells Like Teen Spirit-hTWKbfoikeg')
    assert got == 'Nirvana - Smells Like Teen Spirit', got


def test_clean_video_title_strips_official_video_boilerplate():
    for raw in ('Foo Fighters - Everlong (Official Music Video)',
                'Foo Fighters - Everlong (Official Video)',
                'Foo Fighters - Everlong (Official HD Video)'):
        got = artwork_sync._clean_video_title(raw)
        assert got == 'Foo Fighters - Everlong', f'{raw!r} -> {got!r}'


def test_clean_video_title_keeps_meaningful_parentheticals():
    """"(US Version)" is information; "(Official Video)" is noise. Stripping
    both would lose the distinction between two different uploads."""
    got = artwork_sync._clean_video_title('Blur - Song 2 (US Version)')
    assert 'US Version' in got, got


def test_clean_video_title_never_returns_empty():
    """The documented contract: never return empty. An empty title in Plex is
    worse than a noisy one.

    Note it guards *empty or identical*, not "unchanged for degenerate input" —
    '---' legitimately cleans to '--' because that's neither empty nor identical.
    Asserting non-emptiness is the invariant that actually matters.
    """
    for raw in ('(Official Music Video)', '---', '', '   ', '(((  )))'):
        got = artwork_sync._clean_video_title(raw)
        assert got != '' or raw == '', f'{raw!r} -> empty'
        assert got is not None, raw
    # The specific case the guard exists for: boilerplate-only titles come back
    # untouched rather than becoming blank.
    assert artwork_sync._clean_video_title(
        '(Official Music Video)') == '(Official Music Video)'


def test_clean_video_title_normalises_en_and_em_dashes():
    for dash in ('–', '—'):
        got = artwork_sync._clean_video_title(f'Artist {dash} Song')
        assert got == 'Artist - Song', repr(got)


# ------------------------------------------------------- app path helpers
def test_sanitize_folder_name_strips_filesystem_hostile_characters():
    got = app_module._sanitize_folder_name('AC/DC: Back\\In "Black"?')
    for bad in '<>:"/\\|?*':
        assert bad not in got, f'{bad!r} survived in {got!r}'
    assert got, 'sanitising produced an empty name'


def test_sanitize_folder_name_collapses_separators():
    assert app_module._sanitize_folder_name('Foo   Fighters') == 'Foo_Fighters'


def test_sanitize_folder_name_trims_trailing_dots_and_spaces():
    """Windows cannot create a directory ending in a dot or space, and this
    library gets written to SMB shares."""
    got = app_module._sanitize_folder_name('  Weird Al.  ')
    assert not got.endswith(('.', ' ', '_')), repr(got)


def test_sanitize_folder_name_handles_an_all_invalid_name():
    """An entirely-punctuation name collapses to '_'.

    That is a usable directory name, so this only pins the invariants that
    matter: non-empty, and free of characters SMB/Windows rejects. It does mean
    two different all-punctuation names would collide on '_', which is
    acknowledged rather than fixed: real artist names contain letters, and
    changing this function's output would orphan every folder created by an
    earlier version — the app would create duplicates alongside the originals.
    Folder-name stability is worth more than tidying a degenerate case.
    """
    got = app_module._sanitize_folder_name('///???')
    assert got, 'produced an empty folder name'
    for bad in '<>:"/\\|?*':
        assert bad not in got, f'{bad!r} survived in {got!r}'


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
