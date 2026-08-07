"""Read a completed LIF2TIFF project through the viewer's image interface."""

from __future__ import annotations

import json
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import tifffile


class TiffProjectError(RuntimeError):
    """A converted project is incomplete, unsafe, or unsupported."""


class _TiffHandleCache:
    def __init__(self, max_open: int = 12):
        self.max_open = max(1, int(max_open))
        self._files: OrderedDict[Path, tifffile.TiffFile] = OrderedDict()

    def open(self, path: Path) -> tifffile.TiffFile:
        handle = self._files.pop(path, None)
        if handle is None:
            handle = tifffile.TiffFile(path)
        self._files[path] = handle
        while len(self._files) > self.max_open:
            _old_path, old_handle = self._files.popitem(last=False)
            old_handle.close()
        return handle

    def close(self) -> None:
        for handle in self._files.values():
            handle.close()
        self._files.clear()

    def __len__(self) -> int:
        return len(self._files)


def project_directory(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if resolved.is_file() and resolved.name == "manifest.json":
        return resolved.parent
    return resolved


def is_tiff_project(path: Path) -> bool:
    directory = project_directory(path)
    return directory.is_dir() and (directory / "manifest.json").is_file()


def _safe_project_path(project: Path, relative: str) -> Path:
    candidate = (project / relative).resolve()
    try:
        candidate.relative_to(project)
    except ValueError as exc:
        raise TiffProjectError(f"project path escapes its directory: {relative}") from exc
    return candidate


def _load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TiffProjectError(f"cannot read {path.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise TiffProjectError(f"{path.name} must contain one JSON object")
    return value


class TiffProjectImage:
    def __init__(
        self,
        project: Path,
        series: dict,
        outputs: list[dict],
        handle_cache: _TiffHandleCache,
    ):
        self.project = project
        self.name = str(series["series_name"])
        dims = series["dimensions"]
        self.dims = SimpleNamespace(
            x=int(dims["x"]),
            y=int(dims["y"]),
            z=int(dims["z"]),
            t=int(dims["t"]),
        )
        self.channels = int(dims["channels"])
        self.outputs = {int(output["channel_index"]): output for output in outputs}
        self._handle_cache = handle_cache
        time_increment = next(
            (
                output["file"].get("time_increment_s")
                for output in outputs
                if output["file"].get("time_increment_s") is not None
            ),
            None,
        )
        self.settings = {"CycleTime": time_increment} if time_increment is not None else {}

    def get_frame(self, *, z: int, t: int, c: int) -> np.ndarray:
        if not 0 <= z < self.dims.z or not 0 <= t < self.dims.t:
            raise IndexError(f"frame outside project dimensions: z={z}, t={t}")
        try:
            record = self.outputs[int(c)]["file"]
        except KeyError as exc:
            raise IndexError(f"channel is not present in this series: {c}") from exc
        path = _safe_project_path(self.project, str(record["path"]))
        if not path.is_file():
            raise TiffProjectError(f"project TIFF is missing: {record['path']}")
        page = t * int(record["z_count"]) + z
        tiff = self._handle_cache.open(path)
        if page >= len(tiff.pages):
            raise TiffProjectError(
                f"TIFF page is missing in {record['path']}: {page + 1}"
            )
        frame = np.asarray(tiff.pages[page].asarray())
        expected = (self.dims.y, self.dims.x)
        if frame.shape != expected:
            raise TiffProjectError(
                f"unexpected frame shape in {record['path']}: {frame.shape}, expected {expected}"
            )
        return frame

class TiffProjectAdapter:
    """Duck-typed replacement for LifFileAdapter backed by exported TIFFs."""

    def __init__(self, path: Path):
        self.project = project_directory(path)
        self.manifest, self.plan = load_tiff_project_plan(self.project)
        self._handle_cache = _TiffHandleCache(max_open=12)
        self.images = [
            TiffProjectImage(
                self.project,
                series,
                series["outputs"],
                self._handle_cache,
            )
            for series in self.plan["series"]
        ]
        self.num_images = len(self.images)

    @property
    def open_tiff_count(self) -> int:
        return len(self._handle_cache)

    def close(self) -> None:
        self._handle_cache.close()

    def __enter__(self) -> "TiffProjectAdapter":
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()


def load_tiff_project_plan(path: Path) -> tuple[dict, dict]:
    project = project_directory(path)
    manifest_path = project / "manifest.json"
    if not manifest_path.is_file():
        raise TiffProjectError(f"manifest.json is missing: {project}")
    manifest = _load_json(manifest_path)
    if manifest.get("schema_name") != "candida_lif_conversion_project":
        raise TiffProjectError("not a supported LIF2TIFF project manifest")
    if manifest.get("project_status") != "complete":
        raise TiffProjectError("only complete converted projects can be opened")

    metadata_relative = (manifest.get("metadata_files") or {}).get(
        "bundle", "metadata/metadata.json"
    )
    metadata_path = _safe_project_path(project, str(metadata_relative))
    metadata = _load_json(metadata_path)
    metadata_by_index = {
        int(series["series_index"]): series for series in metadata.get("series", [])
    }

    plan_series = []
    total_planes = 0
    for position, stored in enumerate(manifest.get("series", [])):
        index = int(stored.get("index", position))
        if index != position:
            raise TiffProjectError("project series indexes must be contiguous and ordered")
        detailed = metadata_by_index.get(index, {})
        outputs = []
        for channel in stored.get("channels", []):
            files = channel.get("files") or []
            if len(files) != 1:
                raise TiffProjectError(
                    f"series {index + 1} channel {channel.get('index')} must have one TIFF"
                )
            record = dict(files[0])
            relative = str(record.get("path", ""))
            tiff_path = _safe_project_path(project, relative)
            if not tiff_path.is_file():
                raise TiffProjectError(f"project TIFF is missing: {relative}")
            channel_index = int(channel["index"])
            detail = next(
                (
                    item
                    for item in detailed.get("channel_info", [])
                    if int(item.get("index", -1)) == channel_index
                ),
                {},
            )
            output = {
                "channel_index": channel_index,
                "physical_label": channel.get("physical_label")
                or detail.get("physical_label")
                or detail.get("label"),
                "analysis_label": channel.get("analysis_label"),
                "source_lut": detail.get("lut"),
                "source_display_settings": detail.get("source_display_settings") or {},
                "identity": channel.get("identity") or detail.get("identity") or {},
                "acquisition_properties": {
                    key: detail.get(key)
                    for key in (
                        "detector_name",
                        "detector_type",
                        "scan_type",
                        "detector_is_active",
                        "detector_is_enabled",
                        "gain",
                        "offset",
                        "detection_range_begin_nm",
                        "detection_range_end_nm",
                        "acquisition_mode",
                        "dye_name",
                        "sequential_index",
                        "sequential_setting_name",
                        "excitation_settings",
                        "emission_window_begin_nm",
                        "emission_window_end_nm",
                        "metadata_resolution_status",
                        "metadata_setting_source",
                        "bit_depth",
                    )
                },
                "file": record,
            }
            outputs.append(output)
            total_planes += int(record.get("page_count", 1))
        plan_series.append(
            {
                "series_index": index,
                "series_name": stored.get("name") or detailed.get("series_name") or f"Series{index + 1:03d}",
                "dimension_type": stored.get("dimension_type") or detailed.get("dimension_type"),
                "dimensions": stored.get("dimensions") or detailed.get("dimensions"),
                "pixel_size": stored.get("pixel_size") or detailed.get("pixel_size") or {},
                "stage_position": detailed.get("stage_position") or {},
                "objective": detailed.get("objective") or {},
                "confocal_settings": detailed.get("confocal_settings") or {},
                "microscope": detailed.get("microscope") or {},
                "optical_settings": detailed.get("optical_settings") or {},
                "acquisition_timestamps": detailed.get("acquisition_timestamps") or [],
                "timepoint_timestamps": detailed.get("timepoint_timestamps") or [],
                "time_points_s": detailed.get("time_points_s") or [],
                "outputs": outputs,
            }
        )

    channel_resolution = manifest.get("channel_resolution") or {
        "workflow": {"route": "converted", "status": "complete"},
        "channels": [],
    }
    plan = {
        "source": dict(manifest.get("source") or {}),
        "project": {
            "directory_name": project.name,
            "schema_version": manifest.get("schema_version"),
            "created_at": manifest.get("created_at"),
        },
        "channel_resolution": channel_resolution,
        "summary": {
            "series_count": len(plan_series),
            "output_unit_count": sum(len(series["outputs"]) for series in plan_series),
            "plane_count": total_planes,
        },
        "series": plan_series,
    }
    return manifest, plan
