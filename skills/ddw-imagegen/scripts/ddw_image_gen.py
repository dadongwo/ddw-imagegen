#!/usr/bin/env python3
"""CLI for image generation/editing through a DDW-compatible background job API.

The API shape is based on the user's curl examples:

1. Submit a job:
   POST {base_url}/image-jobs?endpoint=%2Fv1%2Fimages%2Fgenerations
   POST {base_url}/image-jobs?endpoint=%2Fv1%2Fimages%2Fedits

2. Poll the returned id/token:
   GET {base_url}/image-jobs/{id}?token={token}

The CLI never stores the API key. It reads a key from DDW_IMAGE_API_KEY,
DDW_API_KEY, or an explicitly selected environment variable.
"""

from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
import hashlib
import importlib.util
import ipaddress
import io
import json
import mimetypes
import os
from pathlib import Path
import re
import socket
import struct
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen
import uuid
import zlib


def _default_state_dir() -> Path:
    codex_home = os.getenv("CODEX_HOME")
    root = Path(codex_home).expanduser() if codex_home else Path.home() / ".codex"
    return (root / "state" / "ddw-imagegen").resolve()


DEFAULT_BASE_URL = "https://api.ddwapi.dpdns.org"
DEFAULT_MODEL = "gpt-image-2"
DEFAULT_QUALITY = "high"
DEFAULT_OUTPUT_PATH = "output/ddw-imagegen/output.png"
DEFAULT_OUTPUT_FORMAT = "png"
DEFAULT_POLL_INTERVAL = 2.0
DEFAULT_TIMEOUT_SECONDS = 600.0
DEFAULT_REQUEST_TIMEOUT = 300.0
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36 DDWImageGen/5.0"
)
DEFAULT_DONE_WITHOUT_IMAGE_POLLS = 10
DEFAULT_POLL_RETRIES = 2
DEFAULT_SUBMIT_RETRIES = 0
DEFAULT_RETRY_DELAY = 1.0
DEFAULT_STATE_DIR = _default_state_dir()
DEFAULT_JOB_LEDGER = str(DEFAULT_STATE_DIR / "jobs.jsonl")
LEGACY_WORKSPACE_JOB_LEDGER = ".ddw-imagegen/jobs.jsonl"
DEFAULT_LEDGER_LOCK_TIMEOUT = 10.0
DEFAULT_STALE_LOCK_SECONDS = 300.0
TRANSIENT_HTTP_STATUSES = {408, 409, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524}
DEFAULT_KEY_ENVS = ("DDW_IMAGE_API_KEY", "DDW_API_KEY")
MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000

GENERATIONS_ENDPOINT = "/v1/images/generations"
EDITS_ENDPOINT = "/v1/images/edits"
MAX_IMAGE_BYTES = 50 * 1024 * 1024
MAX_BATCH_JOBS = 500
ALLOWED_MODERATIONS = {None, "auto", "low"}
RESERVED_PAYLOAD_FIELDS = {
    "background",
    "compression",
    "input_fidelity",
    "model",
    "moderation",
    "n",
    "output_compression",
    "output_format",
    "prompt",
    "quality",
    "size",
}
BLOCKED_IMAGE_HOSTS = {
    "instance-data",
    "metadata",
    "metadata.google.internal",
}

DONE_STATUSES = {"succeeded", "success", "completed", "complete", "done", "finished", "ok"}
FAILED_STATUSES = {"failed", "failure", "error", "cancelled", "canceled", "expired", "rejected"}
RUNNING_STATUSES = {
    "queued",
    "queueing",
    "pending",
    "running",
    "processing",
    "submitted",
    "in_progress",
    "in-progress",
    "waiting",
    "created",
}
TERMINAL_LEDGER_EVENTS = {
    "completed",
    "completed_from_resume",
    "failed",
    "resolved_not_created",
    "superseded",
}
RECOVERABLE_TOKEN_STATES = {"submitted", "polling", "delivery_failed"}
TERMINAL_LEDGER_STATES = {"completed", "failed", "not_created", "superseded"}


class CliError(RuntimeError):
    pass


class DeliveryError(CliError):
    """The paid job completed, but its local image delivery was invalid."""


class JobFailedError(CliError):
    """The remote job reached an explicit terminal failure state."""


def _die(message: str, code: int = 1) -> None:
    print(f"Error: {_redact_text(message)}", file=sys.stderr)
    raise SystemExit(code)


def _warn(message: str) -> None:
    print(f"Warning: {message}", file=sys.stderr)


def _json_dumps(value: Any, *, indent: Optional[int] = None) -> str:
    return json.dumps(value, ensure_ascii=False, indent=indent, sort_keys=bool(indent))


def _redact_text(value: str) -> str:
    text = re.sub(r"([?&]token=)[^&\s\"']+", r"\1<redacted>", value, flags=re.IGNORECASE)
    text = re.sub(
        r'((?:"|\b)(?:token|job_?token|api_key)(?:"|\b)\s*[:=]\s*["\']?)[^"\'\s,}\]]+',
        r"\1<redacted>",
        text,
        flags=re.IGNORECASE,
    )
    return re.sub(r"(Bearer\s+)[^\s,}\]]+", r"\1<redacted>", text, flags=re.IGNORECASE)


def _read_prompt(prompt: Optional[str], prompt_file: Optional[str]) -> str:
    if prompt and prompt_file:
        _die("Use --prompt or --prompt-file, not both.")
    if prompt_file:
        path = Path(prompt_file)
        if not path.exists():
            _die(f"Prompt file not found: {path}")
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            _die(f"Prompt file is empty: {path}")
        return text
    if prompt and prompt.strip():
        return prompt.strip()
    _die("Missing prompt. Use --prompt or --prompt-file.")
    return ""


def _normalize_base_url(base_url: Optional[str]) -> str:
    raw = (base_url or os.getenv("DDW_IMAGE_BASE_URL") or DEFAULT_BASE_URL).strip()
    if not raw:
        raw = DEFAULT_BASE_URL
    raw = raw.rstrip("/")
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        _die(f"Invalid base URL: {raw}")
    if parsed.scheme == "http" and (parsed.hostname or "").lower() not in {
        "localhost",
        "127.0.0.1",
        "::1",
    }:
        _die("Refusing to send image API credentials over non-local HTTP. Use HTTPS.")
    return raw


def _key_env_candidates(api_key_env: Optional[str]) -> List[str]:
    if api_key_env:
        return [api_key_env]
    return list(DEFAULT_KEY_ENVS)


def _get_api_key(*, api_key_env: Optional[str], dry_run: bool) -> Optional[str]:
    for name in _key_env_candidates(api_key_env):
        value = os.getenv(name)
        if value:
            return value
    if dry_run:
        _warn(
            "No API key found. Dry-run mode is allowed. "
            f"Checked: {', '.join(_key_env_candidates(api_key_env))}."
        )
        return None
    _die(
        "No API key found. Export DDW_IMAGE_API_KEY or DDW_API_KEY. "
        "Use --api-key-env API_KEY only when that generic variable is intentionally the DDW key."
    )
    return None


def _jobs_submit_url(base_url: str, endpoint: str) -> str:
    api_root = base_url if base_url.endswith("/v1") else f"{base_url}/v1"
    return f"{api_root}/image-jobs?endpoint={quote(endpoint, safe='')}"


def _jobs_poll_url(base_url: str, job_id: str, token: str) -> str:
    api_root = base_url if base_url.endswith("/v1") else f"{base_url}/v1"
    return f"{api_root}/image-jobs/{quote(job_id, safe='')}?token={quote(token, safe='')}"


def _base_headers(*, accept: str = "application/json") -> Dict[str, str]:
    """Return stable headers for Cloudflare/WAF-friendly requests.

    urllib's default Python-urllib User-Agent is sometimes blocked by edge
    rules. Keep this explicit, but let users override it if their gateway
    expects a particular client name.
    """
    user_agent = os.getenv("DDW_IMAGE_USER_AGENT", DEFAULT_USER_AGENT).strip() or DEFAULT_USER_AGENT
    return {"User-Agent": user_agent, "Accept": accept}


def _auth_headers(api_key: Optional[str], *, accept: str = "application/json") -> Dict[str, str]:
    headers = _base_headers(accept=accept)
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _read_json_response(response: Any) -> Any:
    body = response.read()
    text = body.decode("utf-8", errors="replace")
    if not text.strip():
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise CliError(
            f"Expected JSON response but got invalid JSON: {exc}: {_redact_text(text[:1000])}"
        ) from exc


def _http_json(
    *,
    method: str,
    url: str,
    headers: Dict[str, str],
    body: Optional[bytes] = None,
    timeout: float = DEFAULT_REQUEST_TIMEOUT,
    retries: int = 0,
    retry_delay: float = DEFAULT_RETRY_DELAY,
    retry_statuses: Optional[Iterable[int]] = None,
) -> Any:
    retry_statuses = set(retry_statuses or TRANSIENT_HTTP_STATUSES)
    last_error: Optional[BaseException] = None
    for attempt in range(max(0, retries) + 1):
        request = Request(url=url, data=body, method=method, headers=headers)
        try:
            with urlopen(request, timeout=timeout) as response:
                return _read_json_response(response)
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            last_error = CliError(
                _redact_text(f"HTTP {exc.code} from {url}: {detail[:3000]}")
            )
            if attempt < retries and exc.code in retry_statuses:
                time.sleep(max(0.0, retry_delay) * (attempt + 1))
                continue
            raise last_error from exc
        except URLError as exc:
            last_error = CliError(_redact_text(f"Network error for {url}: {exc}"))
            if attempt < retries:
                time.sleep(max(0.0, retry_delay) * (attempt + 1))
                continue
            raise last_error from exc
    assert last_error is not None
    raise last_error


def _validate_image_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme.lower() != "https":
        raise DeliveryError("Provider image URLs must use HTTPS.")
    hostname = (parsed.hostname or "").rstrip(".").lower()
    if not hostname or parsed.username is not None or parsed.password is not None:
        raise DeliveryError("Provider image URL has an invalid host.")
    if hostname in BLOCKED_IMAGE_HOSTS or hostname == "localhost" or hostname.endswith(".localhost"):
        raise DeliveryError("Provider image URL targets an unsafe host.")

    try:
        addresses = socket.getaddrinfo(
            hostname,
            parsed.port or 443,
            type=socket.SOCK_STREAM,
        )
    except (OSError, ValueError) as exc:
        raise DeliveryError(f"Could not safely resolve provider image host: {exc}") from exc
    if not addresses:
        raise DeliveryError("Could not safely resolve provider image host.")
    for address in addresses:
        raw_ip = str(address[4][0]).split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(raw_ip)
        except ValueError as exc:
            raise DeliveryError("Provider image host resolved to an invalid address.") from exc
        if not ip.is_global:
            raise DeliveryError(
                f"Provider image URL resolved to a non-public or unsafe address: {ip}."
            )
    return url


class _SafeImageRedirectHandler(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Any,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> Any:
        _validate_image_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _download_bytes(url: str, *, timeout: float = DEFAULT_REQUEST_TIMEOUT) -> Tuple[bytes, str]:
    _validate_image_url(url)
    request = Request(url=url, method="GET", headers=_base_headers(accept="image/*,*/*"))
    opener = build_opener(_SafeImageRedirectHandler())
    try:
        with opener.open(request, timeout=timeout) as response:
            content_type = response.headers.get("Content-Type", "")
            declared = response.headers.get("Content-Length")
            if declared and int(declared) > MAX_DOWNLOAD_BYTES:
                raise DeliveryError(f"Image download exceeds {MAX_DOWNLOAD_BYTES} bytes.")
            data = response.read(MAX_DOWNLOAD_BYTES + 1)
            if len(data) > MAX_DOWNLOAD_BYTES:
                raise DeliveryError(f"Image download exceeds {MAX_DOWNLOAD_BYTES} bytes.")
            return data, content_type
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise CliError(f"HTTP {exc.code} while downloading image: {detail[:1000]}") from exc
    except URLError as exc:
        raise CliError(f"Network error while downloading image: {exc}") from exc


def _submit_json_job(
    *,
    base_url: str,
    endpoint: str,
    api_key: Optional[str],
    payload: Dict[str, Any],
    request_timeout: float,
    operation: Optional[Dict[str, Any]] = None,
    retries: int = DEFAULT_SUBMIT_RETRIES,
    retry_delay: float = DEFAULT_RETRY_DELAY,
) -> Any:
    body = _json_dumps(payload).encode("utf-8")
    headers = {
        **_auth_headers(api_key),
        **(_operation_headers(operation) if operation else {}),
        "X-Image-Job-Endpoint": endpoint,
        "Content-Type": "application/json",
    }
    return _http_json(
        method="POST",
        url=_jobs_submit_url(base_url, endpoint),
        headers=headers,
        body=body,
        timeout=request_timeout,
        retries=retries,
        retry_delay=retry_delay,
    )


def _guess_mime(path: Path) -> str:
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed or "application/octet-stream"


def _encode_multipart(
    fields: Sequence[Tuple[str, str]],
    files: Sequence[Tuple[str, Path]],
) -> Tuple[bytes, str]:
    boundary = f"----ddw-imagegen-{uuid.uuid4().hex}"
    chunks: List[bytes] = []

    for name, value in fields:
        chunks.extend(
            [
                f"--{boundary}\r\n".encode("utf-8"),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"),
                value.encode("utf-8"),
                b"\r\n",
            ]
        )

    for field_name, path in files:
        filename = path.name
        mime = _guess_mime(path)
        chunks.extend(
            [
                f"--{boundary}\r\n".encode("utf-8"),
                (
                    f'Content-Disposition: form-data; name="{field_name}"; '
                    f'filename="{filename}"\r\n'
                ).encode("utf-8"),
                f"Content-Type: {mime}\r\n\r\n".encode("utf-8"),
                path.read_bytes(),
                b"\r\n",
            ]
        )

    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def _submit_multipart_job(
    *,
    base_url: str,
    endpoint: str,
    api_key: Optional[str],
    fields: Sequence[Tuple[str, str]],
    files: Sequence[Tuple[str, Path]],
    request_timeout: float,
    operation: Optional[Dict[str, Any]] = None,
    retries: int = DEFAULT_SUBMIT_RETRIES,
    retry_delay: float = DEFAULT_RETRY_DELAY,
) -> Any:
    body, content_type = _encode_multipart(fields, files)
    headers = {
        **_auth_headers(api_key),
        **(_operation_headers(operation) if operation else {}),
        "X-Image-Job-Endpoint": endpoint,
        "Content-Type": content_type,
    }
    return _http_json(
        method="POST",
        url=_jobs_submit_url(base_url, endpoint),
        headers=headers,
        body=body,
        timeout=request_timeout,
        retries=retries,
        retry_delay=retry_delay,
    )


def _stringify_form_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bool) or value is None:
        return _json_dumps(value)
    if isinstance(value, (int, float)):
        return str(value)
    return _json_dumps(value)


