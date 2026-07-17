---
name: "ddw-imagegen"
description: "Use when the user explicitly wants DDW image generation or editing, names api.ddwapi.dpdns.org, requests their DDW URL/key, or invokes $ddw-imagegen or @ddw-imagegen. Generate, edit, composite, or deliver raster images without exposing API mechanics. Do not use for SVG, vector, or code-native graphics."
---

# DDW Image Generation

Treat DDW image work like a built-in capability: turn the user's natural-language request into a finished image, inspect it, and show it. Keep API mechanics private.

## Quick experience

- Configured users speak naturally; do not ask them to choose generate/edit or provide CLI parameters.
- Infer generate, edit, composite, variants, preview-only, or project-bound intent.
- Run one integrated `create` command and keep mechanics private.
- Treat the explicit request as authorization for exactly the requested output count. Do not ask again before the first submit.
- Do not narrate dry-runs, uploads, job IDs, polling, ledgers, retries, or transport compression.

## Decide without asking

- No edit target -> generate.
- Existing image plus requested change -> edit.
- Multiple role-specific images -> composite; inspect every local input first and name its role in the prompt.
- Same brief, N alternatives -> one `--n N` operation.
- Distinct deliverables -> separate operations, at most two concurrently.
- Use conventional composition, lighting, quality, size, and filename defaults only when the request leaves them open. Preserve exact requested in-image text verbatim.

## One-shot workflow

1. Resolve `$CODEX_HOME/skills/ddw-imagegen/scripts/ddw_image_gen.py`, or `~/.codex/skills/ddw-imagegen/scripts/ddw_image_gen.py` when `CODEX_HOME` is unset. Work in the user's workspace.
2. Before an edit or composite, inspect every local source with `view_image`. Identify the edit target, supporting references, requested changes, and invariants.
3. Shape a production-ready prompt with `references/prompting.md`; keep a detailed user brief intact and do not invent brands, subjects, or text.
4. Run one normal operation:

```powershell
& $python "$skillRoot/scripts/ddw_image_gen.py" create --prompt "<final prompt>" --out "<absolute output path>"
```

For edits, add ordered `--image "<absolute path>"` inputs. Add `--size` or `--quality` only when requested or needed for the deliverable. Omit `--out` only when no destination matters.

`create` performs local validation, reference preparation, one paid submit, recovery, polling, download, output-count checks, image validation, and atomic saving. Never expose keys or job tokens.

## Transparency preflight

Use this preflight before any paid submit. DDW `gpt-image-2` has no native transparent-background control.

Before submitting any transparent request, run `scripts/remove_chroma_key.py --check` with the selected Python runtime. If Pillow is unavailable, switch to the Codex bundled runtime shown by the helper; do not spend a credit until this local check passes.

- **Simple opaque subject:** Do not ask again. Request a flat, high-contrast chroma-key background that is absent from the subject, run one paid `create`, then run `scripts/remove_chroma_key.py` locally. Inspect transparent corners, subject coverage, and color fringe.
- **Complex transparency:** Hair, fur, feathers, smoke, glass, liquids, translucent materials, reflective edges, and soft shadows are complex. Do not submit yet. Explain that chroma-key extraction may damage transparency or edge detail, then ask the user whether to spend one credit on that approximation. Only submit after explicit approval.

If the subject naturally contains the proposed key color, choose another key. A failed extraction is not authorization for another paid generation.

## Deliver

- Inspect every output with `view_image` for the requested subject, composition, exact text, artifacts, and edit invariants.
- Persist every project deliverable in the workspace with a descriptive/versioned name.
- Render successful images inline, report the absolute path briefly, and update consuming code when requested. Read back each changed reference, confirm it resolves to the delivered file, then run the project's relevant build or asset check before claiming the integration is complete.
- A weak result is not authorization for a second paid submit.

## Defaults and limits

- Model: `gpt-image-2`; quality: `high` unless the user asks for a draft or speed; omit size unless specified.
- Default output is a non-destructive unique PNG under `output/ddw-imagegen/`.
- Use visible attachment paths directly; do not ask the user to re-type them.
- Never make an unrequested second paid submit. A quality retry or replacement after an ambiguous submit requires approval.

## Failure contract

On any failure, state: (1) what happened, (2) whether a paid submit is known to have occurred, and (3) the safe next action. Keep raw evidence and recovery state private unless diagnostics are requested.

## Stop conditions

- Missing key: ask the user to configure `DDW_IMAGE_API_KEY` or `DDW_API_KEY`. A generic `API_KEY` is used only with `--api-key-env API_KEY`. Configure `DDW_IMAGE_BASE_URL` when the endpoint is not the default.
- Ambiguous submit without a captured handle: do not replay it; report the operation ID briefly and ask before a new paid submit.
- Captured handle or interrupted polling: rerun the same `create` command; it resumes the existing operation instead of submitting again.
- Terminal provider failure, invalid image payload, incomplete output count, or local delivery failure: preserve evidence, report the failure contract, and do not resubmit automatically.

Read `references/cli.md` only for diagnostics, manual recovery, masks, advanced fields, or acceptance testing. Read `references/codex-network.md` only for network failures.

When changing transport, billing, recovery, or acceptance behavior, preserve the invariants in `references/review-and-test-notes.md` and run the full offline gates listed there.
