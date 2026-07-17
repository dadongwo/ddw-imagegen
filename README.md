# DDW Image Gen Skill

Codex skill for natural-language DDW image generation, editing, compositing, delivery, recovery, and offline validation.

## What it does

- Generates, edits, composites, and delivers raster images through DDW.
- Uses one integrated asynchronous job flow with local recovery state.
- Validates references and outputs before delivery.
- Keeps API keys and recoverable job tokens out of the repository and normal output.
- Includes offline tests, a mock server, and transparency preflight helpers.

## Install from GitHub

Skill path:

```text
https://github.com/dadongwo/ddw-imagegen/tree/main/skills/ddw-imagegen
```

In Codex, ask the agent explicitly:

```text
请从以下 GitHub 路径安装并启用 DDW Image Gen Skill：

https://github.com/dadongwo/ddw-imagegen/tree/main/skills/ddw-imagegen

使用当前环境中的 DDW_IMAGE_API_KEY，不要索要、打印或写入 API Key。
安装后执行离线验证。
```

If installing manually, copy `skills/ddw-imagegen` to:

```text
%USERPROFILE%\.codex\skills\ddw-imagegen
```

or to `$CODEX_HOME/skills/ddw-imagegen` when `CODEX_HOME` is configured.

Do not rely on a repository-local `install-skill-from-github.py` command. The installer is provided by the Codex environment, and its exact local path can vary by installation.

## Configure the API

The skill reads the key from the process environment. The preferred variable is:

```text
DDW_IMAGE_API_KEY
```

`DDW_API_KEY` is also accepted for compatibility. The default API base URL is:

```text
https://api.ddwapi.dpdns.org
```

Override it only when using another compatible endpoint:

```text
DDW_IMAGE_BASE_URL
```

### Windows PowerShell

For a temporary session:

```powershell
$env:DDW_IMAGE_API_KEY = "<your DDW API key>"
$env:DDW_IMAGE_BASE_URL = "https://api.ddwapi.dpdns.org"
```

For persistent user-level configuration, use Windows Environment Variables or a secret manager. Avoid pasting the real key into chat, shell history, screenshots, source files, or issue reports.

### Linux/macOS

```bash
export DDW_IMAGE_API_KEY='<your DDW API key>'
export DDW_IMAGE_BASE_URL='https://api.ddwapi.dpdns.org'
```

Do not send the real key to an Agent as part of the prompt. Configure it in the local environment or the runtime's secret store, then tell the Agent to use the existing environment variable.

## Verify without spending a credit

Run from the installed skill directory:

```powershell
python scripts/ddw_image_gen.py create --dry-run --prompt "test"
python scripts/run_acceptance_tests.py --offline
```

The dry run checks prompt and configuration flow without submitting a paid image job. A real generation request may consume credits and should only be submitted when explicitly requested.

## Common problems

| Symptom | Check |
|---|---|
| `No API key found` | Configure `DDW_IMAGE_API_KEY` in the same runtime that launches Codex, then start a new session. |
| `Pillow` import error | Use the Codex bundled Python runtime or install Pillow in the selected Python environment. |
| Network or TLS failure | Check `DDW_IMAGE_BASE_URL`, HTTPS access, proxy policy, and DNS. |
| Interrupted job | Use the same create/recovery flow; do not blindly submit a second paid job. |
| Transparent output request | The skill performs a local chroma-key preflight; complex edges may require explicit approval before spending a credit. |

## Repository layout

- `skills/ddw-imagegen/SKILL.md`: behavior, safety rules, and workflow.
- `skills/ddw-imagegen/scripts/`: integrated CLI and image helpers.
- `skills/ddw-imagegen/references/`: network, prompting, CLI, and review notes.
- `skills/ddw-imagegen/tests/`: offline contract and regression tests.
- `skills/ddw-imagegen/ANALYSIS-AND-CONFIG.md`: detailed configuration and review guide.

## Security boundary

Never commit or share:

- API keys or bearer credentials;
- asynchronous job tokens;
- local ledgers or token stores;
- generated images containing private data;
- `.env` files, shell history, or local Codex state.

See [`skills/ddw-imagegen/ANALYSIS-AND-CONFIG.md`](skills/ddw-imagegen/ANALYSIS-AND-CONFIG.md) for detailed configuration, recovery, and review notes.
