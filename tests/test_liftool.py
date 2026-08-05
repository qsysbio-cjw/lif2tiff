from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import tifffile


MODULE_DIR = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(MODULE_DIR))

from liftool import (
    ConversionCancelled,
    LifToolError,
    _default_output,
    build_conversion_plan,
    convert_lif,
    prompt_channel_assignments,
    validate_project,
)


class FakeImage:
    def __init__(
        self,
        name: str,
        *,
        x: int = 7,
        y: int = 5,
        z: int = 1,
        t: int = 1,
        channels: int = 2,
        bit_depth: int = 8,
        fail_after: int | None = None,
    ):
        self.name = name
        self.dims = SimpleNamespace(x=x, y=y, z=z, t=t)
        self.channels = channels
        self.bit_depth = tuple(bit_depth for _ in range(channels))
        self.numpy_dtype = np.uint16 if bit_depth > 8 else np.uint8
        self.scale = (2.0, 2.0, 0.5 if z > 1 else 0.0)
        self.settings = {
            "StagePosX": "0.01",
            "StagePosY": "0.02",
            "ZPosition": "0.003",
            "ObjectiveName": "test objective",
        }
        self.time_points_s = [float(index) for index in range(t)]
        self.acquisition_timestamps = []
        self.fail_after = fail_after
        self.frame_calls = 0

    def get_frame(self, *, z: int, t: int, c: int):
        self.frame_calls += 1
        if self.fail_after is not None and self.frame_calls > self.fail_after:
            raise RuntimeError("simulated interruption")
        value = c * 100 + t * 10 + z
        return np.full((self.dims.y, self.dims.x), value, dtype=self.numpy_dtype)


class FakeLif:
    def __init__(self, images):
        self.images = images
        self.num_images = len(images)
        self.xml_root = ET.fromstring(
            "<Root>"
            + "".join(f'<Element Name="{image.name}" />' for image in images)
            + "</Root>"
        )

    def get_iter_image(self):
        return iter(self.images)


def fake_factory(images):
    return lambda _path: FakeLif(images)


def convert_fixture(*args, **kwargs):
    """Synthetic fixtures lack detector metadata and intentionally use review flow."""
    kwargs.setdefault("allow_review", True)
    return convert_lif(*args, **kwargs)


class LifToolConversionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "synthetic.lif"
        self.source.write_bytes(b"synthetic-lif-placeholder")

    def tearDown(self):
        self.temporary.cleanup()

    def test_all_dimension_layouts_and_relocation(self):
        images = [
            FakeImage("2D / unsafe name"),
            FakeImage("Zstack", z=3),
            FakeImage("Time", t=3),
            FakeImage("ZT", z=2, t=2),
        ]
        output = self.root / "project"

        result = convert_fixture(
            self.source,
            output,
            lif_factory=fake_factory(images),
        )

        self.assertEqual(result, output.resolve())
        manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["project_status"], "complete")
        self.assertEqual(len(manifest["source"]["file_sha256"]), 64)
        self.assertEqual(
            [series["dimension_type"] for series in manifest["series"]],
            ["2D", "Z-stack", "time-lapse", "ZT"],
        )
        records = [
            record
            for series in manifest["series"]
            for channel in series["channels"]
            for record in channel["files"]
        ]
        self.assertEqual(len(records), 8)
        self.assertTrue(all(record["status"] == "complete" for record in records))
        self.assertFalse((output / "by_channel").exists())
        self.assertFalse((output / "series").exists())
        self.assertEqual(
            manifest["channel_directories"],
            {"C0": "C00_ch0", "C1": "C01_ch1"},
        )

        by_name = {series["name"]: series for series in manifest["series"]}
        self.assertEqual(
            by_name["2D / unsafe name"]["channels"][0]["files"][0]["path"],
            "C00_ch0/2D_unsafe name.tif",
        )
        for name, axes in (
            ("Zstack", "ZYX"),
            ("Time", "TYX"),
            ("ZT", "TZYX"),
        ):
            record = by_name[name]["channels"][0]["files"][0]
            self.assertEqual(record["axes"], axes)
            self.assertEqual(record["container"], "OME-TIFF")
            self.assertTrue(record["path"].endswith(".ome.tif"))
            with tifffile.TiffFile(output / record["path"]) as tif:
                self.assertTrue(tif.is_ome)
                self.assertEqual(tif.series[0].axes, axes)
                if "T" in axes:
                    self.assertIn('TimeIncrement="1.0"', tif.ome_metadata)

        validation = validate_project(output)
        self.assertTrue(validation["ok"], validation["errors"])
        self.assertEqual(validation["files_checked"], 8)

        moved = self.root / "moved_project"
        shutil.move(output, moved)
        moved_validation = validate_project(moved)
        self.assertTrue(moved_validation["ok"], moved_validation["errors"])

        serialized = (moved / "manifest.json").read_text(encoding="utf-8")
        self.assertNotIn(str(self.root), serialized)

    def test_conversion_plan_is_metadata_only_portable_and_dimension_aware(self):
        images = [
            FakeImage("2D", channels=2),
            FakeImage("Zstack", z=3, channels=2),
            FakeImage("Time", t=3, channels=2),
            FakeImage("ZT", z=2, t=2, channels=2),
        ]

        plan = build_conversion_plan(
            self.source,
            lif_factory=fake_factory(images),
        )

        self.assertEqual(plan["schema_name"], "candida_lif_conversion_plan")
        self.assertEqual(plan["inspection"]["scope"], "metadata_only")
        self.assertFalse(plan["inspection"]["pixels_read"])
        self.assertEqual(sum(image.frame_calls for image in images), 0)
        self.assertEqual(plan["summary"]["series_count"], 4)
        self.assertEqual(
            plan["summary"]["dimension_type_counts"],
            {"2D": 1, "Z-stack": 1, "ZT": 1, "time-lapse": 1},
        )
        self.assertEqual(plan["summary"]["output_unit_count"], 8)
        self.assertEqual(plan["summary"]["plane_count"], 22)
        self.assertEqual(plan["summary"]["estimated_uncompressed_pixel_bytes"], 770)
        self.assertEqual(plan["status"], "review_required")
        self.assertIn("review_acquisition_variants", plan["required_actions"])
        self.assertEqual(
            plan["output_policy"]["master_image_representation"],
            "native-intensity grayscale",
        )
        self.assertFalse(plan["output_policy"]["display_transform_applied"])
        self.assertEqual(plan["source"]["filename"], self.source.name)
        self.assertEqual(
            plan["series"][0]["stage_position"],
            {"x_m": 0.01, "y_m": 0.02, "z_m": 0.003},
        )
        serialized = json.dumps(plan, ensure_ascii=False)
        self.assertNotIn(str(self.root), serialized)

    def test_existing_complete_output_is_never_reused(self):
        output = self.root / "project"
        convert_fixture(
            self.source,
            output,
            lif_factory=fake_factory([FakeImage("Image001")]),
        )

        with self.assertRaisesRegex(LifToolError, "refusing to overwrite"):
            convert_fixture(
                self.source,
                output,
                lif_factory=fake_factory([FakeImage("Image001")]),
            )

    def test_core_conversion_blocks_before_writing_when_storage_preflight_fails(self):
        output = self.root / "no_space_project"
        storage = {"errors": ["simulated insufficient free space"]}

        with patch("liftool.output_storage_preflight", return_value=storage):
            with self.assertRaisesRegex(LifToolError, "storage preflight failed"):
                convert_fixture(
                    self.source,
                    output,
                    lif_factory=fake_factory([FakeImage("Image001")]),
                )

        self.assertFalse(output.exists())
        self.assertFalse(output.with_name("no_space_project.partial").exists())

    def test_review_route_blocks_before_output_until_explicitly_acknowledged(self):
        output = self.root / "review_project"
        factory = fake_factory([FakeImage("Image001", channels=1)])

        with self.assertRaisesRegex(LifToolError, "review required before conversion"):
            convert_lif(self.source, output, lif_factory=factory)

        self.assertFalse(output.exists())
        self.assertFalse(output.with_name("review_project.partial").exists())

        convert_lif(
            self.source,
            output,
            allow_review=True,
            lif_factory=factory,
        )
        manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["channel_resolution"]["workflow"]["route"], "review")
        self.assertTrue(
            manifest["channel_resolution"]["workflow"]["acknowledged"]
        )
        self.assertTrue(manifest["options"]["review_acknowledged"])
        self.assertEqual(
            manifest["metadata_files"]["workflow_review"],
            "metadata/workflow_review.json",
        )
        review_path = output / "metadata/workflow_review.json"
        review = json.loads(review_path.read_text(encoding="utf-8"))
        self.assertEqual(review["decision"]["status"], "accepted")
        self.assertEqual(review["decision"]["action"], "continue_all")
        self.assertEqual(review["decision"]["source"], "explicit_override")
        self.assertNotIn(str(self.root), review_path.read_text(encoding="utf-8"))

    def test_confirmed_channel_identity_is_portable_and_separate_from_inference(self):
        output = self.root / "confirmed_project"
        convert_fixture(
            self.source,
            output,
            channel_overrides={0: "bodipy_493_503", 1: "brightfield"},
            lif_factory=fake_factory([FakeImage("Image001")]),
        )

        manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema_version"], "0.3")
        self.assertEqual(
            manifest["channel_resolution"]["user_assignments"],
            {"C0": "bodipy_493_503", "C1": "brightfield"},
        )
        green = manifest["series"][0]["channels"][0]
        self.assertEqual(green["identity"]["confirmed_dye"]["id"], "bodipy_493_503")
        self.assertEqual(green["identity"]["review_status"], "confirmed")
        self.assertTrue((output / "metadata/channel_resolution.json").is_file())
        self.assertNotIn(str(self.root), (output / "manifest.json").read_text(encoding="utf-8"))

    def test_interactive_prompt_accepts_candidate_number_and_brightfield_shortcut(self):
        candidate = {
            "id": "bodipy_493_503",
            "display_name": "BODIPY 493/503",
            "role": "lipid_droplet",
            "kind": "stain",
            "confidence": "high",
            "source": "frozen_protocol_match",
            "evidence": [],
        }
        report = {
            "channels": [
                {
                    "channel_key": "C00",
                    "stain_candidates": [candidate],
                    "directory_suggestion": "C00_green",
                    "modality": "fluorescence",
                },
                {
                    "channel_key": "C01",
                    "stain_candidates": [],
                    "directory_suggestion": "C01_gray",
                    "modality": "unknown",
                },
            ],
            "series_channels": [
                {
                    "channel_index": 0,
                    "modality": {"value": "fluorescence", "confidence": "high"},
                },
                {
                    "channel_index": 1,
                    "modality": {"value": "unknown", "confidence": "unknown"},
                },
            ],
            "consistency": {"status": "stable", "issues": []},
            "workflow": {
                "route": "standard",
                "status": "ready",
                "reason_codes": [],
            },
        }
        answers = iter(["1", "b"])

        assignments = prompt_channel_assignments(
            report,
            input_func=lambda _prompt: next(answers),
            output_func=lambda _text: None,
        )

        self.assertEqual(assignments, {0: "bodipy_493_503", 1: "brightfield"})

    def test_failed_partial_output_can_resume(self):
        output = self.root / "project"
        with self.assertRaisesRegex(RuntimeError, "simulated interruption"):
            convert_fixture(
                self.source,
                output,
                lif_factory=fake_factory([FakeImage("Image001", t=3, fail_after=4)]),
            )

        partial = self.root / "project.partial"
        self.assertTrue(partial.is_dir())
        failed = json.loads((partial / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(failed["project_status"], "failed")

        resumed_image = FakeImage("Image001", t=3)
        result = convert_fixture(
            self.source,
            output,
            resume=True,
            lif_factory=fake_factory([resumed_image]),
        )

        self.assertEqual(result, output.resolve())
        self.assertFalse(partial.exists())
        self.assertTrue(validate_project(output)["ok"])
        self.assertLess(resumed_image.frame_calls, 6)

    def test_user_cancellation_stops_at_file_boundary_and_keeps_resumable_partial(self):
        output = self.root / "cancelled_project"
        image = FakeImage("Image001", channels=2)

        def cancel_after_first_file():
            return image.frame_calls >= 1

        with self.assertRaisesRegex(ConversionCancelled, "cancelled by user"):
            convert_fixture(
                self.source,
                output,
                lif_factory=fake_factory([image]),
                cancel_callback=cancel_after_first_file,
            )

        partial = self.root / "cancelled_project.partial"
        self.assertTrue(partial.is_dir())
        manifest = json.loads((partial / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["project_status"], "interrupted")
        self.assertEqual(len(list(partial.rglob("*.tif"))), 1)

    def test_cancellation_inside_multiplane_tiff_removes_temporary_and_resumes(self):
        output = self.root / "multiplane_cancelled_project"
        interrupted_image = FakeImage("Time", t=5, channels=1)

        with self.assertRaisesRegex(ConversionCancelled, "cancelled by user"):
            convert_fixture(
                self.source,
                output,
                lif_factory=fake_factory([interrupted_image]),
                cancel_callback=lambda: interrupted_image.frame_calls >= 2,
            )

        partial = self.root / "multiplane_cancelled_project.partial"
        manifest = json.loads((partial / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["project_status"], "interrupted")
        self.assertEqual(interrupted_image.frame_calls, 2)
        self.assertFalse(list(partial.rglob("*.tif")))
        self.assertFalse(
            [path for path in partial.rglob("*") if ".tmp" in path.name]
        )

        resumed_image = FakeImage("Time", t=5, channels=1)
        convert_fixture(
            self.source,
            output,
            resume=True,
            lif_factory=fake_factory([resumed_image]),
        )

        self.assertEqual(resumed_image.frame_calls, 5)
        self.assertTrue(validate_project(output)["ok"])

    def test_plane_progress_is_reported_for_multiplane_output(self):
        output = self.root / "plane_progress_project"
        messages = []

        convert_fixture(
            self.source,
            output,
            lif_factory=fake_factory([FakeImage("Time", t=5, channels=1)]),
            progress_callback=messages.append,
        )

        self.assertIn("Plane progress: 5/5", messages)

    def test_validator_detects_undeclared_duplicate_tiff(self):
        output = self.root / "project"
        convert_fixture(
            self.source,
            output,
            lif_factory=fake_factory([FakeImage("Image001", channels=1)]),
        )
        original = next(output.rglob("*.tif"))
        duplicate = output / "duplicate.tif"
        shutil.copy2(original, duplicate)

        validation = validate_project(output)

        self.assertFalse(validation["ok"])
        self.assertTrue(
            any("undeclared or duplicate TIFF" in error for error in validation["errors"])
        )

    def test_validator_detects_temporary_artifact(self):
        output = self.root / "project"
        convert_fixture(
            self.source,
            output,
            lif_factory=fake_factory([FakeImage("Image001", channels=1)]),
        )
        (output / ".orphan.tmp.tif").write_bytes(b"incomplete")

        validation = validate_project(output)

        self.assertFalse(validation["ok"])
        self.assertTrue(
            any("temporary conversion artifact" in error for error in validation["errors"])
        )

    def test_optional_intensity_summary_is_real_and_declared(self):
        output = self.root / "project"
        convert_fixture(
            self.source,
            output,
            include_intensity=True,
            lif_factory=fake_factory([FakeImage("Image001", channels=1)]),
        )

        summary = output / "metadata/intensity_summary.csv"
        self.assertTrue(summary.is_file())
        self.assertIn("zero_fraction", summary.read_text(encoding="utf-8"))
        manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(
            manifest["metadata_files"]["intensity_summary"],
            "metadata/intensity_summary.csv",
        )
        self.assertTrue(validate_project(output)["ok"])

    def test_original_series_name_is_preserved_in_output_filename(self):
        output = self.root / "project"
        convert_fixture(
            self.source,
            output,
            lif_factory=fake_factory([FakeImage("1. Image 001", channels=1)]),
        )

        manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
        record = manifest["series"][0]["channels"][0]["files"][0]
        self.assertEqual(record["path"], "C00_ch0/1. Image 001.tif")
        self.assertTrue((output / record["path"]).is_file())

    def test_uint16_and_z_spacing_are_preserved(self):
        output = self.root / "project"
        convert_fixture(
            self.source,
            output,
            lif_factory=fake_factory(
                [FakeImage("Image001", z=3, channels=1, bit_depth=16)]
            ),
        )

        manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
        record = manifest["series"][0]["channels"][0]["files"][0]
        self.assertEqual(record["dtype"], "uint16")
        self.assertEqual(record["pixel_size_z_um"], 2.0)
        self.assertTrue(validate_project(output)["ok"])

    def test_numbered_lif_names_get_distinct_default_outputs(self):
        first = _default_output(Path("/data/1. sample.lif"), "_tiff")
        second = _default_output(Path("/data/2. sample.lif"), "_tiff")

        self.assertNotEqual(first, second)
        self.assertEqual(first.name, "1. sample_tiff")
        self.assertEqual(second.name, "2. sample_tiff")

    def test_default_output_preserves_valid_human_readable_characters(self):
        output = _default_output(
            Path("/data/样品 (A), 0.25~1+BODIPY.lif"),
            "_tiff",
        )

        self.assertEqual(output.name, "样品 (A), 0.25~1+BODIPY_tiff")

    def test_default_output_removes_stem_trailing_space_and_period(self):
        output = _default_output(Path("/data/001. .lif"), "_tiff")

        self.assertEqual(output.name, "001_tiff")

    def test_default_output_replaces_windows_invalid_character_runs(self):
        output = _default_output(Path('/data/A:B?C*D"E.lif'), "_tiff")

        self.assertEqual(output.name, "A_B_C_D_E_tiff")

    def test_default_output_protects_reserved_name_without_a_suffix(self):
        output = _default_output(Path("/data/CON.lif"), "")

        self.assertEqual(output.name, "_CON")

    def test_default_output_shortens_oversized_component_deterministically(self):
        source = Path("/data") / (("样" * 100) + ".lif")

        first = _default_output(source, "_tiff")
        second = _default_output(source, "_tiff")

        self.assertEqual(first, second)
        self.assertLessEqual(len(first.name.encode("utf-8")), 240)
        self.assertTrue(first.name.endswith("_tiff"))

    def test_validator_detects_calibration_and_source_xml_tampering(self):
        output = self.root / "project"
        convert_fixture(
            self.source,
            output,
            plate_calibration={"full_x_grid": [], "full_y_grid": [], "test": 1},
            lif_factory=fake_factory([FakeImage("Image001", channels=1)]),
        )

        calibration_path = output / "metadata/plate_calibration.json"
        calibration_path.write_text('{"test": 2}\n', encoding="utf-8")
        calibration_validation = validate_project(output)
        self.assertFalse(calibration_validation["ok"])
        self.assertTrue(
            any("calibration differs" in error for error in calibration_validation["errors"])
        )

        calibration_path.write_text(
            json.dumps({"full_x_grid": [], "full_y_grid": [], "test": 1}),
            encoding="utf-8",
        )
        source_xml = output / "metadata/source_metadata.xml"
        source_xml.write_text("<changed />", encoding="utf-8")
        xml_validation = validate_project(output)
        self.assertFalse(xml_validation["ok"])
        self.assertTrue(
            any("source XML fingerprint" in error for error in xml_validation["errors"])
        )


if __name__ == "__main__":
    unittest.main()
