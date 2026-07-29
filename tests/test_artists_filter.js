/*
 * Tests for the Artists page search/filter/sort logic in
 * static/js/dashboard.js.
 *
 *     node tests/test_artists_filter.js
 *
 * Runs the real shipped file rather than a copy of the logic, so these can't
 * drift from what the browser actually loads.
 *
 * dashboard.js is a browser script: it has top-level statements that touch
 * `document` and call fetch(). Those are stubbed below, and the inevitable
 * throw from the init code is caught and ignored — JS hoists all function
 * declarations in a script before executing any of it, so every function is
 * defined even though execution aborts partway. That's what makes testing the
 * pure helpers in isolation possible without restructuring working UI code.
 */

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const JS_PATH = path.join(__dirname, '..', 'static', 'js', 'dashboard.js');
const source = fs.readFileSync(JS_PATH, 'utf8');

// Minimal stubs: enough for the file to parse and for the functions under test
// to run. getElementById returns null so getArtistFilters() falls back to its
// defaults, which is exactly the path a test wants.
const sandbox = {
    document: {
        getElementById: () => null,
        querySelectorAll: () => [],
        querySelector: () => null,
        addEventListener: () => {},
        body: { classList: { add() {}, remove() {}, toggle() {} } },
    },
    window: {},
    console,
    setTimeout,
    clearTimeout,
    setInterval: () => 0,
    clearInterval: () => {},
    fetch: () => Promise.resolve({ json: () => Promise.resolve({}) }),
    localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
    navigator: { userAgent: 'node' },
    location: { href: '', hash: '' },
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;

const context = vm.createContext(sandbox);
try {
    vm.runInContext(source, context, { filename: 'dashboard.js' });
} catch (e) {
    // Expected: init code runs against the stub DOM. Functions are hoisted.
}

const { filterAndSortArtists } = sandbox;
if (typeof filterAndSortArtists !== 'function') {
    console.error('FAIL  filterAndSortArtists was not defined after loading dashboard.js');
    process.exit(1);
}

// --- fixtures -------------------------------------------------------------
const ARTISTS = [
    { artist: 'Foo Fighters',  folder: 'Foo Fighters',  video_count: 12, has_artwork: true },
    { artist: 'Björk',         folder: 'Bjork',         video_count: 3,  has_artwork: true },
    { artist: 'The Beatles',   folder: 'The Beatles',   video_count: 0,  has_artwork: false },
    { artist: 'Nirvana',       folder: 'Nirvana',       video_count: 7,  has_artwork: false },
    { artist: 'a-ha',          folder: 'a-ha',          video_count: 3,  has_artwork: true },
];

const F = (over = {}) => Object.assign(
    { query: '', artwork: 'all', videos: 'all', sort: 'name-asc' }, over);

const names = list => list.map(a => a.artist);

let failures = 0;
function check(label, actual, expected) {
    const a = JSON.stringify(actual), e = JSON.stringify(expected);
    if (a === e) { console.log('ok    ' + label); }
    else { failures++; console.error(`FAIL  ${label}\n        expected ${e}\n        got      ${a}`); }
}

// --- search --------------------------------------------------------------
check('search is case-insensitive substring',
    names(filterAndSortArtists(ARTISTS, F({ query: 'foo' }))), ['Foo Fighters']);

check('search matches mid-word, not just prefix',
    names(filterAndSortArtists(ARTISTS, F({ query: 'beatles' }))), ['The Beatles']);

// Folder names can differ from display names (folder_to_artist rewrites them),
// and someone browsing the filesystem searches for what they saw there.
check('search matches the folder name too (Bjork -> Björk)',
    names(filterAndSortArtists(ARTISTS, F({ query: 'bjork' }))), ['Björk']);

check('no match yields an empty list, not everything',
    names(filterAndSortArtists(ARTISTS, F({ query: 'zzzz' }))), []);

check('empty query keeps everything',
    filterAndSortArtists(ARTISTS, F()).length, ARTISTS.length);

// --- filters -------------------------------------------------------------
// Order here reflects article-insensitive sorting: "The Beatles" sorts as
// "Beatles", ahead of "Nirvana".
check('artwork=missing',
    names(filterAndSortArtists(ARTISTS, F({ artwork: 'missing' }))), ['The Beatles', 'Nirvana']);

check('artwork=has',
    names(filterAndSortArtists(ARTISTS, F({ artwork: 'has' }))), ['a-ha', 'Björk', 'Foo Fighters']);

check('videos=empty finds only zero-video folders',
    names(filterAndSortArtists(ARTISTS, F({ videos: 'empty' }))), ['The Beatles']);

check('videos=has excludes zero-video folders',
    names(filterAndSortArtists(ARTISTS, F({ videos: 'has' }))),
    ['a-ha', 'Björk', 'Foo Fighters', 'Nirvana']);

check('filters combine (missing artwork AND has videos)',
    names(filterAndSortArtists(ARTISTS, F({ artwork: 'missing', videos: 'has' }))), ['Nirvana']);

// 'Foo Fighters' has artwork but contains no letter 'a', so it is correctly
// excluded here -- worth stating, because it looks like an omission otherwise.
check('search combines with filters',
    names(filterAndSortArtists(ARTISTS, F({ query: 'a', artwork: 'has' }))),
    ['a-ha']);

check('search + filter where both match',
    names(filterAndSortArtists(ARTISTS, F({ query: 'i', artwork: 'has' }))),
    ['Foo Fighters']);

// --- sorting -------------------------------------------------------------
check('sort name-asc is locale-aware, so Björk sorts under B not after Z',
    names(filterAndSortArtists(ARTISTS, F({ sort: 'name-asc' }))),
    ['a-ha', 'The Beatles', 'Björk', 'Foo Fighters', 'Nirvana']);

check('sort name-desc reverses it',
    names(filterAndSortArtists(ARTISTS, F({ sort: 'name-desc' }))),
    ['Nirvana', 'Foo Fighters', 'Björk', 'The Beatles', 'a-ha']);

check('sort videos-desc',
    names(filterAndSortArtists(ARTISTS, F({ sort: 'videos-desc' }))),
    ['Foo Fighters', 'Nirvana', 'a-ha', 'Björk', 'The Beatles']);

check('sort videos-asc',
    names(filterAndSortArtists(ARTISTS, F({ sort: 'videos-asc' }))),
    ['The Beatles', 'a-ha', 'Björk', 'Nirvana', 'Foo Fighters']);

// Ties broken by name so the order is stable between renders rather than
// depending on the engine's sort implementation.
check('equal video counts fall back to name order',
    names(filterAndSortArtists(ARTISTS, F({ sort: 'videos-desc' }))).slice(2, 4),
    ['a-ha', 'Björk']);

// Leading articles are ignored when sorting, matching how Plex files the same
// library. "a-ha" must survive: the pattern requires whitespace after the
// article, so it is not read as the article "a".
check('leading "The" is ignored when sorting',
    names(filterAndSortArtists(
        [{ artist: 'The Beatles', video_count: 1 }, { artist: 'Cure', video_count: 1 }],
        F({ sort: 'name-asc' }))),
    ['The Beatles', 'Cure']);

check('"a-ha" is not treated as the article "a"',
    names(filterAndSortArtists(
        [{ artist: 'Zebra', video_count: 1 }, { artist: 'a-ha', video_count: 1 }],
        F({ sort: 'name-asc' }))),
    ['a-ha', 'Zebra']);

check('searching for an article still matches the full name',
    names(filterAndSortArtists(ARTISTS, F({ query: 'the beat' }))), ['The Beatles']);

// --- robustness ----------------------------------------------------------
check('empty input list',
    filterAndSortArtists([], F()), []);

check('missing fields do not throw',
    names(filterAndSortArtists([{ artist: 'X' }], F({ query: 'x' }))), ['X']);

// The input array must not be reordered in place: _artistsAll is the cached
// source of truth and re-sorting it would make the "reset" view order depend
// on whatever sort was applied last.
const before = names(ARTISTS);
filterAndSortArtists(ARTISTS, F({ sort: 'videos-desc' }));
check('does not mutate the caller\'s array', names(ARTISTS), before);

console.log(`\n${failures === 0 ? 'all' : 'some'} tests done — ${failures} failure(s)`);
process.exit(failures ? 1 : 0);
