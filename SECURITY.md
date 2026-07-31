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

- Secret scanning and push protection are enabled on this repo. If you find a
  live credential in the history, please report it privately — don't test it.

**Removed from this list in v1.7.0.** `/api/artwork/search_noauth` and
`/swap_noauth` were listed here as unauthenticated *on purpose*, on the stated
grounds that requiring auth would break their consumer. That was never checked,
and it was wrong: the only consumer was this app's own already-authenticated
dashboard. Both endpoints now require a session, under the canonical names
`/api/artwork/search` and `/api/artwork/swap`.

Treat anything in this section as a claim to be re-tested, not a settled one — a
"deliberate" label is only as good as the reasoning behind it, and that one went
unexamined through five releases and a full security review.

## Previously found and fixed

A whole-project review before v1.6.1 found and fixed: stored XSS via YouTube
video titles rendered unescaped into the dashboard (which allowed reading the
Plex token from the API); the container being granted `SYS_ADMIN` for a mount it
never performs; and `config.json` written world-readable.

Reviewing that fix before merge found a **second XSS site of the same class** —
the Plex server name and URI, as reported by plex.tv, interpolated raw into an
inline `onclick` handler — plus thirteen further unescaped sites the first pass
had missed. The regression check written alongside the original fix had not
caught them: it matched on a fixed list of variable and field names, so it was
testing naming rather than safety, and it never looked at string concatenation at
all. It has been rewritten, and there are now six checks covering this area.

Every one of them has been verified to fail against a deliberately broken
version of the code — including one ordering assertion that turned out to be
vacuously true and could never have failed. See the v1.6.1 release notes and
`REFERENCE.md`.
