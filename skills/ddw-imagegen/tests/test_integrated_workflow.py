from __future__ import annotations

import argparse
import base64
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import struct
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import URLError
import zlib


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ddw_image_gen.py"
SPEC = importlib.util.spec_from_file_location("ddw_image_gen_integrated", SCRIPT)
assert SPEC and SPEC.loader
ddw = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ddw)

VALID_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class CredentialAndTransportTests(unittest.TestCase):
    def test_default_ledger_is_user_private_state_not_workspace_relative(self) -> None:
        self.assertTrue(Path(ddw.DEFAULT_JOB_LEDGER).is_absolute())
        self.assertIn("ddw-imagegen", Path(ddw.DEFAULT_JOB_LEDGER).parts)

    def test_generic_api_key_is_not_used_implicitly(self) -> None:
        with patch.dict(os.environ, {"API_KEY": "wrong-service-secret"}, clear=True):
            with self.assertRaises(SystemExit):
                ddw._get_api_key(api_key_env=None, dry_run=False)

    def test_generic_api_key_can_be_selected_explicitly(self) -> None:
        with patch.dict(os.environ, {"API_KEY": "explicit-secret"}, clear=True):
            self.assertEqual(
                "explicit-secret",
                ddw._get_api_key(api_key_env="API_KEY", dry_run=False),
            )

    def test_non_local_plain_http_is_rejected(self) -> None:
        with self.assertRaises(SystemExit):
            ddw._normalize_base_url("http://example.com")
        self.assertEqual("http://127.0.0.1:8080", ddw._normalize_base_url("http://127.0.0.1:8080"))

    def test_multipart_booleans_use_json_spelling(self) -> None:
        self.assertEqual("true", ddw._stringify_form_value(True))
        self.assertEqual("false", ddw._stringify_form_value(False))

    def test_saved_raw_response_is_recursively_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "raw.json"
            ddw._save_raw_response(
                str(output),
                {
                    "token": "canary-token",
                    "nested": {"authorization": "Bearer canary-key"},
                    "url": "https://example.test/job?token=canary-token",
                },
                force=False,
            )
            saved = output.read_text(encoding="utf-8")
        self.assertNotIn("canary-token", saved)
        self.assertNotIn("canary-key", saved)

    def test_gpt_image_2_size_constraints_are_checked_before_submit(self) -> None:
        self.assertEqual("3840x2160", ddw._validate_size("3840x2160"))
        self.assertEqual("auto", ddw._validate_size("auto"))
        with self.assertRaises(ddw.CliError):
            ddw._validate_size("4096x4096")
        with self.assertRaises(ddw.CliError):
            ddw._validate_size("1023x1537")

    def test_poll_transport_error_does_not_expose_token_from_url(self) -> None:
        with patch.object(ddw, "urlopen", side_effect=URLError("offline")):
            with self.assertRaises(ddw.CliError) as caught:
                ddw._http_json(
                    method="GET",
                    url="https://example.test/job?token=canary-job-token",
                    headers={},
                    retries=0,
                )
        self.assertNotIn("canary-job-token", str(caught.exception))

    def test_malformed_submit_response_does_not_expose_token(self) -> None:
        with self.assertRaises(ddw.CliError) as caught:
            ddw._extract_job_handle({"token": "canary-job-token", "message": "missing id"})
        self.assertNotIn("canary-job-token", str(caught.exception))

    def test_malformed_submit_response_redacts_camel_case_job_token(self) -> None:
        with self.assertRaises(ddw.CliError) as caught:
            ddw._extract_job_handle(
                {"jobToken": "canary-camel-token", "message": "missing id"}
            )
        self.assertNotIn("canary-camel-token", str(caught.exception))

    def test_reserved_payload_fields_cannot_be_overridden_by_extra_arguments(self) -> None:
        cases = [
            ("--extra-json", '{"n": 2}'),
            ("--extra-json", '{"output_format": "jpeg"}'),
            ("--param", "model=other-model"),
            ("--param", "quality=low"),
            ("--param", "size=2048x2048"),
            ("--param", "moderation=low"),
            ("--param", "compression=10"),
            ("--param", "output_compression=10"),
        ]
        for flag, value in cases:
            with self.subTest(flag=flag, value=value), patch.object(
                sys,
                "argv",
                [str(SCRIPT), "generate", "--prompt", "test", "--dry-run", flag, value],
            ), patch.object(ddw, "_submit_json_job") as submit:
                with self.assertRaises(SystemExit):
                    ddw.main()
                submit.assert_not_called()


class DurableStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.ledger = self.root / "jobs.jsonl"
        token_store = patch.dict(
            os.environ, {"DDW_IMAGE_TOKEN_STORE": str(self.root / "tokens")}
        )
        token_store.start()
        self.addCleanup(token_store.stop)

    def test_corrupt_ledger_fails_closed(self) -> None:
        self.ledger.write_text('{"event":"submit_started"}\n{"event":', encoding="utf-8")
        with self.assertRaisesRegex(ddw.CliError, "corrupt|invalid|JSON"):
            ddw._operation_snapshots(self.ledger)

    def test_same_prompt_with_different_payload_is_not_a_duplicate(self) -> None:
        first = ddw._prepare_operation(
            ledger_path=self.ledger,
            endpoint=ddw.GENERATIONS_ENDPOINT,
            out_key=str(self.root / "one.png"),
            prompt_hash="same-prompt",
            payload_hash="size-1k",
            metadata={},
        )
        ddw._append_operation_event(self.ledger, first, "completed", output_paths=["one.png"])

        second = ddw._prepare_operation(
            ledger_path=self.ledger,
            endpoint=ddw.GENERATIONS_ENDPOINT,
            out_key=str(self.root / "two.png"),
            prompt_hash="same-prompt",
            payload_hash="size-2k",
            metadata={},
        )
        self.assertNotEqual(first["operation_id"], second["operation_id"])

    def test_terminal_failure_blocks_an_automatic_second_paid_operation(self) -> None:
        first = ddw._prepare_operation(
            ledger_path=self.ledger,
            endpoint=ddw.GENERATIONS_ENDPOINT,
            out_key=str(self.root / "first.png"),
            prompt_hash="prompt",
            payload_hash="payload",
            metadata={},
        )
        ddw._append_operation_event(self.ledger, first, "failed", error="provider rejected")
        with self.assertRaisesRegex(ddw.CliError, "failed"):
            ddw._prepare_operation(
                ledger_path=self.ledger,
                endpoint=ddw.GENERATIONS_ENDPOINT,
                out_key=str(self.root / "second.png"),
                prompt_hash="prompt",
                payload_hash="payload",
                metadata={},
            )

    def test_edit_fingerprint_changes_when_file_contents_change(self) -> None:
        image = self.root / "reference.png"
        image.write_bytes(b"first")
        first = ddw._edit_request_hash({"prompt": "edit"}, [image], None)
        image.write_bytes(b"second")
        second = ddw._edit_request_hash({"prompt": "edit"}, [image], None)
        self.assertNotEqual(first, second)

    def test_poll_timeout_does_not_expose_job_token(self) -> None:
        with patch.object(ddw, "_http_json", return_value={"status": "running"}):
            with self.assertRaises(ddw.CliError) as caught:
                ddw._poll_job(
                    base_url="https://example.test",
                    api_key=None,
                    job_id="job-1",
                    token="canary-job-token",
                    poll_interval=0,
                    timeout_seconds=60,
                    request_timeout=1,
                    max_polls=1,
                    quiet=True,
                    poll_retries=0,
                    retry_delay=0,
                    done_without_image_polls=1,
                )
        self.assertNotIn("canary-job-token", str(caught.exception))

    def test_resume_completion_updates_the_operation_that_owned_the_handle(self) -> None:
        common = {
            "endpoint": ddw.GENERATIONS_ENDPOINT,
            "out_key": str(self.root / "out.png"),
            "prompt_hash": "prompt",
            "payload_hash": "payload",
        }
        old = {
            **common,
            "operation_id": "op-old",
            "idempotency_key": "idem-old",
        }
        newer = {
            **common,
            "operation_id": "op-new",
            "idempotency_key": "idem-new",
        }
        ddw._append_operation_event(self.ledger, old, "submit_started")
        ddw._append_operation_event(
            self.ledger, old, "submitted", job_id="job-old", token="token-old"
        )
        ddw._append_operation_event(self.ledger, newer, "submit_started")
        ddw._append_operation_event(
            self.ledger, newer, "submit_failed_unknown", error="timeout"
        )
        args = argparse.Namespace(
            resume_existing=True,
            auto_resume=False,
            out=str(self.root / "out.png"),
            out_dir=None,
        )
        delivered = self.root / "out.png"
        with patch.object(ddw, "_run_submitted_job", return_value=[delivered]):
            self.assertTrue(
                ddw._resume_existing_if_requested(
                    args=args,
                    ledger_path=self.ledger,
                    endpoint=ddw.GENERATIONS_ENDPOINT,
                    out_key=common["out_key"],
                    prompt_hash="prompt",
                    payload_hash="payload",
                    base_url="https://example.test",
                    api_key=None,
                    expected_count=1,
                    output_format="png",
                )
            )
        snapshots = {item["operation_id"]: item for item in ddw._operation_snapshots(self.ledger)}
        self.assertEqual("completed", snapshots["op-old"]["state"])
        self.assertEqual("ambiguous", snapshots["op-new"]["state"])
        self.assertEqual("op-old", args.resumed_operation_id)


