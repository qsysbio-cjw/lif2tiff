from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE_DIR = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(MODULE_DIR))

from channel_resolver import (
    assess_consistency,
    build_workflow_review,
    canonical_assignment_id,
    decide_workflow,
    load_protocol_registry,
    parse_channel_assignments,
    protocol_registry_path,
    resolve_channel,
)


class ChannelResolverTests(unittest.TestCase):
    def test_gray_tld_is_brightfield(self):
        result = resolve_channel(
            {
                "index": 1,
                "lut": "Gray",
                "detector_name": "Trans PMT",
                "detector_type": "PMT",
                "scan_type": "TLD",
            }
        )

        self.assertEqual(result["modality"]["value"], "brightfield")
        self.assertEqual(result["modality"]["confidence"], "high")
        self.assertEqual(result["directory"], "C01_brightfield")

    def test_gray_lut_with_active_laser_is_not_brightfield(self):
        result = resolve_channel(
            {
                "index": 0,
                "lut": "Gray",
                "all_visible_laser_wavelengths_nm": "405;488;561",
                "all_visible_laser_intensities_percent": "0;0.3;0",
            }
        )

        self.assertEqual(result["modality"]["value"], "fluorescence")
        self.assertEqual(result["directory"], "C00_gray")

    def test_leica_bodipy_is_candidate_not_confirmation(self):
        result = resolve_channel(
            {
                "index": 0,
                "lut": "Green",
                "detector_name": "HyD S 1",
                "scan_type": "Internal",
                "dye_name": "Leica/BODIPY FL",
            }
        )

        self.assertIsNone(result["confirmed_assignment"])
        self.assertEqual(result["stain_candidates"][0]["id"], "bodipy_493_503")
        self.assertEqual(result["stain_candidates"][0]["confidence"], "high")

    def test_alexa_preset_is_not_reported_as_stain(self):
        result = resolve_channel(
            {
                "index": 0,
                "lut": "Green",
                "detector_name": "HyD S 1",
                "scan_type": "Internal",
                "dye_name": "Leica/ALEXA 488",
            }
        )

        self.assertEqual(result["stain_candidates"][0]["kind"], "instrument_preset")
        self.assertIsNone(result["confirmed_assignment"])

    def test_user_assignment_is_confirmed(self):
        result = resolve_channel(
            {"index": 2, "lut": "Red"},
            user_assignment="propidium_iodide",
        )

        self.assertEqual(
            result["confirmed_assignment"]["id"],
            "propidium_iodide",
        )
        self.assertEqual(
            result["confirmed_assignment"]["confidence"],
            "confirmed",
        )

    def test_user_names_and_aliases_are_canonicalized(self):
        self.assertEqual(canonical_assignment_id("Nile Red"), "nile_red")
        self.assertEqual(canonical_assignment_id("PI"), "propidium_iodide")
        self.assertEqual(
            parse_channel_assignments(["C0=BODIPY", "1=BF", "C2=unknown"]),
            {0: "bodipy_493_503", 1: "brightfield", 2: "unknown"},
        )

    def test_explicit_unknown_is_recorded_without_guessing(self):
        result = resolve_channel(
            {"index": 0, "lut": "Green"},
            user_assignment="unknown",
        )

        self.assertEqual(result["confirmed_assignment"]["id"], "unknown")
        self.assertIsNone(result["confirmed_dye"])
        self.assertIsNone(result["inferred_dye"])

    def test_private_protocol_match_is_candidate_not_confirmation(self):
        private_example = {
            "groups": [
                {
                    "id": "TEST-bodipy",
                    "acquisition_family": "bodipy_acquisition",
                    "candidate_dye_id": "bodipy_493_503",
                    "channel_count": 1,
                    "signature": {
                        "excitation_nm": "491.0",
                        "laser_intensity_percent": "5.00",
                        "emission_begin_nm": "500.0",
                        "emission_end_nm": "540.0",
                        "gain": "30.0",
                        "offset": "0.0",
                        "detector": "HyD S 1",
                        "detector_type": "SiPM",
                        "scan_type": "Internal",
                        "acquisition_mode": "PhotonCounting",
                        "bit_depth": "8",
                    },
                }
            ]
        }
        result = resolve_channel(
            {
                "index": 0,
                "lut": "Green",
                "detector_name": "HyD S 1",
                "detector_type": "SiPM",
                "scan_type": "Internal",
                "acquisition_mode": "PhotonCounting",
                "excitation_settings": [
                    {"wavelength_nm": 491, "intensity_percent": 5.013}
                ],
                "emission_window_begin_nm": 500,
                "emission_window_end_nm": 540,
                "gain": 30,
                "offset": 0,
                "bit_depth": 8,
            },
            protocol_registry=private_example,
        )

        self.assertEqual(result["inferred_dye"]["id"], "bodipy_493_503")
        self.assertEqual(result["inferred_dye"]["source"], "frozen_protocol_match")
        self.assertIsNone(result["confirmed_dye"])
        self.assertEqual(result["review_status"], "candidate")

    def test_public_protocol_registry_is_an_empty_template(self):
        registry = load_protocol_registry(
            Path(__file__).resolve().parents[1] / "resources/protocol_registry.json"
        )

        self.assertEqual(registry["distribution"], "public_template")
        self.assertEqual(registry["groups"], [])

    def test_environment_override_loads_private_protocol_registry(self):
        with tempfile.TemporaryDirectory() as directory:
            private_path = Path(directory) / "private-registry.json"
            private_path.write_text(
                json.dumps({"schema_version": "test", "groups": [{"id": "private"}]}),
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {"LIF2TIFF_PROTOCOL_REGISTRY": str(private_path)},
            ):
                self.assertEqual(protocol_registry_path(), private_path)
                self.assertEqual(load_protocol_registry()["groups"][0]["id"], "private")

    def test_filename_stain_is_not_assigned_to_brightfield(self):
        result = resolve_channel(
            {
                "index": 1,
                "lut": "Gray",
                "detector_name": "Trans PMT",
                "scan_type": "TLD",
            },
            source_name="BODIPY_PI_experiment.lif",
        )

        self.assertEqual(result["stain_candidates"], [])

    def test_soft_optical_hints_separate_bodipy_and_pi_channels(self):
        green = resolve_channel(
            {
                "index": 0,
                "lut": "Green",
                "detector_name": "HyD S 1",
                "scan_type": "Internal",
                "excitation_settings": [{"wavelength_nm": 488}],
                "emission_window_begin_nm": 500,
                "emission_window_end_nm": 540,
            },
            source_name="BODIPY_PI_experiment.lif",
        )
        red = resolve_channel(
            {
                "index": 2,
                "lut": "Red",
                "detector_name": "HyD S 2",
                "scan_type": "Internal",
                "excitation_settings": [{"wavelength_nm": 553}],
                "emission_window_begin_nm": 600,
                "emission_window_end_nm": 650,
            },
            source_name="BODIPY_PI_experiment.lif",
        )

        self.assertEqual(
            [item["id"] for item in green["stain_candidates"]],
            ["bodipy_493_503"],
        )
        self.assertEqual(
            [item["id"] for item in red["stain_candidates"]],
            ["propidium_iodide"],
        )

    def test_gain_change_is_variation_not_protocol_warning(self):
        rows = [
            {
                "series_index": index,
                "channel_index": 0,
                "lut": "Green",
                "detector_name": "HyD S 1",
                "scan_type": "Internal",
                "excitation_wavelengths_nm": "488",
                "emission_window_begin_nm": "500",
                "emission_window_end_nm": "540",
                "gain": gain,
            }
            for index, gain in enumerate(("40", "50"))
        ]

        consistency = assess_consistency(rows)
        self.assertEqual(consistency["status"], "variation")
        self.assertEqual(decide_workflow(consistency)["route"], "standard")
        self.assertEqual(
            decide_workflow(consistency)["status"], "ready_with_variation"
        )

    def test_explicit_half_nm_tolerance_handles_fractional_detector_values(self):
        rows = [
            {
                "series_index": index,
                "series_name": f"Image{index + 1:03d}",
                "channel_index": 0,
                "detector_name": "HyD S 1",
                "detector_type": "SiPM",
                "scan_type": "Internal",
                "excitation_wavelengths_nm": "488",
                "emission_window_begin_nm": emission,
                "emission_window_end_nm": 540 + emission - 500,
            }
            for index, emission in enumerate((500.0, 500.344234079174))
        ]

        selected = assess_consistency(rows)
        strict = assess_consistency(rows, optical_tolerance_nm=0.1)

        self.assertEqual(selected["status"], "stable")
        self.assertEqual(selected["optical_tolerance_nm"], 0.5)
        self.assertTrue(
            any(issue["code"] == "optical_protocol_varies" for issue in strict["issues"])
        )

    def test_one_nm_excitation_change_requires_review(self):
        rows = [
            {
                "series_index": index,
                "channel_index": 0,
                "detector_name": "HyD S 1",
                "detector_type": "SiPM",
                "scan_type": "Internal",
                "excitation_wavelengths_nm": excitation,
                "emission_window_begin_nm": 600,
                "emission_window_end_nm": 700,
            }
            for index, excitation in enumerate((561, 562))
        ]

        consistency = assess_consistency(rows)

        self.assertEqual(decide_workflow(consistency)["route"], "review")

    def test_workflow_review_groups_protocols_and_keeps_gain_informational(self):
        rows = [
            {
                "series_index": index,
                "series_name": f"Image{index + 1:03d}",
                "channel_index": 0,
                "detector_name": "HyD S 1",
                "detector_type": "SiPM",
                "scan_type": "Internal",
                "acquisition_mode": "PhotonCounting",
                "excitation_wavelengths_nm": excitation,
                "emission_window_begin_nm": 500,
                "emission_window_end_nm": 540,
                "gain": gain,
                "offset": 0,
                "excitation_intensities_percent": 5,
            }
            for index, excitation, gain in (
                (0, 488, 40),
                (1, 488, 50),
                (2, 491, 50),
            )
        ]
        consistency = assess_consistency(rows)
        workflow = decide_workflow(consistency)

        review = build_workflow_review(rows, consistency, workflow)

        self.assertEqual(review["reference_group_id"], "RG-001")
        self.assertEqual(review["groups"][0]["series_count"], 2)
        self.assertEqual(review["groups"][0]["series"][0]["series_index"], 0)
        self.assertEqual(review["groups"][1]["series_count"], 1)
        self.assertTrue(
            any(
                difference.get("field") == "excitation_wavelengths_nm"
                for difference in review["review_differences"]
            )
        )
        self.assertTrue(
            any(item["code"] == "gain_varies" for item in review["informational_variations"])
        )
        self.assertEqual(review["decision"]["status"], "pending")

    def test_unknown_modality_requires_review(self):
        consistency = assess_consistency(
            [{"series_index": 0, "channel_index": 0, "lut": "Gray"}]
        )

        self.assertEqual(consistency["status"], "warning")
        self.assertIn(
            "unknown_modality",
            decide_workflow(consistency)["reason_codes"],
        )

    def test_incomplete_fluorescence_optics_requires_review(self):
        consistency = assess_consistency(
            [
                {
                    "series_index": 0,
                    "channel_index": 0,
                    "detector_name": "HyD S 1",
                    "detector_type": "SiPM",
                    "scan_type": "Internal",
                    "excitation_wavelengths_nm": "488",
                    "emission_window_begin_nm": "",
                    "emission_window_end_nm": "",
                }
            ]
        )

        workflow = decide_workflow(consistency)
        self.assertEqual(workflow["route"], "review")
        self.assertIn("incomplete_fluorescence_optics", workflow["reason_codes"])

    def test_detector_reordering_is_warning(self):
        rows = [
            {
                "series_index": 0,
                "channel_index": 0,
                "lut": "Green",
                "detector_name": "HyD S 1",
                "scan_type": "Internal",
            },
            {
                "series_index": 1,
                "channel_index": 0,
                "lut": "Gray",
                "detector_name": "Trans PMT",
                "scan_type": "TLD",
            },
        ]

        report = assess_consistency(rows)
        self.assertEqual(report["status"], "warning")
        self.assertTrue(
            any(issue["code"] == "modality_varies" for issue in report["issues"])
        )

    def test_equal_channel_counts_with_different_indices_require_review(self):
        rows = [
            {
                "series_index": 0,
                "channel_index": index,
                "detector_name": "HyD S 1",
                "scan_type": "Internal",
                "excitation_wavelengths_nm": 488,
                "emission_window_begin_nm": 500,
                "emission_window_end_nm": 540,
            }
            for index in (0, 1)
        ] + [
            {
                "series_index": 1,
                "channel_index": index,
                "detector_name": "HyD S 1",
                "scan_type": "Internal",
                "excitation_wavelengths_nm": 488,
                "emission_window_begin_nm": 500,
                "emission_window_end_nm": 540,
            }
            for index in (1, 2)
        ]

        consistency = assess_consistency(rows)

        self.assertIn(
            "channel_layout_varies",
            decide_workflow(consistency)["reason_codes"],
        )


if __name__ == "__main__":
    unittest.main()
