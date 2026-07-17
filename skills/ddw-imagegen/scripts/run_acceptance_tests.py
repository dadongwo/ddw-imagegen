#!/usr/bin/env python3
"""Offline workflow acceptance plus a small sequential live test for ddw-imagegen.

Dry-run covers generation, edit, variants, composite, transparent preflight, and
project delivery without paid submits. Live mode intentionally runs only:
- generation at each requested size through the integrated create command
- one edit through the same integrated create command

It never prints the API key. Configure DDW_IMAGE_API_KEY or DDW_API_KEY.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
from pathlib import Path
import re
import struct
import subprocess
import sys
import time
import zlib
from urllib.parse import urlparse
from typing import Iterable, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parent
CLI = ROOT / "ddw_image_gen.py"
CHROMA = ROOT / "remove_chroma_key.py"
DEFAULT_SIZES = ("1024x1024", "2048x2048", "3840x2160")
KEY_ENVS = ("DDW_IMAGE_API_KEY", "DDW_API_KEY")


def _die(message: str, code: int = 1) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(code)


def _warn(message: str) -> None:
    print(f"WARNING: {message}", file=sys.stderr)


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def _redact_sensitive(text: str) -> str:
    # Keep logs useful while avoiding persisted job tokens or bearer values.
    text = re.sub(r'(Authorization\s*[:=]\s*Bearer\s+)[^\s,}]+', r'\1<redacted>', text, flags=re.IGNORECASE)
    text = re.sub(r'("token"\s*:\s*")[^"]+(")', r'\1<redacted>\2', text, flags=re.IGNORECASE)
    text = re.sub(r'("job_token"\s*:\s*")[^"]+(")', r'\1<redacted>\2', text, flags=re.IGNORECASE)
    text = re.sub(r'([?&]token=)[^\s&"\']+', r'\1<redacted>', text, flags=re.IGNORECASE)
    text = re.sub(r'sk-[A-Za-z0-9._-]{12,}', 'sk-<redacted>', text)
    return text



def _network_preflight(base_url: str) -> None:
    parsed = urlparse(base_url)
    host = parsed.hostname
    if not host:
        _die(f"Invalid base URL for network preflight: {base_url}")
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
    except OSError as exc:
        _die(
            f"Network/DNS preflight failed for {host}: {exc}. "
            "This environment cannot reach the API, so live paid tests were not started."
        )
    if not infos:
        _die(f"Network/DNS preflight returned no addresses for {host}.")
    print(f"Network preflight OK: {host} resolved to {infos[0][4][0]}")

def _parse_size(value: str) -> Tuple[int, int]:
    match = re.fullmatch(r"(\d+)x(\d+)", value.strip().lower())
    if not match:
        _die(f"Invalid size {value!r}; expected WIDTHxHEIGHT, for example 1024x1024.")
    return int(match.group(1)), int(match.group(2))


def _label_for_size(size: str) -> str:
    width, height = _parse_size(size)
    if width == height and width % 1024 == 0:
        return f"{width // 1024}k"
    return f"{width}x{height}"


def _split_sizes(value: str) -> List[str]:
    sizes = [part.strip().lower() for part in value.split(",") if part.strip()]
    if not sizes:
        _die("At least one size is required.")
    for size in sizes:
        _parse_size(size)
    return sizes


def _has_key(env: dict) -> bool:
    return any(env.get(name) for name in KEY_ENVS)


def _pillow_python_candidates() -> List[Path]:
    candidates = [Path(sys.executable).resolve()]
    roots = [Path.home(), *Path(sys.executable).resolve().parents]
    for root in roots:
        runtime_root = root / ".cache" / "codex-runtimes" / "codex-primary-runtime" / "dependencies" / "python"
        candidates.extend((runtime_root / "python.exe", runtime_root / "bin" / "python"))

    unique: List[Path] = []
    seen = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        key = str(resolved).lower()
        if key in seen:
            continue
        seen.add(key)
        if resolved == Path(sys.executable).resolve() or resolved.is_file():
            unique.append(resolved)
    return unique


def _select_pillow_python() -> Path:
    for executable in _pillow_python_candidates():
        result = subprocess.run(
            [str(executable), "-c", "from PIL import Image"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            return executable
    _die("No Pillow-capable Python runtime was found for transparent-output acceptance.")


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def _write_reference_png(path: Path, width: int = 512, height: int = 512) -> None:
    """Write a small reference PNG using only the Python standard library."""
    rows = []
    for y in range(height):
        row = bytearray()
        for x in range(width):
            r, g, b = 245, 245, 245
            # black border rectangle
            if 78 <= x <= 434 and 78 <= y <= 434 and (x <= 88 or x >= 424 or y <= 88 or y >= 424):
                r, g, b = 25, 25, 25
            # blue circle
            cx, cy, radius = 256, 226, 88
            if (x - cx) ** 2 + (y - cy) ** 2 <= radius ** 2:
                r, g, b = 70, 145, 240
            # darker circle outline
            if radius ** 2 - 1200 <= (x - cx) ** 2 + (y - cy) ** 2 <= radius ** 2 + 300:
                r, g, b = 20, 60, 130
            # orange base
            if 165 <= x <= 347 and 332 <= y <= 388:
                r, g, b = 240, 140, 60
            row.extend((r, g, b))
        rows.append(b"\x00" + bytes(row))  # filter byte + RGB row

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    data = b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", ihdr) + _png_chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + _png_chunk(b"IEND", b"")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _probe_png(data: bytes) -> Optional[Tuple[int, int]]:
    if len(data) >= 24 and data.startswith(b"\x89PNG\r\n\x1a\n") and data[12:16] == b"IHDR":
        return struct.unpack(">II", data[16:24])
    return None


def _probe_jpeg(data: bytes) -> Optional[Tuple[int, int]]:
    if not data.startswith(b"\xff\xd8"):
        return None
    pos = 2
    while pos + 9 < len(data):
        if data[pos] != 0xFF:
            pos += 1
            continue
        marker = data[pos + 1]
        pos += 2
        while marker == 0xFF and pos < len(data):
            marker = data[pos]
            pos += 1
        if marker in {0xD8, 0xD9}:
            continue
        if pos + 2 > len(data):
            return None
        length = struct.unpack(">H", data[pos : pos + 2])[0]
        if length < 2 or pos + length > len(data):
            return None
        if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
            if length >= 7:
                height = struct.unpack(">H", data[pos + 3 : pos + 5])[0]
                width = struct.unpack(">H", data[pos + 5 : pos + 7])[0]
                return width, height
            return None
        pos += length
    return None


def _probe_webp(data: bytes) -> Optional[Tuple[int, int]]:
    if len(data) < 30 or not (data[:4] == b"RIFF" and data[8:12] == b"WEBP"):
        return None
    kind = data[12:16]
    if kind == b"VP8X" and len(data) >= 30:
        width = 1 + int.from_bytes(data[24:27], "little")
        height = 1 + int.from_bytes(data[27:30], "little")
        return width, height
    if kind == b"VP8 " and len(data) >= 30:
        start = data.find(b"\x9d\x01\x2a", 20)
        if start != -1 and start + 7 <= len(data):
            width = int.from_bytes(data[start + 3 : start + 5], "little") & 0x3FFF
            height = int.from_bytes(data[start + 5 : start + 7], "little") & 0x3FFF
            return width, height
    if kind == b"VP8L" and len(data) >= 25 and data[20] == 0x2F:
        bits = int.from_bytes(data[21:25], "little")
        width = 1 + (bits & 0x3FFF)
        height = 1 + ((bits >> 14) & 0x3FFF)
        return width, height
    return None


def _probe_dimensions(path: Path) -> Optional[Tuple[int, int]]:
    data = path.read_bytes()[:4096]
    return _probe_png(data) or _probe_jpeg(data) or _probe_webp(data)


def _run(name: str, cmd: Sequence[str], *, env: dict, log_path: Path) -> subprocess.CompletedProcess:
    print(f"\n=== {name} ===")
    printable = ["python" if part == sys.executable else part for part in cmd]
    print("$ " + " ".join(printable))
    started = time.time()
    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
        cwd=str(ROOT.parent),
    )
    elapsed = time.time() - started
    log_path.parent.mkdir(parents=True, exist_ok=True)
    safe_stdout = _redact_sensitive(result.stdout or "")
    log_path.write_text(safe_stdout, encoding="utf-8")
    print(safe_stdout[-3000:] if safe_stdout else "")
    print(f"[{name}] exit={result.returncode} elapsed={elapsed:.1f}s log={log_path}")
    if result.returncode != 0:
        _die(f"{name} failed. See log: {log_path}")
    return result


def _validate_image(path: Path, expected_size: Optional[str], *, strict_dimensions: bool) -> dict:
    if not path.exists():
        _die(f"Expected output file was not created: {path}")
    size_bytes = path.stat().st_size
    if size_bytes < 100:
        _die(f"Output file is suspiciously small: {path} ({size_bytes} bytes)")
    dims = _probe_dimensions(path)
    if dims is None:
        _warn(f"Could not determine image dimensions for {path}; file size is {size_bytes} bytes.")
    elif expected_size:
        expected = _parse_size(expected_size)
        if strict_dimensions and dims != expected:
            _die(f"Dimension mismatch for {path}: got {dims[0]}x{dims[1]}, expected {expected[0]}x{expected[1]}")
        if dims != expected:
            _warn(f"Dimension differs for {path}: got {dims[0]}x{dims[1]}, requested {expected[0]}x{expected[1]}")
    return {"path": str(path), "bytes": size_bytes, "dimensions": list(dims) if dims else None}


def _run_project_delivery_contract(out_dir: Path, source_image: Path, logs_dir: Path) -> None:
    print("\n=== project delivery contract ===")
    project_root = out_dir / "project-contract"
    assets_dir = project_root / "src" / "assets"
    consumer = project_root / "src" / "config.ts"
    old_asset = assets_dir / "old-hero.png"
    new_asset = assets_dir / "future-hero-v1.png"
    assets_dir.mkdir(parents=True, exist_ok=True)

    old_asset.write_bytes(source_image.read_bytes())
    consumer.write_text('export const hero = "assets/old-hero.png";\n', encoding="utf-8")

    staged_asset = new_asset.with_suffix(new_asset.suffix + ".tmp")
    staged_asset.write_bytes(source_image.read_bytes())
    os.replace(staged_asset, new_asset)

    updated = consumer.read_text(encoding="utf-8").replace(
        "assets/old-hero.png", "assets/future-hero-v1.png"
    )
    staged_consumer = consumer.with_suffix(consumer.suffix + ".tmp")
    staged_consumer.write_text(updated, encoding="utf-8")
    os.replace(staged_consumer, consumer)

    consumer_text = consumer.read_text(encoding="utf-8")
    if not new_asset.is_file() or "assets/future-hero-v1.png" not in consumer_text:
        _die("Project delivery contract failed to persist and reference the new asset.")
    if "assets/old-hero.png" in consumer_text or not old_asset.is_file():
        _die("Project delivery contract did not preserve the old asset or remove its active reference.")

    evidence = {
        "asset": str(new_asset.resolve()),
        "consumer": str(consumer.resolve()),
        "consumer_reference_verified": True,
        "old_asset_preserved": True,
    }
    log_path = logs_dir / "project_delivery_contract.json"
    log_path.write_text(_json(evidence), encoding="utf-8")
    print(_json(evidence))
    print(f"[project delivery contract] PASS log={log_path}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a small sequential live acceptance test against the DDW image job API.")
    parser.add_argument("--base-url", default=os.getenv("DDW_IMAGE_BASE_URL", "https://api.ddwapi.dpdns.org"))
    parser.add_argument("--model", default="gpt-image-2")
    parser.add_argument("--quality", default="high")
    parser.add_argument("--sizes", default=",".join(DEFAULT_SIZES), help="Comma-separated generation sizes. Default: 1024x1024,2048x2048,3840x2160")
    parser.add_argument("--edit-size", default="1024x1024")
    parser.add_argument("--out-dir", default="output/ddw-imagegen-acceptance")
    parser.add_argument("--poll-interval", type=float, default=3.0)
    parser.add_argument("--timeout-seconds", type=float, default=900.0)
    parser.add_argument("--request-timeout", type=float, default=300.0)
    parser.add_argument("--strict-dimensions", action="store_true", help="Fail when the returned image dimensions do not exactly match requested size.")
    parser.add_argument("--skip-edit", action="store_true")
    parser.add_argument("--yes", action="store_true", help="Required for live paid tests.")
    parser.add_argument("--dry-run", action="store_true", help="Validate commands without submitting paid jobs.")
    parser.add_argument("--skip-preflight", action="store_true", help="Skip DNS preflight before live tests.")
    args = parser.parse_args()

    if not CLI.exists():
        _die(f"CLI not found: {CLI}")

    sizes = _split_sizes(args.sizes)
    _parse_size(args.edit_size)
    env = os.environ.copy()
    env["DDW_IMAGE_BASE_URL"] = args.base_url

    if not args.dry_run and not _has_key(env):
        _die(f"No API key found. Export one of: {', '.join(KEY_ENVS)}")
    if not args.dry_run and not args.yes:
        _die("This runs live image jobs and may spend credits. Re-run with --yes to confirm.")
    if not args.dry_run and not args.skip_preflight:
        _network_preflight(args.base_url)

    out_dir = Path(args.out_dir).resolve()
    logs_dir = out_dir / "logs"
    raw_dir = out_dir / "raw"
    out_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)

    summary = {
        "base_url": args.base_url,
        "model": args.model,
        "quality": args.quality,
        "sizes": sizes,
        "edit_size": None if args.skip_edit else args.edit_size,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "outputs": [],
        "offline_cases": [
            "generation",
            "edit",
            "variants",
            "composite",
            "transparent_preflight",
            "project_delivery",
            "failure_and_delivery_contracts",
        ],
    }

    common = [
        "--base-url", args.base_url,
        "--model", args.model,
        "--quality", args.quality,
        "--n", "1",
        "--poll-interval", str(args.poll_interval),
        "--timeout-seconds", str(args.timeout_seconds),
        "--request-timeout", str(args.request_timeout),
        "--poll-retries", "2",
        "--retry-delay", "1",
        "--done-without-image-polls", "10",
        "--force",
        "--job-ledger", str(out_dir / "jobs.jsonl"),
    ]

    # Dry-run all commands first; this is free and catches payload/field mistakes.
    first_size = sizes[0]
    dry_gen = [
        sys.executable, str(CLI), "create",
        "--prompt", f"DDW acceptance dry-run generation {first_size}: clean geometric icon, no text",
        "--size", first_size,
        "--out", str(out_dir / f"dry_gen_{_label_for_size(first_size)}.png"),
        "--dry-run",
        *common,
    ]
    _run("dry-run generation", dry_gen, env=env, log_path=logs_dir / "dry_gen.log")

    ref = out_dir / "reference.png"
    _write_reference_png(ref)
    _validate_image(ref, "512x512", strict_dimensions=True)

    dry_edit = [
        sys.executable, str(CLI), "create",
        "--prompt", "DDW acceptance dry-run edit: polish the supplied reference into a clean app icon, no text",
        "--image", str(ref),
        "--size", args.edit_size,
        "--out", str(out_dir / f"dry_edit_{_label_for_size(args.edit_size)}.png"),
        "--dry-run",
        *common,
    ]
    _run("dry-run edit", dry_edit, env=env, log_path=logs_dir / "dry_edit.log")

    dry_variants = [
        sys.executable, str(CLI), "create",
        "--prompt", "DDW acceptance dry-run variants: three distinct geometric icon alternatives, no text",
        "--size", first_size,
        "--out", str(out_dir / "dry_variants.png"),
        "--dry-run",
        *common,
        "--n", "3",
    ]
    _run("dry-run variants", dry_variants, env=env, log_path=logs_dir / "dry_variants.log")

    second_ref = out_dir / "reference_scene.png"
    _write_reference_png(second_ref, width=640, height=384)
    dry_composite = [
        sys.executable, str(CLI), "create",
        "--prompt", "DDW acceptance dry-run composite: use Image 1 as the edit target and Image 2 as the scene reference",
        "--image", str(ref),
        "--image", str(second_ref),
        "--size", args.edit_size,
        "--out", str(out_dir / "dry_composite.png"),
        "--dry-run",
        *common,
    ]
    _run("dry-run composite", dry_composite, env=env, log_path=logs_dir / "dry_composite.log")

    if not CHROMA.exists():
        _die(f"Chroma-key helper not found: {CHROMA}")
    _run(
        "transparent preflight",
        [str(_select_pillow_python()), str(CHROMA), "--check"],
        env=env,
        log_path=logs_dir / "transparent_preflight.log",
    )

    project_output = out_dir / "project-assets" / "ddw-acceptance-icon.png"
    dry_project = [
        sys.executable, str(CLI), "create",
        "--prompt", "DDW acceptance project delivery: clean geometric project icon, no text",
        "--size", first_size,
        "--out", str(project_output),
        "--dry-run",
        *common,
    ]
    _run("project delivery", dry_project, env=env, log_path=logs_dir / "project_delivery.log")
    _run_project_delivery_contract(out_dir, ref, logs_dir)

    contract_tests = [
        "tests.test_integrated_workflow.CredentialAndTransportTests.test_generic_api_key_is_not_used_implicitly",
        "tests.test_integrated_workflow.DurableStateTests.test_terminal_failure_blocks_an_automatic_second_paid_operation",
        "tests.test_integrated_workflow.DurableStateTests.test_resume_completion_updates_the_operation_that_owned_the_handle",
        "tests.test_integrated_workflow.DeliveryValidationTests.test_non_image_bytes_are_rejected",
        "tests.test_integrated_workflow.DeliveryValidationTests.test_existing_output_is_rejected_before_submit",
        "tests.test_integrated_workflow.IntegratedCreateCommandTests.test_create_one_shot_submits_once_and_returns_only_deliverables",
        "tests.test_ddw_image_gen.SubmitIntegrationTests.test_invalid_mask_is_rejected_before_paid_submit",
        "tests.test_ddw_image_gen.SubmitIntegrationTests.test_batch_terminal_provider_failure_is_terminal_and_clears_token",
        "tests.test_ddw_image_gen.SubmitIntegrationTests.test_batch_delivery_failure_remains_recoverable",
    ]
    _run(
        "failure and delivery contracts",
        [sys.executable, "-B", "-m", "unittest", "-v", *contract_tests],
        env=env,
        log_path=logs_dir / "failure_and_delivery_contracts.log",
    )

    if args.dry_run:
        summary["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        summary_path = out_dir / "summary.json"
        summary_path.write_text(_json(summary), encoding="utf-8")
        print(f"\nDRY-RUN PASS. Summary: {summary_path}")
        return 0

    # Use the same one-shot command that the Skill uses in normal work.
    one_label = _label_for_size(first_size)
    submit_cmd = [
        sys.executable, str(CLI), "create",
        "--prompt", f"DDW acceptance generation {first_size}: premium geometric app icon, plain background, no text",
        "--size", first_size,
        "--out", str(out_dir / f"gen_{one_label}.png"),
        "--raw-response-out", str(raw_dir / f"gen_{one_label}.json"),
        *common,
    ]
    _run(f"generation {one_label}", submit_cmd, env=env, log_path=logs_dir / f"gen_{one_label}.log")
    summary["outputs"].append({"case": f"generation_{one_label}", **_validate_image(out_dir / f"gen_{one_label}.png", first_size, strict_dimensions=args.strict_dimensions)})

    for size in sizes[1:]:
        label = _label_for_size(size)
        cmd = [
            sys.executable, str(CLI), "create",
            "--prompt", f"DDW acceptance generation {size}: premium geometric app icon, plain background, no text",
            "--size", size,
            "--out", str(out_dir / f"gen_{label}.png"),
            "--raw-response-out", str(raw_dir / f"gen_{label}.json"),
            *common,
        ]
        _run(f"generation {label}", cmd, env=env, log_path=logs_dir / f"gen_{label}.log")
        summary["outputs"].append({"case": f"generation_{label}", **_validate_image(out_dir / f"gen_{label}.png", size, strict_dimensions=args.strict_dimensions)})

    if not args.skip_edit:
        edit_label = _label_for_size(args.edit_size)
        cmd = [
            sys.executable, str(CLI), "create",
            "--prompt", "DDW acceptance edit: transform the reference into a premium app icon; keep the blue circle and orange base; plain background; no text",
            "--image", str(ref),
            "--size", args.edit_size,
            "--out", str(out_dir / f"edit_{edit_label}.png"),
            "--raw-response-out", str(raw_dir / f"edit_{edit_label}.json"),
            *common,
        ]
        _run(f"edit {edit_label}", cmd, env=env, log_path=logs_dir / f"edit_{edit_label}.log")
        summary["outputs"].append({"case": f"edit_{edit_label}", **_validate_image(out_dir / f"edit_{edit_label}.png", args.edit_size, strict_dimensions=args.strict_dimensions)})

    summary["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    summary_path = out_dir / "summary.json"
    summary_path.write_text(_json(summary), encoding="utf-8")
    print("\nACCEPTANCE PASS")
    print(_json(summary))
    print(f"Summary written to: {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
