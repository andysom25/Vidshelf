# Security Policy

## Reporting a vulnerability

**Please don't open a public issue for a security problem.**

Use GitHub's private vulnerability reporting instead:
[**Report a vulnerability**](https://github.com/andysom25/Vidshelf/security/advisories/new).
That opens a private thread visible only to the maintainer, so a fix can ship
before the details are public.

This is a personal project maintained by one person in spare time. I'll
acknowledge reports as soon as I reasonably can, but there's no SLA behind
that — please size your expectations accordingly.

## Supported versions

Only the **latest release** gets fixes. There are no maintenance branches for
older versions; upgrade to the newest tag.

## What's in scope

Vidshelf holds real credentials, so these are the things worth reporting:

- Bypassing the login (session forgery, auth checks missing on an endpoint)
- Disclosure of the Plex token, admin password hash, session secret key, or
  the NAS SMB credentials from `.env`
- SSRF via any endpoint that takes a URL — the artwork search and yt-dlp entry
  points are guarded, so a way around those guards is interesting
- Path traversal into the media or data directories, or command injection via
  a filename or artist name reaching ffmpeg/yt-dlp
- Stored or reflected XSS in the dashboard

## What's out of scope

- **The bundled server is Flask/waitress on a trusted network.** Vidshelf is a
  single-admin app intended for a LAN or a reverse proxy behind auth. Reports
  amounting to "it's exposed if you port-forward it to the internet without a
  proxy" aren't vulnerabilities; that's a documented deployment choice.
- **The admin account can reach the filesystem by design** — configuring media
  paths and browsing folders is the app's job. Escalation *from an
  unauthenticated position* is in scope; the admin using admin features is not.
- Anything requiring an attacker to already have the `.env` file, the `data/`
  directory, or shell on the host.
- Denial of service by asking the app to download or transcode a lot. It's a
  media tool; that's the workload.
- Vulnerabilities in yt-dlp, Flask, Pillow or ffmpeg themselves — report those
  upstream. Do tell me if Vidshelf is pinned to a version with a known issue,
  which Dependabot should catch but might not.

## What's already known and deliberate

Documented in `REFERENCE.md` rather than being oversights:

- `/api/artwork/search_noauth` and `/swap_noauth` are unauthenticated on
  purpose, with the reasoning recorded in `REFERENCE.md`.
- Secret scanning and push protection are enabled on this repo. If you find a
  live credential in the history, please report it privately — don't test it.
