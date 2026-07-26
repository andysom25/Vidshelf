# Vidshelf

See [REFERENCE.md](REFERENCE.md) for architecture, file responsibilities, and the debugging log for the CIFS download issue and the Plex OAuth/collections work.

**Keep REFERENCE.md up to date.** Any time you change code in this project — a new feature, a bug fix, a design change, a regression and its fix — add or update the relevant section in REFERENCE.md before finishing up, in the same style as the existing sections (what broke, why, the fix, how to verify, what to check first if it regresses). This file is what keeps the next session (yours or another agent's) from re-diagnosing something that's already been solved once — it's already saved real time twice in this project (the CIFS mount bug, and the Plex OAuth/collections bug stack). Skipping this because a change feels small is exactly how the next regression report turns into a from-scratch investigation.

## Branching & release workflow

- **`main` is releases-only.** Every commit on `main` should correspond to a tagged release (see `VERSION` file — bump it as part of the merge that cuts a release, following semver: patch for fixes, minor for new features, major for breaking changes).
- **`dev` is where active work happens.** Day-to-day commits — features, fixes, refactors — go on `dev`, not `main`. Don't push directly to `main` outside of a release merge.
- **Cutting a release** = merge `dev` → `main`, bump `VERSION`, tag the merge commit (`git tag vX.Y.Z`), push both the branch and the tag, then optionally draft a GitHub Release from that tag.
- If you're picking up work in this repo and aren't sure which branch you're on, check with `git branch --show-current` before committing — don't assume `main`.

## Known gotcha #1: Docker Desktop on Windows cannot bind-mount a network share

`Y:\` (or any UNC path like `\\192.168.1.100\ppv\MusicVideos`) is a Windows-mapped SMB drive, not a real local path. Docker Desktop **cannot bind-mount it** — and critically, it doesn't error when you try. It silently substitutes a small local decoy volume instead. This looked exactly like "the CIFS mount doesn't support X syscall" for a long time: `/app/music_videos_final` showed up as `/dev/sdd`, type `ext4`, 137MB total, 100% full — never once actually touching the NAS.

**The fix**: mount the SMB share with Docker's native `cifs` volume driver (Linux's own CIFS client, bypassing the Windows redirector entirely) — see `docker-compose.yml`'s `music_videos_final` volume. Credentials go in `.env` (gitignored; see `.env.example`), never in `docker-compose.yml` directly.

**How to tell real vs. decoy, instantly**: `docker exec vidshelf df -h`. A real NAS mount shows type `cifs` with multi-terabyte capacity. A decoy shows `ext4` with a tiny (double/triple-digit MB) size that doesn't match what the NAS actually has free. Run this *before* trying anything else if a network-mounted path in this container starts throwing `ENOSPC` or space-related errors — it takes 5 seconds and tells you which universe you're debugging in.

If Windows-side (`Y:`, Explorer, PowerShell) doesn't immediately show a file the container just wrote, that's a stale SMB directory cache, not a sign the mount is fake — `net use Y: /delete` then remap to force a fresh session before concluding anything is wrong.

## Known gotcha #2: never use `shutil.copy`/`copy2`/`copyfile` onto a CIFS/SMB/NFS mount

Separately from gotcha #1: `os.sendfile()` is unreliable against CIFS/Samba mounts and can raise `OSError: [Errno 28] No space left on device` partway through a transfer even with plenty of real free space. The trap is that `shutil.copy2()`/`copyfile()` on Linux (Python 3.8+) use `os.sendfile()` internally as a fast-path (`shutil._fastcopy_sendfile`), and only fall back to a plain read/write loop if the *very first* `sendfile()` call fails outright. Swapping `shutil.move` for `shutil.copy2` looks like it avoids the problem but doesn't — it just moves the same failure later and makes it look more intermittent.

**The only safe way to copy onto a network mount**:

```python
with open(src, 'rb') as fsrc, open(dst, 'wb') as fdst:
    shutil.copyfileobj(fsrc, fdst)
shutil.copystat(src, dst)  # optional, cosmetic — drop it if the server rejects chmod/utime
```

This is what [downloader.py](downloader.py)'s `download_video()` does.

## Why both gotchas mattered here

Both bugs were stacked on the same code path, which is why the fix looked like it "didn't work" the first time: fixing gotcha #2 (sendfile) made the download fail with the *exact same* `ENOSPC` error, because gotcha #1 (the decoy volume) was still there underneath. Don't assume one fix disproven by an identical-looking error — check `df -h` to see whether you're now hitting a genuinely different layer of the same symptom.

## Known gotcha #3: `docker logs` can print lines out of chronological order

Python's `stdout` is fully block-buffered (not line-buffered) when it isn't attached to a TTY — which it never is under Docker. With multiple concurrent download threads all `print()`-ing to the same buffer, a thread's "completed" line can get flushed to `docker logs` *before* an earlier line from a different thread, including that same thread's own "started" line in extreme cases. This already caused one false "it's still broken" report after a download had actually succeeded.

Fixed via `ENV PYTHONUNBUFFERED=1` in the [Dockerfile](Dockerfile). If logs ever look out-of-order again, check this env var is still set before assuming the download logic regressed — verify against the actual filesystem (`ls`, `df -h`) rather than trusting log order alone.

## Verifying fixes

Code changes to `app.py`/`downloader.py` don't take effect in the running container until it's rebuilt. Changes to `docker-compose.yml`'s `volumes:` section (top-level, not per-service) need a full recreate, not just a rebuild:

```bash
docker-compose down && docker-compose up -d --build
docker logs vidshelf --tail 50
docker exec vidshelf df -h   # confirm mount types/sizes look real
```

Don't declare a download-path fix verified without doing an actual end-to-end download through the UI and confirming the file exists both inside the container and (for NAS paths) from the Windows side — this bug specifically looked fixed on paper twice before it actually was.
