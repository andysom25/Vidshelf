"""Download-time title resolution — the v1.8.0 fix for the "Artist - Song"
assumption.

    python tests/test_titles.py

Every Plex feature in this app (smart collections, title cards, casing
normalization) keys off a title that starts with "Artist - ". REFERENCE.md
records three separate incidents caused by uploaders not following that, and
concludes it can't be fixed because you can't guess a song title out of a title
with no separator.

That's true of the *Plex-side* cleanup, which reverse-engineers from an item
after the fact. It isn't true on the music-video download path, where the artist
is a user-supplied input already snapped to the canonical folder name. These
tests pin that distinction — they're pure string functions, so no network, no
Plex and no CIFS mount is involved, which is rare enough in this repo to be
worth saying out loud.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from titles import (  # noqa: E402
    build_music_video_title,
    clean_video_title,
    normalize_artist_prefix,
    sanitize_filename,
    folder_to_artist,
    artist_to_folder,
)


# --------------------------------------------------------------------------
# Tier 1 — structured metadata from YouTube Music
# --------------------------------------------------------------------------

def test_uses_ytdlp_artist_and_track_when_both_present():
    got = build_music_video_title(
        'Nirvana', 'Nirvana - Smells Like Teen Spirit (Official Music Video)',
        {'artist': 'Nirvana', 'track': 'Smells Like Teen Spirit'})
    assert got == 'Nirvana - Smells Like Teen Spirit', got


def test_prefers_the_users_artist_over_ytdlps_when_they_differ():
    """The user's artist decides the folder and the collection, so the title has
    to agree with it — otherwise the smart filter looks for a prefix the file
    doesn't have."""
    got = build_music_video_title(
        'Weird Al Yankovic', 'whatever',
        {'artist': '"Weird Al" Yankovic', 'track': 'Eat It'})
    assert got == 'Weird Al Yankovic - Eat It', got


def test_falls_back_to_ytdlp_artist_when_none_supplied():
    got = build_music_video_title(
        '', 'x', {'artist': 'Radiohead, Thom Yorke', 'track': 'Creep'})
    assert got == 'Radiohead - Creep', got


def test_partial_metadata_does_not_trigger_tier_one():
    """Only a track, no artist — must not produce a bare song title."""
    got = build_music_video_title(
        'Foo Fighters', 'Foo Fighters - Everlong', {'track': 'Everlong'})
    assert got == 'Foo Fighters - Everlong', got


# --------------------------------------------------------------------------
# Tier 2 — the title already has a usable prefix
# --------------------------------------------------------------------------

def test_cleans_an_existing_prefix_and_strips_boilerplate():
    got = build_music_video_title(
        'Foo Fighters', 'Foo Fighters - Everlong (Official Music Video)')
    assert got == 'Foo Fighters - Everlong', got


def test_normalizes_casing_to_the_canonical_artist():
    got = build_music_video_title('Foo Fighters', 'FOO FIGHTERS - Everlong')
    assert got == 'Foo Fighters - Everlong', got


def test_en_dash_separator_is_accepted():
    """The Raconteurs use an en dash in 7 of their 8 uploads; a literal ' - '
    match would silently never fire."""
    got = build_music_video_title('The Raconteurs',
                                  'The Raconteurs – Steady, As She Goes')
    assert got == 'The Raconteurs - Steady, As She Goes', got


def test_stylized_quotes_in_the_artist_portion_are_stripped():
    got = build_music_video_title(
        'Weird Al Yankovic', '＂Weird Al＂ Yankovic - Eat It')
    assert got == 'Weird Al Yankovic - Eat It', got


def test_a_separator_that_is_not_the_artist_does_not_get_trusted():
    """'Song - Live at Wembley' has a separator, but the prefix is the song.
    Trusting it would file the video under an artist named after the song."""
    got = build_music_video_title('Oasis', 'Wonderwall - Live at Wembley')
    assert got.startswith('Oasis - '), got
    assert 'Wonderwall' in got, got


# --------------------------------------------------------------------------
# Tier 3 — no usable separator at all (the Nine Inch Nails case)
# --------------------------------------------------------------------------

def test_prepends_the_artist_when_there_is_no_separator():
    """REFERENCE.md's example: the collection filter is
    `title contains "Nine Inch Nails -"`, so with no separator it matches zero.
    Splitting the song title perfectly is not the goal; matching is."""
    got = build_music_video_title('Nine Inch Nails', 'Closer')
    assert got == 'Nine Inch Nails - Closer', got


def test_does_not_double_the_artist_name():
    got = build_music_video_title('Nine Inch Nails', 'Nine Inch Nails Closer')
    assert got == 'Nine Inch Nails - Closer', got


def test_leading_artist_match_ignores_punctuation_and_case():
    got = build_music_video_title('AC/DC', 'ACDC Thunderstruck')
    assert got == 'AC/DC - Thunderstruck', got


def test_title_that_is_only_the_artist_name_does_not_produce_a_dangling_dash():
    got = build_music_video_title('Metallica', 'Metallica')
    assert got == 'Metallica', got
    assert not got.endswith('-'), got