def incomplete_png() -> bytes:
    signature = b"\x89PNG\r\n\x1a\n"

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
    return signature + chunk(b"IHDR", ihdr) + chunk(b"IEND", b"")


def oversized_scanline_png() -> bytes:
    signature = b"\x89PNG\r\n\x1a\n"

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    return (
        signature
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(b"\x00" * 100_000, 9))
        + chunk(b"IEND", b"")
    )


def write_large_png(path: Path, width: int = 512, height: int = 512) -> None:
    signature = b"\x89PNG\r\n\x1a\n"

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    rows = []
    for y in range(height):
        row = bytearray([0])
        for x in range(width):
            row.extend(((x * 37 + y * 17) % 256, (x * 13 + y * 43) % 256, (x * 71 + y * 5) % 256))
        rows.append(bytes(row))
    path.write_bytes(signature + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(b"".join(rows), 1)) + chunk(b"IEND", b""))


class DeliveryValidationTests(unittest.TestCase):
    def test_valid_png_is_recognized(self) -> None:
        info = ddw._validate_image_bytes(VALID_PNG)
        self.assertEqual("png", info["format"])
        self.assertEqual((1, 1), info["size"])

    def test_excessive_pixel_count_is_rejected_before_full_decode(self) -> None:
        with patch.object(ddw, "MAX_IMAGE_PIXELS", 0):
            with self.assertRaisesRegex(ddw.DeliveryError, "pixel|dimensions|large"):
                ddw._validate_image_bytes(VALID_PNG)

    def test_non_image_bytes_are_rejected(self) -> None:
        with self.assertRaisesRegex(ddw.DeliveryError, "image"):
            ddw._validate_image_bytes(b"not an image")

    def test_png_without_pixel_data_is_rejected(self) -> None:
        with self.assertRaisesRegex(ddw.DeliveryError, "complete|pixel|image"):
            ddw._validate_image_bytes(incomplete_png())

    def test_png_scanline_decompression_uses_bounded_api(self) -> None:
        with patch.object(
            ddw.zlib,
            "decompress",
            side_effect=AssertionError("unbounded decompression must not be used"),
        ):
            with self.assertRaises(ddw.DeliveryError):
                ddw._validate_image_bytes(oversized_scanline_png())

    def test_jpeg_header_without_scan_data_is_rejected(self) -> None:
        header_only = b"\xff\xd8\xff\xc0\x00\x07\x08\x00\x01\x00\x01\xff\xd9"
        with self.assertRaisesRegex(ddw.DeliveryError, "decode|complete|image|valid"):
            ddw._validate_image_bytes(header_only)

    def test_output_count_must_match_requested_count(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(ddw.DeliveryError, "1.*2|2.*1"):
                ddw._write_image_items(
                    [{"b64_json": base64.b64encode(VALID_PNG).decode("ascii")}],
                    [Path(temp_dir) / "one.png", Path(temp_dir) / "two.png"],
                    force=False,
                    request_timeout=1,
                    output_format="png",
                    expected_count=2,
                    quiet=True,
                )

    def test_existing_output_is_rejected_before_submit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "exists.png"
            output.write_bytes(VALID_PNG)
            with self.assertRaisesRegex(ddw.CliError, "already exists"):
                ddw._preflight_output_paths([output], force=False)

    def test_image_download_requires_https_and_rejects_private_dns(self) -> None:
        with patch.object(ddw, "urlopen") as open_url:
            with self.assertRaisesRegex(ddw.DeliveryError, "HTTPS"):
                ddw._download_bytes("http://example.test/image.png")
        open_url.assert_not_called()

        private_answer = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.8", 443))
        ]
        with patch.object(ddw.socket, "getaddrinfo", return_value=private_answer), patch.object(
            ddw, "urlopen"
        ) as open_url:
            with self.assertRaisesRegex(ddw.DeliveryError, "public|private|unsafe"):
                ddw._download_bytes("https://example.test/image.png")
        open_url.assert_not_called()

    def test_image_redirect_target_is_validated_before_following(self) -> None:
        handler_class = getattr(ddw, "_SafeImageRedirectHandler", None)
        self.assertIsNotNone(handler_class)
        handler = handler_class()
        with self.assertRaises(ddw.DeliveryError):
            handler.redirect_request(
                object(),
                None,
                302,
                "Found",
                {},
                "https://127.0.0.1/internal.png",
            )


