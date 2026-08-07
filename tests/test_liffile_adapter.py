from __future__ import annotations

import hashlib
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODULE_DIR = PROJECT_ROOT / "src"
sys.path.insert(0, str(MODULE_DIR))

from liffile_adapter import LifFileAdapter


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


if __name__ == "__main__":
    unittest.main()
