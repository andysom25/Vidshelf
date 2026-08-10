"""Title and folder-name handling, shared by the download path and the Plex path.

These functions used to live in artwork_sync.py, which meant the only code that
could clean a title was the code that talked to Plex — i.e. cleaning could only
ever happen *after* a file had already been written with a bad name. That
ordering is what made three separate incidents in REFERENCE.md unfixable:

  - the Nine Inch Nails videos whose titles contain no " - " separator at all,
    so the smart collection filter `title contains "Nine Inch Nails -"` matches
    nothing (REFERENCE.md "The Artist - Song assumption")
  - a Dead Weather video sitting in the Raconteurs folder
  - the "Weird Al" catalogue, whose own uploads use stylized quote characters
    inconsistently

The log concluded normalisation was impossible because you cannot guess a song
title from a title with no separator. That is true of plex_clean_video_titles(),
which reverse-engineers from a Plex item after the fact. It is *not* true on the
music-video download path, where the artist is a user-supplied input that
_resolve_existing_artist() has already snapped to the canonical folder name. The
information isn't missing there; it was simply being thrown away.

So: this module holds the pure, network-free string logic. artwork_sync.py
re-exports the names it used to own, so nothing outside had to move.
"""

import re

# ---------------------------------------------------------------------------
# Folder name <-> artist name
# ---------------------------------------------------------------------------

def folder_to_artist(folder_name):
    """Convert a filesystem folder name back to a clean artist name.

    Foo_Fighters  → "Foo Fighters"
    The_Killers   → "The Killers"
    Sugar_Ray     → "Sugar Ray"
    AC_DC         → "AC/DC" (edge case, keep as AC_DC since we can't know)
    """
    name = folder_name.strip()
    # Replace underscores with spaces
    name = name.replace('_', ' ')
    # Collapse multiple spaces
    name = re.sub(r'\s+', ' ', name).strip()
    return name


def artist_to_folder(artist):
    """Convert an artist name to a safe folder name (mirrors _sanitize_folder_name)."""
    invalid_chars = '<>:"/\\|?*'
    for c in invalid_chars:
        artist = artist.replace(c, '_')
    artist = artist.strip().strip('.')
    artist = re.sub(r'[_\s]+', '_', artist)
    if not artist:
        artist = 'Unknown_Artist'
    return artist


# ---------------------------------------------------------------------------
# Title cleaning
# ---------------------------------------------------------------------------

_TRAILING_YOUTUBE_ID_RE = re.compile(r'-[A-Za-z0-9_-]{11}$')
_TRAILING_URL_RE = re.compile(r'\s*www\.\S+\s*$', re.IGNORECASE)
_OFFICIAL_VIDEO_PHRASE_RE = re.compile(r'\bofficial(?:\s+hd|\s+music)?\s+video\b', re.IGNORECASE)
# Brackets get the same treatment as parentheses. They did not, and the residue
# was visible in the UI: "Planetary (GO!) [Official Video] [HD]" cleaned to
# "Planetary (GO!) [] [HD]", and "[Official Video - 4K Film Restored]" to
# "[ - 4K Film Restored]". The phrase was removed correctly; the punctuation
# around it was only handled for one bracket style, so uploaders who use square
# brackets got the wreckage. Character classes rather than two sets of patterns,
# so a third style cannot drift out of sync.
_DANGLING_DASH_BEFORE_CLOSE_RE = re.compile(r'\s*-\s*([\)\]])')
_DANGLING_DASH_AFTER_OPEN_RE = re.compile(r'([\(\[])\s*-\s*')
_EMPTY_GROUP_RE = re.compile(r'\(\s*\)|\[\s*\]')
_MULTI_SPACE_RE = re.compile(r'\s{2,}')
_TRAILING_DASH_RE = re.compile(r'\s*-\s*$')

