# Contributing

- Keep `goodeye.py` and `board.html` dependency-free: Python standard library, plain HTML/JS, no build step.
- Run the tests before you open a pull request: `python3 -m unittest discover tests`.
- New mockups go in `mockup()` in `board.html` and in `CONTEXTS` in `goodeye.py`. Show the real placement rules (crop, safe areas, overlaps), not a pixel copy of the site.
- Every new verdict or flow must end with a clear `next:` line in `goodeye wait`, because that line is what the agent acts on. Update `skills/goodeye/SKILL.md` to match.
- Never commit a real store (`~/.goodeye`), client assets, or keys.