class IntegratedCreateCommandTests(unittest.TestCase):
    def run_main(self, argv: list[str], cwd: Path) -> dict:
        stdout = io.StringIO()
        old_cwd = Path.cwd()
        try:
            os.chdir(cwd)
            with patch.object(sys, "argv", [str(SCRIPT), *argv]), contextlib.redirect_stdout(stdout):
                self.assertEqual(0, ddw.main())
        finally:
            os.chdir(old_cwd)
        return json.loads(stdout.getvalue())

    def test_create_dry_run_auto_selects_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            result = self.run_main(["create", "--prompt", "未来感产品海报", "--dry-run"], root)
        self.assertEqual(ddw.GENERATIONS_ENDPOINT, result["headers"]["X-Image-Job-Endpoint"])
        self.assertTrue(Path(result["outputs"][0]).is_absolute())

    def test_create_dry_run_does_not_migrate_or_write_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ledger = root / "legacy.jsonl"
            original = (
                '{"event":"submitted","operation_id":"op-legacy",'
                '"job_id":"job-1","token":"plaintext-token"}\n'
            )
            ledger.write_text(original, encoding="utf-8")

            self.run_main(
                [
                    "create",
                    "--prompt",
                    "未来感产品海报",
                    "--dry-run",
                    "--job-ledger",
                    str(ledger),
                ],
                root,
            )

            self.assertEqual(original, ledger.read_text(encoding="utf-8"))
            self.assertFalse(Path(str(ledger) + ".lock").exists())

    def test_create_one_shot_submits_once_and_returns_only_deliverables(self) -> None:
        final = {
            "status": "succeeded",
            "data": [{"b64_json": base64.b64encode(VALID_PNG).decode("ascii")}],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.dict(os.environ, {"DDW_IMAGE_API_KEY": "canary-api-key"}, clear=True), patch.object(
                ddw, "_submit_json_job", return_value={"id": "job-1", "token": "canary-job-token"}
            ) as submit, patch.object(ddw, "_poll_job", return_value=final) as poll:
                result = self.run_main(["create", "--prompt", "未来感产品海报"], root)
            self.assertEqual(1, submit.call_count)
            self.assertEqual(1, poll.call_count)
            self.assertEqual("completed", result["state"])
            self.assertEqual(1, len(result["outputs"]))
            self.assertTrue(Path(result["outputs"][0]).exists())
            serialized = json.dumps(result)
            self.assertNotIn("canary-api-key", serialized)
            self.assertNotIn("canary-job-token", serialized)

    def test_create_resumes_same_job_after_atomic_multi_output_delivery_failure(self) -> None:
        final = {
            "status": "succeeded",
            "data": [
                {"b64_json": base64.b64encode(VALID_PNG).decode("ascii")},
                {"b64_json": base64.b64encode(VALID_PNG).decode("ascii")},
            ],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            out = root / "result.png"
            outputs = [root / "result-1.png", root / "result-2.png"]
            ledger = root / "jobs.jsonl"
            argv = [
                "create",
                "--prompt",
                "two outputs",
                "--n",
                "2",
                "--out",
                str(out),
                "--job-ledger",
                str(ledger),
            ]
            real_replace = os.replace
            failed_once = False

            def fail_second_commit(source: object, destination: object) -> None:
                nonlocal failed_once
                if Path(destination) == outputs[1] and not failed_once:
                    failed_once = True
                    raise OSError("simulated second output failure")
                real_replace(source, destination)

            with patch.dict(
                os.environ,
                {
                    "DDW_IMAGE_API_KEY": "canary-api-key",
                    "DDW_IMAGE_TOKEN_STORE": str(root / "tokens"),
                },
                clear=True,
            ), patch.object(
                ddw, "_submit_json_job", return_value={"id": "job-1", "token": "job-token"}
            ) as submit, patch.object(ddw, "_poll_job", return_value=final) as poll, patch.object(
                ddw.os, "replace", side_effect=fail_second_commit
            ):
                with patch.object(sys, "argv", [str(SCRIPT), *argv]), self.assertRaises(
                    SystemExit
                ):
                    ddw.main()

                self.assertFalse(outputs[0].exists())
                self.assertFalse(outputs[1].exists())

                with patch.object(sys, "argv", [str(SCRIPT), *argv]), contextlib.redirect_stdout(
                    io.StringIO()
                ):
                    self.assertEqual(0, ddw.main())

            self.assertEqual(1, submit.call_count)
            self.assertEqual(2, poll.call_count)
            self.assertTrue(outputs[0].exists())
            self.assertTrue(outputs[1].exists())

    def test_create_dry_run_auto_selects_edit_for_image_input(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            image = root / "reference.png"
            image.write_bytes(VALID_PNG)
            result = self.run_main(
                ["create", "--prompt", "把背景换成雪山", "--image", str(image), "--dry-run"],
                root,
            )
        self.assertEqual(ddw.EDITS_ENDPOINT, result["headers"]["X-Image-Job-Endpoint"])
        self.assertEqual(str(image), result["files"][0]["path"])

    def test_create_automatically_prepares_large_reference_upload(self) -> None:
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow runtime is required for automatic reference preparation")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            image = root / "large-reference.png"
            Image.effect_noise((900, 900), 100).convert("RGB").save(image, "PNG")
            result = self.run_main(
                [
                    "create",
                    "--prompt",
                    "把产品放进雪山场景",
                    "--image",
                    str(image),
                    "--upload-threshold",
                    "80000",
                    "--dry-run",
                ],
                root,
            )
            prepared = Path(result["files"][0]["path"])
            self.assertNotEqual(image, prepared)
            self.assertTrue(prepared.exists())
            self.assertLess(prepared.stat().st_size, image.stat().st_size)

    def test_plain_python_discovers_bundled_runtime_for_large_reference(self) -> None:
        try:
            import PIL  # noqa: F401
        except ImportError:
            pass
        else:
            self.skipTest("This test exercises the no-Pillow launcher fallback")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            image = root / "large-reference.png"
            write_large_png(image)
            result = self.run_main(
                [
                    "create",
                    "--prompt",
                    "把产品放进雪山场景",
                    "--image",
                    str(image),
                    "--upload-threshold",
                    "80000",
                    "--dry-run",
                ],
                root,
            )
            prepared = Path(result["files"][0]["path"])
            self.assertTrue(prepared.exists())
            self.assertNotEqual(image, prepared)


if __name__ == "__main__":
    unittest.main()