def test_no_artist_supplied_falls_back_to_a_cleaned_title():
    got = build_music_video_title('', 'Some Band - A Song (Official Video)')
    assert got == 'Some Band - A Song', got


# --------------------------------------------------------------------------
# Filename safety — the media root is usually a Windows SMB share
# --------------------------------------------------------------------------

def test_sanitize_filename_replaces_characters_windows_rejects():
    got = sanitize_filename('AC/DC - Back In Black: Live?')
    for bad in '<>:"/\\|?*':
        assert bad not in got, f'{bad!r} survived in {got!r}'


def test_sanitize_filename_strips_trailing_dot_and_space():
    """Windows silently drops these, which would make the written filename
    differ from the one we recorded."""
    assert sanitize_filename('Song Title. ') == 'Song Title'


def test_sanitize_filename_falls_back_rather_than_returning_empty():
    assert sanitize_filename('///', fallback='dQw4w9WgXcQ') == 'dQw4w9WgXcQ'


def test_sanitize_filename_leaves_a_normal_title_alone():
    assert sanitize_filename('Foo Fighters - Everlong') == 'Foo Fighters - Everlong'


# --------------------------------------------------------------------------
# The helpers moved out of artwork_sync.py — behaviour must be identical
# --------------------------------------------------------------------------

def test_clean_video_title_still_strips_the_trailing_youtube_id():
    assert clean_video_title('Foo - Bar-dQw4w9WgXcQ') == 'Foo - Bar'


def test_clean_video_title_returns_the_original_when_cleaning_empties_it():
    assert clean_video_title('Official Video') == 'Official Video'


def test_boilerplate_in_square_brackets_leaves_no_residue():
    """Regression, v1.10.0. The dangling-dash and empty-group rules only knew
    about parentheses, so removing the phrase from a square-bracketed title left
    the brackets standing. Both of these were visible on the dashboard's
    Recently Added panel, and would have gone into filenames on the next
    download."""
    assert (clean_video_title('My Chemical Romance - Planetary (GO!) [Official Video] [HD]')
            == 'My Chemical Romance - Planetary (GO!) [HD]')
    assert (clean_video_title('MCR - I Am Not Okay (I Promise) [Official Video - 4K Film Restored]')
            == 'MCR - I Am Not Okay (I Promise) [4K Film Restored]')
    assert clean_video_title('X - Y [Official Music Video]') == 'X - Y'
    assert clean_video_title('A - B [Official Video][HD]') == 'A - B [HD]'


def test_parenthesised_boilerplate_still_leaves_no_residue():
    """The paren cases the bracket fix generalised — same rules now serve both,
    so this guards against fixing one style by breaking the other."""
    assert clean_video_title('X - Y (Official Video)') == 'X - Y'
    assert clean_video_title('X - Y (Official Video) [4K]') == 'X - Y [4K]'
    assert clean_video_title('X - Y (Official Video - Remastered)') == 'X - Y (Remastered)'


def test_meaningful_bracketed_text_is_preserved():
    """The point of removing only the boilerplate phrase: brackets that carry
    real information must survive untouched."""
    assert clean_video_title('Keep [US Version] intact') == 'Keep [US Version] intact'
    assert clean_video_title('Song [Live at Wembley]') == 'Song [Live at Wembley]'


def test_no_cleaned_title_keeps_an_empty_bracket_pair():
    """Property-style sweep over the shapes uploaders actually use, so a future
    edit to one rule cannot reintroduce residue in a combination nobody wrote a
    named test for."""
    phrases = ['Official Video', 'Official Music Video', 'Official HD Video']
    wrappers = ['({})', '[{}]', '- {}', '({} - Remastered)', '[{} - 4K]']
    for phrase in phrases:
        for wrapper in wrappers:
            raw = f'Artist - Song {wrapper.format(phrase)}'
            out = clean_video_title(raw)
            for residue in ('()', '[]', '( ', ' )', '[ ', ' ]', ' -)', ' -]'):
                assert residue not in out, f'{raw!r} -> {out!r} left {residue!r}'
            assert not out.endswith('-'), f'{raw!r} -> {out!r}'
            assert 'Song' in out, f'{raw!r} -> {out!r} lost the song title'


def test_normalize_artist_prefix_is_a_noop_for_unknown_artists():
    assert normalize_artist_prefix('Xyz - Song', ['Foo']) == 'Xyz - Song'


def test_folder_and_artist_round_trip():
    assert folder_to_artist('Foo_Fighters') == 'Foo Fighters'
    assert artist_to_folder('Foo Fighters') == 'Foo_Fighters'


def test_artwork_sync_still_exports_the_moved_helpers():
    """app.py and artwork_swap.py import these from artwork_sync by their old
    private names. The re-export is what let titles.py be extracted without
    touching either."""
    import artwork_sync
    for name in ('folder_to_artist', 'artist_to_folder', '_clean_video_title',
                 '_normalize_artist_prefix', '_strip_artist_prefix_quotes'):
        assert hasattr(artwork_sync, name), f'artwork_sync.{name} disappeared'
    assert artwork_sync._clean_video_title('A - B-dQw4w9WgXcQ') == 'A - B'


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
