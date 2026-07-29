<!--
Thanks for contributing. A note on how this repo works, since it isn't obvious
from the outside:

  * `dev` is where work lands. `main` is releases only.
  * PRs should target `dev` unless you're cutting a release.
  * A release happens by bumping VERSION on dev and merging to main — CI then
    tags it, publishes the image, and creates the release. Don't tag by hand.

See CONTRIBUTING.md for the full picture.
-->

## What this changes

<!-- What and why. If it fixes an issue, "Fixes #123". -->

## How it was verified

<!--
Test output, or what you exercised by hand. Anything touching downloads,
transcoding or media paths really wants an end-to-end run rather than only
unit tests — REFERENCE.md documents two bugs that looked fixed on paper and
weren't.
-->

## Checklist

- [ ] Targets `dev` (not `main`, unless this is a release)
- [ ] `python tests/test_state.py && python tests/test_updates.py && python tests/test_routes.py` pass
- [ ] `node tests/test_artists_filter.js` passes (if front-end code changed)
- [ ] `REFERENCE.md` updated — required for anything non-trivial; see CLAUDE.md
- [ ] `VERSION` left alone, unless this PR is cutting a release
- [ ] No credentials, tokens or private paths in the diff
