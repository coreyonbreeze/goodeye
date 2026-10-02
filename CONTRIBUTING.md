# Contributing

- Keep `goodeye.py` and `board.html` dependency-free: Python standard library, plain HTML/JS, no build step.
- Run the tests before you open a pull request: `python3 -m unittest discover tests`.
- New mockups go in `mockup()` in `board.html` and in `CONTEXTS` in `goodeye.py`. Show the real placement rules (crop, safe areas, overlaps), not a pixel copy of the site.
- Every new verdict or flow must end with a clear `next:` line in `goodeye wait`, because that line is what the agent acts on. Update `skills/goodeye/SKILL.md` to match.
- Never commit a real store (`~/.goodeye`), client assets, or keys.

Optional phone-player integration test (uses a temporary store and a generated video):

```sh
# Requires ffmpeg and a Playwright installation with its WebKit browser.
node tests/phone-playback.cjs
# Or reuse an existing Playwright installation:
PLAYWRIGHT_MODULE=/absolute/path/to/node_modules/playwright node tests/phone-playback.cjs
```

This checks normal playback, a blocked autoplay attempt followed by a tap, retry after a failed download, overlapping verdict prevention, retry after a lost save response, and a failed refresh after a successful save. It does not verify a physical iPhone or its Home Screen app.

Review-studio integration checks (Playwright with WebKit, no ffmpeg needed):

```sh
node tests/studio.cjs
# PLAYWRIGHT_MODULE supports an existing installation, as above.
```

This checks queue search, project isolation, per-version feedback drafts, keyboard controls, placement details, desktop action visibility, dark mode, and phone navigation. It uses an isolated demo store.

Project integration checks (Playwright with WebKit):

```sh
node tests/projects.cjs
# Optional screenshots from the synthetic test fixture:
SCREENSHOTS=/tmp/goodeye-projects node tests/projects.cjs
```

This checks empty project creation, duplicate-ID drafts and verdicts, scoped agents and history, collection assignment, immutable profile display, delayed profile saves, and phone deep links. `tests/test_projects.py` covers scoped storage and exports, legacy file paths, profile conflicts, and collection inheritance.

Brand-direction integration checks (Playwright with WebKit):

```sh
node tests/direction.cjs
```

This exercises an empty profile, source references, lost-response retries, saved requests resumed by a pull agent, questions and steering, proposal adoption through strategy/concept/system, scoped revisions, source-edit conflicts, and phone drafts. Image fixtures are synthetic; this test does not call an image provider or dispatch to a live Codex conversation. `tests/test_direction.py` covers durable delivery, handoffs, project isolation, evidence/provenance requirements, and stale approval rejection.

The bundled `skills/goodeye-brand` workflow must keep proposed rules separate from approved rules. Provider execution belongs to the connected agent. Do not add model credentials or arbitrary filesystem reads to browser routes.
