---
name: goodeye
description: >-
  Use whenever you produce visual files a person has to look at and approve: images, GIFs, videos, renders,
  screenshots, mockups, banners, icons, slides as images, or variations of any of these. Trigger it instead of
  telling the user to open a folder, Finder or an output directory and click through files one by one, instead
  of pasting file paths to look at, and instead of asking "does this look good?" or "which one do you like?" in
  chat. Also use it when the user says "send for review", "put it up for review", "I need to approve this",
  "which one should we use", "let me see them", "open the folder", or "show me the renders". Submits each file
  to the GoodEye board with versions, judge scores and reasoning; a persistent subscription queues the verdict into the subscribed Codex conversation.
---

# GoodEye review board

GoodEye is a local review board at http://localhost:4400. You submit work with the `goodeye` command. The
reviewer opens the board, looks at each item (also inside mockups such as a LinkedIn banner or an email), and
clicks a verdict with spoken or typed notes. `goodeye subscribe` routes new feedback to an existing Codex conversation. A terminal `wait` alone does not guarantee an agent wake-up.

Do not ask the reviewer to open a folder. Do not ask "does this look good?" in chat. Submit to the board.

## When to use it

Use GoodEye when **you made visual files and a person needs to look at them to decide something.** The test: if you
are about to run `open <folder>`, list file paths for the user to click through, or ask "which one?" about images,
submit to GoodEye instead. Typical cases:

- A batch of generated or edited images, icons, logos, banners, social posts, email GIFs.
- Rendered videos, animations, website loops, or frames from them.
- Screenshots of a UI or page you changed, where the person must judge how it looks.
- Charts, diagrams or slides exported as images for a person to approve.
- Several variations of one thing (submit a choice, section 6), or several candidates for one placement (a slot, section 7).
- A new version of anything the person gave feedback on before.

Skip it when:

- The person asked to see one image right now in chat, or asked for the file itself.
- The files are not visual (code, CSV, text, PDFs to read).
- The image is only for your own debugging, and nobody else needs to decide anything.
- The person explicitly asks you to open the folder instead.

When in doubt and there is more than one image, or anything will ship, use GoodEye.

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

## 4. Subscribe to verdicts (Codex)

Before leaving work for review, register this exact conversation:

```bash
goodeye subscribe --project <Project>
goodeye subscription-test --project <Project>
goodeye subscriptions
```

`subscribe` uses `CODEX_THREAD_ID`, or pass `--thread <exact UUID>`. It stores one owner per project and uses the installed `codex queue` command. No model override, new thread, separate model API key, or periodic model polling. Requires a Codex CLI that supports `queue`.

- Subscribe **before submitting** new work. A new subscription starts with future verdicts; it does not replay historical decisions. Registering the same thread again preserves its cursor. Use `goodeye unsubscribe --project <Project>` before explicitly changing owners.
- The board server runs the delivery worker. It keeps delivery state across server restarts and retries failed queue requests. Codex handles queued prompts after the current response. Do not claim automatic wake-up from a terminal listener.
- The subscription test is a notification only, never an asset or approval. Confirm receipt in the target conversation before claiming the integration is verified. A test queued during an active turn may arrive after that turn finishes.
- When notified, run the supplied `goodeye inbox --store <path> --delivery <id>` command. Read every decision. Then run the supplied `goodeye ack --store <path> --delivery <id>` command. The subscribed `CODEX_THREAD_ID` is required (or `--thread <UUID>`).
- **Queue acceptance is not agent receipt. Receipt is not completion or approval.** `subscriptions` and the board distinguish pending, queued, and acknowledged deliveries.
- Deliveries can repeat after a crash or ambiguous timeout. Deduplicate actions using `decision_id` and the project's decision records. Never infer approval from a notification or acknowledgment.
- If Codex accepted a delivery but it remains unacknowledged, inspect `goodeye inbox` and `goodeye subscriptions`. Use `goodeye retry-delivery --delivery <id>` for an explicit retry with the same ID. Do not start another competing subscriber.
- Keep one subscription per project. Do not run `goodeye wait` as the Codex wake-up mechanism alongside it. Subscription delivery does not consume or depend on legacy `delivered.json`.

### Other agents / terminal fallback

```bash
goodeye wait --project <Project>
```

This prints new verdicts and exits. It only wakes an agent if that agent's runtime explicitly resumes on background process completion. Otherwise it must be polled during active work. Keep exactly one legacy waiter per project, handle every returned verdict, then restart it. The board labels this as a terminal listener, not a confirmed agent subscription.

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
