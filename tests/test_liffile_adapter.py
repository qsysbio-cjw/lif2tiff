from __future__ import annotations

import hashlib
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODULE_DIR = PROJECT_ROOT / "src"
sys.path.insert(0, str(MODULE_DIR))

from liffile_adapter import LifFileAdapter, LifImageAdapter


class LiffileAdapterTests(unittest.TestCase):
    @patch("liffile_adapter.liffile.LifFile")
    def test_singleton_dimensions_are_squeezed_at_reader_boundary(self, liffile_mock):
        backend = MagicMock()
        backend.images = []
        backend.xml_element = MagicMock()
        backend.xml_header.return_value = "<LMSDataContainerHeader/>"
        liffile_mock.return_value = backend

        source = Path("singleton-layout.lif")
        adapter = LifFileAdapter(source)

        liffile_mock.assert_called_once_with(source, squeeze=True)
        adapter.close()

    def test_mosaic_axis_expands_without_losing_zt_planes(self):
        source = (
            PROJECT_ROOT
            / "tests/fixtures/public/ome_pr2729/output_collision/Sample.lif"
        )

        with LifFileAdapter(source) as lif:
            self.assertEqual(len(lif.images), 4)
            self.assertEqual(
                [image.mosaic_index for image in lif.images],
                [0, 1, 2, 3],
            )
            self.assertTrue(all(image.source_series_index == 0 for image in lif.images))
            self.assertTrue(all(image.dims.t == 2 for image in lif.images))
            self.assertTrue(all(image.dims.z == 3 for image in lif.images))
            self.assertTrue(all(image.channels == 2 for image in lif.images))
            self.assertTrue(all(len(image.acquisition_timestamps) == 12 for image in lif.images))
            self.assertTrue(all(len(image.timepoint_timestamps) == 2 for image in lif.images))

            plane = lif.images[0].get_frame(t=0, z=0, c=0)
            self.assertEqual(
                hashlib.sha256(plane.tobytes()).hexdigest(),
                "37e23b3cc1ec2ca62f21294291905dc25d566436e1d2fc8d93d235952ab7ac18",
            )


class RecordedTimestampTests(unittest.TestCase):
    """A timestamp count that disagrees with the plane layout must keep UTC+8."""

    @staticmethod
    def _adapter(timestamps, dims="TCYX", sizes=None):
        adapter = object.__new__(LifImageAdapter)
        adapter._image = SimpleNamespace(
            timestamps=timestamps,
            dims=dims,
            sizes=sizes if sizes is not None else {"T": 2, "C": 3, "Y": 4, "X": 4},
        )
        adapter.mosaic_index = None
        return adapter

    def test_matching_count_applies_recorded_timezone(self):
        stamps = np.array(
            ["2026-08-05T07:52:59.088"] * 6,
            dtype="datetime64[ms]",
        )
        result = self._adapter(stamps)._timestamps()

        self.assertEqual(len(result), 6)
        self.assertTrue(all(item == "2026-08-05T15:52:59.088+08:00" for item in result))

    def test_short_timestamp_array_still_applies_recorded_timezone(self):
        # Leica writes one timestamp fewer when acquisition stops mid-frame.
        stamps = np.array(
            ["2026-08-05T07:52:59.088"] * 5,
            dtype="datetime64[ms]",
        )
        result = self._adapter(stamps)._timestamps()

        self.assertEqual(len(result), 5)
        self.assertTrue(all(item.endswith("+08:00") for item in result))
        self.assertTrue(all(item.startswith("2026-08-05T15:52:59") for item in result))

    def test_object_array_applies_recorded_timezone_and_keeps_other_values(self):
        stamps = np.array(
            [np.datetime64("2026-08-05T07:52:59.088", "ms"), None],
            dtype=object,
        )
        result = self._adapter(stamps)._timestamps()

        self.assertEqual(result[0], "2026-08-05T15:52:59.088+08:00")
        self.assertEqual(result[1], "None")

    def test_not_a_time_is_left_untouched(self):
        stamps = np.array(["NaT", "NaT", "NaT"], dtype="datetime64[ms]")
        result = self._adapter(stamps, sizes={"T": 1, "C": 3, "Y": 4, "X": 4})._timestamps()

        self.assertEqual(result, ["NaT", "NaT", "NaT"])


if __name__ == "__main__":
    unittest.main()
