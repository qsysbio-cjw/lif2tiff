from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from app import (
        discover_lifs,
        dry_run_report,
        find_batch_output_collisions,
        PresentationExportDialog,
        QtWorkbench,
        series_table_rows,
        suggested_assignment,
        unconfirmed_channel_keys,
    )
    from stage_map_view import expand_plate_grid, stage_map_points
except ModuleNotFoundError as exc:
    if exc.name == "PySide6":
        raise unittest.SkipTest("isolated PySide6 GUI dependency is unavailable") from exc
    raise


def sample_plan(route: str = "standard") -> dict:
    return {
        "channel_resolution": {
            "workflow": {"route": route},
            "channels": [
                {"channel_key": "C00", "confirmed_assignment": None},
                {"channel_key": "C01", "confirmed_assignment": "brightfield"},
            ],
        },
        "summary": {
            "series_count": 2,
            "output_unit_count": 4,
            "plane_count": 8,
            "estimated_uncompressed_pixel_bytes": 4096,
        },
    }


class QtGuiCoreTests(unittest.TestCase):
    def test_explicit_unknown_satisfies_channel_confirmation_gate(self):
        plan = sample_plan()

        self.assertEqual(unconfirmed_channel_keys(plan, {}), ["C00"])
        self.assertEqual(unconfirmed_channel_keys(plan, {0: "unknown"}), [])

    def test_confirmed_plate_calibration_expands_to_384_wells(self):
        calibration = {
            "plate": {"rows": 16, "columns": 24},
            "x_axis": {
                "first_grid_index": 13,
                "fit": {
                    "first_left_or_top_boundary_um": 64711.968,
                    "pitch_um": 4499.945,
                    "well_opening_um": 3341.921,
                },
            },
            "y_axis": {
                "first_grid_index": 5,
                "fit": {
                    "first_left_or_top_boundary_um": 24027.49,
                    "pitch_um": 4495.687,
                    "well_opening_um": 3252.028,
                },
            },
        }

        columns, rows = expand_plate_grid(calibration)

        self.assertEqual((len(columns), len(rows)), (24, 16))
        self.assertAlmostEqual(columns[12]["start_um"], 64711.968)
        self.assertAlmostEqual(rows[4]["start_um"], 24027.49)

    def test_stage_map_points_use_physical_fov_dimensions(self):
        plan = {
            "series": [
                {
                    "series_index": 0,
                    "series_name": "Image001",
                    "stage_position": {"x_m": 0.01, "y_m": 0.02, "z_m": 0.003},
                    "dimensions": {"x": 512, "y": 256},
                    "pixel_size": {"x_um_per_px": 0.5, "y_um_per_px": 0.25},
                }
            ]
        }

        points = stage_map_points(plan, Path("sample.lif"))

        self.assertEqual(len(points), 1)
        self.assertEqual(points[0]["x_um"], 10000.0)
        self.assertEqual(points[0]["y_um"], 20000.0)
        self.assertEqual(points[0]["width_um"], 256.0)
        self.assertEqual(points[0]["height_um"], 64.0)

    def test_channel_suggestion_prefers_dye_then_brightfield(self):
        self.assertEqual(
            suggested_assignment({"modality": "fluorescence", "inferred_dye": {"id": "nile_red"}}),
            "nile_red",
        )
        self.assertEqual(suggested_assignment({"modality": "brightfield"}), "brightfield")
        self.assertIsNone(suggested_assignment({"modality": "unknown"}))

    def test_series_table_rows_keep_human_browsing_fields(self):
        plan = {
            "series": [
                {
                    "series_index": 3,
                    "series_name": "Image004",
                    "dimension_type": "T",
                    "dimensions": {"x": 512, "y": 256},
                }
            ]
        }

        self.assertEqual(series_table_rows(plan), [(4, "Image004", "T", "512 x 256")])

    def test_recursive_discovery_is_deduplicated(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            nested = root / "nested"
            nested.mkdir()
            first = root / "A.lif"
            second = nested / "B.LIF"
            first.touch()
            second.touch()

            discovered = discover_lifs([first, root, second])

            self.assertEqual(discovered, [first.resolve(), second.resolve()])

    def test_batch_output_collisions_are_windows_case_insensitive(self):
        collisions = find_batch_output_collisions(
            [
                (Path("/data/A.lif"), Path("/data/Sample_tiff")),
                (Path("/data/B.lif"), Path("/data/sample_tiff")),
                (Path("/other/C.lif"), Path("/other/sample_tiff")),
            ]
        )

        self.assertEqual(len(collisions), 1)
        self.assertEqual(
            collisions[0]["sources"],
            [Path("/data/A.lif"), Path("/data/B.lif")],
        )

    def test_dry_run_ready_does_not_create_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "new-output"

            report = dry_run_report(
                sample_plan(),
                output,
                review_acknowledged=False,
                resume=False,
                channel_overrides={0: "bodipy_493_503"},
            )

            self.assertEqual(report["status"], "ready")
            self.assertEqual(report["output_state"], "new")
            self.assertFalse(output.exists())

    def test_dry_run_blocks_unacknowledged_review_and_existing_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "existing-output"
            output.mkdir()

            report = dry_run_report(
                sample_plan("review"),
                output,
                review_acknowledged=False,
                resume=False,
                channel_overrides={},
            )

            self.assertEqual(report["status"], "blocked")
            self.assertEqual(report["output_state"], "complete_exists")
            self.assertEqual(len(report["errors"]), 3)
            self.assertEqual(report["unconfirmed_channels"], ["C00"])

    def test_dry_run_blocks_unconfirmed_channel_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            report = dry_run_report(
                sample_plan(),
                Path(temporary) / "new-output",
                review_acknowledged=False,
                resume=False,
                channel_overrides={},
            )

            self.assertEqual(report["status"], "blocked")
            self.assertEqual(report["unconfirmed_channels"], ["C00"])
            self.assertTrue(any("explicit confirmation" in item for item in report["errors"]))


class QtPreviewSchedulingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def test_scrubbing_displays_completed_intermediate_then_requests_latest(self):
        window = QtWorkbench()
        try:
            window.plan = {
                "series": [
                    {
                        "series_index": 0,
                        "series_name": "Time",
                        "dimensions": {"x": 4, "y": 3, "z": 1, "t": 10},
                    }
                ]
            }
            window.series_combo.addItem("001 Time", 0)
            window.z_spin.setRange(0, 0)
            window.time_slider.setRange(0, 9)
            window.time_slider.setValue(5)
            window.preview_token = 7
            window.pending_frame_key = (0, 0, 2)
            composed = []
            requested = []
            window.compose_current_frames = lambda: composed.append(window.current_frame_key)
            window.render_frame = lambda: requested.append(window.time_slider.value())

            window._preview_ready(
                (7, (0, 0, 2), {0: np.zeros((3, 4), dtype=np.uint16)})
            )
            self.app.processEvents()

            self.assertEqual(composed, [(0, 0, 2)])
            self.assertEqual(requested, [5])
            self.assertEqual(window.current_frame_key, (0, 0, 2))
        finally:
            window.close()

    def test_non_plane_messages_do_not_reset_conversion_progress(self):
        window = QtWorkbench()
        try:
            window._conversion_progress(
                "Batch 1/1 | Plane progress: 10/100 | Overall planes 10/100"
            )
            self.assertEqual(window.progress.value(), 10)

            window._conversion_progress("Batch 1/1 | Series 2/10: series-1 (2D)")
            window._conversion_progress(
                "Batch 1/1 | TIFF progress: 2/10 | Overall TIFF 2/10"
            )

            self.assertEqual(window.progress.value(), 10)
        finally:
            window.close()

    def test_resume_mode_changes_primary_action_label(self):
        window = QtWorkbench()
        try:
            self.assertEqual(window.convert_button.text(), "Convert")
            window.resume_check.setChecked(True)
            self.app.processEvents()
            self.assertEqual(window.convert_button.text(), "Resume")
            window.resume_check.setChecked(False)
            self.app.processEvents()
            self.assertEqual(window.convert_button.text(), "Convert")
        finally:
            window.close()

    def test_presentation_dialog_accepts_nested_output_parent(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary) / "nested" / "report outputs"
            parent.mkdir(parents=True)
            plan = {
                "series": [
                    {
                        "series_index": 0,
                        "dimensions": {"z": 1, "t": 1},
                        "outputs": [
                            {
                                "channel_index": 0,
                                "physical_label": "green",
                            }
                        ],
                    }
                ]
            }
            dialog = PresentationExportDialog(
                plan,
                {0},
                True,
                parent,
                "new presentation",
            )
            try:
                self.assertEqual(
                    dialog.output_path(), (parent / "new presentation").resolve()
                )
                dialog._accept_if_valid()
                self.assertEqual(dialog.result(), dialog.DialogCode.Accepted)
                self.assertFalse(dialog.output_path().exists())
            finally:
                dialog.close()


if __name__ == "__main__":
    unittest.main()
