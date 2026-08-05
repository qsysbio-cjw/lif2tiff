from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np


MODULE_DIR = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(MODULE_DIR))

from liftool_gui import (
    LifToolApp,
    _normalize_frame,
    _render_overlay,
    _render_single,
    _validated_zoom_percent,
    build_dry_run_report,
    discover_lif_paths,
)


class GuiInputDiscoveryTests(unittest.TestCase):
    def test_discovers_folder_lifs_recursively_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "folder with spaces"
            nested = root / "nested"
            nested.mkdir(parents=True)
            first = root / "A.lif"
            second = nested / "B.LIF"
            first.touch()
            second.touch()
            (nested / "notes.txt").touch()

            discovered = discover_lif_paths([first, root, second])

            self.assertEqual(discovered, [first.resolve(), second.resolve()])

    def test_ignores_missing_paths_and_non_lif_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            text_file = root / "not-an-image.txt"
            text_file.touch()

            discovered = discover_lif_paths([text_file, root / "missing"])

            self.assertEqual(discovered, [])


class GuiDryRunTests(unittest.TestCase):
    def _plan(self, route: str = "standard") -> dict:
        return {
            "channel_resolution": {
                "workflow": {"route": route},
                "channels": [
                    {"channel_key": "C00", "confirmed_assignment": None},
                    {"channel_key": "C01", "confirmed_assignment": "brightfield"},
                ],
            },
            "summary": {
                "series_count": 3,
                "output_unit_count": 6,
                "plane_count": 12,
                "estimated_uncompressed_pixel_bytes": 4096,
            },
        }

    def test_ready_dry_run_does_not_create_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "new-output"

            report = build_dry_run_report(
                self._plan(),
                output,
                review_acknowledged=False,
                resume=False,
                channel_overrides={0: "bodipy_493_503"},
            )

            self.assertEqual(report["status"], "ready")
            self.assertEqual(report["output_state"], "new")
            self.assertFalse(output.exists())

    def test_dry_run_blocks_review_and_existing_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "existing-output"
            output.mkdir()

            report = build_dry_run_report(
                self._plan("review"),
                output,
                review_acknowledged=False,
                resume=False,
                channel_overrides={},
            )

            self.assertEqual(report["status"], "blocked")
            self.assertEqual(report["output_state"], "complete_exists")
            self.assertEqual(len(report["errors"]), 2)
            self.assertEqual(report["unconfirmed_channels"], ["C00"])


class GuiRenderingTests(unittest.TestCase):
    def test_zoom_percent_accepts_manual_values_and_clamps_limits(self):
        self.assertEqual(_validated_zoom_percent("275"), 275.0)
        self.assertEqual(_validated_zoom_percent("1"), 5.0)
        self.assertEqual(_validated_zoom_percent("20000"), 10000.0)
        self.assertEqual(_validated_zoom_percent("invalid", 160), 160.0)

    def test_default_contrast_separates_source_display_from_full_range(self):
        app = object.__new__(LifToolApp)
        output = {
            "file": {"dtype": "uint16"},
            "source_display_settings": {
                "value_min": 0,
                "value_max": 65535,
                "black": 7,
                "white": 23908,
                "gamma": 1,
                "source": "leica_channel_scaling",
            },
        }

        settings = app._default_channel_contrast(output)

        self.assertEqual(settings["value_max"], 65535)
        self.assertEqual(settings["source_black"], 7)
        self.assertEqual(settings["source_white"], 23908)
        self.assertEqual(settings["mode"], "Source display")

    def test_single_channel_manual_contrast_returns_rgb_image(self):
        frame = np.arange(16, dtype=np.uint8).reshape(4, 4)

        image = _render_single(frame, 0, 15, color=(0.0, 1.0, 0.0))
        array = np.asarray(image)

        self.assertEqual(image.mode, "RGB")
        self.assertEqual(image.size, (4, 4))
        self.assertEqual(int(array[..., 0].max()), 0)
        self.assertEqual(int(array[..., 1].max()), 255)

    def test_normalization_handles_constant_frame(self):
        frame = np.full((3, 3), 7, dtype=np.uint8)

        normalized = _normalize_frame(frame, 7, 7)

        self.assertTrue(np.isfinite(normalized).all())
        self.assertTrue((normalized == 0).all())

    def test_normalization_uses_fixed_16_bit_range(self):
        frame = np.asarray([[0, 32768, 65535]], dtype=np.uint16)

        normalized = _normalize_frame(frame, 0, 65535)

        self.assertAlmostEqual(float(normalized[0, 0]), 0.0)
        self.assertAlmostEqual(float(normalized[0, 1]), 0.5, places=4)
        self.assertAlmostEqual(float(normalized[0, 2]), 1.0)

    def test_overlay_combines_brightfield_and_fluorescence(self):
        frames = {
            0: np.arange(16, dtype=np.uint8).reshape(4, 4),
            1: np.flipud(np.arange(16, dtype=np.uint8).reshape(4, 4)),
        }
        channel_info = {
            0: {
                "physical_label": "brightfield",
                "identity": {"modality": {"value": "brightfield"}},
            },
            1: {
                "source_lut": "Green",
                "identity": {"modality": {"value": "fluorescence"}},
            },
        }

        image = _render_overlay(frames, channel_info, 1, (0, 15))
        array = np.asarray(image)

        self.assertEqual(image.mode, "RGB")
        self.assertEqual(image.size, (4, 4))
        self.assertGreater(int(array[..., 1].max()), int(array[..., 2].max()))

    def test_overlay_accepts_independent_fixed_channel_settings(self):
        frames = {
            0: np.asarray([[0, 50]], dtype=np.uint8),
            1: np.asarray([[100, 200]], dtype=np.uint8),
        }
        channel_info = {
            0: {
                "physical_label": "brightfield",
                "identity": {"modality": {"value": "brightfield"}},
            },
            1: {
                "source_lut": "Green",
                "identity": {"modality": {"value": "fluorescence"}},
            },
        }
        settings = {
            0: {"black": 0, "white": 100, "gamma": 1.0},
            1: {"black": 100, "white": 200, "gamma": 1.0},
        }

        image = _render_overlay(
            frames,
            channel_info,
            visible_channels={1},
            settings_by_channel=settings,
        )
        array = np.asarray(image)

        self.assertEqual(int(array[0, 0, 1]), 0)
        self.assertGreater(int(array[0, 1, 1]), 220)
        self.assertGreater(int(array[..., 1].max()), int(array[..., 2].max()))


if __name__ == "__main__":
    unittest.main()