# Some channels title uploads "Artist – Song" with an en dash (U+2013) or em
# dash (U+2014) instead of a plain hyphen (confirmed on The Raconteurs' own
# uploads — 7 of their 8 videos use this). Every artist-prefix match in this
# codebase (plex_ensure_smart_collection's filter, plex_find_videos_by_artist,
# normalize_artist_prefix, the title-card song-title split) looks for a
# literal " - ", so an en/em-dash title silently never matches — no smart
# collection item, no title-card, no casing normalization, with no error
# anywhere. Normalizing here means every downstream consumer reads it off
# Plex's already-cleaned, already-hyphenated title field instead of needing
# its own dash-variant handling.
_EN_EM_DASH_SEPARATOR_RE = re.compile(r'\s[–—]\s')
# Full-width and curly quote characters some artists stylize part of their
# own stage name with in their video titles - e.g. "＂Weird Al＂ Yankovic" /
# "＂Weird＂ Al Yankovic" (confirmed inconsistent even across that one
# artist's own uploads). Deliberately narrow (not ASCII " or ') so this
# never touches a song title that's legitimately quoted, e.g. Death Cab's
# "＂Black Sun＂" - see _strip_artist_prefix_quotes() below for why only the
# portion before the first " - " ever gets this treatment.
_QUOTE_DECORATION_CHARS_RE = re.compile(r'[＂“”]')


def _strip_artist_prefix_quotes(title):
    """Strip decorative quote characters from the ARTIST-NAME portion of a
    title only (everything before the first " - "), leaving any quotes in
    the song-title portion untouched. Without this, a title like
    '＂Weird Al＂ Yankovic - Eat It' never matches the plain "ArtistName -"
    prefix every collection filter / title-card / casing-normalization step
    in this codebase looks for - not because the artist name is wrong, but
    because of stylized punctuation actually inside it.
    """
    parts = title.split(' - ', 1)
    if len(parts) != 2:
        return title
    artist_part, rest = parts
    cleaned_artist = _QUOTE_DECORATION_CHARS_RE.sub('', artist_part)
    cleaned_artist = _MULTI_SPACE_RE.sub(' ', cleaned_artist).strip()
    if cleaned_artist == artist_part:
        return title
    return f'{cleaned_artist} - {rest}'


def clean_video_title(raw_title):
    """Strip the trailing YouTube ID, any embedded artist-website URL, and
    generic "(Official [HD/Music] Video)" boilerplate from a raw video title,
    while preserving other meaningful parenthetical text (e.g. "(US Version)").

    Returns the original title unchanged if cleaning would produce an empty
    or identical result.
    """
    title = raw_title
    title = _EN_EM_DASH_SEPARATOR_RE.sub(' - ', title)
    title = _strip_artist_prefix_quotes(title)
    title = _TRAILING_YOUTUBE_ID_RE.sub('', title)
    title = _TRAILING_URL_RE.sub('', title)
    title = _OFFICIAL_VIDEO_PHRASE_RE.sub('', title)
    title = _DANGLING_DASH_BEFORE_CLOSE_RE.sub(r'\1', title)
    title = _DANGLING_DASH_AFTER_OPEN_RE.sub(r'\1', title)
    title = _EMPTY_GROUP_RE.sub('', title)
    title = _MULTI_SPACE_RE.sub(' ', title)
    title = _TRAILING_DASH_RE.sub('', title)
    title = title.strip()

    if not title or title == raw_title:
        return raw_title
    return title


def normalize_artist_prefix(title, canonical_names):
    """Rewrite the "ArtistName -" prefix of title to match its canonical
    capitalization, leaving everything else (including the song title)
    untouched. No-op if title doesn't start with any known artist's prefix."""
    for name in canonical_names:
        prefix = f"{name} -"
        if title.lower().startswith(prefix.lower()) and not title.startswith(prefix):
            return name + title[len(name):]
    return title


# ---------------------------------------------------------------------------
# Download-time naming
# ---------------------------------------------------------------------------

