#!/usr/bin/env python3
"""Prepare reference uploads and normalize generated raster dimensions."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
from typing import Any, Dict, Tuple
import uuid

try:
    from PIL import Image, ImageOps
except ImportError as exc:  # pragma: no cover - exercised by the runtime check.
    raise SystemExit(
        "Pillow is required. In Codex, run this script with the bundled workspace Python runtime."
    ) from exc


MAX_REFERENCE_PIXELS = 40_000_000


def _ensure_pixel_budget(image: Image.Image) -> None:
    pixels = int(image.width) * int(image.height)
    if pixels > MAX_REFERENCE_PIXELS:
        raise ValueError(
            f"reference dimensions {image.width}x{image.height} exceed the "
            f"{MAX_REFERENCE_PIXELS}-pixel safety limit"
        )


def _summary(path: Path, image: Image.Image) -> Dict[str, Any]:
    return {
        "path": str(path.resolve()),
        "format": str(image.format or path.suffix.lstrip(".")).upper(),
        "size": tuple(image.size),
        "bytes": path.stat().st_size,
    }


def _ensure_distinct(source: Path, output: Path) -> None:
    if source.expanduser().resolve() == output.expanduser().resolve():
        raise ValueError("source and output must be different files")


def _has_transparency(image: Image.Image) -> bool:
    if image.mode in {"RGBA", "LA"}:
        alpha = image.getchannel("A")
        return alpha.getextrema()[0] < 255
    return image.mode == "P" and "transparency" in image.info


def _save_atomic(image: Image.Image, output: Path, format_name: str, **options: Any) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_name(f".{output.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temp.open("wb") as handle:
            image.save(handle, format_name, **options)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, output)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def _write_bytes_atomic(output: Path, data: bytes) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_name(f".{output.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temp.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, output)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def _encode_reference(image: Image.Image, *, transparent: bool, quality: int | None) -> bytes:
    buffer = io.BytesIO()
    if transparent:
        options: Dict[str, Any] = {"method": 4}
        if quality is None:
            options["lossless"] = True
        else:
            options["quality"] = quality
        image.convert("RGBA").save(buffer, "WEBP", **options)
    else:
        image.convert("RGB").save(
            buffer,
            "JPEG",
            quality=quality or 88,
            optimize=True,
            progressive=True,
        )
    return buffer.getvalue()


def _reference_bytes_within_limit(
    image: Image.Image, *, transparent: bool, max_bytes: int
) -> bytes:
    qualities: Tuple[int | None, ...] = (
        (None, 86, 68, 50, 38)
        if transparent
        else (88, 74, 60, 48, 38)
    )
    original = image.convert("RGBA" if transparent else "RGB")
    max_edge = max(original.size)
    target_edge = max_edge
    smallest: bytes | None = None

    while True:
        if target_edge == max_edge:
            candidate = original
        else:
            scale = target_edge / max_edge
            candidate = original.resize(
                (
                    max(1, round(original.width * scale)),
                    max(1, round(original.height * scale)),
                ),
                Image.Resampling.LANCZOS,
            )
        for quality in qualities:
            if quality is None and target_edge != max_edge:
                continue
            data = _encode_reference(
                candidate, transparent=transparent, quality=quality
            )
            if smallest is None or len(data) < len(smallest):
                smallest = data
            if len(data) <= max_bytes:
                return data

        if target_edge <= 48:
            break
        target_edge = max(48, int(target_edge * 0.65))

    smallest_size = len(smallest) if smallest is not None else 0
    raise ValueError(
        f"reference cannot be reduced below {max_bytes} bytes "
        f"(smallest candidate: {smallest_size} bytes)"
    )


def _save_for_suffix(image: Image.Image, output: Path) -> None:
    suffix = output.suffix.lower()
    icc = image.info.get("icc_profile")
    common = {"icc_profile": icc} if icc else {}
    if suffix in {".jpg", ".jpeg"}:
        converted = image.convert("RGB")
        _save_atomic(converted, output, "JPEG", quality=92, optimize=True, progressive=True, **common)
    elif suffix == ".webp":
        _save_atomic(image, output, "WEBP", quality=92, method=6, **common)
    elif suffix == ".png":
        _save_atomic(image, output, "PNG", optimize=True, **common)
    else:
        raise ValueError("output extension must be .png, .jpg, .jpeg, or .webp")


def compress_reference(source: Path, output: Path, *, quality: int = 82) -> Dict[str, Any]:
    if not 1 <= quality <= 95:
        raise ValueError("quality must be between 1 and 95")
    _ensure_distinct(source, output)
    with Image.open(source) as image:
        _ensure_pixel_budget(image)
        prepared = ImageOps.exif_transpose(image)
        prepared.load()
        if output.suffix.lower() == ".webp":
            _save_atomic(prepared, output, "WEBP", quality=quality, method=6)
        else:
            _save_atomic(
                prepared.convert("RGB"),
                output,
                "JPEG",
                quality=quality,
                optimize=True,
                progressive=True,
            )
    with Image.open(output) as result:
        return _summary(output, result)


def normalize_long_edge(
    source: Path, output: Path, *, target: int, allow_upscale: bool = False
) -> Dict[str, Any]:
    if target < 1:
        raise ValueError("target must be positive")
    _ensure_distinct(source, output)
    with Image.open(source) as image:
        _ensure_pixel_budget(image)
        prepared = ImageOps.exif_transpose(image)
        prepared.load()
        scale = target / max(prepared.size)
        if scale > 1 and not allow_upscale:
            scale = 1
        size: Tuple[int, int] = (
            max(1, round(prepared.width * scale)),
            max(1, round(prepared.height * scale)),
        )
        resized = (
            prepared.copy()
            if size == prepared.size
            else prepared.resize(size, Image.Resampling.LANCZOS)
        )
        _save_for_suffix(resized, output)
    with Image.open(output) as result:
        return _summary(output, result)


def probe(path: Path) -> Dict[str, Any]:
    with Image.open(path) as image:
        _ensure_pixel_budget(image)
        image.verify()
    with Image.open(path) as image:
        _ensure_pixel_budget(image)
        image.load()
        return _summary(path, image)


def prepare_reference(source: Path, cache_dir: Path, *, max_bytes: int = 1_000_000) -> Path:
    if max_bytes < 1:
        raise ValueError("max_bytes must be positive")
    source = source.expanduser().resolve()
    digest = hashlib.sha256(source.read_bytes()).hexdigest()[:24]
    cache_dir.mkdir(parents=True, exist_ok=True)
    with Image.open(source) as image:
        _ensure_pixel_budget(image)
        if source.stat().st_size <= max_bytes:
            return source
        prepared = ImageOps.exif_transpose(image)
        prepared.load()
        transparent = _has_transparency(prepared)
        output = cache_dir / (
            f"{digest}-{max_bytes}{'.webp' if transparent else '.jpg'}"
        )
        if output.exists() and 0 < output.stat().st_size <= max_bytes:
            try:
                probe(output)
            except Exception:
                output.unlink(missing_ok=True)
            else:
                return output.resolve()

        data = _reference_bytes_within_limit(
            prepared, transparent=transparent, max_bytes=max_bytes
        )
        _write_bytes_atomic(output, data)
    if output.stat().st_size > max_bytes:
        output.unlink(missing_ok=True)
        raise ValueError(f"prepared reference exceeds {max_bytes} bytes")
    return output.resolve()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    compress = subparsers.add_parser("compress-reference")
    compress.add_argument("--input", required=True)
    compress.add_argument("--out", required=True)
    compress.add_argument("--quality", type=int, default=82)

    normalize = subparsers.add_parser("normalize-long-edge")
    normalize.add_argument("--input", required=True)
    normalize.add_argument("--out", required=True)
    normalize.add_argument("--target", type=int, required=True)
    normalize.add_argument("--allow-upscale", action="store_true")

    inspect = subparsers.add_parser("probe")
    inspect.add_argument("--input", required=True)

    prepare = subparsers.add_parser("prepare-reference")
    prepare.add_argument("--input", required=True)
    prepare.add_argument("--cache-dir", required=True)
    prepare.add_argument("--max-bytes", type=int, default=1_000_000)

    args = parser.parse_args()
    if args.command == "compress-reference":
        result = compress_reference(Path(args.input), Path(args.out), quality=args.quality)
    elif args.command == "normalize-long-edge":
        result = normalize_long_edge(
            Path(args.input),
            Path(args.out),
            target=args.target,
            allow_upscale=args.allow_upscale,
        )
    elif args.command == "prepare-reference":
        source = Path(args.input)
        prepared = prepare_reference(
            source, Path(args.cache_dir), max_bytes=args.max_bytes
        )
        result = probe(prepared)
        result["source_path"] = str(source.resolve())
    else:
        result = probe(Path(args.input))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
