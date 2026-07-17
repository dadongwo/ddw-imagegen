from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "remove_chroma_key.py"


class RemoveChromaKeyCliTests(unittest.TestCase):
    def test_check_validates_current_pillow_runtime_without_writing_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "--check"],
                cwd=temp_dir,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Pillow runtime ready", result.stdout)
            self.assertEqual(list(Path(temp_dir).iterdir()), [])

    def test_missing_pillow_error_has_windows_and_unix_bundled_runtime_commands(self) -> None:
        result = subprocess.run(
            [sys.executable, "-S", str(SCRIPT), "--check"],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Pillow is required", result.stderr)
        self.assertIn("%USERPROFILE%", result.stderr)
        self.assertIn("python.exe", result.stderr)
        self.assertIn("$HOME/", result.stderr)
        self.assertIn("/bin/python", result.stderr)
        self.assertIn("--check", result.stderr)
        self.assertNotIn("source .venv/bin/activate", result.stderr)

    def test_removes_key_background_and_keeps_opaque_subject_edge(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = Path(temp_dir) / "input.png"
            output_path = Path(temp_dir) / "output.png"
            image = Image.new("RGB", (5, 5), (0, 255, 0))
            for y in range(1, 4):
                for x in range(1, 4):
                    image.putpixel((x, y), (220, 20, 30))
            image.save(input_path)

            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--input",
                    str(input_path),
                    "--out",
                    str(output_path),
                    "--key-color",
                    "#00ff00",
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            with Image.open(output_path) as output:
                rgba = output.convert("RGBA")
                self.assertEqual(rgba.getpixel((0, 2))[3], 0)
                self.assertEqual(rgba.getpixel((1, 2)), (220, 20, 30, 255))
                self.assertEqual(rgba.getpixel((2, 2)), (220, 20, 30, 255))


if __name__ == "__main__":
    unittest.main()