# Characters a filename cannot contain. Deliberately the same set
# artist_to_folder() rejects, so a name is safe on Windows shares too — the
# media root is usually CIFS.
_UNSAFE_FILENAME_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_filename(name, fallback='video'):
    """Make a string safe to use as a filename stem on Windows, SMB and Linux.

    Does NOT touch the extension or the -<video_id> suffix; callers append
    those. A trailing dot or space is stripped because Windows silently drops
    them, which would make the written name differ from the name we recorded.
    """
    cleaned = _UNSAFE_FILENAME_CHARS_RE.sub('_', name or '')
    cleaned = _MULTI_SPACE_RE.sub(' ', cleaned).strip().rstrip('. ')
    # Not just "is it empty": '///' substitutes to '___', which is non-empty and
    # useless. Anything with no letters or digits left is worse than the video id.
    if not re.search(r'[A-Za-z0-9]', cleaned):
        return fallback
    return cleaned


def _looks_like(a, b):
    """Loose equality for artist names: case, spacing and punctuation-insensitive."""
    norm = lambda s: re.sub(r'[^a-z0-9]', '', (s or '').lower())
    return bool(norm(a)) and norm(a) == norm(b)


def build_music_video_title(artist, raw_title, info=None):
    """Resolve the "Artist - Song" title for a music-video download.

    The whole Plex feature set (smart collections, title cards, casing
    normalization) keys off a title starting with "Artist - ". Uploaders honour
    that inconsistently, and the Plex-side cleanup cannot invent a separator
    that was never there. Here the artist is known, so it can.

    Three tiers, most trustworthy first:

    1. yt-dlp's parsed music metadata (`artist` + `track`). Present for VEVO and
       topic uploads, already structured, and free — we pre-extract anyway.
    2. The raw title already has a " - " and its prefix matches the artist we
       were given. Clean it and normalise the casing; this is the common case.
    3. No usable separator (the Nine Inch Nails case). Prepend the known artist,
       first removing a bare leading copy of the artist name so we don't emit
       "Nine Inch Nails - Nine Inch Nails Closer". Splitting the song title
       perfectly is not the goal — matching `title contains "Artist -"` is.

    Returns a plain string, unsanitized; callers pass it through
    sanitize_filename() for a path or use it directly as a display title.
    """
    artist = (artist or '').strip()
    cleaned = clean_video_title(raw_title or '')

    # Tier 1 — structured metadata from YouTube Music.
    if info:
        meta_artist = (info.get('artist') or '').strip()
        meta_track = (info.get('track') or '').strip()
        if meta_artist and meta_track:
            # Prefer the artist the user is filing this under, so the folder,
            # the collection and the title all agree. Fall back to yt-dlp's when
            # we have nothing better.
            name = artist if artist else meta_artist.split(',')[0].strip()
            return f'{name} - {meta_track}'

    if not artist:
        return cleaned

    # Tier 2 — a separator is already present and the prefix is this artist.
    if ' - ' in cleaned:
        prefix = cleaned.split(' - ', 1)[0]
        if _looks_like(prefix, artist):
            return normalize_artist_prefix(cleaned, [artist])
        # A separator belonging to something else (e.g. "Song - Live at X").
        # Fall through and prepend, rather than trusting a prefix that isn't
        # the artist.

    # Tier 3 — no usable prefix. Strip a bare leading artist name if present,
    # then prepend the canonical one.
    rest = cleaned
    norm_artist = re.sub(r'[^a-z0-9]', '', artist.lower())
    probe = re.sub(r'[^a-z0-9]', '', rest.lower())
    if norm_artist and probe.startswith(norm_artist):
        # Walk the raw string to find where the artist portion ends, since the
        # normalised comparison dropped spacing and punctuation.
        consumed = 0
        seen = 0
        for i, ch in enumerate(rest):
            if re.match(r'[a-z0-9]', ch.lower()):
                seen += 1
            if seen == len(norm_artist):
                consumed = i + 1
                break
        candidate = rest[consumed:].lstrip(' -–—:').strip()
        if candidate:
            rest = candidate
        else:
            # The title was nothing but the artist name. Prepending would give
            # "Metallica - Metallica"; there is no song title to be had here.
            return artist

    return f'{artist} - {rest}' if rest else artist
