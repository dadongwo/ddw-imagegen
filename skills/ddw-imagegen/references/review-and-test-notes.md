# Operational Safety Reference

Read this only when reviewing or changing transport, recovery, billing, or acceptance behavior. Normal image work does not need it.

## Invariants

- A user request authorizes only the requested output count.
- A normal `create` operation performs at most one paid POST.
- A timeout or unknown response is not permission to submit again.
- A captured remote handle resumes the existing operation; it does not create another job.
- An ambiguous operation without a handle stops until the user approves a replacement submit.
- Terminal operations remain terminal and their private job token is removed.
- Dry-run and inspection paths are read-only and do not migrate or rewrite recovery state.
- Reference images and masks fail local safety checks before any paid submit.
- Returned image URLs must pass public HTTPS destination checks on every redirect.
- Provider output is preserved; requested and returned dimensions are reported rather than silently resampled.

## Required offline gates

- Full unit-test discovery.
- Python syntax compilation for all bundled scripts.
- Dry-run request-shape checks for generation, edit, composite, and variants.
- Local transparent-output preflight and chroma-key functional test.
- Recovery tests for ambiguous submit, captured-job resume, terminal provider failure, invalid output, and local delivery failure.
- Redaction checks proving API keys and job tokens do not enter logs or JSONL state.

## Live acceptance boundary

Live acceptance spends credits. Run it only when the user explicitly requests it and confirms the exact operation count. Never use a live retry to compensate for a failed acceptance case. Record returned dimensions and provider terminal state without manufacturing a replacement result.
