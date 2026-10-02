# GoodEye direction protocol

Use `GOODEYE_HOME` from the workflow starter when it differs from the default. The installed `goodeye` command or this repository's `python3 goodeye.py` accepts these commands.

```sh
goodeye direction show --project 'Project name'
goodeye direction join --project 'Project name' --as brand
```

`join` registers the current agent as a project watcher and claims `brand-direction`. Codex uses the current thread through the existing local queue. Other runtimes use `--runtime pull`, then run `goodeye wait --project 'Project name' --as brand` in the background. If the role is occupied, inspect `goodeye watchers` and use an agreed handoff; do not invent a new conversation or steal the role. Joining resumes a saved request, or starts evidence recovery if none exists.

`show` returns the current profile, revision, source paths, requests, agent updates, proposals, and delivery states. Notifications carry `kind: direction`, the project, request ID, operation (`request` or `accepted`), and this skill's path. Always read the current conversation; deduplicate by `decision_id`. Acknowledge the delivery after reading it with `goodeye ack --delivery ID --as brand`. Never send a direction event through an asset-verdict handler.

```sh
goodeye direction update --project 'Project name' --request REQUEST_ID \
  --status working --message 'Reviewing the existing bible and approved assets.'
goodeye direction update --project 'Project name' --request REQUEST_ID \
  --status needs_input --message 'The earlier brief targets shop owners; the new request names accountants. Which audience should this direction prioritize?'
goodeye direction propose --project 'Project name' --request REQUEST_ID --file proposal.json
```

A proposal JSON contains:

```json
{
  "id": "a-stable-unique-proposal-id",
  "phase": "revision",
  "title": "A more readable display typeface",
  "rationale": "Explain the specific problem, evidence, change, and tradeoff.",
  "evidence": ["Path or source URL, relevant section, and observed/approved fact"],
  "profile": {"typography": "Exact proposed type rules, supported by the inspected specimens."},
  "artifacts": [
    {
      "id": "type-study",
      "version": "v1",
      "origin": "native",
      "provenance": "Rendered from the existing approved layouts with the proposed typeface."
    }
  ],
  "production_notes": "List actual verification and remaining limits; omit unsupported claims."
}
```

- `id`: optional stable ID, 16–80 letters/digits/hyphens. Reusing it with identical content is safe; changing content needs a new ID.
- `phase`: `strategy`, `concept`, `system`, `revision`, or `import`.
- `profile`: a patch containing one or more of `strategy`, `logos`, `colors`, `typography`, `voice`, `references`, `visual_rules`, `export_rules`. Values are text, up to 20,000 characters each. Strategy proposals change only `strategy`. A scoped request changes only its named field.
- `evidence`: 1–30 specific citations or explicit user decisions. Do not cite a generated image as independent market evidence.
- `artifacts`: up to 12 assets already submitted to this exact project, with explicit versions. `concept` requires visible artifacts and at least one generated study or inspected reference. The server resolves the files from the submitted version; no arbitrary filesystem read is exposed to the browser.
- Artifact `origin`: `generated`, `reference`, or `native`. Every artifact needs `provenance`. Generated artifacts also require `tool` and the actual `prompt`. Put source references, edit constraints, and observed outcomes in provenance; keep the original files in the project.
- `production_notes`: checks performed, production paths, and limitations. A raster logo study is a concept, not a production vector.

Submit visual evidence using the main GoodEye skill:

```sh
goodeye submit study.png --id type-study --project 'Project name' \
  --collection Brand --reasoning reasoning.json
```

A request can have multiple proposals. The reviewer chooses in Brand & direction. Acceptance merges the proposed fields into the approved profile in one atomic store update. Older submissions retain their snapshots. An accepted strategy or concept creates the next turn automatically; a system, revision, or import completes that turn and notifies the agent. Read acceptance from the saved state, not your own inference.

To attach known source paths (no automatic import or file loading by the server):

```sh
goodeye direction sources --project 'Project name' --file sources.txt
```

Read these sources yourself. Follow their repository instructions. If an approval record contradicts an older bible, record the conflict and resolve it with evidence or an in-app question.
