from __future__ import annotations

import importlib.util
import json
import argparse
import base64
import contextlib
import io
import os
from pathlib import Path
import tempfile
import threading
import unittest
import sys
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ddw_image_gen.py"
SPEC = importlib.util.spec_from_file_location("ddw_image_gen", SCRIPT)
assert SPEC and SPEC.loader
ddw = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ddw)

VALID_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class OperationLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.ledger = Path(self.temp_dir.name) / "jobs.jsonl"
        token_store = patch.dict(
            os.environ,
            {"DDW_IMAGE_TOKEN_STORE": str(Path(self.temp_dir.name) / "tokens")},
        )
        token_store.start()
        self.addCleanup(token_store.stop)

    def write_records(self, records: list[dict]) -> None:
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        with self.ledger.open("w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")

    def test_legacy_records_reconstruct_distinct_operations(self) -> None:
        self.write_records(
            [
                {
                    "ts": "2026-07-10T06:14:45Z",
                    "event": "submit_started",
                    "endpoint": ddw.EDITS_ENDPOINT,
                    "out_key": "same.png",
                    "prompt_hash": "p",
                    "payload_hash": "a",
                },
                {
                    "ts": "2026-07-10T06:16:08Z",
                    "event": "submitted",
                    "endpoint": ddw.EDITS_ENDPOINT,
                    "out_key": "same.png",
                    "prompt_hash": "p",
                    "payload_hash": "a",
                    "job_id": "job-1",
                    "token": "secret-1",
                },
                {
                    "ts": "2026-07-10T06:27:55Z",
                    "event": "submit_started",
                    "endpoint": ddw.EDITS_ENDPOINT,
                    "out_key": "same.png",
                    "prompt_hash": "p",
                    "payload_hash": "a",
                },
                {
                    "ts": "2026-07-10T06:29:56Z",
                    "event": "submit_failed_unknown",
                    "endpoint": ddw.EDITS_ENDPOINT,
                    "out_key": "same.png",
                    "prompt_hash": "p",
                    "payload_hash": "a",
                },
            ]
        )

        operations = ddw._operation_snapshots(
            self.ledger,
            endpoint=ddw.EDITS_ENDPOINT,
            out_key="same.png",
            prompt_hash="p",
        )

        self.assertEqual(2, len(operations))
        self.assertEqual("submitted", operations[0]["state"])
        self.assertEqual("job-1", operations[0]["job_id"])
        self.assertEqual("ambiguous", operations[1]["state"])
        self.assertNotEqual(operations[0]["operation_id"], operations[1]["operation_id"])

    def test_plaintext_tokens_are_migrated_and_terminal_tokens_removed(self) -> None:
        self.write_records(
            [
                {
                    "event": "submitted",
                    "operation_id": "op-active",
                    "endpoint": ddw.GENERATIONS_ENDPOINT,
                    "out_key": "active.png",
                    "prompt_hash": "active",
                    "payload_hash": "active",
                    "job_id": "job-active",
                    "token": "active-secret",
                },
                {
                    "event": "submitted",
                    "operation_id": "op-done",
                    "endpoint": ddw.GENERATIONS_ENDPOINT,
                    "out_key": "done.png",
                    "prompt_hash": "done",
                    "payload_hash": "done",
                    "job_id": "job-done",
                    "token": "done-secret",
                },
                {
                    "event": "completed",
                    "operation_id": "op-done",
                    "endpoint": ddw.GENERATIONS_ENDPOINT,
                    "out_key": "done.png",
                    "prompt_hash": "done",
                    "payload_hash": "done",
                    "job_id": "job-done",
                },
            ]
        )

        ddw._migrate_ledger_tokens(self.ledger)

        raw = self.ledger.read_text(encoding="utf-8")
        self.assertNotIn("active-secret", raw)
        self.assertNotIn("done-secret", raw)
        snapshots = {
            item["operation_id"]: item
            for item in ddw._operation_snapshots(self.ledger)
        }
        self.assertEqual("active-secret", snapshots["op-active"]["token"])
        self.assertNotIn("token", snapshots["op-done"])

    def test_terminal_snapshot_ignores_orphaned_token_and_startup_cleans_it(self) -> None:
        operation = ddw._prepare_operation(
            ledger_path=self.ledger,
            endpoint=ddw.GENERATIONS_ENDPOINT,
            out_key="out.png",
            prompt_hash="prompt",
            payload_hash="payload",
            metadata={},
        )
        ddw._append_operation_event(
            self.ledger,
            operation,
            "submitted",
            job_id="job-1",
            token="orphan-secret",
        )
        token_path = ddw._token_path(ddw._token_ref(operation["operation_id"]))
        with patch.object(ddw, "_delete_job_token"):
            ddw._append_operation_event(
                self.ledger, operation, "completed", job_id="job-1"
            )

        snapshot = ddw._find_operation(self.ledger, operation["operation_id"])
        self.assertNotIn("token", snapshot)
        self.assertTrue(token_path.exists())

        ddw._migrate_ledger_tokens(self.ledger)

        self.assertFalse(token_path.exists())
        self.assertNotIn("token_ref", self.ledger.read_text(encoding="utf-8"))

    def test_terminal_operation_cannot_be_resurrected_by_stale_event(self) -> None:
        operation = ddw._prepare_operation(
            ledger_path=self.ledger,
            endpoint=ddw.GENERATIONS_ENDPOINT,
            out_key="out.png",
            prompt_hash="prompt",
            payload_hash="payload",
            metadata={},
        )
        ddw._append_operation_event(
            self.ledger,
            operation,
            "submitted",
            job_id="job-1",
            token="captured-token",
        )
        ddw._append_operation_event(
            self.ledger, operation, "completed", job_id="job-1"
        )

        with self.assertRaisesRegex(ddw.CliError, "terminal"):
            ddw._append_operation_event(
                self.ledger,
                operation,
                "poll_interrupted",
                job_id="job-1",
                token="stale-token",
            )

        snapshot = ddw._find_operation(self.ledger, operation["operation_id"])
        self.assertEqual("completed", snapshot["state"])
        self.assertNotIn("token", snapshot)

    def test_migrated_legacy_operation_uses_stable_id_for_supersede_cleanup(self) -> None:
        self.write_records(
            [
                {
                    "event": "submit_started",
                    "endpoint": ddw.EDITS_ENDPOINT,
                    "out_key": "legacy.png",
                    "prompt_hash": "prompt",
                    "payload_hash": "payload",
                },
                {
                    "event": "submitted",
                    "endpoint": ddw.EDITS_ENDPOINT,
                    "out_key": "legacy.png",
                    "prompt_hash": "prompt",
                    "payload_hash": "payload",
                    "job_id": "legacy-job",
                    "token": "legacy-token",
                },
            ]
        )
        ddw._migrate_ledger_tokens(self.ledger)
        prior = ddw._operation_snapshots(self.ledger)[0]
        token_path = ddw._token_path(ddw._token_ref(prior["operation_id"]))
        self.assertTrue(token_path.exists())

        ddw._prepare_operation(
            ledger_path=self.ledger,
            endpoint=ddw.EDITS_ENDPOINT,
            out_key="legacy.png",
            prompt_hash="prompt",
            payload_hash="payload",
            metadata={},
            force_new_job=True,
            supersede_operation=prior["operation_id"],
        )

        self.assertFalse(token_path.exists())

    def test_event_append_waits_for_ledger_migration_lock(self) -> None:
        operation = ddw._prepare_operation(
            ledger_path=self.ledger,
            endpoint=ddw.GENERATIONS_ENDPOINT,
            out_key="locked.png",
            prompt_hash="prompt",
            payload_hash="payload",
            metadata={},
        )
        finished = threading.Event()

        def append_event() -> None:
            ddw._append_operation_event(
                self.ledger,
                operation,
                "submitted",
                job_id="job-1",
                token="token-1",
            )
            finished.set()

        with ddw._ledger_lock(self.ledger):
            worker = threading.Thread(target=append_event)
            worker.start()
            self.assertFalse(finished.wait(0.15))
        worker.join(timeout=2)

        self.assertTrue(finished.is_set())
        self.assertEqual(
            "submitted", ddw._find_operation(self.ledger, operation["operation_id"])["state"]
        )

    def test_prepare_requires_exact_supersede_for_ambiguous_operation(self) -> None:
        first = ddw._prepare_operation(
            ledger_path=self.ledger,
            endpoint=ddw.EDITS_ENDPOINT,
            out_key="out.png",
            prompt_hash="prompt",
            payload_hash="payload",
            metadata={"size": "1024x1024"},
        )
        ddw._append_operation_event(self.ledger, first, "submit_failed_unknown", error="timeout")

        with self.assertRaisesRegex(ddw.CliError, first["operation_id"]):
            ddw._prepare_operation(
                ledger_path=self.ledger,
                endpoint=ddw.EDITS_ENDPOINT,
                out_key="out.png",
                prompt_hash="prompt",
                payload_hash="payload",
                metadata={},
                force_new_job=True,
            )

        second = ddw._prepare_operation(
            ledger_path=self.ledger,
            endpoint=ddw.EDITS_ENDPOINT,
            out_key="out.png",
            prompt_hash="prompt",
            payload_hash="payload",
            metadata={},
            force_new_job=True,
            supersede_operation=first["operation_id"],
        )
        self.assertNotEqual(first["operation_id"], second["operation_id"])
        states = ddw._operation_snapshots(self.ledger)
        self.assertEqual("superseded", states[0]["state"])
        self.assertEqual(second["operation_id"], states[0]["superseded_by"])
        self.assertEqual("prepared", states[1]["state"])

    def test_superseding_captured_job_removes_private_token(self) -> None:
        first = ddw._prepare_operation(
            ledger_path=self.ledger,
            endpoint=ddw.EDITS_ENDPOINT,
            out_key="out.png",
            prompt_hash="prompt",
            payload_hash="payload",
            metadata={},
        )
        ddw._append_operation_event(
            self.ledger,
            first,
            "submitted",
            job_id="job-1",
            token="captured-token",
        )
        token_path = ddw._token_path(ddw._token_ref(first["operation_id"]))
        self.assertTrue(token_path.exists())

        ddw._prepare_operation(
            ledger_path=self.ledger,
            endpoint=ddw.EDITS_ENDPOINT,
            out_key="out.png",
            prompt_hash="prompt",
            payload_hash="payload",
            metadata={},
            force_new_job=True,
            supersede_operation=first["operation_id"],
        )

        self.assertFalse(token_path.exists())
        superseded = ddw._find_operation(self.ledger, first["operation_id"])
        self.assertEqual("superseded", superseded["state"])
        self.assertNotIn("token", superseded)

    def test_completed_operation_is_not_silently_resubmitted(self) -> None:
        first = ddw._prepare_operation(
            ledger_path=self.ledger,
            endpoint=ddw.GENERATIONS_ENDPOINT,
            out_key="done.png",
            prompt_hash="prompt",
            payload_hash="payload",
            metadata={},
        )
        ddw._append_operation_event(
            self.ledger,
            first,
            "completed",
            job_id="job-done",
            token="secret",
            output_paths=["done.png"],
        )

        with self.assertRaisesRegex(ddw.CliError, "completed"):
            ddw._prepare_operation(
                ledger_path=self.ledger,
                endpoint=ddw.GENERATIONS_ENDPOINT,
                out_key="done.png",
                prompt_hash="prompt",
                payload_hash="payload",
                metadata={},
                force_new_job=True,
            )

    def test_concurrent_prepare_creates_one_active_operation(self) -> None:
        barrier = threading.Barrier(2)
        successes: list[dict] = []
        errors: list[Exception] = []

        def run() -> None:
            barrier.wait()
            try:
                successes.append(
                    ddw._prepare_operation(
                        ledger_path=self.ledger,
                        endpoint=ddw.GENERATIONS_ENDPOINT,
                        out_key="race.png",
                        prompt_hash="prompt",
                        payload_hash="payload",
                        metadata={},
                    )
                )
            except Exception as exc:  # Test captures the losing process result.
                errors.append(exc)

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        self.assertEqual(1, len(successes))
        self.assertEqual(1, len(errors))
        self.assertIsInstance(errors[0], ddw.CliError)
        self.assertEqual(1, len(ddw._operation_snapshots(self.ledger)))


