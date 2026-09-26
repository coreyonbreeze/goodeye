---
name: goodeye
description: Send creative work (images, GIFs, videos, banners, icons, social posts, email GIFs, website loops) to the GoodEye review board for human sign-off, with versions, judge scores, and required reasoning, then act on the verdict. Use whenever you make or revise marketing, brand, video, or other creative work that a person must approve, instead of asking them to open a folder or approve in chat. Covers "send for review", "I need to approve this", "put it up for review", "which one should we use", "get sign-off".
---

# GoodEye review board

GoodEye is a local review board at http://localhost:4400. You submit work with the `goodeye` command. The
reviewer opens the board, looks at each item (also inside mockups such as a LinkedIn banner or an email), and
clicks a verdict with spoken or typed notes. `goodeye wait` wakes you with the verdict.

Do not ask the reviewer to open a folder. Do not ask "does this look good?" in chat. Submit to the board.

If `goodeye` is not installed, tell the user to run `./install.sh` from a clone of the GoodEye repository.

## 1. Gate first

If the project has a judge (an LLM judge, a linter, measured checks), run it before you submit. Submit only work
you would defend. If the judge fails it and you still submit, say why in `reasoning.summary`.

## 2. Write the reasoning file (required)

`goodeye submit` refuses a submission without it.

```json
{
  "summary": "One or two sentences: what this is and where it will be used.",
  "decisions": [
    {"choice": "What you decided", "why": "The evidence: a judge score, a measurement, or the reviewer's instruction"}
  ],
  "changes": [
    {"change": "What changed since the last version", "why": "Why", "feedback": "The reviewer's words this answers"}
  ],
  "open_questions": ["Only questions the reviewer must decide"]
}
```

- `decisions`: at least one. Give the real reason. Quote numbers.
- `changes`: required from the second version on. Map each point of the last feedback to one change and quote
  the reviewer's words in `feedback`. If you did not act on a point, add an entry that says so and why.
- Keep it plain and short. The reviewer reads it next to the asset.

## 3. Submit

```bash
goodeye submit <file> --id <stable-id> --title "<human title>" --project <Project> \
  --context <placements> --reasoning reasoning.json [--scores scores.json]
```

- `--id` stays the same across versions (`launch-video`, `linkedin-banner`). Each submit is a new, frozen version.
- `--version` is optional. Pass your own version string if the project has one. Otherwise GoodEye uses `v1`, `v2`, ...
  Never reuse a version string: versions are frozen and a repeat is refused.
- `--project` must be the same on every submit and on `goodeye wait` (section 4), or the wait never wakes.
- `--scores` shows judge scores and a trend chart across versions. Use the same metric names on every version:
  `{"scores": {"clarity": {"value": 2.9, "max": 4, "bar": 2.8, "group": "Quality (0 to 4)"}}, "judge": {"name": "My judge", "pass": true, "notes": ["..."]}}`.
  A flat `{"clarity": 2.9}` also works. Metrics with the same `group` share one chart. `bar` draws the pass line.
- `--context` (comma list) picks the mockups and the size checks. `goodeye submit --help` lists them and is the
  source of truth. Today: `linkedin-banner` (personal profile), `linkedin-company-cover` (company page),
  `linkedin-post`, `x-banner` (profile header), `x-post`, `instagram-post`, `instagram-story`, `email`,
  `website-hero`, `browser-tab`, `youtube-thumbnail`, `phone`. Pick every place the asset will really appear. If none fits, leave it off and say so in
  `open_questions`. Never pick the nearest wrong one. Contact sheets get no context.
- Read the submit output. `SPEC WARNING` lines mean the file does not fit a placement you named (wrong size, shape,
  or file weight). Fix it and resubmit before the reviewer looks, or explain in `reasoning` why it is right.
- After a batch, tell the reviewer in one line how many items are up, with the link http://localhost:4400.

## 4. Wait for the verdict

Right after you submit, start this in the background (Claude Code: the Bash tool with `run_in_background: true`):

```bash
goodeye wait --project <Project>
```

It exits when the reviewer's verdict arrives, and your session wakes with the output. Keep working meanwhile.

