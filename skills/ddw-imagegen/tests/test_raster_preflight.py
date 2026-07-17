from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import unittest

from PIL import Image


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "raster_preflight.py"


class RasterPreflightTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        spec = importlib.util.spec_from_file_location("raster_preflight", SCRIPT)
        assert spec and spec.loader
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.source = self.root / "source.png"
        image = Image.new("RGB", (120, 80), (30, 90, 160))
        image.save(self.source, "PNG")

    def test_compress_reference_preserves_dimensions(self) -> None:
        output = self.root / "upload.jpg"
        result = self.module.compress_reference(self.source, output, quality=82)

        self.assertEqual((120, 80), result["size"])
        self.assertEqual("JPEG", result["format"])
        self.assertTrue(output.exists())
        with Image.open(output) as image:
            self.assertEqual((120, 80), image.size)

    def test_normalize_long_edge_preserves_aspect_ratio(self) -> None:
        output = self.root / "normalized.png"
        result = self.module.normalize_long_edge(
            self.source, output, target=240, allow_upscale=True
        )

        self.assertEqual((240, 160), result["size"])
        with Image.open(output) as image:
            self.assertEqual((240, 160), image.size)

    def test_normalize_does_not_upscale_by_default(self) -> None:
        output = self.root / "not-upscaled.png"
        result = self.module.normalize_long_edge(self.source, output, target=240)
        self.assertEqual((120, 80), result["size"])

    def test_source_and_output_must_differ(self) -> None:
        with self.assertRaises(ValueError):
            self.module.compress_reference(self.source, self.source)

    def test_webp_output_preserves_alpha(self) -> None:
        source = self.root / "alpha.png"
        Image.new("RGBA", (80, 60), (10, 20, 30, 100)).save(source, "PNG")
        output = self.root / "alpha.webp"
        self.module.normalize_long_edge(source, output, target=80)
        with Image.open(output) as image:
            self.assertEqual("WEBP", image.format)
            self.assertIn("A", image.mode)

    def test_prepare_reference_automatically_reduces_large_upload(self) -> None:
        source = self.root / "large.png"
        Image.effect_noise((900, 900), 100).convert("RGB").save(source, "PNG")
        max_bytes = 80_000
        prepared = self.module.prepare_reference(
            source, self.root / "cache", max_bytes=max_bytes
        )
        self.assertNotEqual(source, prepared)
        self.assertLessEqual(prepared.stat().st_size, max_bytes)
        with Image.open(prepared) as image:
            self.assertEqual(image.width, image.height)
            self.assertLessEqual(image.width, 900)

    def test_prepare_reference_preserves_alpha_while_meeting_limit(self) -> None:
        source = self.root / "large-alpha.png"
        noise = Image.effect_noise((800, 800), 100).convert("L")
        image = Image.merge("RGBA", (noise, noise, noise, noise))
        image.save(source, "PNG")
        max_bytes = 70_000

        prepared = self.module.prepare_reference(
            source, self.root / "cache", max_bytes=max_bytes
        )

        self.assertLessEqual(prepared.stat().st_size, max_bytes)
        with Image.open(prepared) as result:
            self.assertIn("A", result.mode)

    def test_prepare_reference_does_not_reuse_oversized_cache(self) -> None:
        source = self.root / "large.png"
        Image.effect_noise((700, 700), 100).convert("RGB").save(source, "PNG")
        cache = self.root / "cache"
        first = self.module.prepare_reference(source, cache, max_bytes=100_000)
        first.write_bytes(b"x" * 120_000)

        prepared = self.module.prepare_reference(source, cache, max_bytes=100_000)

        self.assertLessEqual(prepared.stat().st_size, 100_000)
        with Image.open(prepared) as result:
            result.verify()

    def test_prepare_reference_rejects_excessive_pixel_count(self) -> None:
        source = self.root / "too-many-pixels.png"
        Image.new("RGB", (200, 200), (10, 20, 30)).save(source, "PNG")
        original_limit = self.module.MAX_REFERENCE_PIXELS
        self.module.MAX_REFERENCE_PIXELS = 10_000
        self.addCleanup(
            setattr, self.module, "MAX_REFERENCE_PIXELS", original_limit
        )

        with self.assertRaisesRegex(ValueError, "pixel|dimensions|large"):
            self.module.prepare_reference(
                source, self.root / "cache", max_bytes=1
            )


if __name__ == "__main__":
    unittest.main()