class OperationHeaderTests(unittest.TestCase):
    def test_operation_headers_are_stable_and_sanitized(self) -> None:
        operation = {
            "operation_id": "op-123",
            "idempotency_key": "idem-456",
            "token": "job-secret",
        }

        headers = ddw._operation_headers(operation)
        sanitized = ddw._sanitise_operation(operation)

        self.assertEqual("idem-456", headers["Idempotency-Key"])
        self.assertEqual("idem-456", headers["X-Idempotency-Key"])
        self.assertEqual("op-123", headers["X-Client-Request-Id"])
        self.assertNotIn("token", sanitized)
        self.assertTrue(sanitized["has_token"])

    def test_json_submit_sends_operation_headers(self) -> None:
        operation = {"operation_id": "op-json", "idempotency_key": "idem-json"}
        with patch.object(ddw, "_http_json", return_value={"id": "j", "token": "t"}) as http:
            ddw._submit_json_job(
                base_url="https://example.test",
                endpoint=ddw.GENERATIONS_ENDPOINT,
                api_key="key",
                payload={"prompt": "test"},
                request_timeout=1,
                operation=operation,
            )

        headers = http.call_args.kwargs["headers"]
        self.assertEqual("idem-json", headers["Idempotency-Key"])
        self.assertEqual("op-json", headers["X-Client-Request-Id"])

    def test_multipart_submit_sends_operation_headers(self) -> None:
        operation = {"operation_id": "op-form", "idempotency_key": "idem-form"}
        with tempfile.TemporaryDirectory() as temp_dir:
            image = Path(temp_dir) / "input.png"
            image.write_bytes(b"not-a-real-png")
            with patch.object(ddw, "_http_json", return_value={"id": "j", "token": "t"}) as http:
                ddw._submit_multipart_job(
                    base_url="https://example.test",
                    endpoint=ddw.EDITS_ENDPOINT,
                    api_key="key",
                    fields=[("prompt", "test")],
                    files=[("image[]", image)],
                    request_timeout=1,
                    operation=operation,
                )

        headers = http.call_args.kwargs["headers"]
        self.assertEqual("idem-form", headers["X-Idempotency-Key"])
        self.assertEqual("op-form", headers["X-Client-Request-Id"])

    def test_poll_sends_job_token_header_and_query(self) -> None:
        response = {
            "status": "succeeded",
            "data": [{"b64_json": "aGVsbG8="}],
        }
        with patch.object(ddw, "_http_json", return_value=response) as http:
            result = ddw._poll_job(
                base_url="https://example.test",
                api_key=None,
                job_id="job-1",
                token="token-1",
                poll_interval=0,
                timeout_seconds=1,
                request_timeout=1,
                max_polls=1,
                quiet=True,
                poll_retries=0,
                retry_delay=0,
                done_without_image_polls=1,
            )

        self.assertEqual(response, result)
        self.assertEqual("token-1", http.call_args.kwargs["headers"]["X-Image-Job-Token"])
        self.assertIn("token=token-1", http.call_args.kwargs["url"])

    def test_live_submit_cannot_disable_ledger(self) -> None:
        argv = [
            str(SCRIPT),
            "generate",
            "--prompt",
            "test",
            "--no-job-ledger",
        ]
        with patch.object(sys, "argv", argv), patch.object(ddw, "_generate") as generate:
            with self.assertRaises(SystemExit):
                ddw.main()
        generate.assert_not_called()


