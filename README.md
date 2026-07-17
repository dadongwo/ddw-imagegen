# DDW Image Gen Skill

Codex skill for natural-language DDW image generation, editing, compositing, delivery, recovery, and offline validation.

## Install

Use the `skills/ddw-imagegen` directory as the skill path:

```text
https://github.com/dadongwo/ddw-imagegen/tree/main/skills/ddw-imagegen
```

Or install it with the Codex skill installer:

```powershell
python install-skill-from-github.py --url https://github.com/dadongwo/ddw-imagegen/tree/main/skills/ddw-imagegen
```

## Configure

Set the API key outside the repository:

```powershell
[Environment]::SetEnvironmentVariable("DDW_IMAGE_API_KEY", "你的 DDW API Key", "User")
```

The default API base URL is `https://api.ddwapi.dpdns.org`. It can be overridden with `DDW_IMAGE_BASE_URL`.

Never commit API keys, task tokens, ledgers, generated images, or `.env` files.

See [`skills/ddw-imagegen/ANALYSIS-AND-CONFIG.md`](skills/ddw-imagegen/ANALYSIS-AND-CONFIG.md) for configuration and review notes.
