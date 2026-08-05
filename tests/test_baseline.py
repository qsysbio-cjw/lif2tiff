from __future__ import annotations

import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path


MODULE_DIR = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(MODULE_DIR))

from lif2tiff_project import extract_xml_metadata, infer_channel_assignment, parse_channel_overrides
from batch_metadata_catalog import _annotate_sources, _compact_wells, _natural_key
from acquisition_conditions_report import _fmt
from plate_calibration import (
    _fit_paired_boundaries,
    _full_x_grid,
    _full_y_grid,
    _reference_well,
    _row_label,
)
from stage_map import _annotate_well_overlap, _plate_geometry


class ChannelAssignmentTests(unittest.TestCase):
    def test_bodipy_from_leica_dye_name(self):
        result = infer_channel_assignment("green", "Leica/BODIPY FL")

        self.assertEqual(result["analysis_label"], "bodipy")
        self.assertEqual(result["biological_role"], "lipid_droplet")
        self.assertEqual(result["assignment_source"], "leica_dye_name")

    def test_brightfield_from_physical_label(self):
        result = infer_channel_assignment("brightfield")

        self.assertEqual(result["analysis_label"], "brightfield")
        self.assertEqual(result["biological_role"], "morphology")

    def test_manual_override_parser_is_case_insensitive(self):
        self.assertEqual(
            parse_channel_overrides(["C0=BODIPY", "1=brightfield"]),
            {0: "bodipy", 1: "brightfield"},
        )


class LeicaSequentialMetadataTests(unittest.TestCase):
    def test_channel_uses_its_sequential_detector_laser_and_band(self):
        root = ET.fromstring(
            """
            <Root><Element Name="Image001"><Attachment Name="HardwareSetting">
              <ATLConfocalSettingDefinition>
                <Detector Name="HyD S 1" Gain="2.5" Offset="0" IsActive="0" />
              </ATLConfocalSettingDefinition>
              <LDM_Block_Sequential><LDM_Block_Sequential_List>
                <ATLConfocalSettingDefinition UserSettingName="Green">
                  <LaserLineSetting IsVisible="1" LaserLine="491" IntensityDev="15" />
                  <Detector Name="HyD S 1" Gain="52.25" Offset="0" IsActive="1" Type="SiPM" />
                  <MultiBand SpectralPosition="0" TargetWaveLengthBegin="501.5" TargetWaveLengthEnd="640.2" />
                </ATLConfocalSettingDefinition>
                <ATLConfocalSettingDefinition UserSettingName="Red">
                  <LaserLineSetting IsVisible="1" LaserLine="553" IntensityDev="4" />
                  <Detector Name="HyD S 2" Gain="44.1" Offset="0" IsActive="1" Type="SiPM" />
                  <MultiBand SpectralPosition="1" TargetWaveLengthBegin="562" TargetWaveLengthEnd="732" />
                </ATLConfocalSettingDefinition>
              </LDM_Block_Sequential_List></LDM_Block_Sequential>
              <ChannelProperty><Key>ChannelGroup</Key><Value>0</Value></ChannelProperty>
              <ChannelProperty><Key>BeamRoute</Key><Value>40;1</Value></ChannelProperty>
              <ChannelProperty><Key>DetectorName</Key><Value>HyD S 1</Value></ChannelProperty>
              <ChannelProperty><Key>DyeName</Key><Value>Leica/ALEXA 488</Value></ChannelProperty>
              <ChannelProperty><Key>SequentialSettingIndex</Key><Value>0</Value></ChannelProperty>
              <ChannelProperty><Key>ChannelGroup</Key><Value>1</Value></ChannelProperty>
              <ChannelProperty><Key>BeamRoute</Key><Value>40;2</Value></ChannelProperty>
              <ChannelProperty><Key>DetectorName</Key><Value>HyD S 2</Value></ChannelProperty>
              <ChannelProperty><Key>DyeName</Key><Value>Leica/ALEXA 546</Value></ChannelProperty>
              <ChannelProperty><Key>SequentialSettingIndex</Key><Value>1</Value></ChannelProperty>
            </Attachment></Element></Root>
            """
        )

        metadata = extract_xml_metadata(root, "Image001")
        green, red = metadata["channel_detector_map"]

        self.assertEqual(green["gain"], 52.25)
        self.assertEqual(green["excitation_settings"][0]["wavelength_nm"], 491.0)
        self.assertEqual(green["emission_window_begin_nm"], 501.5)
        self.assertEqual(red["gain"], 44.1)
        self.assertEqual(red["excitation_settings"][0]["wavelength_nm"], 553.0)
        self.assertEqual(red["emission_window_end_nm"], 732.0)

    def test_channel_value_range_and_source_display_scaling_are_preserved(self):
        root = ET.fromstring(
            """
            <Root><Element Name="Image001">
              <ChannelDescription BytesInc="0" LUTName="Green" Min="0"
                Max="65535" Resolution="16" IsLUTInverted="0" />
              <ChannelScalingInfo BlackValue="0.1" WhiteValue="0.5"
                GammaValue="1.2" />
            </Element></Root>
            """
        )

        metadata = extract_xml_metadata(root, "Image001")

        self.assertEqual(metadata["channel_descriptions"][0]["Max"], "65535")
        self.assertEqual(metadata["channel_descriptions"][0]["Resolution"], "16")
        self.assertEqual(metadata["channel_scaling"][0]["BlackValue"], "0.1")
        self.assertEqual(metadata["channel_scaling"][0]["WhiteValue"], "0.5")
        self.assertEqual(metadata["channel_scaling"][0]["GammaValue"], "1.2")