def _read_extra_json(extra_json: Optional[str]) -> Dict[str, Any]:
    if not extra_json:
        return {}
    raw = extra_json.strip()
    if raw.startswith("@"):
        path = Path(raw[1:])
        if not path.exists():
            _die(f"Extra JSON file not found: {path}")
        raw = path.read_text(encoding="utf-8")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        _die(f"Invalid --extra-json: {exc}")
    if not isinstance(value, dict):
        _die("--extra-json must be a JSON object or @path-to-object.json")
    return value


def _parse_param(values: Optional[Sequence[str]]) -> Dict[str, Any]:
    params: Dict[str, Any] = {}
    for item in values or []:
        if "=" not in item:
            _die(f"Invalid --param {item!r}; use key=value.")
        key, raw_value = item.split("=", 1)
        key = key.strip()
        if not key:
            _die(f"Invalid --param {item!r}; key is empty.")
        raw_value = raw_value.strip()
        # Allow lightweight JSON values without forcing quotes around ordinary strings.
        if raw_value[:1] in {'{', '[', '"'} or raw_value in {"true", "false", "null"}:
            try:
                params[key] = json.loads(raw_value)
                continue
            except json.JSONDecodeError:
                pass
        if re.fullmatch(r"-?[0-9]+", raw_value):
            try:
                params[key] = int(raw_value)
                continue
            except ValueError:
                pass
        if re.fullmatch(r"-?[0-9]+\.[0-9]+", raw_value):
            try:
                params[key] = float(raw_value)
                continue
            except ValueError:
                pass
        params[key] = raw_value
    return params


def _compact_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in payload.items() if v is not None}


def _validate_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    n = payload.get("n", 1)
    if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= 10:
        raise CliError("Payload n must be between 1 and 10.")
    model = payload.get("model")
    if not isinstance(model, str) or not model.strip():
        raise CliError("Payload model must be a non-empty string.")
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise CliError("Payload prompt must be a non-empty string.")
    quality = payload.get("quality")
    if quality is not None and (
        not isinstance(quality, str)
        or quality.lower() not in {"low", "medium", "high", "auto"}
    ):
        raise CliError("Payload quality must be one of: low, medium, high, auto.")
    moderation = payload.get("moderation")
    if moderation is not None and (
        not isinstance(moderation, str) or moderation.lower() not in {"auto", "low"}
    ):
        raise CliError("Payload moderation must be one of: auto, low.")
    size = payload.get("size")
    if size is not None and str(model).startswith("gpt-image-2"):
        if not isinstance(size, str):
            raise CliError("Payload size must be a string.")
        payload["size"] = _validate_size(size)
    compression = payload.get("output_compression")
    if compression is not None:
        if isinstance(compression, bool) or not isinstance(compression, int) or not 0 <= compression <= 100:
            raise CliError("Payload output_compression must be between 0 and 100.")
        if _normalise_output_format(payload.get("output_format")) not in {"jpeg", "webp"}:
            raise CliError("Payload output_compression requires output_format jpeg or webp.")
    return payload


def _merge_payload_extras(
    payload: Dict[str, Any],
    *extras: Tuple[str, Dict[str, Any]],
) -> Dict[str, Any]:
    for source, values in extras:
        blocked = sorted(RESERVED_PAYLOAD_FIELDS.intersection(values))
        if blocked:
            raise CliError(
                f"{source} cannot override reserved payload field(s): {', '.join(blocked)}."
            )
        payload.update(values)
    return _validate_payload(payload)



def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_json(value: Any) -> str:
    return _sha256_text(_json_dumps(value))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _edit_request_hash(
    payload: Dict[str, Any], image_paths: Sequence[Path], mask_path: Optional[Path]
) -> str:
    return _sha256_json(
        {
            "payload": payload,
            "images": [_sha256_file(path) for path in image_paths],
            "mask": _sha256_file(mask_path) if mask_path else None,
        }
    )


def _safe_resolve(path: Path) -> str:
    try:
        return str(path.expanduser().resolve())
    except Exception:
        return str(path.expanduser().absolute())


def _job_ledger_path(args: argparse.Namespace, *, prepare: bool = True) -> Optional[Path]:
    if getattr(args, "no_job_ledger", False):
        return None
    configured = getattr(args, "job_ledger", None) or os.getenv("DDW_IMAGE_JOB_LEDGER")
    raw = configured or DEFAULT_JOB_LEDGER
    if not str(raw).strip():
        return None
    path = Path(str(raw)).expanduser().resolve()
    if prepare:
        _prepare_ledger_path(path, import_workspace_legacy=not bool(configured))
    return path


def _output_key_from_args(args: argparse.Namespace) -> str:
    out_dir = getattr(args, "out_dir", None)
    if out_dir:
        return _safe_resolve(Path(out_dir)) + "/"
    return _safe_resolve(Path(getattr(args, "out", DEFAULT_OUTPUT_PATH)))


def _load_ledger(path: Optional[Path]) -> List[Dict[str, Any]]:
    if path is None or not path.exists():
        return []
    records: List[Dict[str, Any]] = []
    for line_no, line in enumerate(
        path.read_text(encoding="utf-8", errors="strict").splitlines(), start=1
    ):
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CliError(f"Job ledger contains invalid JSON at {path}:{line_no}: {exc}") from exc
        if not isinstance(item, dict):
            raise CliError(f"Job ledger is corrupt at {path}:{line_no}: expected a JSON object.")
        records.append(item)
    return records


def _token_store_root() -> Path:
    override = os.getenv("DDW_IMAGE_TOKEN_STORE")
    return Path(override).expanduser().resolve() if override else DEFAULT_STATE_DIR / "job-tokens"


def _token_ref(operation_id: str) -> str:
    return hashlib.sha256(operation_id.encode("utf-8")).hexdigest() + ".token"