class SubmitIntegrationTests(unittest.TestCase):
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

    def common_args(self) -> dict:
        return {
            "prompt": "test prompt",
            "prompt_file": None,
            "base_url": "https://example.test",
            "api_key_env": None,
            "model": "gpt-image-2",
            "n": 1,
            "quality": "high",
            "size": "1024x1024",
            "background": None,
            "output_format": None,
            "output_compression": None,
            "moderation": None,
            "extra_json": None,
            "param": None,
            "out": str(self.root / "out.png"),
            "out_dir": None,
            "raw_response_out": None,
            "force": False,
            "dry_run": False,
            "submit_only": True,
            "job_ledger": str(self.ledger),
            "no_job_ledger": False,
            "resume_existing": False,
            "force_new_job": False,
            "supersede_operation": None,
            "request_timeout": 1,
            "submit_retries": 0,
            "retry_delay": 0,
            "poll_interval": 0,
            "timeout_seconds": 1,
            "max_polls": 1,
            "poll_retries": 0,
            "done_without_image_polls": 1,
            "poll_with_auth": False,
            "quiet": True,
        }

    def batch_args(self, batch: Path) -> argparse.Namespace:
        return argparse.Namespace(
            input=str(batch),
            out_dir=str(self.root / "batch-out"),
            base_url="https://example.test",
            api_key_env=None,
            model="gpt-image-2",
            n=1,
            quality="high",
            size="1024x1024",
            background=None,
            output_format=None,
            output_compression=None,
            moderation=None,
            dry_run=False,
            submit_only=False,
            raw_response_dir=None,
            force=False,
            fail_fast=False,
            job_ledger=str(self.ledger),
            no_job_ledger=False,
            request_timeout=1,
            submit_retries=0,
            retry_delay=0,
            poll_interval=0,
            timeout_seconds=1,
            max_polls=1,
            poll_retries=0,
            done_without_image_polls=1,
            poll_with_auth=False,
            quiet=True,
        )

    def test_generate_timeout_records_one_ambiguous_operation(self) -> None:
        args = argparse.Namespace(**self.common_args())
        with patch.object(ddw, "_get_api_key", return_value="key"), patch.object(
            ddw, "_submit_json_job", side_effect=ddw.CliError("timeout")
        ) as submit:
            with self.assertRaisesRegex(ddw.CliError, r"operation_id=op-.*timeout"):
                ddw._generate(args)

        operation = submit.call_args.kwargs["operation"]
        self.assertTrue(operation["operation_id"].startswith("op-"))
        snapshots = ddw._operation_snapshots(self.ledger)
        self.assertEqual(1, len(snapshots))
        self.assertEqual("ambiguous", snapshots[0]["state"])

    def test_poll_timeout_keeps_submitted_operation_recoverable(self) -> None:
        args = argparse.Namespace(**self.common_args())
        with patch.object(ddw, "_get_api_key", return_value="key"), patch.object(
            ddw, "_submit_json_job", return_value={"id": "job-1", "token": "token-1"}
        ), patch.object(
            ddw, "_run_submitted_job", side_effect=ddw.CliError("poll timeout")
        ):
            with self.assertRaisesRegex(ddw.CliError, "poll timeout"):
                ddw._generate(args)

        snapshot = ddw._operation_snapshots(self.ledger)[0]
        self.assertEqual("submitted", snapshot["state"])
        self.assertEqual("poll_interrupted", snapshot["event"])
        self.assertEqual("job-1", snapshot["job_id"])

    def test_edit_timeout_records_one_ambiguous_operation(self) -> None:
        image = self.root / "input.png"
        image.write_bytes(VALID_PNG)
        values = self.common_args()
        values.update(
            {
                "image": [str(image)],
                "image_field": "image[]",
                "mask": None,
                "mask_field": "mask",
                "input_fidelity": None,
            }
        )
        args = argparse.Namespace(**values)
        with patch.object(ddw, "_get_api_key", return_value="key"), patch.object(
            ddw, "_submit_multipart_job", side_effect=ddw.CliError("timeout")
        ) as submit:
            with self.assertRaisesRegex(ddw.CliError, r"operation_id=op-.*timeout"):
                ddw._edit(args)

        operation = submit.call_args.kwargs["operation"]
        self.assertTrue(operation["idempotency_key"].startswith("ddw-"))
        snapshots = ddw._operation_snapshots(self.ledger)
        self.assertEqual(1, len(snapshots))
        self.assertEqual("ambiguous", snapshots[0]["state"])

    def test_invalid_mask_is_rejected_before_paid_submit(self) -> None:
        image = self.root / "input.png"
        image.write_bytes(VALID_PNG)
        mask = self.root / "mask.png"
        mask.write_bytes(b"not a complete image")
        values = self.common_args()
        values.update(
            {
                "image": [str(image)],
                "image_field": "image[]",
                "mask": str(mask),
                "mask_field": "mask",
                "input_fidelity": None,
            }
        )

        with patch.object(ddw, "_get_api_key", return_value="key"), patch.object(
            ddw,
            "_submit_multipart_job",
            return_value={"id": "should-not-submit", "token": "should-not-exist"},
        ) as submit, self.assertRaises(SystemExit):
            ddw._edit(argparse.Namespace(**values))

        submit.assert_not_called()
        self.assertFalse(self.ledger.exists())

    def test_batch_timeout_is_ledgered_and_cannot_resubmit(self) -> None:
        batch = self.root / "batch-input.jsonl"
        batch.write_text(json.dumps({"prompt": "batch prompt", "out": "batch.png"}) + "\n")
        args = argparse.Namespace(
            input=str(batch),
            out_dir=str(self.root / "batch-out"),
            base_url="https://example.test",
            api_key_env=None,
            model="gpt-image-2",
            n=1,
            quality="high",
            size="1024x1024",
            background=None,
            output_format=None,
            output_compression=None,
            moderation=None,
            dry_run=False,
            submit_only=True,
            raw_response_dir=None,
            force=False,
            fail_fast=False,
            job_ledger=str(self.ledger),
            no_job_ledger=False,
            request_timeout=1,
            submit_retries=0,
            retry_delay=0,
            poll_interval=0,
            timeout_seconds=1,
            max_polls=1,
            poll_retries=0,
            done_without_image_polls=1,
            poll_with_auth=False,
            quiet=True,
        )
        with patch.object(ddw, "_get_api_key", return_value="key"), patch.object(
            ddw, "_submit_json_job", side_effect=ddw.CliError("timeout")
        ) as submit:
            with self.assertRaises(SystemExit):
                ddw._generate_batch(args)
        self.assertIn("operation", submit.call_args.kwargs)
        self.assertEqual("ambiguous", ddw._operation_snapshots(self.ledger)[0]["state"])

        with patch.object(ddw, "_get_api_key", return_value="key"), patch.object(
            ddw, "_submit_json_job", return_value={"id": "new", "token": "new-token"}
        ) as second_submit:
            with self.assertRaises(SystemExit):
                ddw._generate_batch(args)
        second_submit.assert_not_called()

    def test_batch_revalidates_final_job_payload_before_submit(self) -> None:
        batch = self.root / "invalid-batch.jsonl"
        batch.write_text(json.dumps({"prompt": "bad count", "n": 0}) + "\n")
        args = self.batch_args(batch)
        args.dry_run = True

        with patch.object(ddw, "_submit_json_job") as submit, self.assertRaisesRegex(
            ddw.CliError, "n.*between 1 and 10"
        ):
            ddw._generate_batch(args)
        submit.assert_not_called()

    def test_batch_terminal_provider_failure_is_terminal_and_clears_token(self) -> None:
        batch = self.root / "terminal-batch.jsonl"
        batch.write_text(json.dumps({"prompt": "terminal"}) + "\n")
        args = self.batch_args(batch)

        with patch.object(ddw, "_get_api_key", return_value="key"), patch.object(
            ddw, "_submit_json_job", return_value={"id": "job-1", "token": "secret-1"}
        ), patch.object(ddw, "_poll_job", side_effect=ddw.JobFailedError("rejected")):
            with self.assertRaises(SystemExit):
                ddw._generate_batch(args)

        snapshot = ddw._operation_snapshots(self.ledger)[0]
        self.assertEqual("failed", snapshot["state"])
        self.assertEqual("failed", snapshot["event"])
        self.assertNotIn("token", snapshot)
        self.assertFalse(any((self.root / "tokens").glob("*.token")))

    def test_batch_delivery_failure_remains_recoverable(self) -> None:
        batch = self.root / "delivery-batch.jsonl"
        batch.write_text(json.dumps({"prompt": "delivery"}) + "\n")
        args = self.batch_args(batch)

        with patch.object(ddw, "_get_api_key", return_value="key"), patch.object(
            ddw, "_submit_json_job", return_value={"id": "job-2", "token": "secret-2"}
        ), patch.object(ddw, "_poll_job", return_value={"status": "succeeded"}), patch.object(
            ddw, "_write_image_items", side_effect=ddw.DeliveryError("invalid image")
        ):
            with self.assertRaises(SystemExit):
                ddw._generate_batch(args)

        snapshot = ddw._operation_snapshots(self.ledger)[0]
        self.assertEqual("delivery_failed", snapshot["state"])
        self.assertEqual("delivery_failed", snapshot["event"])
        self.assertEqual("secret-2", snapshot["token"])

    def test_batch_poll_interruption_remains_submitted(self) -> None:
        batch = self.root / "poll-batch.jsonl"
        batch.write_text(json.dumps({"prompt": "poll"}) + "\n")
        args = self.batch_args(batch)

        with patch.object(ddw, "_get_api_key", return_value="key"), patch.object(
            ddw, "_submit_json_job", return_value={"id": "job-3", "token": "secret-3"}
        ), patch.object(ddw, "_poll_job", side_effect=ddw.CliError("poll timeout")):
            with self.assertRaises(SystemExit):
                ddw._generate_batch(args)

        snapshot = ddw._operation_snapshots(self.ledger)[0]
        self.assertEqual("submitted", snapshot["state"])
        self.assertEqual("poll_interrupted", snapshot["event"])
        self.assertEqual("secret-3", snapshot["token"])


