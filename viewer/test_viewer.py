from __future__ import annotations

import os
import unittest
from pathlib import Path

import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from viewer import (
        ViewerWindow,
        clamp,
        format_elapsed,
        lut_color,
        normalize,
        recommended_playback_fps,
    )
except ModuleNotFoundError as exc:
    if exc.name == "PySide6":
        raise unittest.SkipTest("isolated PySide6 benchmark dependency is unavailable") from exc
    raise


class ViewerMathTests(unittest.TestCase):
    def test_elapsed_time_format(self):
        self.assertEqual(format_elapsed(0), "00:00.00")
        self.assertEqual(format_elapsed(61.25), "01:01.25")
        self.assertEqual(format_elapsed(3661.5), "1:01:01.50")

    def test_recommended_playback_targets_about_25_seconds(self):
        self.assertEqual(recommended_playback_fps(5), 1)
        self.assertEqual(recommended_playback_fps(100), 4)
        self.assertEqual(recommended_playback_fps(600), 24)
        self.assertEqual(recommended_playback_fps(10000), 120)

    def test_zoom_limits(self):
        self.assertEqual(clamp(0.01, 0.05, 100.0), 0.05)
        self.assertEqual(clamp(2.5, 0.05, 100.0), 2.5)
        self.assertEqual(clamp(200.0, 0.05, 100.0), 100.0)

    def test_uint16_source_display_normalization(self):
        frame = np.asarray([[0, 11954, 23908]], dtype=np.uint16)
        values = normalize(frame, 0, 23908, 1.0)

        self.assertAlmostEqual(float(values[0, 0]), 0.0)
        self.assertAlmostEqual(float(values[0, 1]), 0.5, places=4)
        self.assertAlmostEqual(float(values[0, 2]), 1.0)

    def test_lut_color_uses_source_label(self):
        green = lut_color("Green", 0)
        self.assertGreater(float(green[1]), float(green[0]))
        self.assertGreater(float(green[1]), float(green[2]))


class ViewerRealLifTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def test_frap_time_metadata_loads_into_viewer(self):
        source = (
            Path(__file__).resolve().parents[2]
            / "lif2tiff_reader_comparison/fixtures/public/ome_frap_150519"
            / "150519_FRAP_test_ROIs_chromagreen.lif"
        )
        if not source.is_file():
            self.skipTest("public FRAP fixture is unavailable")
        window = ViewerWindow()
        try:
            window.load_lif(source)
            self.assertIsNone(window.load_error)
            self.assertEqual(window.time_slider.maximum() + 1, 5)
            self.assertAlmostEqual(window.time_increment_s, 1.627, places=6)
            self.assertAlmostEqual(window.configured_cycle_s, 1.61728571428571)
            self.assertAlmostEqual(window.display_time_interval_s, 1.61728571428571)
            self.assertEqual(window.fps_spin.value(), 1.0)
            self.assertEqual((window.time_spin.minimum(), window.time_spin.maximum()), (1, 5))
            window.time_spin.setValue(5)
            self.assertEqual(window.time_slider.value(), 4)
            self.assertEqual(window.time_total_label.text(), "/ 5")
        finally:
            window.close()

    def test_real_zt_series_navigates_z_and_t_independently(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "tests/fixtures/public/ome_pr2729/output_collision/Sample.lif"
        )
        if not source.is_file():
            self.skipTest("public ZT fixture is unavailable")
        window = ViewerWindow()
        try:
            window.load_lif(source)
            self.assertIsNone(window.load_error)
            self.assertEqual(window.z_slider.maximum() + 1, 3)
            self.assertEqual(window.time_slider.maximum() + 1, 2)
            self.assertAlmostEqual(window.z_spacing_um, 6.227615)
            window.z_slider.setValue(2)
            window.time_slider.setValue(1)
            self.assertEqual(window.current_frame_key, (0, 2, 1))
            self.assertEqual(set(window.current_frames), {0, 1})
            self.assertIn("Z plane 3 / 3", window.z_summary.text())
        finally:
            window.close()

    def test_contrast_sync_can_be_disabled_and_reset(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "tests/fixtures/public/ome_pr2729/output_collision/Sample.lif"
        )
        if not source.is_file():
            self.skipTest("public multi-series fixture is unavailable")
        window = ViewerWindow()
        try:
            window.load_lif(source)
            self.assertTrue(window.sync_contrast.isChecked())
            window.black_spin.setValue(10)
            window.white_spin.setValue(200)
            window.series_combo.setCurrentIndex(1)
            self.assertEqual(window.channel_states[0]["mode"], "Manual")
            self.assertEqual(window.channel_states[0]["black"], 10)
            self.assertEqual(window.channel_states[0]["white"], 200)

            window.sync_contrast.setChecked(False)
            window.black_spin.setValue(20)
            window.series_combo.setCurrentIndex(0)
            self.assertEqual(window.channel_states[0]["black"], 10)

            window.reset_lif_contrast()
            self.assertTrue(
                all(
                    state["mode"] == "Source display"
                    and state["black"] == state["source_black"]
                    and state["white"] == state["source_white"]
                    for state in window.series_channel_states.values()
                )
            )
        finally:
            window.close()

    def test_playback_waits_for_pending_background_frame(self):
        window = ViewerWindow()
        try:
            window.time_slider.setRange(0, 4)
            window.time_slider.setValue(0)
            window.pending_frame_key = (0, 0, 1)

            window.advance_time()
            self.assertEqual(window.time_slider.value(), 0)

            window.pending_frame_key = None
            window.advance_time()
            self.assertEqual(window.time_slider.value(), 1)
        finally:
            window.close()


if __name__ == "__main__":
    unittest.main()
