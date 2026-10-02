---
name: goodeye-brand
description: Develop or revise a project's brand strategy and art direction through GoodEye. Recover existing brand evidence, explore visual directions with image tools, propose guidelines for review, and respond to steering in the same conversation. Use for GoodEye direction requests; ordinary asset reviews use the goodeye skill.
---

# GoodEye brand workflow

The reviewer works in **Brand & direction**. Their typed requests are durable workflow turns. Your proposals are drafts until the reviewer accepts them. Keep working in the same project and conversation, including narrow changes to one part of an established identity.

Read [references/protocol.md](references/protocol.md) for the CLI, proposal schema, artifact provenance, and delivery handling. Use the store and exact project name supplied in the starter prompt or delivery. Read the current conversation before acting on each notification. An older request can be superseded; never work from the notification alone.

## Recover what already exists

An empty GoodEye profile is not evidence of a new brand. Read the attached source paths, repository instructions, brand bible, strategy, product truth, asset releases, design tokens, approval records, and rejection feedback. Open the actual reference images. Distinguish approved direction, proposed studies, outdated rules, and unverified assumptions. Identify the document or decision that resolves conflicts; do not average conflicting directions.

If a brand already exists, propose an **import** with source citations and a faithful summary. Preserve its authorship, quirks, vocabulary, and constraints. Importing does not authorize editing source repositories or making new marks. For another project's brand bible, borrow the process of documenting decisions, not its palette, mascot, typefaces, or visual motifs.

Save a compact evidence ledger in the project's working directory: source, observed fact, approval state, and implication. The ledger should make it possible to explain why this brand belongs to this product and these people. Do not invent customer research, personas, founder quotes, approval, or competitor facts. Verify external claims using primary sources when research is needed.

## Find the strategy before choosing the look

Establish the product's purpose, audience, real use situations, origin, differentiator, and the feeling it should earn. Ask the reviewer only for missing decisions that change the work. Send questions through `direction update --status needs_input`; the reviewer answers in the same GoodEye window. Do not send a standard ten-question intake when the sources already answer it.

Propose a short **strategy**: what is true, who it serves, the specific position, personality expressed as behavior, what it rejects, and the visual implications. Cite evidence and mark unresolved assumptions. Do not hide a generic identity behind a long document, an invented agency valuation, or a deliverable count. Strategy acceptance continues the workflow automatically.

## Explore with a point of view

For a new direction, develop a small set of genuinely distinct concepts. Each needs a rationale tied to the strategy, references inspected for a stated purpose, and visible tradeoffs. A different hue of the same stock logo is not another direction. Choose typography, materials, composition, imagery, and motion together. Avoid a universal house style: warm cream and serif, neon gradients, glass panels, geometric symbols, and mascots are all choices that require a reason.

For raster studies, use the agent's **Codex image-generation tool** or an available **Nano Banana image workflow/API**. Read its installed instructions and use its supported interface; do not assume this GoodEye server can invoke a chat-only image tool. Use the environment's configured credentials for API access. Never put credentials in GoodEye, prompts, or artifacts. If neither image tool is available, report that blocker in GoodEye and continue evidence/strategy work. Do not substitute fake images or claim generation ran.

Give each input image an explicit role: approved asset to preserve, reference for material/composition, or edit target. Build prompts from the strategy and observed references. State what stays fixed and what may vary. Record the actual tool/model when available, prompt, references, and subsequent edits. Inspect every generated result; reject generic symbolism, bogus lettering, inconsistent geometry, visual imitation, and context failures. Use targeted edits rather than restarting the identity for each iteration.

Submit studies to this project's Brand collection with `goodeye submit`. Publish each **concept** as a separate proposal with submitted artifact IDs/versions and provenance. Show applications that expose weaknesses: a small mark, real interface or game view, typography with real copy, and an appropriate light/dark or print context. The set depends on the product. A polished moodboard alone is not proof of a usable identity. Concept acceptance continues to the system stage.

## Resolve the system

After the selected concept, resolve production assets and coherent rules. Use native SVG/code tools for final vectors, typography, tokens, and layouts; generated raster concepts are not finished vector logos. Verify letterforms, small sizes, color contrast in actual pairings, references, exports, and intended applications. Label generated mockups separately from tested production assets. Preserve editable sources.

Propose the **system** with exact, supported palette/type values, logo references, voice, visual/motion rules, and export requirements. Include rationale, evidence, reviewed artifacts, and production limitations. Scale the output to the need. A brand bible should record decisions and examples, not pages of generic advice. Source documents remain unchanged until acceptance authorizes the specific update and repository ownership rules permit it.

## Respond to steering

A request may change the entire direction or just color, type, voice, logos, imagery/motion, references, exports, or strategy. Read the scope and previous turns. State what changes and what must remain. For a scoped request, propose only that profile field; preserve all others. Reuse the approved files and reference images. Explain if a requested change conflicts with an established constraint, then ask for that decision in the app.

Use **revision** for bounded changes and **import** for reconstructing an existing system. These can skip a full exploration when evidence supports the choice. A new request supersedes outstanding drafts; the latest request and profile revision are checked by the CLI. Re-read and revise rather than forcing stale work through.

A delivery acknowledgment means “read,” not “done.” Report working/questions with `direction update`, publish proposals with `direction propose`, and wait for the user's response. On an accepted final proposal, read the adopted profile before applying the authorized changes to canonical documents. Record links and approval provenance there. Do not auto-publish an unapproved brand or treat proposed guidelines as current rules.
