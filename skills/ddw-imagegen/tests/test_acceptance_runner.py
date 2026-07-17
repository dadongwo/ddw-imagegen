import json
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "run_acceptance_tests.py"
SPEC = importlib.util.spec_from_file_location("ddw_acceptance_runner", RUNNER)
RUNNER_MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(RUNNER_MODULE)


class AcceptanceRunnerTests(unittest.TestCase):
    def test_transparent_preflight_can_select_a_bundled_pillow_runtime(self):
        current = Path(sys.executable).resolve()
        bundled = current.parent / "bundled-python.exe"
        with mock.patch.object(RUNNER_MODULE, "_pillow_python_candidates", return_value=[current, bundled]), mock.patch.object(
            RUNNER_MODULE.subprocess, "run"
        ) as run:
            run.side_effect = [
                subprocess.CompletedProcess([], 1, "", "missing PIL"),
                subprocess.CompletedProcess([], 0, "", ""),
            ]
            self.assertEqual(RUNNER_MODULE._select_pillow_python(), bundled)

    def test_dry_run_covers_integrated_offline_workflows_without_ledger_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "acceptance"
            env = os.environ.copy()
            env["DDW_IMAGE_API_KEY"] = "offline_acceptance_only"
            result = subprocess.run(
                [sys.executable, str(RUNNER), "--dry-run", "--out-dir", str(out_dir)],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env=env,
                timeout=60,
            )

            self.assertEqual(result.returncode, 0, result.stdout)
            for label in (
                "dry-run generation",
                "dry-run edit",
                "dry-run variants",
                "dry-run composite",
                "transparent preflight",
                "project delivery",
                "failure and delivery contracts",
            ):
                self.assertIn(label, result.stdout.lower())

            self.assertFalse((out_dir / "jobs.jsonl").exists())
            variants = (out_dir / "logs" / "dry_variants.log").read_text(encoding="utf-8")
            self.assertEqual(json.loads(variants)["payload"]["n"], 3)

            composite = (out_dir / "logs" / "dry_composite.log").read_text(encoding="utf-8")
            self.assertEqual(len(json.loads(composite)["files"]), 2)

            summary = json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(
                summary["offline_cases"],
                [
                    "generation",
                    "edit",
                    "variants",
                    "composite",
                    "transparent_preflight",
                    "project_delivery",
                    "failure_and_delivery_contracts",
                ],
            )
            contracts = (out_dir / "logs" / "failure_and_delivery_contracts.log").read_text(encoding="utf-8")
            self.assertIn("OK", contracts)

            project_root = out_dir / "project-contract"
            asset = project_root / "src" / "assets" / "future-hero-v1.png"
            consumer = project_root / "src" / "config.ts"
            self.assertTrue(asset.is_file())
            consumer_text = consumer.read_text(encoding="utf-8")
            self.assertIn("assets/future-hero-v1.png", consumer_text)
            self.assertNotIn("assets/old-hero.png", consumer_text)
            project_log = json.loads((out_dir / "logs" / "project_delivery_contract.json").read_text(encoding="utf-8"))
            self.assertEqual(Path(project_log["asset"]).resolve(), asset.resolve())
            self.assertTrue(project_log["consumer_reference_verified"])


if __name__ == "__main__":
    unittest.main()
