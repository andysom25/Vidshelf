# Contributing

This is a personal project. Issues and PRs are welcome, but it's maintained in
spare time and there's no support commitment — please read responses as
best-effort.

If you're reporting a **security** problem, don't open an issue: use
[private vulnerability reporting](https://github.com/andysom25/Vidshelf/security/advisories/new).
See [SECURITY.md](SECURITY.md).

## Branching

- **`dev`** is where work happens. Target your PRs here.
- **`main`** is releases only. Every commit on it corresponds to a tagged
  release, and it's protected — no direct pushes, no force-pushes, and CI must
  be green.

## Releasing

Don't tag by hand. Bump `VERSION` on `dev` and merge to `main`; CI does the
rest — it tags the release, builds and pushes multi-arch images to
`ghcr.io/andysom25/vidshelf`, and creates the GitHub release. A merge that
doesn't change `VERSION` releases nothing, which is what makes docs-only merges
to `main` safe.

Semver as you'd expect: patch for fixes, minor for features, major for
breaking changes.

## Running it locally

```bash
pip install -r requirements.txt
python app.py            # http://localhost:5000
```

ffmpeg must be on `PATH` (or set `FFMPEG_PATH`). State lands in `./data` —
override with `VIDSHELF_DATA_DIR`. The admin password is generated and printed
once on first run if `ADMIN_PASSWORD` isn't set.

For development with the interactive debugger: `FLASK_DEBUG=true python app.py`.
Don't do that on anything reachable by others — Werkzeug's debugger executes
arbitrary code.

## Tests

```bash
python tests/test_state.py         # atomic writes, locking, migration
python tests/test_updates.py       # version comparison, update-check caching
python tests/test_routes.py        # every route: registered, no 500s, auth enforced
python tests/test_scheduler.py     # monitoring logic + retention safety guards
python tests/test_notify.py        # notification targets, payloads, gating
python tests/test_media.py         # transcode decisions, SSRF guard, title/folder helpers
python tests/test_downloads.py     # format selector, cancellation
python tests/test_titles.py        # download-time "Artist - Song" naming
python tests/test_download_state.py# interrupted downloads, history pruning, copystat
python tests/test_library.py       # library scan, caching, chart series
python tests/test_invariants.py    # source-level rules for bugs CI can't reproduce
node tests/test_artists_filter.js   # Artists page search/filter/sort logic

# Optional, and skipped (exit 0) unless a live instance is configured and
# Playwright is installed. Catches the bugs only a rendered page shows.
pip install playwright && playwright install chromium
VIDSHELF_URL=http://127.0.0.1:5000 VIDSHELF_PASSWORD=... python tests/test_browser.py
```

Plain assertions, no pytest and no `npm install` — so they run identically on
your machine and in CI. Please keep it that way; the lack of dev dependencies
is deliberate.

If you add a route, `test_routes.py` should still pass unchanged — it asserts
that every non-public route rejects unauthenticated callers, which is a
standing invariant rather than a per-feature test.

## Where things live

`app.py` is the Flask app, the 66 route handlers and the server startup. As of
v1.11.0 the shared logic lives beside it, in modules that know nothing about
Flask:

| Module | Owns |
|---|---|
| `config_store.py` | `config.json` I/O; the secret key and admin credentials it seeds |
| `webauth.py` | `@require_auth`, the login throttle, the security headers |
| `tracker.py` | `downloaded_videos.json` — the "do we already have this?" record |
| `youtube.py` | every yt-dlp **metadata** call (names, listings, search, quality) |
| `library.py` | the cached media-root walk that feeds the dashboard |
| `downloader.py` | actually downloading; `transcode.py` converts |
| `artwork_sync.py` | artwork providers, title cards, the Plex client, Plex OAuth |
| `state.py` | atomic JSON reads/writes for everything above |

**Two rules that are load-bearing, not stylistic:**

- **Nothing in that table may import `app`.** The routes are being moved into
  blueprints (v1.12.0), and a blueprint that imports `app` while `app` imports
  blueprints is a cycle. If a route needs shared state, the state moves into a
  module of its own — not into `app` with a deferred import to dodge the cycle.
- **Bound anything a browser waits on; leave downloads patient.** `youtube.py`
  builds every request through `_probe_opts()`, which carries a socket timeout and
  a retry cap. `downloader.py` deliberately keeps yt-dlp's defaults, because there
  a retry is the difference between getting the video and not. Mixing these up is
  what made the music-video search hang in v1.10.1.

**After moving code between modules, run `python -m pyflakes <files>`.** A
module-level name that is used but not imported still lets `import <module>`
succeed — Python resolves it at call time — so CI's import check passes while the
function is a `NameError` waiting for its first call. That shipped once as a
guaranteed startup crash and was caught by pyflakes, not by any test.

**And check what your tests actually patch.** `app.py` re-exports the extracted
names for convenience. Rebinding `app.some_name` does *not* change what the owning
module sees, so a test that patches the re-export silently stops testing anything.
Patch the module that defines it.

## Things worth knowing before you change anything

**Read [REFERENCE.md](REFERENCE.md) first.** It's an engineering log, not user
documentation: what broke, why, the fix, and how to verify. It exists because
several bugs in this project were expensive to diagnose and trivially easy to
reintroduce. A few that will bite you:

- **Never use `shutil.copy`/`copy2`/`copyfile` onto a network mount.** They use
  `os.sendfile()` internally, which fails with `ENOSPC` partway through on
  CIFS even with terabytes free. Use `shutil.copyfileobj` on two open handles.
- **All state goes through `state.py`.** A bare `open('config.json', 'w')`
  anywhere reintroduces both the torn-write bug and the lost-update race.
- **Don't bind-mount individual JSON files.** Docker mounts a single file by
  inode; atomic writes replace the inode, so writes silently stop reaching the
  host. Mount the `data` directory.
- **Verify media-path changes end to end**, not just with tests. Two separate
  fixes in this project looked correct on paper and weren't.

## Using AI assistants

Use them if you like — much of this project was written with one, and saying
otherwise would be dishonest. There is no disclosure requirement and no
prohibition. What there is, is a bar, and it is the same bar for everyone:

**Claims in a PR must be backed by something you ran.** "This fixes X" means you
reproduced X first and confirmed it stopped. Several bugs here looked fixed on
paper twice before they actually were, and the log records every one.

**A test you have not watched fail proves nothing.** This is the requirement AI
output misses most often, because a plausible-looking assertion that is
vacuously true still shows green. Real examples from this repo:

- an ordering assertion comparing `index('os.replace')` against
  `index('os.chmod')` — the module docstring mentioned `os.replace` four times
  before any code did, so it could never fail
- a CSS rule requiring `display: block`, satisfied by the *comment* explaining
  why `display: block` mattered
- an XSS guard matching a fixed list of variable names, which passed while four
  unescaped values sat in the same file

Each was written in good faith, each passed, and each was worthless. Break the
code deliberately and watch the test go red before you trust it.

**Verify against the real thing where the real thing is what breaks.** The
recurring failures in this project — CIFS mounts, container restarts, Docker
port collisions, browser rendering — are invisible to unit tests by
construction. `python tests/*.py` passing is necessary, not sufficient. If you
touch the media path, do a real download to a real network mount.

**Don't add dependencies to make something easier to generate.** No pytest, no
npm, no chart library. These constraints are deliberate and long-standing; a PR
that relaxes one to simplify its own implementation will be asked to put it
back.

**Comments must say why, and be true.** A comment restating what the line does
is noise. A comment asserting something false about the system is worse than
none, and stale comments have actively misled work here before — one entry in
REFERENCE.md pointed at a function that had already been deleted.

None of this is unique to AI-assisted work. It is just where that work tends to
fall short, and it is cheaper to say so once here than to find it in review.

## Style

Match the surrounding code — it's plain Flask and vanilla JS with no build
step, deliberately. Comments explain *why*, especially where something looks
odd on purpose; there's a lot of that here, and most of it is load-bearing.