class PlateCalibrationTests(unittest.TestCase):
    def test_paired_boundary_fit_and_full_grid(self):
        rows = []
        first_left = 1000.0
        pitch = 4500.0
        opening = 3300.0
        for pair in range(3):
            for edge in (0.0, opening):
                rows.append(
                    {
                        "series_index": str(len(rows)),
                        "series_name": f"Image{len(rows) + 1:03d}",
                        "stage_x_um": str(first_left + pair * pitch + edge),
                    }
                )

        axis = _fit_paired_boundaries(rows, "stage_x_um", 13, "C")
        grid = _full_x_grid(axis, 24)

        self.assertAlmostEqual(axis["fit"]["pitch_um"], pitch, places=6)
        self.assertAlmostEqual(axis["fit"]["well_opening_um"], opening, places=6)
        self.assertAlmostEqual(axis["fit"]["rms_residual_um"], 0.0, places=6)
        self.assertEqual(axis["measured_boundaries"][0]["label"], "C13")
        self.assertAlmostEqual(grid[12]["left_boundary_um"], first_left, places=6)

    def test_absolute_row_grid_and_cross_axis_reference_well(self):
        x_rows = [
            {"series_index": "0", "series_name": "Image001", "stage_x_um": "1000", "stage_y_um": "2500"},
            {"series_index": "1", "series_name": "Image002", "stage_x_um": "4300", "stage_y_um": "2500"},
        ]
        y_rows = [
            {"series_index": "2", "series_name": "Image003", "stage_x_um": "2650", "stage_y_um": "900"},
            {"series_index": "3", "series_name": "Image004", "stage_x_um": "2650", "stage_y_um": "4200"},
        ]
        x_axis = _fit_paired_boundaries(x_rows, "stage_x_um", 13, "C")
        y_axis = _fit_paired_boundaries(y_rows, "stage_y_um", 5, "R")
        reference = _reference_well(x_rows, y_rows, x_axis, y_axis)
        grid = _full_y_grid(y_axis, 16)

        self.assertEqual(_row_label(5), "E")
        self.assertEqual(y_axis["measured_boundaries"][0]["label"], "E")
        self.assertEqual(grid[4]["label"], "E")
        self.assertEqual(reference["well"], "E13")
        self.assertTrue(reference["cross_axis_validation"]["x_boundary_scan_inside_first_row"])
        self.assertTrue(reference["cross_axis_validation"]["y_boundary_scan_inside_first_column"])

    def test_fov_on_well_boundary_has_half_inside(self):
        points = [{"x_um": 0.0, "y_um": 5.0}]
        columns = [
            {"column": 1, "left_boundary_um": 0.0, "right_boundary_um": 10.0}
        ]
        rows = [
            {"label": "A", "top_boundary_um": 0.0, "bottom_boundary_um": 10.0}
        ]

        _annotate_well_overlap(points, 2.0, 2.0, columns, rows)

        self.assertEqual(points[0]["best_well"], "A1")
        self.assertEqual(points[0]["inside_well_fraction"], 0.5)

    def test_plate_geometry_keeps_physical_outer_rim(self):
        columns = [{"center_um": 0.0}, {"center_um": 103_500.0}]
        rows = [{"center_um": 0.0}, {"center_um": 67_500.0}]

        geometry = _plate_geometry(columns, rows)

        self.assertAlmostEqual(geometry["maxX"] - geometry["minX"], 127_760.0)
        self.assertAlmostEqual(geometry["maxY"] - geometry["minY"], 85_480.0)


class BatchCatalogTests(unittest.TestCase):
    def test_compact_contiguous_wells(self):
        self.assertEqual(_compact_wells(["I7", "E7", "G7", "F7", "H7"]), "E7-I7")
        self.assertEqual(_compact_wells(["E7", "E8", "E9"]), "E7-E9")

    def test_natural_sort_accepts_numbered_and_named_files(self):
        values = ["10. sample.lif", "DIC.lif", "2. sample.lif", "1. sample.lif"]
        self.assertEqual(
            sorted(values, key=_natural_key),
            ["1. sample.lif", "2. sample.lif", "10. sample.lif", "DIC.lif"],
        )

    def test_source_annotations_mark_moves_and_probable_duplicates(self):
        rows = [
            {
                "person": "A",
                "source_relative_path": "day/original.lif",
                "lif_name": "original.lif",
                "source_size_bytes": 10,
                "source_metadata_sha256": "abc123",
                "source_status": "normal",
                "source_note": "",
                "duplicate_group": "",
                "duplicate_of": "",
            },
            {
                "person": "B",
                "source_relative_path": "day/copy.lif",
                "lif_name": "copy.lif",
                "source_size_bytes": 10,
                "source_metadata_sha256": "abc123",
                "source_status": "normal",
                "source_note": "",
                "duplicate_group": "",
                "duplicate_of": "",
            },
        ]
        annotations = {
            "relocated_prefixes": [{"current": "A/day", "previous": "Old/day"}]
        }

        _annotate_sources(rows, annotations)

        self.assertEqual(rows[0]["source_status"], "relocated")
        self.assertEqual(rows[1]["source_status"], "probable_duplicate")
        self.assertEqual(rows[1]["duplicate_of"], "A/day/original.lif")

    def test_integer_format_does_not_strip_significant_zeroes(self):
        self.assertEqual(_fmt("400", 0), "400")


if __name__ == "__main__":
    unittest.main()
