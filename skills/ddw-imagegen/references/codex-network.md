# Network notes

This skill needs outbound HTTPS access to the configured `DDW_IMAGE_BASE_URL`.

Default:

```text
https://api.ddwapi.dpdns.org
```

The CLI adds `/v1` automatically; `https://api.ddwapi.dpdns.org/v1` is also accepted.

All image requests are routed through `/image-jobs` and then polled by `id/token`. This avoids keeping the original generation request open for a long time.

If the environment blocks network access, the CLI can still be tested with `--dry-run`, but actual image generation will fail until network access is available.