def _token_path(token_ref: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{64}\.token", token_ref):
        raise CliError("Job token reference is invalid.")
    return _token_store_root() / token_ref


def _store_job_token(operation_id: str, token: str) -> str:
    token_ref = _token_ref(operation_id)
    output = _token_path(token_ref)
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        os.chmod(output.parent, 0o700)
    temp = output.with_name(f".{output.name}.{uuid.uuid4().hex}.tmp")
    fd: Optional[int] = None
    try:
        fd = os.open(str(temp), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as handle:
            fd = None
            handle.write(token.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, output)
        if os.name != "nt":
            os.chmod(output, 0o600)
    finally:
        if fd is not None:
            os.close(fd)
        try:
            temp.unlink()
        except FileNotFoundError:
            pass
    return token_ref


def _load_job_token(token_ref: str) -> Optional[str]:
    try:
        token = _token_path(token_ref).read_text(encoding="utf-8", errors="strict").strip()
    except FileNotFoundError:
        return None
    return token or None


def _delete_job_token(operation_id: str) -> None:
    try:
        _token_path(_token_ref(operation_id)).unlink()
    except FileNotFoundError:
        pass


def _append_ledger(path: Optional[Path], record: Dict[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        os.chmod(path.parent, 0o700)
    full = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        **record,
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(_json_dumps(full) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    if os.name != "nt":
        os.chmod(path, 0o600)


def _legacy_operation_id(record: Dict[str, Any]) -> str:
    identity = {
        "ts": record.get("ts"),
        "endpoint": record.get("endpoint"),
        "out_key": record.get("out_key"),
        "prompt_hash": record.get("prompt_hash"),
        "payload_hash": record.get("payload_hash"),
    }
    return "legacy-" + _sha256_json(identity)[:20]


def _event_state(event: Optional[str]) -> str:
    return {
        "submit_started": "prepared",
        "submitting": "submitting",
        "submit_failed_unknown": "ambiguous",
        "resolved_not_created": "not_created",
        "submitted": "submitted",
        "polling": "polling",
        "poll_interrupted": "submitted",
        "delivery_failed": "delivery_failed",
        "completed": "completed",
        "completed_from_resume": "completed",
        "failed": "failed",
        "superseded": "superseded",
    }.get(str(event or ""), str(event or "unknown"))


def _operation_snapshots(
    path: Optional[Path],
    *,
    endpoint: Optional[str] = None,
    out_key: Optional[str] = None,
    prompt_hash: Optional[str] = None,
) -> List[Dict[str, Any]]:
    snapshots: List[Dict[str, Any]] = []
    by_id: Dict[str, Dict[str, Any]] = {}
    latest_legacy: Dict[Tuple[Any, Any, Any, Any], str] = {}

    for record in _load_ledger(path):
        event = str(record.get("event") or "")
        operation_id = record.get("operation_id")
        legacy_key = (
            record.get("endpoint"),
            record.get("out_key"),
            record.get("prompt_hash"),
            record.get("payload_hash"),
        )

        if event == "submit_started" and not operation_id:
            operation_id = _legacy_operation_id(record)
            latest_legacy[legacy_key] = operation_id
        elif not operation_id:
            operation_id = latest_legacy.get(legacy_key)
            if not operation_id:
                for candidate in reversed(snapshots):
                    if (
                        candidate.get("endpoint") == record.get("endpoint")
                        and candidate.get("out_key") == record.get("out_key")
                        and candidate.get("prompt_hash") == record.get("prompt_hash")
                    ):
                        operation_id = candidate["operation_id"]
                        break
        if not operation_id:
            operation_id = _legacy_operation_id(record)

        snapshot = by_id.get(str(operation_id))
        if snapshot is None:
            snapshot = {
                "operation_id": str(operation_id),
                "idempotency_key": record.get("idempotency_key")
                or "legacy-" + _sha256_text(str(operation_id))[:32],
            }
            snapshots.append(snapshot)
            by_id[str(operation_id)] = snapshot

        snapshot.update(record)
        snapshot["operation_id"] = str(operation_id)
        snapshot["state"] = _event_state(event)

    for snapshot in snapshots:
        if snapshot.get("state") not in RECOVERABLE_TOKEN_STATES:
            snapshot.pop("token", None)
            continue
        if not snapshot.get("token") and snapshot.get("token_ref"):
            token = _load_job_token(str(snapshot["token_ref"]))
            if token:
                snapshot["token"] = token

    return [
        item
        for item in snapshots
        if (endpoint is None or item.get("endpoint") == endpoint)
        and (out_key is None or item.get("out_key") == out_key)
        and (prompt_hash is None or item.get("prompt_hash") == prompt_hash)
    ]


@contextmanager
def _ledger_lock(
    ledger_path: Optional[Path],
    *,
    timeout: float = DEFAULT_LEDGER_LOCK_TIMEOUT,
    stale_after: float = DEFAULT_STALE_LOCK_SECONDS,
) -> Iterable[None]:
    if ledger_path is None:
        yield
        return

    ledger_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        os.chmod(ledger_path.parent, 0o700)
    lock_path = Path(str(ledger_path) + ".lock")
    deadline = time.monotonic() + max(0.1, timeout)
    fd: Optional[int] = None
    while fd is None:
        try:
            fd = os.open(
                str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
            )
            os.write(fd, f"pid={os.getpid()} ts={time.time()}\n".encode("ascii"))
        except FileExistsError:
            try:
                if time.time() - lock_path.stat().st_mtime > stale_after:
                    lock_path.unlink()
                    continue
            except FileNotFoundError:
                continue
            if time.monotonic() >= deadline:
                raise CliError(f"Timed out waiting for job ledger lock: {lock_path}")
            time.sleep(0.05)

    try:
        yield
    finally:
        if fd is not None:
            os.close(fd)
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def _rewrite_ledger(path: Path, records: Sequence[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        os.chmod(path.parent, 0o700)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    fd: Optional[int] = None
    try:
        fd = os.open(str(temp), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            fd = None
            for record in records:
                handle.write(_json_dumps(record) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        if os.name != "nt":
            os.chmod(path, 0o600)
    finally:
        if fd is not None:
            os.close(fd)
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def _ledger_record_key(record: Dict[str, Any]) -> Tuple[Any, Any, Any, Any]:
    return (
        record.get("endpoint"),
        record.get("out_key"),
        record.get("prompt_hash"),
        record.get("payload_hash"),
    )


def _migrate_ledger_tokens(path: Path) -> None:
    if not path.exists():
        return
    refs_to_delete: set[str] = set()
    with _ledger_lock(path):
        records = _load_ledger(path)
        normalized: List[Dict[str, Any]] = []
        latest_legacy: Dict[Tuple[Any, Any, Any, Any], str] = {}
        normalized_changed = False
        for original in records:
            record = dict(original)
            key = _ledger_record_key(record)
            operation_id = record.get("operation_id")
            if not operation_id:
                if record.get("event") == "submit_started":
                    operation_id = _legacy_operation_id(record)
                else:
                    operation_id = latest_legacy.get(key)
                if not operation_id:
                    operation_id = "legacy-" + _sha256_json(
                        {"job_id": record.get("job_id"), "key": key}
                    )[:32]
                record["operation_id"] = str(operation_id)
                normalized_changed = True
            latest_legacy[key] = str(operation_id)
            normalized.append(record)
        records = normalized
        terminal_ids = {
            str(record["operation_id"])
            for record in records
            if record.get("operation_id")
            and record.get("event") in TERMINAL_LEDGER_EVENTS
        }
        terminal_jobs = {
            str(record["job_id"])
            for record in records
            if record.get("job_id")
            and record.get("event") in TERMINAL_LEDGER_EVENTS
        }
        changed = normalized_changed
        migrated: List[Dict[str, Any]] = []
        for original in records:
            record = dict(original)
            operation_id = str(record.get("operation_id") or "")
            job_id = str(record.get("job_id") or "")
            terminal = (
                (bool(operation_id) and operation_id in terminal_ids)
                or (bool(job_id) and job_id in terminal_jobs)
            )
            token = record.pop("token", None)
            token_ref = record.get("token_ref")
            if token is not None:
                changed = True
            if terminal:
                if token_ref:
                    refs_to_delete.add(str(token_ref))
                    record.pop("token_ref", None)
                    changed = True
            elif token:
                identity = operation_id or (
                    "legacy-"
                    + _sha256_json(
                        {
                            "job_id": job_id,
                            "key": _ledger_record_key(record),
                        }
                    )[:32]
                )
                record["token_ref"] = _store_job_token(identity, str(token))
            migrated.append(record)
        if changed:
            _rewrite_ledger(path, migrated)
    for token_ref in refs_to_delete:
        try:
            _token_path(token_ref).unlink()
        except (FileNotFoundError, CliError):
            pass


def _merge_ledger_records(source: Path, destination: Path) -> None:
    if source.resolve() == destination.resolve() or not source.exists():
        return
    source_records = _load_ledger(source)
    with _ledger_lock(destination):
        destination_records = _load_ledger(destination)
        seen = {_json_dumps(record) for record in destination_records}
        missing = [record for record in source_records if _json_dumps(record) not in seen]
        if missing:
            _rewrite_ledger(destination, [*destination_records, *missing])


def _prepare_ledger_path(path: Path, *, import_workspace_legacy: bool) -> None:
    _migrate_ledger_tokens(path)
    if not import_workspace_legacy:
        return
    legacy = Path(LEGACY_WORKSPACE_JOB_LEDGER).resolve()
    if legacy == path or not legacy.exists():
        return
    _migrate_ledger_tokens(legacy)
    _merge_ledger_records(legacy, path)


def _append_operation_event(
    ledger_path: Optional[Path],
    operation: Dict[str, Any],
    event: str,
    *,
    _locked: bool = False,
    **fields: Any,
) -> None:
    if ledger_path is not None and not _locked:
        with _ledger_lock(ledger_path):
            _append_operation_event(
                ledger_path,
                operation,
                event,
                _locked=True,
                **fields,
            )
        return

    operation_id = str(operation.get("operation_id") or "")
    if ledger_path is not None and operation_id:
        current = next(
            (
                item
                for item in _operation_snapshots(ledger_path)
                if item.get("operation_id") == operation_id
            ),
            None,
        )
        if current and current.get("state") in TERMINAL_LEDGER_STATES:
            if _event_state(event) == current.get("state"):
                _delete_job_token(operation_id)
                return
            raise CliError(
                f"Operation {operation_id} is already terminal "
                f"(state={current.get('state')}); refusing event {event!r}."
            )

    inherited = {
        key: operation.get(key)
        for key in (
            "operation_id",
            "idempotency_key",
            "endpoint",
            "out_key",
            "prompt_hash",
            "payload_hash",
        )
        if operation.get(key) is not None
    }
    safe_fields = dict(fields)
    token = safe_fields.pop("token", None)
    terminal = event in TERMINAL_LEDGER_EVENTS
    if token and operation_id and ledger_path is not None and not terminal:
        safe_fields["token_ref"] = _store_job_token(operation_id, str(token))
    _append_ledger(ledger_path, {"event": event, **inherited, **safe_fields})
    if terminal and operation_id and ledger_path is not None:
        _delete_job_token(operation_id)


def _ambiguous_submit_error(operation: Dict[str, Any], exc: BaseException) -> CliError:
    return CliError(
        "Paid submit result is ambiguous; the server may already have created the image job. "
        f"operation_id={operation['operation_id']}. Do not submit again. Use `inspect` first; "
        "use `recover` only when a job handle exists. Original error: "
        + _redact_text(str(exc))
    )


def _operation_matches(
    operation: Dict[str, Any],
    *,
    endpoint: str,
    out_key: str,
    prompt_hash: str,
    payload_hash: Optional[str] = None,
) -> bool:
    if operation.get("endpoint") != endpoint:
        return False
    if operation.get("out_key") == out_key:
        return True
    if payload_hash is None:
        return operation.get("prompt_hash") == prompt_hash
    return (
        operation.get("prompt_hash") == prompt_hash
        and operation.get("payload_hash") == payload_hash
        and operation.get("state")
        in {
            "prepared",
            "submitting",
            "ambiguous",
            "submitted",
            "polling",
            "delivery_failed",
            "failed",
        }
    )


def _request_matches(
    operation: Dict[str, Any],
    *,
    endpoint: str,
    prompt_hash: str,
    payload_hash: Optional[str],
) -> bool:
    if operation.get("endpoint") != endpoint or operation.get("prompt_hash") != prompt_hash:
        return False
    return payload_hash is None or operation.get("payload_hash") == payload_hash


def _prepare_operation(
    *,
    ledger_path: Optional[Path],
    endpoint: str,
    out_key: str,
    prompt_hash: str,
    payload_hash: str,
    metadata: Dict[str, Any],
    force_new_job: bool = False,
    supersede_operation: Optional[str] = None,
) -> Dict[str, Any]:
    operation_id = "op-" + uuid.uuid4().hex
    idempotency_key = "ddw-" + uuid.uuid4().hex
    with _ledger_lock(ledger_path):
        matches = [
            operation
            for operation in _operation_snapshots(ledger_path)
            if _operation_matches(
                operation,
                endpoint=endpoint,
                out_key=out_key,
                prompt_hash=prompt_hash,
                payload_hash=payload_hash,
            )
            and operation.get("state") not in {"superseded", "not_created"}
        ]
        prior = matches[-1] if matches else None
        if prior is not None:
            prior_id = str(prior["operation_id"])
            prior_state = str(prior.get("state") or "unknown")
            if not force_new_job or supersede_operation != prior_id:
                raise CliError(
                    "A previous paid operation already exists "
                    f"(operation_id={prior_id}, state={prior_state}). "
                    "Inspect or recover it first. Creating another paid job requires both "
                    f"--force-new-job and --supersede-operation {prior_id!r}."
                )
            _append_operation_event(
                ledger_path,
                prior,
                "superseded",
                _locked=True,
                superseded_by=operation_id,
            )
        elif supersede_operation:
            raise CliError(
                f"Cannot supersede operation {supersede_operation!r}: no matching operation exists."
            )

        operation: Dict[str, Any] = {
            "operation_id": operation_id,
            "idempotency_key": idempotency_key,
            "endpoint": endpoint,
            "out_key": out_key,
            "prompt_hash": prompt_hash,
            "payload_hash": payload_hash,
            **metadata,
        }
        _append_operation_event(
            ledger_path, operation, "submit_started", _locked=True, **metadata
        )
        return operation


def _operation_headers(operation: Dict[str, Any]) -> Dict[str, str]:
    operation_id = str(operation["operation_id"])
    idempotency_key = str(operation["idempotency_key"])
    return {
        "Idempotency-Key": idempotency_key,
        "X-Idempotency-Key": idempotency_key,
        "X-Client-Request-Id": operation_id,
    }


def _sanitise_operation(operation: Dict[str, Any]) -> Dict[str, Any]:
    sanitized = dict(operation)
    token = sanitized.pop("token", None)
    sanitized.pop("token_ref", None)
    sanitized.pop("submit_response", None)
    secret = str(token) if token else None

    def redact(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: redact(item) for key, item in value.items() if key.lower() != "token"}
        if isinstance(value, list):
            return [redact(item) for item in value]
        if isinstance(value, str):
            text = value.replace(secret, "<redacted>") if secret else value
            return re.sub(r"([?&]token=)[^&\s]+", r"\1<redacted>", text, flags=re.IGNORECASE)
        return value

    sanitized = redact(sanitized)
    sanitized["has_token"] = bool(token)
    return sanitized


def _resume_existing_if_requested(
    *,
    args: argparse.Namespace,
    ledger_path: Optional[Path],
    endpoint: str,
    out_key: str,
    prompt_hash: str,
    base_url: str,
    api_key: Optional[str],
    expected_count: int,
    output_format: Optional[str],
    payload_hash: Optional[str] = None,
) -> bool:
    if not getattr(args, "resume_existing", False):
        return False
    candidates = [
        operation
        for operation in _operation_snapshots(ledger_path)
        if _request_matches(
            operation,
            endpoint=endpoint,
            prompt_hash=prompt_hash,
            payload_hash=payload_hash,
        )
        and operation.get("job_id")
        and operation.get("token")
        and operation.get("state") in {"submitted", "polling", "delivery_failed"}
    ]
    prior = candidates[-1] if candidates else None
    if prior is None:
        if getattr(args, "auto_resume", False):
            return False
        raise CliError("No existing job id/token found in the local ledger for this output or prompt.")
    if prior.get("out_key") and not getattr(args, "out_dir", None):
        args.out = str(prior["out_key"])
    submit_response = {"id": prior["job_id"], "token": prior["token"], "ledger_resume": True}
    paths = _run_submitted_job(
        args=args,
        base_url=base_url,
        api_key=api_key,
        submit_response=submit_response,
        expected_count=expected_count,
        output_format=output_format,
    )
    if paths:
        _append_operation_event(
            ledger_path,
            prior,
            "completed_from_resume",
            job_id=prior.get("job_id"),
            token=prior.get("token"),
            output_paths=[str(p) for p in paths],
        )
        args.resumed_operation_id = str(prior["operation_id"])
        args.resumed_output_paths = [str(path) for path in paths]
    return True

def _check_image_file(raw: str, *, label: str) -> Path:
    path = Path(raw)
    if not path.exists():
        _die(f"{label} file not found: {path}")
    if not path.is_file():
        _die(f"{label} path is not a file: {path}")
    if path.stat().st_size > MAX_IMAGE_BYTES:
        _die(f"{label} exceeds the 50MB upload limit: {path}")
    try:
        _validate_image_bytes(path.read_bytes())
    except DeliveryError as exc:
        _die(f"Invalid {label.lower()} {path}: {exc}")
    return path


def _check_image_paths(paths: Iterable[str]) -> List[Path]:
    out: List[Path] = []
    for raw in paths:
        out.append(_check_image_file(raw, label="Reference image"))
    if not out:
        _die("At least one --image is required for edit.")
    return out


def _maybe_parse_json_string(value: str) -> Optional[Any]:
    stripped = value.strip()
    if not stripped or stripped[:1] not in {"{", "["}:
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return None


def _find_first_key(obj: Any, keys: Sequence[str]) -> Optional[Any]:
    if isinstance(obj, str):
        parsed = _maybe_parse_json_string(obj)
        if parsed is not None:
            return _find_first_key(parsed, keys)
        return None
    if isinstance(obj, dict):
        lowered = {str(k).lower(): v for k, v in obj.items()}
        for key in keys:
            if key.lower() in lowered and lowered[key.lower()] is not None:
                return lowered[key.lower()]
        for value in obj.values():
            found = _find_first_key(value, keys)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = _find_first_key(value, keys)
            if found is not None:
                return found
    return None


def _extract_job_handle(response: Any) -> Tuple[str, str]:
    """Extract id/token from common background-job response shapes."""

    def walk(obj: Any) -> Optional[Tuple[str, str]]:
        if isinstance(obj, dict):
            id_value = None
            token_value = None
            for key in ("id", "job_id", "jobId"):
                if key in obj and obj[key] is not None:
                    id_value = obj[key]
                    break
            for key in ("token", "job_token", "jobToken"):
                if key in obj and obj[key] is not None:
                    token_value = obj[key]
                    break
            if id_value is not None and token_value is not None:
                return str(id_value), str(token_value)
            for value in obj.values():
                nested = walk(value)
                if nested is not None:
                    return nested
        elif isinstance(obj, list):
            for value in obj:
                nested = walk(value)
                if nested is not None:
                    return nested
        return None

    found = walk(response)
    if found is not None:
        return found

    id_value = _find_first_key(response, ("job_id", "jobId", "id"))
    token_value = _find_first_key(response, ("job_token", "jobToken", "token"))
    if id_value is not None and token_value is not None:
        return str(id_value), str(token_value)

    raise CliError(
        "Could not find job id/token in submit response. Response starts with: "
        + _redact_text(_json_dumps(response)[:2000])
    )


def _extract_status(obj: Any) -> Optional[str]:
    value = _find_first_key(obj, ("status", "state"))
    if value is None:
        return None
    return str(value).strip().lower()


def _extract_error_message(obj: Any) -> Optional[str]:
    value = _find_first_key(obj, ("error", "message", "detail", "reason"))
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return _json_dumps(value)
    return str(value)


def _looks_like_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _looks_like_data_uri(value: str) -> bool:
    return value.startswith("data:image/") and "," in value


def _looks_like_base64(value: str) -> bool:
    value = value.strip()
    if _looks_like_data_uri(value):
        return True
    if len(value) < 80:
        return False
    return re.fullmatch(r"[A-Za-z0-9+/=\r\n]+", value) is not None


def _image_item_from_string(value: str) -> Optional[Dict[str, str]]:
    if _looks_like_url(value):
        return {"url": value}
    if _looks_like_base64(value):
        return {"b64_json": value}
    return None


def _has_image_payload(item: Any) -> bool:
    if isinstance(item, dict):
        for key in ("b64_json", "b64", "base64", "image_base64", "url", "image_url"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return True
    if isinstance(item, str):
        return _image_item_from_string(item) is not None
    return False


def _normalise_image_item(item: Any) -> Optional[Dict[str, str]]:
    if isinstance(item, str):
        return _image_item_from_string(item)
    if not isinstance(item, dict):
        return None
    for key in ("b64_json", "b64", "base64", "image_base64"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return {"b64_json": value.strip()}
    for key in ("url", "image_url"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return {"url": value.strip()}
    return None


def _find_image_items(obj: Any) -> List[Dict[str, str]]:
    """Find image payloads in OpenAI-compatible or lightly wrapped responses."""
    if isinstance(obj, str):
        item = _image_item_from_string(obj)
        if item:
            return [item]
        parsed = _maybe_parse_json_string(obj)
        if parsed is not None:
            return _find_image_items(parsed)
        return []
    if isinstance(obj, dict):
        # OpenAI-compatible: {"data": [{"b64_json": ...}, {"url": ...}]}
        data_value = obj.get("data")
        if isinstance(data_value, list) and any(_has_image_payload(item) for item in data_value):
            items = [_normalise_image_item(item) for item in data_value]
            return [item for item in items if item is not None]

        # Common wrapper fields from background jobs.
        for key in ("result", "response", "output", "outputs", "payload", "body"):
            if key in obj:
                nested = _find_image_items(obj[key])
                if nested:
                    return nested

        # Other common image arrays.
        for key in ("images", "image", "files"):
            value = obj.get(key)
            if isinstance(value, list) and any(_has_image_payload(item) for item in value):
                items = [_normalise_image_item(item) for item in value]
                return [item for item in items if item is not None]
            if isinstance(value, str):
                item = _image_item_from_string(value)
                if item:
                    return [item]

        # Top-level single item.
        item = _normalise_image_item(obj)
        if item:
            return [item]

        # Recursive fallback. Prefer first subtree with image payloads.
        for value in obj.values():
            nested = _find_image_items(value)
            if nested:
                return nested
    elif isinstance(obj, list):
        if any(_has_image_payload(item) for item in obj):
            items = [_normalise_image_item(item) for item in obj]
            return [item for item in items if item is not None]
        for value in obj:
            nested = _find_image_items(value)
            if nested:
                return nested
    return []


def _poll_job(
    *,
    base_url: str,
    api_key: Optional[str],
    job_id: str,
    token: str,
    poll_interval: float,
    timeout_seconds: float,
    request_timeout: float,
    max_polls: Optional[int],
    quiet: bool,
    poll_retries: int = DEFAULT_POLL_RETRIES,
    retry_delay: float = DEFAULT_RETRY_DELAY,
    done_without_image_polls: int = DEFAULT_DONE_WITHOUT_IMAGE_POLLS,
) -> Any:
    started = time.time()
    poll_count = 0
    last_status: Optional[str] = None
    done_without_image_count = 0
    last_response: Any = None
    url = _jobs_poll_url(base_url, job_id, token)
    headers = {**_auth_headers(api_key), "X-Image-Job-Token": token}

    while True:
        poll_count += 1
        response = _http_json(
            method="GET",
            url=url,
            headers=headers,
            timeout=request_timeout,
            retries=poll_retries,
            retry_delay=retry_delay,
        )
        last_response = response
        status = _extract_status(response)
        image_items = _find_image_items(response)

        if status != last_status and not quiet:
            print(f"Job {job_id} status: {status or 'unknown'}", file=sys.stderr)
            last_status = status

        if status in FAILED_STATUSES:
            message = _extract_error_message(response) or _json_dumps(response)[:2000]
            raise JobFailedError(f"Job {job_id} failed with status {status}: {message}")

        if image_items:
            if status is None or status in DONE_STATUSES or status not in RUNNING_STATUSES:
                return response

        if status in DONE_STATUSES:
            done_without_image_count += 1
            if done_without_image_count >= max(1, done_without_image_polls):
                raise CliError(
                    f"Job {job_id} reached status {status} but no image payload was found after "
                    f"{done_without_image_count} poll(s). Response starts with: "
                    + _json_dumps(last_response)[:2000]
                )
            if not quiet:
                print(
                    f"Job {job_id} is {status} but image payload is not visible yet; "
                    f"polling again ({done_without_image_count}/{done_without_image_polls}).",
                    file=sys.stderr,
                )

        elapsed = time.time() - started
        if timeout_seconds >= 0 and elapsed >= timeout_seconds:
            raise CliError(
                f"Timed out while polling job {job_id} after {elapsed:.1f}s. "
                "Resume it by operation id from the local job ledger; do not submit again."
            )
        if max_polls is not None and poll_count >= max_polls:
            raise CliError(
                f"Reached --max-polls={max_polls} for job {job_id}. "
                "Resume it by operation id from the local job ledger; do not submit again."
            )
        time.sleep(max(0.1, poll_interval))


def _normalise_output_format(fmt: Optional[str]) -> str:
    if not fmt:
        return DEFAULT_OUTPUT_FORMAT
    value = fmt.strip().lower()
    if value == "jpg":
        return "jpeg"
    if value not in {"png", "jpeg", "webp", "gif"}:
        _warn(f"Unknown output format {fmt!r}; using it as a file extension only.")
    return value


def _validate_size(value: str) -> str:
    normalized = value.strip().lower()
    if normalized == "auto":
        return normalized
    match = re.fullmatch(r"(\d+)x(\d+)", normalized)
    if not match:
        raise CliError("--size must be auto or WIDTHxHEIGHT.")
    width, height = int(match.group(1)), int(match.group(2))
    pixels = width * height
    if max(width, height) > 3840:
        raise CliError("gpt-image-2 size edges cannot exceed 3840 pixels.")
    if width % 16 or height % 16:
        raise CliError("gpt-image-2 width and height must both be multiples of 16.")
    if max(width, height) > 3 * min(width, height):
        raise CliError("gpt-image-2 long-to-short edge ratio cannot exceed 3:1.")
    if not 655_360 <= pixels <= 8_294_400:
        raise CliError("gpt-image-2 total pixels must be between 655360 and 8294400.")
    return f"{width}x{height}"


def _extension_from_content_type(content_type: str) -> Optional[str]:
    lower = content_type.lower().split(";", 1)[0].strip()
    return {
        "image/png": "png",
        "image/jpeg": "jpeg",
        "image/jpg": "jpeg",
        "image/webp": "webp",
        "image/gif": "gif",
    }.get(lower)


def _extension_from_url(url: str) -> Optional[str]:
    suffix = Path(urlparse(url).path).suffix.lower().lstrip(".")
    if suffix == "jpg":
        return "jpeg"
    if suffix in {"png", "jpeg", "webp", "gif"}:
        return suffix
    return None


def _build_output_paths(
    *,
    out: str,
    out_dir: Optional[str],
    output_format: Optional[str],
    count: int,
) -> List[Path]:
    ext = "." + _normalise_output_format(output_format)
    if out_dir:
        directory = Path(out_dir)
        directory.mkdir(parents=True, exist_ok=True)
        return [directory / f"image_{i}{ext}" for i in range(1, count + 1)]

    out_path = Path(out)
    if out_path.suffix == "":
        out_path = out_path.with_suffix(ext)
    if count == 1:
        return [out_path]
    return [out_path.with_name(f"{out_path.stem}-{i}{out_path.suffix}") for i in range(1, count + 1)]


def _ensure_output_count(paths: List[Path], count: int, output_format: Optional[str]) -> List[Path]:
    if len(paths) >= count:
        return paths[:count]
    if not paths:
        return _build_output_paths(
            out=DEFAULT_OUTPUT_PATH,
            out_dir=None,
            output_format=output_format,
            count=count,
        )
    base = paths[-1]
    suffix = base.suffix or ("." + _normalise_output_format(output_format))
    expanded = list(paths)
    for idx in range(len(paths) + 1, count + 1):
        expanded.append(base.with_name(f"{base.stem}-{idx}{suffix}"))
    return expanded


def _decode_b64_image(value: str) -> bytes:
    raw = value.strip()
    if _looks_like_data_uri(raw):
        raw = raw.split(",", 1)[1]
    raw = re.sub(r"\s+", "", raw)
    padding = (-len(raw)) % 4
    raw = raw + ("=" * padding)
    try:
        return base64.b64decode(raw, validate=True)
    except Exception as exc:
        raise DeliveryError(f"Could not decode base64 image payload: {exc}") from exc


def _jpeg_size(data: bytes) -> Optional[Tuple[int, int]]:
    if len(data) < 4 or not data.startswith(b"\xff\xd8") or not data.endswith(b"\xff\xd9"):
        return None
    pos = 2
    sof_markers = {
        0xC0,
        0xC1,
        0xC2,
        0xC3,
        0xC5,
        0xC6,
        0xC7,
        0xC9,
        0xCA,
        0xCB,
        0xCD,
        0xCE,
        0xCF,
    }
    while pos + 4 <= len(data):
        if data[pos] != 0xFF:
            pos += 1
            continue
        while pos < len(data) and data[pos] == 0xFF:
            pos += 1
        if pos >= len(data):
            break
        marker = data[pos]
        pos += 1
        if marker in {0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
            continue
        if pos + 2 > len(data):
            return None
        length = struct.unpack(">H", data[pos : pos + 2])[0]
        if length < 2 or pos + length > len(data):
            return None
        if marker in sof_markers and length >= 7:
            height, width = struct.unpack(">HH", data[pos + 3 : pos + 7])
            return (width, height) if width > 0 and height > 0 else None
        pos += length
    return None


def _png_size(
    data: bytes, *, max_pixels: Optional[int] = None
) -> Optional[Tuple[int, int]]:
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    pos = 8
    size: Optional[Tuple[int, int]] = None
    header: Optional[Tuple[int, int, int]] = None
    idat = bytearray()
    saw_iend = False
    while pos + 12 <= len(data):
        length = struct.unpack(">I", data[pos : pos + 4])[0]
        chunk_type = data[pos + 4 : pos + 8]
        end = pos + 12 + length
        if end > len(data):
            return None
        payload = data[pos + 8 : pos + 8 + length]
        expected_crc = struct.unpack(">I", data[pos + 8 + length : end])[0]
        if zlib.crc32(chunk_type + payload) & 0xFFFFFFFF != expected_crc:
            return None
        if chunk_type == b"IHDR":
            if length != 13:
                return None
            width, height = struct.unpack(">II", payload[:8])
            if width < 1 or height < 1:
                return None
            if max_pixels is not None and width * height > max_pixels:
                raise DeliveryError(
                    f"Image dimensions {width}x{height} exceed the "
                    f"{max_pixels}-pixel safety limit."
                )
            size = (width, height)
            bit_depth, color_type, compression, filter_method, interlace = payload[8:13]
            if compression != 0 or filter_method != 0 or interlace not in {0, 1}:
                return None
            channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color_type)
            if channels is None:
                return None
            header = (bit_depth, channels, interlace)
        elif chunk_type == b"IDAT":
            idat.extend(payload)
        if chunk_type == b"IEND":
            saw_iend = True
            break
        pos = end
    if not (size and header and saw_iend and idat):
        return None
    bit_depth, channels, interlace = header
    bits_per_pixel = bit_depth * channels
    if interlace == 0:
        row_bytes = (size[0] * bits_per_pixel + 7) // 8
        expected_pixels = size[1] * (row_bytes + 1)
    else:
        expected_pixels = 0
        for x_start, y_start, x_step, y_step in (
            (0, 0, 8, 8),
            (4, 0, 8, 8),
            (0, 4, 4, 8),
            (2, 0, 4, 4),
            (0, 2, 2, 4),
            (1, 0, 2, 2),
            (0, 1, 1, 2),
        ):
            pass_width = (
                0
                if size[0] <= x_start
                else (size[0] - x_start + x_step - 1) // x_step
            )
            pass_height = (
                0
                if size[1] <= y_start
                else (size[1] - y_start + y_step - 1) // y_step
            )
            if pass_width and pass_height:
                pass_row_bytes = (pass_width * bits_per_pixel + 7) // 8
                expected_pixels += pass_height * (pass_row_bytes + 1)
    try:
        decompressor = zlib.decompressobj()
        pixels = decompressor.decompress(bytes(idat), expected_pixels + 1)
    except zlib.error:
        return None
    if (
        len(pixels) != expected_pixels
        or not decompressor.eof
        or decompressor.unconsumed_tail
        or decompressor.unused_data
    ):
        return None
    return size


def _webp_size(data: bytes) -> Optional[Tuple[int, int]]:
    if len(data) < 30 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return None
    if struct.unpack("<I", data[4:8])[0] + 8 > len(data):
        return None
    kind = data[12:16]
    if kind == b"VP8X" and len(data) >= 30:
        width = int.from_bytes(data[24:27], "little") + 1
        height = int.from_bytes(data[27:30], "little") + 1
        return width, height
    if kind == b"VP8L" and len(data) >= 25 and data[20] == 0x2F:
        bits = int.from_bytes(data[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if kind == b"VP8 " and len(data) >= 30 and data[23:26] == b"\x9d\x01\x2a":
        width, height = struct.unpack("<HH", data[26:30])
        return width & 0x3FFF, height & 0x3FFF
    return None


def _validate_image_bytes(data: bytes, expected_format: Optional[str] = None) -> Dict[str, Any]:
    if len(data) > MAX_DOWNLOAD_BYTES:
        raise DeliveryError(f"Image payload exceeds {MAX_DOWNLOAD_BYTES} bytes.")
    detected: Optional[str] = None
    size: Optional[Tuple[int, int]] = None
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        detected, size = "png", _png_size(data, max_pixels=MAX_IMAGE_PIXELS)
    elif data.startswith(b"\xff\xd8"):
        detected, size = "jpeg", _jpeg_size(data)
    elif data[:6] in {b"GIF87a", b"GIF89a"} and len(data) >= 14 and data.endswith(b";"):
        detected = "gif"
        width, height = struct.unpack("<HH", data[6:10])
        size = (width, height) if width > 0 and height > 0 else None
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        detected, size = "webp", _webp_size(data)
    if not detected or not size:
        raise DeliveryError("Completed response did not contain a complete, supported image payload.")
    if size[0] * size[1] > MAX_IMAGE_PIXELS:
        raise DeliveryError(
            f"Image dimensions {size[0]}x{size[1]} exceed the "
            f"{MAX_IMAGE_PIXELS}-pixel safety limit."
        )
    expected = _normalise_output_format(expected_format) if expected_format else None
    if expected and expected == "jpg":
        expected = "jpeg"
    if expected and detected != expected:
        raise DeliveryError(
            f"Image payload format is {detected}, but output format {expected} was requested."
        )
    try:
        from PIL import Image
    except ImportError:
        _verify_image_with_workspace_runtime(data, detected, size)
    else:
        try:
            with Image.open(io.BytesIO(data)) as image:
                image.verify()
            with Image.open(io.BytesIO(data)) as image:
                image.load()
                if tuple(image.size) != size:
                    raise ValueError("decoded dimensions differ from image header")
        except Exception as exc:
            raise DeliveryError(f"Image payload could not be completely decoded: {exc}") from exc
    return {"format": detected, "size": size, "bytes": len(data)}


def _workspace_python_candidates() -> List[Path]:
    candidates: List[Path] = []
    configured = os.getenv("CODEX_WORKSPACE_PYTHON")
    if configured:
        candidates.append(Path(configured))
    roots: List[Path] = []
    try:
        roots.append(Path.home())
    except RuntimeError:
        pass
    roots.extend(Path(sys.executable).resolve().parents)
    for root in roots:
        runtime_root = (
            root
            / ".cache"
            / "codex-runtimes"
            / "codex-primary-runtime"
            / "dependencies"
            / "python"
        )
        candidates.extend([runtime_root / "python.exe", runtime_root / "bin" / "python"])
    current = Path(sys.executable).resolve()
    unique: List[Path] = []
    seen = set()
    for path in candidates:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        key = str(resolved).lower()
        if key in seen or resolved == current or not resolved.is_file():
            continue
        seen.add(key)
        unique.append(resolved)
    return unique


def _verify_image_with_workspace_runtime(
    data: bytes, detected_format: str, expected_size: Tuple[int, int]
) -> None:
    helper_path = Path(__file__).with_name("raster_preflight.py")
    errors: List[str] = []
    with tempfile.TemporaryDirectory(prefix="ddw-image-verify-") as temp_dir:
        image_path = Path(temp_dir) / f"image.{detected_format}"
        image_path.write_bytes(data)
        for python_path in _workspace_python_candidates():
            result = subprocess.run(
                [str(python_path), str(helper_path), "probe", "--input", str(image_path)],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode != 0:
                errors.append(_redact_text(result.stderr.strip())[:500])
                continue
            try:
                summary = json.loads(result.stdout)
                decoded_size = tuple(int(value) for value in summary["size"])
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                errors.append(f"invalid verifier output: {exc}")
                continue
            if decoded_size != expected_size:
                raise DeliveryError("Decoded image dimensions differ from the image header.")
            return
    detail = "; ".join(item for item in errors if item)
    raise DeliveryError(
        "Image payload could not be completely decoded by a Pillow-capable workspace runtime."
        + (f" {detail}" if detail else "")
    )


def _preflight_output_paths(paths: Sequence[Path], *, force: bool) -> None:
    if force:
        return
    existing = [str(path) for path in paths if path.exists()]
    if existing:
        raise CliError("Output already exists; refusing a paid submit: " + ", ".join(existing))


def _write_bytes(path: Path, data: bytes, *, force: bool, quiet: bool = False) -> None:
    if path.exists() and not force:
        raise CliError(f"Output already exists: {path} (use --force to overwrite)")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temp_path.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
    if not quiet:
        print(f"Wrote {path}")


def _commit_image_outputs(
    prepared: Sequence[Tuple[Path, bytes]], *, force: bool, quiet: bool
) -> None:
    staged: List[Tuple[Path, Path]] = []
    claimed: List[Path] = []
    committed: List[Path] = []
    backups: List[Tuple[Path, Path]] = []
    succeeded = False
    try:
        for path, data in prepared:
            path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
            with temp_path.open("xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            staged.append((path, temp_path))

        for path, temp_path in staged:
            if force and path.exists():
                backup_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.bak")
                os.replace(path, backup_path)
                backups.append((path, backup_path))
            elif not force:
                try:
                    with path.open("xb"):
                        pass
                except FileExistsError as exc:
                    raise DeliveryError(f"Output already exists: {path}") from exc
                claimed.append(path)
            os.replace(temp_path, path)
            committed.append(path)

    except Exception as exc:
        for path in set(claimed + committed):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        restore_errors: List[str] = []
        for path, backup_path in reversed(backups):
            if backup_path.exists():
                try:
                    os.replace(backup_path, path)
                except OSError as restore_exc:
                    restore_errors.append(f"{path}: {restore_exc}")
        detail = f"; backup restore failed: {'; '.join(restore_errors)}" if restore_errors else ""
        if isinstance(exc, DeliveryError):
            raise
        raise DeliveryError(f"Could not atomically deliver image outputs: {exc}{detail}") from exc
    else:
        succeeded = True
    finally:
        for _, temp_path in staged:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass
        if succeeded:
            for _, backup_path in backups:
                try:
                    backup_path.unlink()
                except FileNotFoundError:
                    pass

    if not quiet:
        for path, _ in prepared:
            print(f"Wrote {path}")


def _write_image_items(
    items: List[Dict[str, str]],
    output_paths: List[Path],
    *,
    force: bool,
    request_timeout: float,
    output_format: Optional[str],
    expected_count: Optional[int] = None,
    quiet: bool = False,
) -> List[Path]:
    if not items:
        raise CliError("Completed response did not contain image payloads such as data[].b64_json or data[].url.")

    if expected_count is not None and len(items) != expected_count:
        raise DeliveryError(
            f"Completed response returned {len(items)} image(s), but {expected_count} were requested."
        )
    output_paths = _ensure_output_count(output_paths, len(items), output_format)
    _preflight_output_paths(output_paths, force=force)
    prepared: List[Tuple[Path, bytes]] = []

    for idx, item in enumerate(items):
        path = output_paths[idx]
        if "b64_json" in item:
            data = _decode_b64_image(item["b64_json"])
            _validate_image_bytes(data, output_format or path.suffix.lstrip("."))
            prepared.append((path, data))
            continue

        if "url" in item:
            data, content_type = _download_bytes(item["url"], timeout=request_timeout)
            # Keep the caller's path stable. If the caller gave no extension, add a likely one.
            if path.suffix == "":
                inferred = _extension_from_content_type(content_type) or _extension_from_url(item["url"]) or _normalise_output_format(output_format)
                path = path.with_suffix("." + inferred)
            _validate_image_bytes(data, output_format or path.suffix.lstrip("."))
            prepared.append((path, data))
            continue

        raise DeliveryError(f"Unsupported image item shape: {_json_dumps(item)[:1000]}")

    _commit_image_outputs(prepared, force=force, quiet=quiet)
    return [path for path, _ in prepared]


def _save_raw_response(path: Optional[str], response: Any, *, force: bool) -> None:
    if not path:
        return
    out = Path(path)
    if out.exists() and not force:
        raise CliError(f"Raw response output already exists: {out} (use --force to overwrite)")
    out.parent.mkdir(parents=True, exist_ok=True)
    def redact(value: Any) -> Any:
        if isinstance(value, dict):
            result: Dict[str, Any] = {}
            for key, item in value.items():
                if str(key).lower() in {
                    "token",
                    "job_token",
                    "jobtoken",
                    "api_key",
                    "authorization",
                }:
                    result[str(key)] = "<redacted>"
                else:
                    result[str(key)] = redact(item)
            return result
        if isinstance(value, list):
            return [redact(item) for item in value]
        if isinstance(value, str):
            text = re.sub(r"([?&]token=)[^&\s]+", r"\1<redacted>", value, flags=re.IGNORECASE)
            return re.sub(r"(Bearer\s+)[^\s,}\]]+", r"\1<redacted>", text, flags=re.IGNORECASE)
        return value

    out.write_text(_json_dumps(redact(response), indent=2), encoding="utf-8")
    print(f"Wrote raw response {out}")


def _print_handle(response: Any) -> Tuple[str, str]:
    job_id, token = _extract_job_handle(response)
    print(_json_dumps({"id": job_id, "token_saved_to_ledger": True}, indent=2))
    return job_id, token


def _run_submitted_job(
    *,
    args: argparse.Namespace,
    base_url: str,
    api_key: Optional[str],
    submit_response: Any,
    expected_count: int,
    output_format: Optional[str],
) -> List[Path]:
    job_id, token = _extract_job_handle(submit_response)
    if args.submit_only:
        operation_id = getattr(args, "current_operation_id", None)
        print(
            _json_dumps(
                {
                    "id": job_id,
                    "operation_id": operation_id,
                    "state": "submitted",
                    "recover_with": "recover --operation-id " + str(operation_id),
                },
                indent=2,
            )
        )
        return []

    if not args.quiet:
        print(f"Submitted job {job_id}; polling until complete.", file=sys.stderr)

    final_response = _poll_job(
        base_url=base_url,
        api_key=api_key if getattr(args, "poll_with_auth", False) else None,
        job_id=job_id,
        token=token,
        poll_interval=args.poll_interval,
        timeout_seconds=args.timeout_seconds,
        request_timeout=args.request_timeout,
        max_polls=args.max_polls,
        quiet=args.quiet,
        poll_retries=args.poll_retries,
        retry_delay=args.retry_delay,
        done_without_image_polls=args.done_without_image_polls,
    )
    _save_raw_response(args.raw_response_out, final_response, force=args.force)
    items = _find_image_items(final_response)
    output_paths = _build_output_paths(
        out=args.out,
        out_dir=args.out_dir,
        output_format=output_format,
        count=max(expected_count, len(items) or 1),
    )
    return _write_image_items(
        items,
        output_paths,
        force=args.force,
        request_timeout=args.request_timeout,
        output_format=output_format,
        expected_count=expected_count,
        quiet=args.quiet,
    )


def _generate(args: argparse.Namespace) -> None:
    prompt = _read_prompt(args.prompt, args.prompt_file)
    base_url = _normalize_base_url(args.base_url)
    api_key = _get_api_key(api_key_env=args.api_key_env, dry_run=args.dry_run)

    payload = _compact_payload(
        {
            "model": args.model,
            "prompt": prompt,
            "n": args.n,
            "quality": args.quality,
            "size": args.size,
            "background": args.background,
            "output_format": args.output_format,
            "output_compression": args.output_compression,
            "moderation": args.moderation,
        }
    )
    payload = _merge_payload_extras(
        payload,
        ("--extra-json", _read_extra_json(args.extra_json)),
        ("--param", _parse_param(args.param)),
    )

    out_key = _output_key_from_args(args)
    prompt_hash = _sha256_text(prompt)
    payload_hash = _sha256_json(payload)

    if args.dry_run:
        print(
            _json_dumps(
                {
                    "method": "POST",
                    "url": _jobs_submit_url(base_url, GENERATIONS_ENDPOINT),
                    "headers": {
                        "Authorization": "Bearer <redacted>" if api_key else "<not set>",
                        "User-Agent": os.getenv("DDW_IMAGE_USER_AGENT", DEFAULT_USER_AGENT),
                        "Accept": "application/json",
                        "X-Image-Job-Endpoint": GENERATIONS_ENDPOINT,
                        "Idempotency-Key": "<persisted-on-paid-submit>",
                        "X-Idempotency-Key": "<persisted-on-paid-submit>",
                        "X-Client-Request-Id": "<persisted-on-paid-submit>",
                        "Content-Type": "application/json",
                    },
                    "payload": payload,
                    "outputs": [
                        str(p)
                        for p in _build_output_paths(
                            out=args.out,
                            out_dir=args.out_dir,
                            output_format=args.output_format,
                            count=args.n,
                        )
                    ],
                },
                indent=2,
            )
        )
        return

    ledger_path = _job_ledger_path(args)
    expected_count = int(payload.get("n", 1))
    if _resume_existing_if_requested(
        args=args,
        ledger_path=ledger_path,
        endpoint=GENERATIONS_ENDPOINT,
        out_key=out_key,
        prompt_hash=prompt_hash,
        payload_hash=payload_hash,
        base_url=base_url,
        api_key=api_key,
        expected_count=expected_count,
        output_format=args.output_format,
    ):
        return

    _preflight_output_paths(
        _build_output_paths(
            out=args.out,
            out_dir=args.out_dir,
            output_format=args.output_format,
            count=expected_count,
        ),
        force=args.force,
    )

    operation = _prepare_operation(
        ledger_path=ledger_path,
        endpoint=GENERATIONS_ENDPOINT,
        out_key=out_key,
        prompt_hash=prompt_hash,
        payload_hash=payload_hash,
        metadata={
            "model": payload.get("model"),
            "size": payload.get("size"),
            "quality": payload.get("quality"),
            "n": payload.get("n"),
        },
        force_new_job=getattr(args, "force_new_job", False),
        supersede_operation=getattr(args, "supersede_operation", None),
    )
    _append_operation_event(ledger_path, operation, "submitting")
    args.current_operation_id = operation["operation_id"]
    try:
        submit_response = _submit_json_job(
            base_url=base_url,
            endpoint=GENERATIONS_ENDPOINT,
            api_key=api_key,
            payload=payload,
            request_timeout=args.request_timeout,
            operation=operation,
            retries=args.submit_retries,
            retry_delay=args.retry_delay,
        )
        job_id, token = _extract_job_handle(submit_response)
    except Exception as exc:
        _append_operation_event(
            ledger_path,
            operation,
            "submit_failed_unknown",
            error=_redact_text(str(exc))[:1000],
        )
        raise _ambiguous_submit_error(operation, exc) from exc
    _append_operation_event(
        ledger_path,
        operation,
        "submitted",
        job_id=job_id,
        token=token,
        model=payload.get("model"),
        size=payload.get("size"),
        quality=payload.get("quality"),
        n=payload.get("n"),
    )
    try:
        paths = _run_submitted_job(
            args=args,
            base_url=base_url,
            api_key=api_key,
            submit_response=submit_response,
            expected_count=expected_count,
            output_format=args.output_format,
        )
    except Exception as exc:
        event = (
            "failed"
            if isinstance(exc, JobFailedError)
            else "delivery_failed"
            if isinstance(exc, DeliveryError)
            else "poll_interrupted"
        )
        _append_operation_event(
            ledger_path,
            operation,
            event,
            job_id=job_id,
            token=token,
            error=_redact_text(str(exc))[:1000],
        )
        raise
    if paths:
        _append_operation_event(
            ledger_path,
            operation,
            "completed",
            job_id=job_id,
            token=token,
            output_paths=[str(p) for p in paths],
        )


def _edit(args: argparse.Namespace) -> None:
    prompt = _read_prompt(args.prompt, args.prompt_file)
    image_paths = _check_image_paths(args.image)
    mask_path = _check_image_file(args.mask, label="Mask") if args.mask else None

    base_url = _normalize_base_url(args.base_url)
    api_key = _get_api_key(api_key_env=args.api_key_env, dry_run=args.dry_run)

    payload = _compact_payload(
        {
            "model": args.model,
            "prompt": prompt,
            "n": args.n,
            "quality": args.quality,
            "size": args.size,
            "background": args.background,
            "output_format": args.output_format,
            "output_compression": args.output_compression,
            "input_fidelity": args.input_fidelity,
            "moderation": args.moderation,
        }
    )
    payload = _merge_payload_extras(
        payload,
        ("--extra-json", _read_extra_json(args.extra_json)),
        ("--param", _parse_param(args.param)),
    )

    out_key = _output_key_from_args(args)
    prompt_hash = _sha256_text(prompt)
    payload_hash = _edit_request_hash(payload, image_paths, mask_path)

    fields = [(key, _stringify_form_value(value)) for key, value in payload.items()]
    files: List[Tuple[str, Path]] = [(args.image_field, path) for path in image_paths]
    if mask_path:
        files.append((args.mask_field, mask_path))

    if args.dry_run:
        print(
            _json_dumps(
                {
                    "method": "POST",
                    "url": _jobs_submit_url(base_url, EDITS_ENDPOINT),
                    "headers": {
                        "Authorization": "Bearer <redacted>" if api_key else "<not set>",
                        "User-Agent": os.getenv("DDW_IMAGE_USER_AGENT", DEFAULT_USER_AGENT),
                        "Accept": "application/json",
                        "X-Image-Job-Endpoint": EDITS_ENDPOINT,
                        "Idempotency-Key": "<persisted-on-paid-submit>",
                        "X-Idempotency-Key": "<persisted-on-paid-submit>",
                        "X-Client-Request-Id": "<persisted-on-paid-submit>",
                        "Content-Type": "multipart/form-data; boundary=<generated>",
                    },
                    "fields": payload,
                    "files": [{"field": field, "path": str(path)} for field, path in files],
                    "outputs": [
                        str(p)
                        for p in _build_output_paths(
                            out=args.out,
                            out_dir=args.out_dir,
                            output_format=args.output_format,
                            count=args.n,
                        )
                    ],
                },
                indent=2,
            )
        )
        return

    ledger_path = _job_ledger_path(args)
    expected_count = int(payload.get("n", 1))
    if _resume_existing_if_requested(
        args=args,
        ledger_path=ledger_path,
        endpoint=EDITS_ENDPOINT,
        out_key=out_key,
        prompt_hash=prompt_hash,
        payload_hash=payload_hash,
        base_url=base_url,
        api_key=api_key,
        expected_count=expected_count,
        output_format=args.output_format,
    ):
        return

    _preflight_output_paths(
        _build_output_paths(
            out=args.out,
            out_dir=args.out_dir,
            output_format=args.output_format,
            count=expected_count,
        ),
        force=args.force,
    )

    operation = _prepare_operation(
        ledger_path=ledger_path,
        endpoint=EDITS_ENDPOINT,
        out_key=out_key,
        prompt_hash=prompt_hash,
        payload_hash=payload_hash,
        metadata={
            "model": payload.get("model"),
            "size": payload.get("size"),
            "quality": payload.get("quality"),
            "n": payload.get("n"),
            "images": [_safe_resolve(p) for p in image_paths],
        },
        force_new_job=getattr(args, "force_new_job", False),
        supersede_operation=getattr(args, "supersede_operation", None),
    )
    _append_operation_event(ledger_path, operation, "submitting")
    args.current_operation_id = operation["operation_id"]
    try:
        submit_response = _submit_multipart_job(
            base_url=base_url,
            endpoint=EDITS_ENDPOINT,
            api_key=api_key,
            fields=fields,
            files=files,
            request_timeout=args.request_timeout,
            operation=operation,
            retries=args.submit_retries,
            retry_delay=args.retry_delay,
        )
        job_id, token = _extract_job_handle(submit_response)
    except Exception as exc:
        _append_operation_event(
            ledger_path,
            operation,
            "submit_failed_unknown",
            error=_redact_text(str(exc))[:1000],
        )
        raise _ambiguous_submit_error(operation, exc) from exc
    _append_operation_event(
        ledger_path,
        operation,
        "submitted",
        job_id=job_id,
        token=token,
        model=payload.get("model"),
        size=payload.get("size"),
        quality=payload.get("quality"),
        n=payload.get("n"),
        images=[_safe_resolve(p) for p in image_paths],
    )
    try:
        paths = _run_submitted_job(
            args=args,
            base_url=base_url,
            api_key=api_key,
            submit_response=submit_response,
            expected_count=expected_count,
            output_format=args.output_format,
        )
    except Exception as exc:
        event = (
            "failed"
            if isinstance(exc, JobFailedError)
            else "delivery_failed"
            if isinstance(exc, DeliveryError)
            else "poll_interrupted"
        )
        _append_operation_event(
            ledger_path,
            operation,
            event,
            job_id=job_id,
            token=token,
            error=_redact_text(str(exc))[:1000],
        )
        raise
    if paths:
        _append_operation_event(
            ledger_path,
            operation,
            "completed",
            job_id=job_id,
            token=token,
            output_paths=[str(p) for p in paths],
        )


def _poll(args: argparse.Namespace) -> None:
    base_url = _normalize_base_url(args.base_url)
    api_key = _get_api_key(api_key_env=args.api_key_env, dry_run=False) if args.with_auth else None

    response = _poll_job(
        base_url=base_url,
        api_key=api_key,
        job_id=args.job_id,
        token=args.token,
        poll_interval=args.poll_interval,
        timeout_seconds=args.timeout_seconds,
        request_timeout=args.request_timeout,
        max_polls=args.max_polls,
        quiet=args.quiet,
        poll_retries=args.poll_retries,
        retry_delay=args.retry_delay,
        done_without_image_polls=args.done_without_image_polls,
    )
    _save_raw_response(args.raw_response_out, response, force=args.force)
    items = _find_image_items(response)
    output_paths = _build_output_paths(
        out=args.out,
        out_dir=args.out_dir,
        output_format=args.output_format,
        count=max(len(items), 1),
    )
    _write_image_items(
        items,
        output_paths,
        force=args.force,
        request_timeout=args.request_timeout,
        output_format=args.output_format,
        expected_count=len(items),
        quiet=args.quiet,
    )


def _find_operation(ledger_path: Optional[Path], operation_id: str) -> Dict[str, Any]:
    for operation in _operation_snapshots(ledger_path):
        if operation.get("operation_id") == operation_id:
            return operation
    raise CliError(f"Operation not found in the local ledger: {operation_id}")


def _inspect(args: argparse.Namespace) -> None:
    ledger_path = _job_ledger_path(args, prepare=False)
    operations = _operation_snapshots(ledger_path)
    operation_id = getattr(args, "operation_id", None)
    out = getattr(args, "out", None)
    if operation_id:
        operations = [item for item in operations if item.get("operation_id") == operation_id]
    if out:
        out_key = _safe_resolve(Path(out))
        operations = [item for item in operations if item.get("out_key") == out_key]
    if not operations:
        raise CliError("No matching operations found in the local job ledger.")
    print(_json_dumps([_sanitise_operation(item) for item in operations], indent=2))


def _recover(args: argparse.Namespace) -> None:
    ledger_path = _job_ledger_path(args)
    operation = _find_operation(ledger_path, args.operation_id)
    job_id = operation.get("job_id") or operation.get("id")
    token = operation.get("token")
    if not job_id or not token:
        state = operation.get("state") or "unknown"
        raise CliError(
            f"Operation {args.operation_id} is {state} and has no captured job handle. "
            "Recovery will not replay a paid POST. Inspect the provider records, then explicitly "
            "supersede this exact operation only if a new paid job is authorized."
        )

    base_url = _normalize_base_url(args.base_url)
    with_auth = bool(
        getattr(args, "with_auth", False) or getattr(args, "poll_with_auth", False)
    )
    api_key = _get_api_key(api_key_env=args.api_key_env, dry_run=False) if with_auth else None
    _append_operation_event(ledger_path, operation, "polling", job_id=job_id, token=token)
    try:
        final_response = _poll_job(
            base_url=base_url,
            api_key=api_key,
            job_id=str(job_id),
            token=str(token),
            poll_interval=args.poll_interval,
            timeout_seconds=args.timeout_seconds,
            request_timeout=args.request_timeout,
            max_polls=args.max_polls,
            quiet=args.quiet,
            poll_retries=args.poll_retries,
            retry_delay=args.retry_delay,
            done_without_image_polls=args.done_without_image_polls,
        )
    except Exception as exc:
        _append_operation_event(
            ledger_path,
            operation,
            "failed" if isinstance(exc, JobFailedError) else "poll_interrupted",
            job_id=job_id,
            token=token,
            error=_redact_text(str(exc))[:1000],
        )
        raise
    _save_raw_response(args.raw_response_out, final_response, force=args.force)
    items = _find_image_items(final_response)
    out = getattr(args, "out", None) or operation.get("out_key") or DEFAULT_OUTPUT_PATH
    output_paths = _build_output_paths(
        out=out,
        out_dir=getattr(args, "out_dir", None),
        output_format=args.output_format,
        count=max(int(operation.get("n") or 1), len(items) or 1),
    )
    try:
        paths = _write_image_items(
            items,
            output_paths,
            force=args.force,
            request_timeout=args.request_timeout,
            output_format=args.output_format,
            expected_count=int(operation.get("n") or 1),
            quiet=args.quiet,
        )
    except DeliveryError as exc:
        _append_operation_event(
            ledger_path,
            operation,
            "delivery_failed",
            job_id=job_id,
            token=token,
            error=_redact_text(str(exc))[:1000],
        )
        raise
    _append_operation_event(
        ledger_path,
        operation,
        "completed",
        job_id=job_id,
        token=token,
        output_paths=[str(path) for path in paths],
    )
    print(
        _json_dumps(
            {
                "operation_id": operation["operation_id"],
                "state": "completed",
                "output_paths": [str(path) for path in paths],
            },
            indent=2,
        )
    )


def _resolve(args: argparse.Namespace) -> None:
    ledger_path = _job_ledger_path(args)
    operation = _find_operation(ledger_path, args.operation_id)
    state = str(operation.get("state") or "unknown")
    if state not in {"prepared", "submitting", "ambiguous"}:
        raise CliError(
            f"Operation {args.operation_id} is {state}; only an unhandled submit can be resolved as not created."
        )
    evidence = str(args.evidence or "").strip()
    if len(evidence) < 12:
        raise CliError("--evidence must briefly identify the authoritative server log or provider record.")
    if not args.not_created:
        raise CliError("Resolution requires the explicit --not-created assertion.")
    _append_operation_event(
        ledger_path,
        operation,
        "resolved_not_created",
        evidence=evidence,
    )
    print(
        _json_dumps(
            {
                "operation_id": operation["operation_id"],
                "state": "not_created",
                "evidence": evidence,
            },
            indent=2,
        )
    )


def _normalise_job(job: Any, idx: int) -> Dict[str, Any]:
    if isinstance(job, str):
        prompt = job.strip()
        if not prompt:
            _die(f"Empty prompt at job {idx}")
        return {"prompt": prompt}
    if isinstance(job, dict):
        prompt = str(job.get("prompt", "")).strip()
        if not prompt:
            _die(f"Missing prompt for job {idx}")
        return dict(job, prompt=prompt)
    _die(f"Invalid job at line/index {idx}; expected string or JSON object.")
    return {}


def _choose_output_path(prompt: str, output_format: Optional[str]) -> Path:
    words = re.findall(r"[A-Za-z0-9]+", prompt.lower())[:5]
    stem = "-".join(words)[:48].strip("-") or "image"
    timestamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    suffix = "." + _normalise_output_format(output_format)
    directory = Path.cwd() / "output" / "ddw-imagegen"
    return (directory / f"{stem}-{timestamp}-{uuid.uuid4().hex[:8]}{suffix}").resolve()


def _prepare_reference_uploads(
    paths: Sequence[str], *, max_bytes: int, disabled: bool = False
) -> List[str]:
    checked = _check_image_paths(paths)
    if disabled or all(path.stat().st_size <= max_bytes for path in checked):
        return [str(path) for path in checked]
    helper_path = Path(__file__).with_name("raster_preflight.py")
    cache_dir = Path.cwd() / ".ddw-imagegen" / "uploads"
    spec = importlib.util.spec_from_file_location("ddw_raster_preflight", helper_path)
    if spec is None or spec.loader is None:
        raise CliError(f"Could not load reference preflight helper: {helper_path}")
    try:
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
    except SystemExit as exc:
        errors: List[str] = []
        for python_path in _workspace_python_candidates():
            prepared: List[str] = []
            for path in checked:
                if path.stat().st_size <= max_bytes:
                    prepared.append(str(path))
                    continue
                result = subprocess.run(
                    [
                        str(python_path),
                        str(helper_path),
                        "prepare-reference",
                        "--input",
                        str(path),
                        "--cache-dir",
                        str(cache_dir),
                        "--max-bytes",
                        str(max_bytes),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                if result.returncode != 0:
                    errors.append(_redact_text(result.stderr.strip())[:500])
                    prepared = []
                    break
                try:
                    summary = json.loads(result.stdout)
                    prepared.append(str(Path(summary["path"])))
                except (json.JSONDecodeError, KeyError, TypeError) as parse_exc:
                    errors.append(f"invalid helper output: {parse_exc}")
                    prepared = []
                    break
            if prepared:
                return prepared
        detail = "; ".join(item for item in errors if item) or str(exc)
        raise CliError(
            "A large reference could not be prepared automatically because no Pillow-capable "
            f"workspace Python runtime was available. {detail}"
        ) from exc
    return [
        str(helper.prepare_reference(path, cache_dir, max_bytes=max_bytes))
        for path in checked
    ]


def _create(args: argparse.Namespace) -> None:
    prompt = _read_prompt(args.prompt, args.prompt_file)
    if not args.output_format and args.out:
        suffix = Path(args.out).suffix.lower().lstrip(".")
        if suffix in {"png", "jpg", "jpeg", "webp"}:
            args.output_format = "jpeg" if suffix == "jpg" else suffix
    if not args.out and not args.out_dir:
        args.out = str(_choose_output_path(prompt, args.output_format))
    elif args.out:
        args.out = _safe_resolve(Path(args.out))
    if args.out_dir:
        args.out_dir = _safe_resolve(Path(args.out_dir))

    args.quiet = not bool(getattr(args, "verbose", False))
    args.resume_existing = True
    args.auto_resume = True
    args.force_new_job = False
    args.supersede_operation = None

    mode = "edit" if getattr(args, "image", None) else "generate"
    if mode == "edit":
        args.image = _prepare_reference_uploads(
            args.image,
            max_bytes=args.upload_threshold,
            disabled=args.no_auto_preflight,
        )
        _edit(args)
    else:
        _generate(args)

    if args.dry_run:
        return

    endpoint = EDITS_ENDPOINT if mode == "edit" else GENERATIONS_ENDPOINT
    ledger_path = _job_ledger_path(args)
    resumed_operation_id = getattr(args, "resumed_operation_id", None)
    if resumed_operation_id:
        operation = _find_operation(ledger_path, resumed_operation_id)
    else:
        operations = [
            operation
            for operation in _operation_snapshots(ledger_path)
            if operation.get("endpoint") == endpoint
            and operation.get("out_key") == _output_key_from_args(args)
        ]
        operation = operations[-1] if operations else {}
    output_paths = [Path(path).resolve() for path in operation.get("output_paths") or []]
    images: List[Dict[str, Any]] = []
    for path in output_paths:
        info = _validate_image_bytes(path.read_bytes())
        images.append(
            {
                "path": str(path),
                "format": info["format"],
                "width": info["size"][0],
                "height": info["size"][1],
                "bytes": info["bytes"],
            }
        )
    print(
        _json_dumps(
            {
                "mode": mode,
                "operation_id": operation.get("operation_id"),
                "state": operation.get("state"),
                "outputs": [str(path) for path in output_paths],
                "images": images,
            },
            indent=2,
        )
    )


def _read_jobs(path: str) -> List[Dict[str, Any]]:
    p = Path(path)
    if not p.exists():
        _die(f"Batch input not found: {p}")
    jobs: List[Dict[str, Any]] = []
    for line_no, raw in enumerate(p.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            value: Any = json.loads(line) if line.startswith("{") or line.startswith('"') else line
        except json.JSONDecodeError as exc:
            _die(f"Invalid JSONL at line {line_no}: {exc}")
        jobs.append(_normalise_job(value, line_no))
    if not jobs:
        _die("No jobs found in batch input.")
    if len(jobs) > MAX_BATCH_JOBS:
        _die(f"Too many jobs ({len(jobs)}). Max is {MAX_BATCH_JOBS}.")
    return jobs


def _job_payload_from_base(args: argparse.Namespace, job: Dict[str, Any]) -> Dict[str, Any]:
    payload = _compact_payload(
        {
            "model": job.get("model", args.model),
            "prompt": job["prompt"],
            "n": job.get("n", args.n),
            "quality": job.get("quality", args.quality),
            "size": job.get("size", args.size),
            "background": job.get("background", args.background),
            "output_format": job.get("output_format", args.output_format),
            "output_compression": job.get("output_compression", args.output_compression),
            "moderation": job.get("moderation", args.moderation),
        }
    )
    extra = job.get("extra")
    if isinstance(extra, dict):
        return _merge_payload_extras(payload, ("batch extra", extra))
    return _validate_payload(payload)


def _slugify(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "-", value)
    value = re.sub(r"-{2,}", "-", value).strip("-")
    return value[:60] or "image"


def _batch_job_out(base_out_dir: Path, idx: int, job: Dict[str, Any], output_format: Optional[str]) -> str:
    explicit = job.get("out")
    if explicit:
        return str(base_out_dir / Path(str(explicit)).name)
    ext = "." + _normalise_output_format(str(job.get("output_format") or output_format or DEFAULT_OUTPUT_FORMAT))
    return str(base_out_dir / f"{idx:03d}-{_slugify(str(job['prompt'])[:80])}{ext}")


def _generate_batch(args: argparse.Namespace) -> None:
    jobs = _read_jobs(args.input)
    base_url = _normalize_base_url(args.base_url)
    api_key = _get_api_key(api_key_env=args.api_key_env, dry_run=args.dry_run)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ledger_path = None if args.dry_run else _job_ledger_path(args)

    any_failed = False
    for idx, job in enumerate(jobs, start=1):
        payload = _job_payload_from_base(args, job)
        output_format = str(payload.get("output_format") or args.output_format or DEFAULT_OUTPUT_FORMAT)
        out_path = _batch_job_out(out_dir, idx, job, output_format)

        if args.dry_run:
            print(
                _json_dumps(
                    {
                        "job": idx,
                        "method": "POST",
                        "url": _jobs_submit_url(base_url, GENERATIONS_ENDPOINT),
                        "headers": {
                            "Idempotency-Key": "<persisted-on-paid-submit>",
                            "X-Client-Request-Id": "<persisted-on-paid-submit>",
                        },
                        "payload": payload,
                        "out": out_path,
                    },
                    indent=2,
                )
            )
            continue

        operation: Optional[Dict[str, Any]] = None
        phase = "pre_submit"
        try:
            operation = _prepare_operation(
                ledger_path=ledger_path,
                endpoint=GENERATIONS_ENDPOINT,
                out_key=_safe_resolve(Path(out_path)),
                prompt_hash=_sha256_text(str(payload["prompt"])),
                payload_hash=_sha256_json(payload),
                metadata={
                    "model": payload.get("model"),
                    "size": payload.get("size"),
                    "quality": payload.get("quality"),
                    "n": payload.get("n"),
                    "batch_index": idx,
                },
            )
            _append_operation_event(ledger_path, operation, "submitting")
            print(f"[job {idx}/{len(jobs)}] submitting", file=sys.stderr)
            phase = "submit"
            submit_response = _submit_json_job(
                base_url=base_url,
                endpoint=GENERATIONS_ENDPOINT,
                api_key=api_key,
                payload=payload,
                request_timeout=args.request_timeout,
                operation=operation,
                retries=args.submit_retries,
                retry_delay=args.retry_delay,
            )
            job_id, token = _extract_job_handle(submit_response)
            _append_operation_event(
                ledger_path,
                operation,
                "submitted",
                job_id=job_id,
                token=token,
            )
            print(f"[job {idx}/{len(jobs)}] submitted {job_id}", file=sys.stderr)
            if args.submit_only:
                print(
                    _json_dumps(
                        {
                            "index": idx,
                            "id": job_id,
                            "operation_id": operation["operation_id"],
                            "state": "submitted",
                        },
                        indent=2,
                    )
                )
                continue
            phase = "poll"
            final_response = _poll_job(
                base_url=base_url,
                api_key=api_key if getattr(args, "poll_with_auth", False) else None,
                job_id=job_id,
                token=token,
                poll_interval=args.poll_interval,
                timeout_seconds=args.timeout_seconds,
                request_timeout=args.request_timeout,
                max_polls=args.max_polls,
                quiet=args.quiet,
                poll_retries=args.poll_retries,
                retry_delay=args.retry_delay,
                done_without_image_polls=args.done_without_image_polls,
            )
            phase = "delivery"
            raw_path = None
            if args.raw_response_dir:
                raw_path = str(Path(args.raw_response_dir) / f"{idx:03d}-{job_id}.json")
            _save_raw_response(raw_path, final_response, force=args.force)
            items = _find_image_items(final_response)
            paths = _build_output_paths(
                out=out_path,
                out_dir=None,
                output_format=output_format,
                count=max(int(payload.get("n", 1)), len(items) or 1),
            )
            written = _write_image_items(
                items,
                paths,
                force=args.force,
                request_timeout=args.request_timeout,
                output_format=output_format,
                expected_count=int(payload.get("n", 1)),
                quiet=args.quiet,
            )
            _append_operation_event(
                ledger_path,
                operation,
                "completed",
                job_id=job_id,
                token=token,
                output_paths=[str(path) for path in written],
            )
        except Exception as exc:  # Continue batch unless fail-fast.
            if operation is not None:
                snapshots = _operation_snapshots(ledger_path)
                current = next(
                    (
                        item
                        for item in reversed(snapshots)
                        if item.get("operation_id") == operation.get("operation_id")
                    ),
                    None,
                )
                if isinstance(exc, JobFailedError):
                    _append_operation_event(
                        ledger_path,
                        operation,
                        "failed",
                        job_id=current.get("job_id") if current else None,
                        token=current.get("token") if current else None,
                        error=_redact_text(str(exc))[:1000],
                    )
                elif isinstance(exc, DeliveryError) or phase == "delivery":
                    _append_operation_event(
                        ledger_path,
                        operation,
                        "delivery_failed",
                        job_id=current.get("job_id") if current else None,
                        token=current.get("token") if current else None,
                        error=_redact_text(str(exc))[:1000],
                    )
                elif current and current.get("state") in {"prepared", "submitting"}:
                    _append_operation_event(
                        ledger_path,
                        operation,
                        "submit_failed_unknown",
                        error=_redact_text(str(exc))[:1000],
                    )
                elif current and current.get("state") in {"submitted", "polling", "delivery_failed"}:
                    _append_operation_event(
                        ledger_path,
                        operation,
                        "poll_interrupted",
                        job_id=current.get("job_id"),
                        token=current.get("token"),
                        error=_redact_text(str(exc))[:1000],
                    )
            any_failed = True
            print(f"[job {idx}/{len(jobs)}] failed: {exc}", file=sys.stderr)
            if args.fail_fast:
                raise

    if any_failed:
        raise SystemExit(1)


def _add_common_network_args(parser: argparse.ArgumentParser, *, integrated: bool = False) -> None:
    parser.add_argument("--base-url", help=f"Base API URL. Default env DDW_IMAGE_BASE_URL or {DEFAULT_BASE_URL}")
    parser.add_argument("--api-key-env", help="Read the DDW key from this explicit env var instead of DDW_IMAGE_API_KEY/DDW_API_KEY.")
    parser.add_argument("--request-timeout", type=float, default=DEFAULT_REQUEST_TIMEOUT, help="Per-request timeout in seconds.")
    parser.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL, help="Seconds between poll requests.")
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS, help="Overall polling timeout. Use -1 to disable.")
    parser.add_argument("--max-polls", type=int, help="Maximum poll attempts.")
    parser.add_argument("--poll-retries", type=int, default=DEFAULT_POLL_RETRIES, help="Retry transient poll GET errors this many times per poll request.")
    if integrated:
        parser.set_defaults(submit_retries=0, quiet=True)
    else:
        parser.add_argument("--submit-retries", type=int, default=DEFAULT_SUBMIT_RETRIES, help="Retry transient submit POST errors. Default 0 to avoid accidental duplicate paid jobs.")
    parser.add_argument("--retry-delay", type=float, default=DEFAULT_RETRY_DELAY, help="Base delay in seconds between HTTP retries.")
    parser.add_argument("--done-without-image-polls", type=int, default=DEFAULT_DONE_WITHOUT_IMAGE_POLLS, help="After a done/succeeded status with no visible image payload, poll this many times before failing.")
    parser.add_argument(
        "--poll-with-auth",
        action="store_true",
        help="Also send Authorization while polling submitted jobs. Default follows the user curl example: id/token only.",
    )
    if not integrated:
        parser.add_argument("--quiet", action="store_true", help="Reduce progress output.")


def _add_common_image_args(
    parser: argparse.ArgumentParser,
    *,
    default_out: Optional[str] = DEFAULT_OUTPUT_PATH,
    integrated: bool = False,
) -> None:
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--prompt")
    parser.add_argument("--prompt-file")
    parser.add_argument("--n", type=int, default=1)
    parser.add_argument("--quality", default=DEFAULT_QUALITY)
    parser.add_argument("--size")
    parser.add_argument("--background")
    parser.add_argument("--output-format", dest="output_format")
    parser.add_argument("--output-compression", dest="output_compression", type=int)
    parser.add_argument("--moderation")
    parser.add_argument("--extra-json", help="Merge extra request fields from a JSON object or @file.json.")
    parser.add_argument("--param", action="append", help="Add/override one request field as key=value. May be repeated.")
    parser.add_argument("--out", default=default_out)
    parser.add_argument("--out-dir")
    parser.add_argument("--raw-response-out")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--job-ledger", help=f"Local JSONL ledger for paid submit attempts. Default env DDW_IMAGE_JOB_LEDGER or {DEFAULT_JOB_LEDGER}")
    parser.add_argument(
        "--no-job-ledger",
        action="store_true",
        help="Disable ledger output during --dry-run only. Paid submits require a ledger.",
    )
    if integrated:
        parser.set_defaults(
            submit_only=False,
            resume_existing=False,
            force_new_job=False,
            supersede_operation=None,
        )
    else:
        parser.add_argument("--submit-only", action="store_true", help="Submit once and print a recoverable operation id without polling. This is a paid submit.")
        parser.add_argument("--resume-existing", action="store_true", help="Poll the latest matching job from the ledger instead of submitting a new paid job.")
        parser.add_argument(
            "--force-new-job",
            action="store_true",
            help="Create another paid operation only with the exact matching --supersede-operation id.",
        )
        parser.add_argument(
            "--supersede-operation",
            help="Exact operation id to supersede when --force-new-job is explicitly authorized.",
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate/edit images through a DDW-compatible background image job API."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    create_parser = subparsers.add_parser(
        "create",
        help="One-shot generation or editing: preflight, submit once, poll, validate, and save.",
    )
    _add_common_network_args(create_parser, integrated=True)
    _add_common_image_args(create_parser, default_out=None, integrated=True)
    create_parser.add_argument("--image", action="append", help="Reference image. Repeat for multiple images; presence selects edit mode.")
    create_parser.add_argument("--image-field", default="image[]")
    create_parser.add_argument("--mask")
    create_parser.add_argument("--mask-field", default="mask")
    create_parser.add_argument("--input-fidelity", dest="input_fidelity")
    create_parser.add_argument(
        "--upload-threshold",
        type=int,
        default=1_000_000,
        help="Automatically create a smaller transport copy for larger references.",
    )
    create_parser.add_argument(
        "--no-auto-preflight",
        action="store_true",
        help="Upload reference files unchanged.",
    )
    create_parser.add_argument("--verbose", action="store_true", help="Show internal job progress.")
    create_parser.set_defaults(func=_create)

    generate_parser = subparsers.add_parser("generate", help="Submit and poll an image generation job.")
    _add_common_network_args(generate_parser)
    _add_common_image_args(generate_parser)
    generate_parser.set_defaults(func=_generate)

    edit_parser = subparsers.add_parser("edit", help="Submit and poll an image edit job with one or more images.")
    _add_common_network_args(edit_parser)
    _add_common_image_args(edit_parser)
    edit_parser.add_argument("--image", action="append", required=True, help="Input image path. Repeat for multiple images.")
    edit_parser.add_argument("--image-field", default="image[]", help="Multipart image field name. Default: image[]")
    edit_parser.add_argument("--mask")
    edit_parser.add_argument("--mask-field", default="mask")
    edit_parser.add_argument("--input-fidelity", dest="input_fidelity")
    edit_parser.set_defaults(func=_edit)

    poll_parser = subparsers.add_parser("poll", help="Poll an existing job id/token and save the result image(s).")
    _add_common_network_args(poll_parser)
    poll_parser.add_argument("--job-id", required=True)
    poll_parser.add_argument("--token", required=True)
    poll_parser.add_argument("--with-auth", action="store_true", help="Also send Authorization while polling. The curl example does not require this.")
    poll_parser.add_argument("--out", default=DEFAULT_OUTPUT_PATH)
    poll_parser.add_argument("--out-dir")
    poll_parser.add_argument("--output-format", dest="output_format")
    poll_parser.add_argument("--raw-response-out")
    poll_parser.add_argument("--force", action="store_true")
    poll_parser.set_defaults(func=_poll)

    inspect_parser = subparsers.add_parser(
        "inspect", help="Inspect sanitized local operation states without network requests."
    )
    inspect_parser.add_argument("--job-ledger")
    inspect_parser.add_argument("--operation-id")
    inspect_parser.add_argument("--out", help="Filter by output path.")
    inspect_parser.set_defaults(func=_inspect, no_job_ledger=False)

    recover_parser = subparsers.add_parser(
        "recover", help="Poll a captured operation handle without creating a paid job."
    )
    _add_common_network_args(recover_parser)
    recover_parser.add_argument("--job-ledger")
    recover_parser.add_argument("--operation-id", required=True)
    recover_parser.add_argument("--out")
    recover_parser.add_argument("--out-dir")
    recover_parser.add_argument("--output-format", dest="output_format")
    recover_parser.add_argument("--raw-response-out")
    recover_parser.add_argument("--force", action="store_true")
    recover_parser.set_defaults(func=_recover, no_job_ledger=False)

    resolve_parser = subparsers.add_parser(
        "resolve",
        help="Resolve an ambiguous submit as not created using authoritative external evidence.",
    )
    resolve_parser.add_argument("--job-ledger")
    resolve_parser.add_argument("--operation-id", required=True)
    resolve_parser.add_argument(
        "--not-created",
        action="store_true",
        help="Assert that server/provider evidence proves no image job was created.",
    )
    resolve_parser.add_argument(
        "--evidence",
        required=True,
        help="Short reference to the server log, request id, or provider record supporting the resolution.",
    )
    resolve_parser.set_defaults(func=_resolve, no_job_ledger=False)

    batch_parser = subparsers.add_parser("generate-batch", help="Generate many prompts from a JSONL file, sequentially.")
    _add_common_network_args(batch_parser)
    batch_parser.add_argument("--input", required=True, help="JSONL: each line is a prompt string or an object with prompt/out/options.")
    batch_parser.add_argument("--out-dir", required=True)
    batch_parser.add_argument("--model", default=DEFAULT_MODEL)
    batch_parser.add_argument("--n", type=int, default=1)
    batch_parser.add_argument("--quality", default=DEFAULT_QUALITY)
    batch_parser.add_argument("--size")
    batch_parser.add_argument("--background")
    batch_parser.add_argument("--output-format", dest="output_format")
    batch_parser.add_argument("--output-compression", dest="output_compression", type=int)
    batch_parser.add_argument("--moderation")
    batch_parser.add_argument("--dry-run", action="store_true")
    batch_parser.add_argument("--submit-only", action="store_true")
    batch_parser.add_argument("--raw-response-dir")
    batch_parser.add_argument(
        "--job-ledger",
        default=DEFAULT_JOB_LEDGER,
        help=f"Local JSONL ledger for paid submit attempts. Default: {DEFAULT_JOB_LEDGER}",
    )
    batch_parser.add_argument("--force", action="store_true")
    batch_parser.add_argument("--fail-fast", action="store_true")
    batch_parser.set_defaults(func=_generate_batch, no_job_ledger=False)

    args = parser.parse_args()

    if hasattr(args, "n") and (args.n < 1 or args.n > 10):
        _die("--n must be between 1 and 10.")
    if getattr(args, "size", None) is not None and str(getattr(args, "model", "")).startswith(
        "gpt-image-2"
    ):
        try:
            args.size = _validate_size(str(args.size))
        except CliError as exc:
            _die(str(exc))
    quality = getattr(args, "quality", None)
    if quality is not None and str(quality).lower() not in {"low", "medium", "high", "auto"}:
        _die("--quality must be one of: low, medium, high, auto.")
    if getattr(args, "upload_threshold", 1) < 1:
        _die("--upload-threshold must be positive.")
    moderation = getattr(args, "moderation", None)
    if moderation is not None and str(moderation).lower() not in {"auto", "low"}:
        _die("--moderation must be one of: auto, low.")
    if getattr(args, "output_compression", None) is not None and not (0 <= args.output_compression <= 100):
        _die("--output-compression must be between 0 and 100.")
    if getattr(args, "output_compression", None) is not None:
        fmt = _normalise_output_format(getattr(args, "output_format", None))
        if fmt not in {"jpeg", "webp"}:
            _die("--output-compression requires --output-format jpeg or webp.")
    if getattr(args, "poll_interval", DEFAULT_POLL_INTERVAL) < 0:
        _die("--poll-interval must be >= 0.")
    if getattr(args, "request_timeout", DEFAULT_REQUEST_TIMEOUT) <= 0:
        _die("--request-timeout must be > 0.")
    if getattr(args, "max_polls", None) is not None and args.max_polls < 1:
        _die("--max-polls must be >= 1.")
    if getattr(args, "poll_retries", DEFAULT_POLL_RETRIES) < 0:
        _die("--poll-retries must be >= 0.")
    if getattr(args, "submit_retries", DEFAULT_SUBMIT_RETRIES) < 0:
        _die("--submit-retries must be >= 0.")
    if getattr(args, "submit_retries", DEFAULT_SUBMIT_RETRIES) != 0:
        _die("Paid submit retries are disabled because a lost response can create duplicate charges.")
    if getattr(args, "retry_delay", DEFAULT_RETRY_DELAY) < 0:
        _die("--retry-delay must be >= 0.")
    if getattr(args, "done_without_image_polls", DEFAULT_DONE_WITHOUT_IMAGE_POLLS) < 1:
        _die("--done-without-image-polls must be >= 1.")
    if getattr(args, "resume_existing", False) and getattr(args, "force_new_job", False):
        _die("Use --resume-existing or --force-new-job, not both.")
    if getattr(args, "supersede_operation", None) and not getattr(args, "force_new_job", False):
        _die("--supersede-operation requires --force-new-job.")
    if getattr(args, "no_job_ledger", False) and not getattr(args, "dry_run", False):
        _die("Paid submits require the local job ledger; --no-job-ledger is allowed only with --dry-run.")

    try:
        args.func(args)
    except CliError as exc:
        _die(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
