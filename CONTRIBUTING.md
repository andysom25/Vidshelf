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
python tests/test_invariants.py    # source-level rules for bugs CI can't reproduce
node tests/test_artists_filter.js   # Artists page search/filter/sort logic
```

Plain assertions, no pytest and no `npm install` — so they run identically on
your machine and in CI. Please keep it that way; the lack of dev dependencies
is deliberate.

If you add a route, `test_routes.py` should still pass unchanged — it asserts
that every non-public route rejects unauthenticated callers, which is a
standing invariant rather than a per-feature test.

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

## Style

Match the surrounding code — it's plain Flask and vanilla JS with no build
step, deliberately. Comments explain *why*, especially where something looks
odd on purpose; there's a lot of that here, and most of it is load-bearing.