class AtomicDeliveryTests(unittest.TestCase):
    def test_multi_output_commit_failure_rolls_back_all_new_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            outputs = [root / "result-1.png", root / "result-2.png"]
            items = [
                {"b64_json": base64.b64encode(VALID_PNG).decode("ascii")},
                {"b64_json": base64.b64encode(VALID_PNG).decode("ascii")},
            ]
            real_replace = os.replace

            def fail_second_commit(source: object, destination: object) -> None:
                if Path(destination) == outputs[1]:
                    raise OSError("simulated second output failure")
                real_replace(source, destination)

            with patch.object(ddw.os, "replace", side_effect=fail_second_commit):
                with self.assertRaises(ddw.DeliveryError):
                    ddw._write_image_items(
                        items,
                        outputs,
                        force=False,
                        request_timeout=1,
                        output_format="png",
                        expected_count=2,
                        quiet=True,
                    )

            self.assertFalse(outputs[0].exists())
            self.assertFalse(outputs[1].exists())


class RecoveryCommandTests(unittest.TestCase):
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

    def test_job_token_is_kept_out_of_ledger_and_can_be_recovered(self) -> None:
        operation = ddw._prepare_operation(
            ledger_path=self.ledger,
            endpoint=ddw.GENERATIONS_ENDPOINT,
            out_key=str(self.root / "out.png"),
            prompt_hash="prompt",
            payload_hash="payload",
            metadata={"n": 1},
        )

        ddw._append_operation_event(
            self.ledger,
            operation,
            "submitted",
            job_id="job-1",
            token="canary-job-token",
        )

        self.assertNotIn("canary-job-token", self.ledger.read_text(encoding="utf-8"))
        snapshot = ddw._find_operation(self.ledger, operation["operation_id"])
        self.assertEqual("canary-job-token", snapshot["token"])
        self.assertTrue(snapshot.get("token_ref"))

        ddw._append_operation_event(
            self.ledger, operation, "completed", job_id="job-1", token="canary-job-token"
        )
        terminal = ddw._find_operation(self.ledger, operation["operation_id"])
        self.assertNotIn("token", terminal)

    def create_ambiguous(self) -> dict:
        operation = ddw._prepare_operation(
            ledger_path=self.ledger,
            endpoint=ddw.EDITS_ENDPOINT,
            out_key=str(self.root / "out.png"),
            prompt_hash="prompt",
            payload_hash="payload",
            metadata={"n": 1},
        )
        ddw._append_operation_event(
            self.ledger, operation, "submit_failed_unknown", error="timeout"
        )
        return operation

    def test_inspect_redacts_job_token(self) -> None:
        operation = self.create_ambiguous()
        ddw._append_operation_event(
            self.ledger,
            operation,
            "submitted",
            job_id="job-1",
            token="do-not-print",
        )
        args = argparse.Namespace(
            job_ledger=str(self.ledger),
            no_job_ledger=False,
            operation_id=operation["operation_id"],
            out=None,
        )

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            ddw._inspect(args)

        self.assertNotIn("do-not-print", output.getvalue())
        parsed = json.loads(output.getvalue())
        self.assertTrue(parsed[0]["has_token"])
        self.assertEqual("job-1", parsed[0]["job_id"])

    def test_inspect_redacts_token_embedded_in_error_url(self) -> None:
        operation = self.create_ambiguous()
        ddw._append_operation_event(
            self.ledger,
            operation,
            "submitted",
            job_id="job-1",
            token="embedded-secret",
        )
        ddw._append_operation_event(
            self.ledger,
            operation,
            "submit_failed_unknown",
            error="GET https://example.test/job?token=embedded-secret timed out",
        )
        args = argparse.Namespace(
            job_ledger=str(self.ledger),
            no_job_ledger=False,
            operation_id=operation["operation_id"],
            out=None,
        )

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            ddw._inspect(args)

        self.assertNotIn("embedded-secret", output.getvalue())
        self.assertIn("<redacted>", output.getvalue())

    def test_inspect_does_not_migrate_or_write_ledger(self) -> None:
        original = (
            '{"event":"submitted","operation_id":"op-readonly",'
            '"job_id":"job-1","token":"plaintext-token"}\n'
        )
        self.ledger.write_text(original, encoding="utf-8")
        args = argparse.Namespace(
            job_ledger=str(self.ledger),
            no_job_ledger=False,
            operation_id="op-readonly",
            out=None,
        )

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            ddw._inspect(args)

        self.assertEqual(original, self.ledger.read_text(encoding="utf-8"))
        self.assertFalse(Path(str(self.ledger) + ".lock").exists())
        self.assertNotIn("plaintext-token", output.getvalue())

    def test_recover_ambiguous_without_handle_never_polls_or_submits(self) -> None:
        operation = self.create_ambiguous()
        args = argparse.Namespace(
            job_ledger=str(self.ledger),
            no_job_ledger=False,
            operation_id=operation["operation_id"],
            base_url="https://example.test",
            api_key_env=None,
            with_auth=False,
            out=None,
            out_dir=None,
            output_format=None,
            raw_response_out=None,
            force=False,
            poll_interval=0,
            timeout_seconds=1,
            request_timeout=1,
            max_polls=1,
            quiet=True,
            poll_retries=0,
            retry_delay=0,
            done_without_image_polls=1,
        )

        with patch.object(ddw, "_poll_job") as poll, self.assertRaisesRegex(
            ddw.CliError, "no captured job handle"
        ):
            ddw._recover(args)
        poll.assert_not_called()

    def test_recover_poll_timeout_stays_recoverable(self) -> None:
        operation = self.create_ambiguous()
        ddw._append_operation_event(
            self.ledger, operation, "submitted", job_id="job-1", token="token-1"
        )
        args = argparse.Namespace(
            job_ledger=str(self.ledger),
            no_job_ledger=False,
            operation_id=operation["operation_id"],
            base_url="https://example.test",
            api_key_env=None,
            with_auth=False,
            out=None,
            out_dir=None,
            output_format=None,
            raw_response_out=None,
            force=False,
            poll_interval=0,
            timeout_seconds=1,
            request_timeout=1,
            max_polls=1,
            quiet=True,
            poll_retries=0,
            retry_delay=0,
            done_without_image_polls=1,
        )
        with patch.object(ddw, "_poll_job", side_effect=ddw.CliError("poll timeout")):
            with self.assertRaisesRegex(ddw.CliError, "poll timeout"):
                ddw._recover(args)

        snapshot = ddw._find_operation(self.ledger, operation["operation_id"])
        self.assertEqual("submitted", snapshot["state"])
        self.assertEqual("poll_interrupted", snapshot["event"])

    def test_resume_skips_newer_ambiguous_operation_and_uses_captured_handle(self) -> None:
        out_key = str(self.root / "out.png")
        records = [
            {
                "ts": "2026-07-10T01:00:00Z",
                "event": "submit_started",
                "endpoint": ddw.EDITS_ENDPOINT,
                "out_key": out_key,
                "prompt_hash": "prompt",
                "payload_hash": "payload",
            },
            {
                "ts": "2026-07-10T01:00:01Z",
                "event": "submitted",
                "endpoint": ddw.EDITS_ENDPOINT,
                "out_key": out_key,
                "prompt_hash": "prompt",
                "payload_hash": "payload",
                "job_id": "job-good",
                "token": "token-good",
            },
            {
                "ts": "2026-07-10T01:01:00Z",
                "event": "submit_started",
                "endpoint": ddw.EDITS_ENDPOINT,
                "out_key": out_key,
                "prompt_hash": "prompt",
                "payload_hash": "payload",
            },
            {
                "ts": "2026-07-10T01:01:01Z",
                "event": "submit_failed_unknown",
                "endpoint": ddw.EDITS_ENDPOINT,
                "out_key": out_key,
                "prompt_hash": "prompt",
                "payload_hash": "payload",
                "error": "timeout",
            },
        ]
        with self.ledger.open("w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")
        args = argparse.Namespace(resume_existing=True)

        with patch.object(ddw, "_run_submitted_job", return_value=[]) as run:
            resumed = ddw._resume_existing_if_requested(
                args=args,
                ledger_path=self.ledger,
                endpoint=ddw.EDITS_ENDPOINT,
                out_key=out_key,
                prompt_hash="prompt",
                base_url="https://example.test",
                api_key=None,
                expected_count=1,
                output_format=None,
            )

        self.assertTrue(resumed)
        self.assertEqual("job-good", run.call_args.kwargs["submit_response"]["id"])

    def test_resolve_not_created_allows_a_clean_retry(self) -> None:
        operation = self.create_ambiguous()
        args = argparse.Namespace(
            job_ledger=str(self.ledger),
            no_job_ledger=False,
            operation_id=operation["operation_id"],
            not_created=True,
            evidence="server request returned 400 before store.Submit",
        )

        ddw._resolve(args)

        resolved = ddw._find_operation(self.ledger, operation["operation_id"])
        self.assertEqual("not_created", resolved["state"])
        retry = ddw._prepare_operation(
            ledger_path=self.ledger,
            endpoint=ddw.EDITS_ENDPOINT,
            out_key=str(self.root / "out.png"),
            prompt_hash="prompt",
            payload_hash="payload-2",
            metadata={},
        )
        self.assertNotEqual(operation["operation_id"], retry["operation_id"])

if __name__ == "__main__":
    unittest.main()
