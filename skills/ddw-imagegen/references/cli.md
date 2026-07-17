# DDW Image CLI

## Contents

- Configuration
- One-shot usage
- Actionable errors
- Advanced controls
- Offline and live verification

## Configuration

Normal use accepts these two environment variables; no other setting is needed. The API key is required. The base URL below is the built-in default, so set it when using another DDW-compatible endpoint or when you prefer explicit configuration:

```text
DDW_IMAGE_API_KEY=<your key>
DDW_IMAGE_BASE_URL=https://api.ddwapi.dpdns.org
```

The base URL should point to the DDW API origin. Non-local plain HTTP is rejected.

## One-shot usage

Always run the installed script by absolute path while keeping the user's workspace as the current directory.

```powershell
& $python "$skillRoot/scripts/ddw_image_gen.py" create `
  --prompt "未来感产品海报" `
  --out "C:/absolute/workspace/output/poster.png"
```

Edit or composite:

```powershell
& $python "$skillRoot/scripts/ddw_image_gen.py" create `
  --prompt "Put the product from Image 1 into the outdoor scene from Image 2" `
  --image "C:/absolute/product.png" `
  --image "C:/absolute/scene.png" `
  --out "C:/absolute/workspace/output/composite.png"
```

`create` selects generation or edit automatically. It defaults to quiet one-shot execution, creates unique output names when `--out` is omitted, rejects unsafe pixel counts before full decode, reduces references over 1 MB until they meet the upload threshold, submits once, automatically resumes captured jobs, validates real image bytes and output count, and prints a sanitized JSON result.

## Actionable errors

| Symptom | Paid-submit certainty | Safe action |
|---|---|---|
| Configuration error before a request is shown | No paid submit | Set both variables above, then rerun the identical command. |
| Input validation or unsafe pixel-count error | No paid submit | Fix the prompt, image, mask, or size and rerun. |
| Request failed before an operation ID was captured | Unknown | Do not retry blindly; inspect the sanitized state first. |
| Polling or output validation failed after an operation ID was captured | A submit may already exist | Rerun the identical `create` command so captured work can be resumed without another POST. |
| Operation is ambiguous and no recovery handle exists | Existing submit cannot be confirmed | Use `inspect`/`recover`; request explicit approval before any low-level supersede or new paid submit. |

Useful controls:

```text
--model gpt-image-2
--quality low|medium|high|auto
--size WIDTHxHEIGHT|auto
--n 1..10
--output-format png|jpeg|webp
--image <path>                 repeat for multiple references
--input-fidelity <value>
--upload-threshold 1000000
--no-auto-preflight
--verbose
```

The user request itself authorizes the first requested output. Do not run a separate dry-run for normal work.

## Advanced controls

Legacy compatibility and low-level controls belong here. `DDW_API_KEY` is also accepted; `API_KEY` is never read implicitly, and can be selected intentionally with `--api-key-env API_KEY`. `--base-url` overrides `DDW_IMAGE_BASE_URL` for diagnostics. `--mask <path>`, raw parameters, and manual recovery are advanced-only controls.

Low-level `generate`, `edit`, `poll`, `inspect`, `recover`, `resolve`, and `generate-batch` remain available for diagnostics. Do not use `submit-only` as the normal path. Paid POST retries are disabled; only transient poll GETs retry.

`--extra-json '{...}'` and repeated `--param key=value` pass provider-specific fields. They cannot override protected fields such as model, count, size, quality, output format, moderation, or compression. Use them only when the configured endpoint documents the additional field.

For transparent cutouts, first run `scripts/remove_chroma_key.py --check`. Only after that local dependency check passes, use a flat chroma-key background and the helper; do not assume `gpt-image-2` supports native transparency.

### Recovery and diagnostics

The default ledger is user-private state at `$CODEX_HOME/state/ddw-imagegen/jobs.jsonl` (or `~/.codex/state/ddw-imagegen/jobs.jsonl`). It is append-only during normal operation, fsynced, locked across processes, and fails closed if corrupted. Job tokens are not written into JSONL: resumable handles use a separate private token store, and terminal-job tokens are removed. On upgrade, legacy workspace ledgers are atomically scrubbed, imported into private state, and any recoverable token is moved to the private store. Override the ledger with `DDW_IMAGE_JOB_LEDGER` or `--job-ledger` only for diagnostics.

Normal recovery is automatic: rerun the identical `create` command. A captured submitted job is polled again without another POST.

Inspect sanitized state:

```powershell
& $python "$skillRoot/scripts/ddw_image_gen.py" inspect --out "C:/absolute/output.png"
```

Recover by operation ID:

```powershell
& $python "$skillRoot/scripts/ddw_image_gen.py" recover `
  --operation-id "op-..." `
  --out "C:/absolute/output.png"
```

Neither command prints the job token. An ambiguous operation with no handle is never replayed automatically. A new paid submit requires explicit user approval and the exact low-level supersede flags documented by `generate --help` or `edit --help`.

## Offline and live verification

Free request-shape check:

```powershell
& $python "$skillRoot/scripts/ddw_image_gen.py" create --prompt "test" --dry-run
```

The acceptance runner defaults to `1024x1024`, `2048x2048`, and `3840x2160`. Dry-run mode does not call the API or write recovery state; it checks generation, edit, three variants in one operation, two-reference composite, transparent runtime preflight, and project output paths:

```powershell
& $python "$skillRoot/scripts/run_acceptance_tests.py" --dry-run --out-dir "C:/absolute/acceptance"
```

Live acceptance spends credits and requires the user's explicit request plus `--yes`. It uses the same integrated `create` path and never performs a separate submit/poll token handoff.
