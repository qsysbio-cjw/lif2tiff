from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import tifffile
from PIL import Image


MODULE_DIR = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(MODULE_DIR))

from presentation_export import (  # noqa: E402
    PresentationExportError,
    export_presentations,
    presentation_frame_count,
)
from tiff_project_adapter import (  # noqa: E402
    TiffProjectAdapter,
    TiffProjectError,
    load_tiff_project_plan,
)


def build_project(root: Path) -> Path:
    project = root / "portable_tiff_project"
    metadata = project / "metadata"
    metadata.mkdir(parents=True)
    series_specs = [
        ("Time Z", 2, 2),
        ("Single", 1, 1),
    ]
    stored_series = []
    detailed_series = []
    for series_index, (name, z_count, t_count) in enumerate(series_specs):
        dimensions = {"x": 4, "y": 3, "z": z_count, "t": t_count, "channels": 2}
        channels = []
        channel_info = []
        for channel_index, (label, lut, modality) in enumerate(
            (("brightfield", "Gray", "brightfield"), ("green", "Green", "fluorescence"))
        ):
            folder = project / f"C{channel_index:02d}_{label}"
            folder.mkdir(exist_ok=True)
            suffix = ".ome.tif" if z_count * t_count > 1 else ".tif"
            relative = Path(folder.name) / f"S{series_index + 1}{suffix}"
            pages = np.stack(
                [
                    np.full(
                        (3, 4),
                        series_index * 1000 + channel_index * 100 + page,
                        dtype=np.uint16,
                    )
                    for page in range(z_count * t_count)
                ]
            )
            tifffile.imwrite(project / relative, pages, photometric="minisblack")
            record = {
                "channel_index": channel_index,
                "axes": "TZYX" if z_count > 1 and t_count > 1 else "YX",
                "z_count": z_count,
                "t_count": t_count,
                "page_count": z_count * t_count,
                "path": relative.as_posix(),
                "shape": list(pages.shape),
                "dtype": "uint16",
                "time_increment_s": 30.0 if t_count > 1 else None,
                "status": "complete",
            }
            identity = {"modality": {"value": modality, "confidence": "confirmed"}}
            channels.append(
                {
                    "index": channel_index,
                    "physical_label": label,
                    "analysis_label": label,
                    "identity": identity,
                    "files": [record],
                }
            )
            channel_info.append(
                {
                    "index": channel_index,
                    "label": label,
                    "physical_label": label,
                    "lut": lut,
                    "identity": identity,
                    "source_display_settings": {
                        "value_min": 0.0,
                        "value_max": 65535.0,
                        "black": 0.0,
                        "white": 200.0,
                        "gamma": 1.0,
                    },
                }
            )
        stored_series.append(
            {
                "index": series_index,
                "name": name,
                "dimension_type": "ZT" if z_count > 1 and t_count > 1 else "2D",
                "dimensions": dimensions,
                "pixel_size": {"x_um_per_px": 0.2, "y_um_per_px": 0.2},
                "channels": channels,
            }
        )
        detailed_series.append(
            {
                "series_index": series_index,
                "series_name": name,
                "dimensions": dimensions,
                "channel_info": channel_info,
                "stage_position": {"x_m": 0.01 + series_index, "y_m": 0.02},
                "time_points_s": [30.0 * index for index in range(t_count)],
            }
        )
    manifest = {
        "schema_name": "candida_lif_conversion_project",
        "schema_version": "0.3",
        "project_status": "complete",
        "source": {"filename": "synthetic.lif"},
        "metadata_files": {"bundle": "metadata/metadata.json"},
        "series": stored_series,
    }
    (project / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (metadata / "metadata.json").write_text(
        json.dumps({"series": detailed_series}), encoding="utf-8"
    )
    return project


class ConvertedProjectAdapterTests(unittest.TestCase):
    def test_project_reopens_and_uses_t_then_z_page_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            adapter = TiffProjectAdapter(build_project(Path(temporary)))

            self.assertEqual(adapter.num_images, 2)
            self.assertEqual(adapter.plan["summary"]["plane_count"], 10)
            frame = adapter.images[0].get_frame(z=1, t=1, c=1)
            self.assertTrue(np.all(frame == 103))
            self.assertEqual(adapter.images[0].settings["CycleTime"], 30.0)
            handle = next(iter(adapter._handle_cache._files.values()))
            self.assertFalse(handle.filehandle.closed)
            adapter.close()
            self.assertTrue(handle.filehandle.closed)

    def test_project_limits_open_tiff_handles(self):
        with tempfile.TemporaryDirectory() as temporary:
            with TiffProjectAdapter(build_project(Path(temporary))) as adapter:
                adapter._handle_cache.max_open = 1

                adapter.images[0].get_frame(z=0, t=0, c=0)
                first = next(iter(adapter._handle_cache._files.values()))
                adapter.images[0].get_frame(z=0, t=0, c=1)

                self.assertTrue(first.filehandle.closed)
                self.assertEqual(adapter.open_tiff_count, 1)

    def test_project_rejects_path_escape_and_incomplete_status(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = build_project(Path(temporary))
            manifest_path = project / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["series"][0]["channels"][0]["files"][0]["path"] = "../outside.tif"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(TiffProjectError, "escapes"):
                load_tiff_project_plan(project)

            manifest["project_status"] = "interrupted"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(TiffProjectError, "only complete"):
                load_tiff_project_plan(project)


class PresentationExportTests(unittest.TestCase):
    def test_batch_overlay_is_portable_and_records_exact_render_recipe(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with TiffProjectAdapter(build_project(root)) as adapter:
                output = root / "presentation"
                current = {
                    (0, 0): {"black": 1.0, "white": 50.0, "gamma": 1.2},
                    (0, 1): {"black": 2.0, "white": 80.0, "gamma": 0.8},
                }

                result = export_presentations(
                    adapter=adapter,
                    plan=adapter.plan,
                    output=output,
                    channels=[0, 1],
                    scope="all_series_first_plane",
                    contrast_mode="current_adjusted",
                    use_lut=True,
                    current_states=current,
                )

            pngs = sorted(result.glob("*.png"))
            self.assertEqual(len(pngs), 2)
            self.assertEqual(Image.open(pngs[0]).mode, "RGB")
            recipe = json.loads((result / "presentation_recipe.json").read_text())
            self.assertEqual(recipe["exported_frame_count"], 2)
            self.assertEqual(recipe["files"][0]["render_states"]["C01"]["white"], 80.0)
            self.assertEqual(
                recipe["files"][1]["render_states"]["C01"]["white"], 200.0
            )
            self.assertNotIn(str(root), json.dumps(recipe))

    def test_counts_planes_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with TiffProjectAdapter(build_project(root)) as adapter:
                self.assertEqual(
                    presentation_frame_count(adapter.plan, "all_planes", [1]), 5
                )
                output = root / "presentation"
                output.mkdir()
                with self.assertRaisesRegex(
                    PresentationExportError, "refusing to overwrite"
                ):
                    export_presentations(
                        adapter=adapter,
                        plan=adapter.plan,
                        output=output,
                        channels=[1],
                        scope="current_frame",
                        contrast_mode="source_display",
                        use_lut=True,
                        current_frame=(0, 0, 0),
                    )

    def test_export_creates_a_new_folder_below_nested_parent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with TiffProjectAdapter(build_project(root)) as adapter:
                parent = root / "nested" / "report outputs"
                parent.mkdir(parents=True)
                output = parent / "new presentation"

                export_presentations(
                    adapter=adapter,
                    plan=adapter.plan,
                    output=output,
                    channels=[1],
                    scope="current_frame",
                    contrast_mode="source_display",
                    use_lut=True,
                    current_frame=(0, 0, 0),
                )

            self.assertTrue((output / "presentation_recipe.json").is_file())
            self.assertEqual(len(list(output.glob("*.png"))), 1)

    def test_missing_selected_channel_is_reported(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with TiffProjectAdapter(build_project(root)) as adapter:
                with self.assertRaisesRegex(PresentationExportError, "not present"):
                    export_presentations(
                        adapter=adapter,
                        plan=adapter.plan,
                        output=root / "presentation",
                        channels=[99],
                        scope="current_frame",
                        contrast_mode="source_display",
                        use_lut=True,
                        current_frame=(0, 0, 0),
                    )


if __name__ == "__main__":
    unittest.main()