- Keep **exactly one** `goodeye wait` running per project while anything is pending. A verdict goes to whichever
  wait sees it first, so two waits (two sessions, or a second one you forgot) split the verdicts and one session
  never hears about some of them. One running wait is also what shows the reviewer "Agent listening".
- After you handle the output, start `goodeye wait` again if items are still pending (`goodeye status` lists them).
- The reviewer has a few seconds to undo a verdict, so it arrives a few seconds after the click. That is normal.

## 5. Act on the verdict

One `wait` can return **several** verdicts at once (for example an approval plus the NOT_CHOSEN items it closed).
Handle every `VERDICT` block in the output. Each block ends with a `next:` line. Follow it.

### Verdicts

- **APPROVED** or **PICKED**, no notes: record the approval where the project keeps approvals, then do the
  follow-up work. Do not ask again.
- **APPROVED / PICKED WITH NOTES**: the reviewer trusts you with small changes. Apply every note, re-run the judge,
  record the approval with the notes and the final file path, then continue. **Do not resubmit for review.**
  Ask only if a note is impossible.
- **CHANGES**: the reviewer wants to see it again. Make a new version that answers every point, re-run the
  judge, and resubmit with the same `--id` and a `changes` list.
- **REJECTED**: stop work on that item. Do not resubmit unless asked.
- **NOT_CHOSEN**: another item won its slot. Stop work on it. Do not resubmit it.
- **REOPENED**: the reviewer brought a rejected or not-chosen item back into review. Do not change it; wait for
  its next verdict.
- **REMINDER** (not a verdict; `wait` exits with it): changes the reviewer asked for have not come back. Submit
  those new versions, then run `goodeye wait` again.

If the project has no approval record of its own, `goodeye export DIR --project <Project>` copies every approved
file with a manifest (who approved, when, notes to apply).

### Reading notes

- **Time stamps:** a note that starts with `[0:03.20]` is about that moment in the video or GIF.
- **Pins:** `At pin 2 (top left, 12% across, 20% down): the stroke is too thin` points at a spot on the image.
  The percentages are measured from the image's top-left corner, so 12% across and 20% down is that exact point
  at any size. The words are only a rough region.
- **Notes on choice options** ("take the M from this one") mean: take that part from that option into pick 1.
- Notes are dictated, so expect spoken phrasing and small transcription errors. Act on the intent. Ask only if the
  intent is unclear.

## 6. Choices: when the reviewer should pick between options

Never submit a comparison sheet and ask the reviewer to name a row. Submit a choice with one file per option:

```json
{
  "question": "Which font should the logo use?",
  "recommended": "anton",
  "options": [
    {"key": "anton", "label": "Anton", "file": "fonts/anton.png", "note": "Why it is here, one line", "scores": {"judge": 0.92}},
    {"key": "bebas", "label": "Bebas", "file": "fonts/bebas.png", "scores": {"judge": 0.82}}
  ]
}
```

```bash
goodeye submit --options options.json --id logo-font --title "Logo font" --project <Project> --reasoning reasoning.json
```

- 2 to 9 options (number keys 1 to 9 rank them on the board). File paths are relative to the JSON file.
- Show each option at the size it will be used, all at the same scale, each cropped to its own file.
- Leave out options that fail the judge badly and list them in `reasoning.decisions`.
- The reviewer ranks options (1st, 2nd, ...) and can note any option ("take the M from this one").
- PICKED: build pick 1 with the notes applied. A note on another option means take that part from it. Pick 2 is
  the fallback. CHANGES with picks: build from the picks and submit again. CHANGES with no picks: none worked;
  submit new options.
- Do not open a choice for something the reviewer already decided. Check `goodeye status` first.

## 7. Slots: separate items that compete for one placement

A slot is one place where only one asset gets used (one app store hero image, one profile header). When several
full candidates for the same place are separate items, give them the same slot:

```bash
goodeye submit <file> --id hero-dark ... --slot store-hero --slot-label "App store hero image"
goodeye slot store-hero hero-dark hero-light      # put existing items in a slot
```

When the reviewer approves one, GoodEye closes the others, and you get one NOT_CHOSEN verdict per closed item.
Use a choice (section 6) for cheap variants of one decision. Use a slot when each candidate is its own item with
its own versions and history.
