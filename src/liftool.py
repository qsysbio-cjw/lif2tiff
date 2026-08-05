#!/usr/bin/env python3
"""Safe, relocatable Leica LIF project converter and validator."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sys
import unicodedata
import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Callable
from pathlib import Path, PurePosixPath

import numpy as np
import tifffile

from channel_resolver import (
    canonical_assignment_id,
    compact_channel_table,
    parse_channel_assignments,
    summarize_resolved_channels,
)
from liffile_adapter import LifFileAdapter as LifFile
from lif2tiff_project import (
    extract_metadata,
    sanitize_name,
    write_tiff,
)
from lif_metadata_inspect import (
    _channel_rows,
    _flatten_series,
    _stage_row,
    _write_csv,
    _write_report,
    inspect_lif,
)
from stage_map import write_stage_map


SCHEMA_NAME = "candida_lif_conversion_project"
SCHEMA_VERSION = "0.3"
SUPPORTED_SCHEMA_VERSIONS = {"0.2", SCHEMA_VERSION}
SOFTWARE_NAME = "lif2tiff"
SOFTWARE_VERSION = "2.0.0-beta.1"
WINDOWS_INVALID_FILENAME_CHARACTERS = frozenset('<>:"/\\|?*')
WINDOWS_RESERVED_FILENAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)
MAX_OUTPUT_COMPONENT_BYTES = 240
OUTPUT_FIXED_OVERHEAD_BYTES = 64 * 1024 * 1024
OUTPUT_PROPORTIONAL_OVERHEAD_PERCENT = 5
MIN_POST_CONVERSION_FREE_BYTES = 1024 * 1024 * 1024


class LifToolError(RuntimeError):
    """Expected user-facing conversion or validation error."""


class ConversionCancelled(LifToolError):
    """A user-requested stop at a safe conversion boundary."""


def _iso_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def _json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _write_json_atomic(path: Path, value: object) -> None:
    _write_text_atomic(path, _json_text(value))


def _append_log(path: Path, message: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{_iso_now()}  {message}\n")


def _xml_bytes(lif) -> bytes:
    if hasattr(lif, "xml_bytes"):
        return lif.xml_bytes
    return ET.tostring(lif.xml_root, encoding="utf-8")


def _close_lif(lif) -> None:
    close = getattr(lif, "close", None)
    if callable(close):
        close()


def _file_sha256(
    path: Path,
    *,
    progress_callback: Callable[[int, int], None] | None = None,
    cancel_callback: Callable[[], bool] | None = None,
) -> str:
    digest = hashlib.sha256()
    total_bytes = path.stat().st_size
    completed_bytes = 0
    progress_interval = max(1, (total_bytes + 99) // 100)
    last_reported_bytes = 0
    with path.open("rb") as handle:
        while block := handle.read(8 * 1024 * 1024):
            if cancel_callback is not None and cancel_callback():
                raise ConversionCancelled("conversion cancelled by user")
            digest.update(block)
            completed_bytes += len(block)
            if progress_callback is not None and (
                completed_bytes == total_bytes
                or completed_bytes - last_reported_bytes >= progress_interval
            ):
                progress_callback(completed_bytes, total_bytes)
                last_reported_bytes = completed_bytes
    return digest.hexdigest()


def _json_sha256(value: object) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _source_signature(
    lif_path: Path,
    lif,
    *,
    progress_callback: Callable[[int, int], None] | None = None,
    cancel_callback: Callable[[], bool] | None = None,
) -> dict:
    stat = lif_path.stat()
    return {
        "filename": lif_path.name,
        "size_bytes": stat.st_size,
        "file_sha256": _file_sha256(
            lif_path,
            progress_callback=progress_callback,
            cancel_callback=cancel_callback,
        ),
        "metadata_sha256": hashlib.sha256(_xml_bytes(lif)).hexdigest(),
    }


def _safe_relative_path(value: str) -> bool:
    path = PurePosixPath(value)
    return bool(value) and not path.is_absolute() and ".." not in path.parts


def _series_id(index: int) -> str:
    return f"series-{index}"


def _series_filename(name: str) -> str:
    """Preserve a Leica series name while making it portable as one filename."""
    value = re.sub(r'[\x00-\x1f<>:"/\\|?*]+', "_", str(name))
    value = re.sub(r"\s*_\s*", "_", value).rstrip(" .")
    if not value:
        value = "unnamed"
    if value.upper() in {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }:
        value = f"_{value}"
    return value[:180]


def _channel_directory(index: int, label: str | None) -> str:
    safe_label = sanitize_name(label or f"ch{index}").lower() or f"ch{index}"
    return f"C{index:02d}_{safe_label[:40]}"


def _dtype_name(bit_depth: int) -> str:
    return "uint16" if bit_depth > 8 else "uint8"


def _load_calibration(path: Path | None) -> dict | None:
    if path is None:
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LifToolError(f"cannot read plate calibration: {exc}") from exc
    if not isinstance(value, dict):
        raise LifToolError("plate calibration must contain one JSON object")
    return value


def _metadata_paths(calibration: dict | None, include_intensity: bool) -> dict:
    paths = {
        "bundle": "metadata/metadata.json",
        "series_table": "metadata/series_metadata.csv",
        "channel_table": "metadata/channel_metadata.csv",
        "channel_resolution": "metadata/channel_resolution.json",
        "workflow_review": "metadata/workflow_review.json",
        "stage_table": "metadata/stage_positions.csv",
        "report": "metadata/metadata_report.md",
        "source_xml": "metadata/source_metadata.xml",
        "file_index": "metadata/files.csv",
        "stage_map": "views/stage_map.html",
        "conversion_log": "logs/conversion.log",
    }
    if calibration is not None:
        paths["plate_calibration"] = "metadata/plate_calibration.json"
    if include_intensity:
        paths["intensity_summary"] = "metadata/intensity_summary.csv"
    return paths


def _axes_and_shape(metadata: dict) -> tuple[str, list[int]]:
    dims = metadata["dimensions"]
    dim_type = metadata["dimension_type"]
    if dim_type == "2D":
        return "YX", [dims["y"], dims["x"]]
    if dim_type == "Z-stack":
        return "ZYX", [dims["z"], dims["y"], dims["x"]]
    if dim_type == "time-lapse":
        return "TYX", [dims["t"], dims["y"], dims["x"]]
    if dim_type == "ZT":
        return "TZYX", [dims["t"], dims["z"], dims["y"], dims["x"]]
    raise LifToolError(f"unsupported dimension type: {dim_type}")


def _file_specs(metadata: dict, channel_directories: dict[int, str]) -> list[dict]:
    dims = metadata["dimensions"]
    px = metadata["pixel_size"]
    dim_type = metadata["dimension_type"]
    axes, shape = _axes_and_shape(metadata)
    time_points = metadata.get("time_points_s") or []
    time_increment = None
    if len(time_points) > 1:
        increments = np.diff(np.asarray(time_points, dtype=float))
        if np.allclose(increments, increments[0], rtol=1e-6, atol=1e-9):
            time_increment = float(increments[0])
    specs = []
    series_name = _series_filename(metadata["series_name"])

    for channel in metadata["channel_info"]:
        c = int(channel["index"])
        bit_depth = int(channel.get("bit_depth") or metadata["bit_depth_per_channel"][c] or 8)
        extension = ".tif" if dim_type == "2D" else ".ome.tif"
        specs.append(
            {
                "channel_index": c,
                "axes": axes,
                "z_count": int(dims["z"]),
                "t_count": int(dims["t"]),
                "page_count": int(dims["z"]) * int(dims["t"]),
                "container": "TIFF" if dim_type == "2D" else "OME-TIFF",
                "path": f"{channel_directories[c]}/{series_name}{extension}",
                "shape": shape,
                "dtype": _dtype_name(bit_depth),
                "pixel_size_x_um": px.get("x_um_per_px"),
                "pixel_size_y_um": px.get("y_um_per_px"),
                "pixel_size_z_um": px.get("z_um_per_px") if dims["z"] > 1 else None,
                "time_increment_s": time_increment,
                "status": "pending",
            }
        )
    return specs


def _apply_channel_resolution(metadata: list[dict], report: dict) -> None:
    """Attach structured identity while keeping inferred and confirmed values separate."""
    resolved = {
        (int(row["series_index"]), int(row["channel_index"])): row
        for row in report["series_channels"]
    }
    for series in metadata:
        series_index = int(series["series_index"])
        for channel in series["channel_info"]:
            row = resolved[(series_index, int(channel["index"]))]
            physical_label = row["directory"].split("_", 1)[1]
            confirmed = row["confirmed_assignment"]
            confirmed_dye = row["confirmed_dye"]
            channel["physical_label"] = physical_label
            if confirmed is not None:
                channel["analysis_label"] = confirmed["id"]
                channel["stain"] = (
                    confirmed_dye["display_name"] if confirmed_dye is not None else None
                )
                channel["biological_role"] = confirmed.get("role") or "unknown"
                channel["assignment_source"] = confirmed["source"]
                channel["assignment_confidence"] = "manual"
            elif row["modality"]["value"] == "brightfield":
                channel["analysis_label"] = "brightfield"
                channel["stain"] = None
                channel["biological_role"] = "morphology"
                channel["assignment_source"] = "acquisition_modality"
                channel["assignment_confidence"] = row["modality"]["confidence"]
            else:
                channel["analysis_label"] = None
                channel["stain"] = None
                channel["biological_role"] = "unknown"
                channel["assignment_source"] = "unconfirmed"
                channel["assignment_confidence"] = "unknown"
            channel["identity"] = {
                "modality": {
                    "value": row["modality"]["value"],
                    "confidence": row["modality"]["confidence"],
                },
                "inferred_dye": (
                    {
                        key: row["inferred_dye"].get(key)
                        for key in ("id", "confidence", "source")
                    }
                    if row["inferred_dye"] is not None
                    else None
                ),
                "confirmed_dye": (
                    {
                        key: confirmed_dye.get(key)
                        for key in ("id", "source")
                    }
                    if confirmed_dye is not None
                    else None
                ),
                "confirmed_assignment": (
                    {
                        key: confirmed.get(key)
                        for key in ("id", "kind", "source")
                    }
                    if confirmed is not None
                    else None
                ),
                "review_status": row["review_status"],
                "protocol_match_ids": [
                    match["id"] for match in row["protocol_matches"]
                ],
            }


def _channel_resolution_summary(report: dict) -> dict:
    summary = {
        key: value
        for key, value in report.items()
        if key not in {"series_channels", "workflow_review"}
    }
    summary["workflow_review_file"] = "metadata/workflow_review.json"
    return summary


def _series_manifest(metadata: dict, channel_directories: dict[int, str]) -> dict:
    series_id = _series_id(metadata["series_index"])
    specs = _file_specs(metadata, channel_directories)
    channels = []
    for channel in metadata["channel_info"]:
        c = int(channel["index"])
        channels.append(
            {
                "index": c,
                "physical_label": channel.get("physical_label") or channel.get("label"),
                "analysis_label": channel.get("analysis_label"),
                "stain": channel.get("stain"),
                "biological_role": channel.get("biological_role"),
                "assignment_source": channel.get("assignment_source"),
                "assignment_confidence": channel.get("assignment_confidence"),
                "identity": channel.get("identity"),
                "files": [spec for spec in specs if spec["channel_index"] == c],
            }
        )
    return {
        "id": series_id,
        "index": metadata["series_index"],
        "source_series_index": metadata.get(
            "source_series_index", metadata["series_index"]
        ),
        "mosaic_index": metadata.get("mosaic_index"),
        "name": metadata["series_name"],
        "dimension_type": metadata["dimension_type"],
        "dimensions": metadata["dimensions"],
        "pixel_size": metadata["pixel_size"],
        "status": "pending",
        "channels": channels,
    }


def _channel_layout(metadata: list[dict]) -> tuple[dict[int, str], list[str]]:
    labels: dict[int, list[str]] = {}
    for item in metadata:
        for channel in item["channel_info"]:
            index = int(channel["index"])
            label = channel.get("physical_label") or channel.get("label") or f"ch{index}"
            labels.setdefault(index, [])
            if label not in labels[index]:
                labels[index].append(label)

    directories = {
        index: _channel_directory(index, values[0])
        for index, values in sorted(labels.items())
    }
    warnings = []
    for index, values in sorted(labels.items()):
        if len(values) > 1:
            warnings.append(
                f"C{index} physical label varies across series: {', '.join(values)}; "
                f"all outputs use {directories[index]}"
            )
    return directories, warnings


def _new_manifest(
    source: dict,
    metadata: list[dict],
    channel_overrides: dict[int, str],
    calibration: dict | None,
    include_intensity: bool,
    channel_resolution: dict,
) -> dict:
    now = _iso_now()
    channel_directories, warnings = _channel_layout(metadata)
    manifest = {
        "schema_name": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "software": {
            "name": SOFTWARE_NAME,
            "version": SOFTWARE_VERSION,
            "pixel_reader": {
                "name": LifFile.reader_name,
                "version": LifFile.reader_version,
            },
        },
        "project_status": "converting",
        "created_at": now,
        "updated_at": now,
        "completed_at": None,
        "source": source,
        "options": {
            "channel_overrides": {
                f"C{index}": role for index, role in sorted(channel_overrides.items())
            },
            "channel_assignment_mode": (
                "user_confirmed" if channel_overrides else "inferred_only"
            ),
            "review_acknowledged": channel_resolution["workflow"].get(
                "acknowledged", False
            ),
            "plate_calibration_used": calibration is not None,
            "plate_calibration_sha256": (
                _json_sha256(calibration) if calibration is not None else None
            ),
            "intensity_summary": include_intensity,
            "layout": "channel-first",
            "two_dimensional_format": "TIFF",
            "multidimensional_format": "OME-TIFF",
        },
        "channel_directories": {
            f"C{index}": path for index, path in channel_directories.items()
        },
        "channel_resolution": _channel_resolution_summary(channel_resolution),
        "metadata_files": _metadata_paths(calibration, include_intensity),
        "series": [
            _series_manifest(item, channel_directories)
            for item in metadata
        ],
        "warnings": warnings,
        "errors": [],
        "validation": None,
    }
    mosaic_outputs = sum(item.get("mosaic_index") is not None for item in metadata)
    if mosaic_outputs:
        manifest["warnings"].append(
            f"expanded Leica mosaic axis into {mosaic_outputs} virtual series; "
            "mosaic_index and source_series_index are recorded"
        )
    paths = [record["path"] for _, _, record in _iter_file_records(manifest)]
    duplicate_paths = [path for path, count in Counter(paths).items() if count > 1]
    if duplicate_paths:
        raise LifToolError(
            "series names produce duplicate output paths: "
            + ", ".join(sorted(duplicate_paths))
        )
    return manifest


def _iter_file_records(manifest: dict):
    for series in manifest.get("series", []):
        for channel in series.get("channels", []):
            for record in channel.get("files", []):
                yield series, channel, record


def _write_manifest(project_dir: Path, manifest: dict) -> None:
    manifest["updated_at"] = _iso_now()
    _write_json_atomic(project_dir / "manifest.json", manifest)


def _write_metadata_outputs(
    project_dir: Path,
    lif_path: Path,
    lif,
    metadata: list[dict],
    calibration: dict | None,
    include_intensity: bool,
    channel_resolution: dict,
) -> None:
    metadata_dir = project_dir / "metadata"
    views_dir = project_dir / "views"
    metadata_dir.mkdir(parents=True, exist_ok=True)
    views_dir.mkdir(parents=True, exist_ok=True)

    series_rows = [_flatten_series(item) for item in metadata]
    channel_rows = [row for item in metadata for row in _channel_rows(item)]
    stage_rows = [_stage_row(item) for item in metadata]
    full = {
        "schema_name": "candida_lif_metadata_bundle",
        "schema_version": "0.2",
        "created_at": _iso_now(),
        "source_lif": lif_path.name,
        "source_lif_name": lif_path.name,
        "source_lif_size_bytes": lif_path.stat().st_size,
        "series_count": len(metadata),
        "series": metadata,
    }

    _write_text_atomic(metadata_dir / "source_metadata.xml", _xml_bytes(lif).decode("utf-8"))
    _write_json_atomic(metadata_dir / "metadata.json", full)
    portable_resolution = {
        key: value
        for key, value in channel_resolution.items()
        if key != "workflow_review"
    }
    portable_resolution["workflow_review_file"] = "workflow_review.json"
    _write_json_atomic(metadata_dir / "channel_resolution.json", portable_resolution)
    _write_json_atomic(
        metadata_dir / "workflow_review.json",
        channel_resolution["workflow_review"],
    )
    _write_csv(metadata_dir / "series_metadata.csv", series_rows)
    _write_csv(metadata_dir / "channel_metadata.csv", channel_rows)
    _write_csv(metadata_dir / "stage_positions.csv", stage_rows)
    _write_report(
        metadata_dir / "metadata_report.md",
        lif_path,
        series_rows,
        channel_rows,
        [],
        include_intensity_file=include_intensity,
    )
    report_path = metadata_dir / "metadata_report.md"
    report = report_path.read_text(encoding="utf-8").replace(
        "`stage_map.html`", "`../views/stage_map.html`"
    )
    _write_text_atomic(report_path, report)
    write_stage_map(views_dir / "stage_map.html", stage_rows, series_rows, calibration=calibration)
    if calibration is not None:
        _write_json_atomic(metadata_dir / "plate_calibration.json", calibration)
def _frame_coordinates(record: dict):
    for t in range(int(record["t_count"])):
        for z in range(int(record["z_count"])):
            yield t, z


def _frame_iter(
    image,
    record: dict,
    *,
    check_cancelled: Callable[[], None] | None = None,
    plane_progress_callback: Callable[[int, int], None] | None = None,
):
    c = int(record["channel_index"])
    dtype = np.dtype(record["dtype"])
    total_planes = int(record["page_count"])
    for plane_number, (t, z) in enumerate(_frame_coordinates(record), start=1):
        if check_cancelled is not None:
            check_cancelled()
        yield np.asarray(image.get_frame(z=z, t=t, c=c), dtype=dtype)
        if plane_progress_callback is not None:
            plane_progress_callback(plane_number, total_planes)


def _write_tiff_atomic(
    project_dir: Path,
    image,
    record: dict,
    *,
    check_cancelled: Callable[[], None] | None = None,
    plane_progress_callback: Callable[[int, int], None] | None = None,
) -> None:
    destination = project_dir / record["path"]
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.stem}.tmp{destination.suffix}")
    try:
        if record["container"] == "TIFF":
            array = next(
                _frame_iter(
                    image,
                    record,
                    check_cancelled=check_cancelled,
                )
            )
            write_tiff(
                array,
                temporary,
                record.get("pixel_size_x_um"),
                record.get("pixel_size_y_um"),
            )
            if plane_progress_callback is not None:
                plane_progress_callback(1, 1)
        else:
            metadata = {"axes": record["axes"]}
            for axis, key in (
                ("X", "pixel_size_x_um"),
                ("Y", "pixel_size_y_um"),
                ("Z", "pixel_size_z_um"),
            ):
                value = record.get(key)
                if value:
                    metadata[f"PhysicalSize{axis}"] = value
                    metadata[f"PhysicalSize{axis}Unit"] = "um"
            if record.get("time_increment_s") is not None:
                metadata["TimeIncrement"] = record["time_increment_s"]
                metadata["TimeIncrementUnit"] = "s"
            kwargs = {
                "shape": tuple(record["shape"]),
                "dtype": np.dtype(record["dtype"]),
                "ome": True,
                "metadata": metadata,
                "photometric": "minisblack",
                "compression": "deflate",
                "bigtiff": (
                    int(np.prod(record["shape"])) * np.dtype(record["dtype"]).itemsize
                    >= 4_000_000_000
                ),
            }
            pixel_size_x = record.get("pixel_size_x_um")
            pixel_size_y = record.get("pixel_size_y_um")
            if pixel_size_x and pixel_size_y:
                kwargs["resolution"] = (1.0 / pixel_size_x, 1.0 / pixel_size_y)
            tifffile.imwrite(
                str(temporary),
                _frame_iter(
                    image,
                    record,
                    check_cancelled=check_cancelled,
                    plane_progress_callback=plane_progress_callback,
                ),
                **kwargs,
            )
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _resolution_um(page, tag_name: str) -> float | None:
    tag = page.tags.get(tag_name)
    if tag is None:
        return None
    value = tag.value
    if isinstance(value, tuple) and len(value) == 2:
        numerator, denominator = value
        pixels_per_um = numerator / denominator if denominator else 0
    else:
        pixels_per_um = float(value)
    return (1.0 / pixels_per_um) if pixels_per_um else None


def _validate_tiff(path: Path, record: dict) -> list[str]:
    errors = []
    try:
        with tifffile.TiffFile(path) as tif:
            if not tif.series:
                return [f"{record['path']}: TIFF contains no image series"]
            image_series = tif.series[0]
            actual_shape = list(image_series.shape)
            actual_dtype = str(image_series.dtype)
            if actual_shape != record["shape"]:
                errors.append(
                    f"{record['path']}: shape {actual_shape}, expected {record['shape']}"
                )
            if actual_dtype != record["dtype"]:
                errors.append(
                    f"{record['path']}: dtype {actual_dtype}, expected {record['dtype']}"
                )
            actual_axes = image_series.axes
            if actual_axes != record["axes"]:
                errors.append(
                    f"{record['path']}: axes {actual_axes}, expected {record['axes']}"
                )
            expected_pages = int(record["page_count"])
            if len(tif.pages) != expected_pages:
                errors.append(
                    f"{record['path']}: {len(tif.pages)} TIFF pages, expected {expected_pages}"
                )
            if record["container"] == "OME-TIFF" and not tif.is_ome:
                errors.append(f"{record['path']}: OME metadata is missing")
            if tif.pages:
                for tag_name, expected in (
                    ("XResolution", record.get("pixel_size_x_um")),
                    ("YResolution", record.get("pixel_size_y_um")),
                ):
                    actual = _resolution_um(tif.pages[0], tag_name)
                    if expected and actual and abs(actual - expected) > max(1e-5, expected * 1e-4):
                        errors.append(
                            f"{record['path']}: {tag_name} implies {actual:.6g} um/px, "
                            f"expected {expected:.6g}"
                        )
            expected_z = record.get("pixel_size_z_um")
            if expected_z and record["z_count"] > 1:
                pixels = None
                if tif.ome_metadata:
                    root = ET.fromstring(tif.ome_metadata)
                    pixels = next(
                        (element for element in root.iter() if element.tag.endswith("Pixels")),
                        None,
                    )
                actual_z = pixels.get("PhysicalSizeZ") if pixels is not None else None
                if actual_z is None:
                    errors.append(f"{record['path']}: OME PhysicalSizeZ is missing")
                elif abs(float(actual_z) - expected_z) > max(1e-5, expected_z * 1e-4):
                    errors.append(
                        f"{record['path']}: PhysicalSizeZ is {actual_z} um, "
                        f"expected {expected_z}"
                    )
            expected_t = record.get("time_increment_s")
            if expected_t is not None and record["t_count"] > 1:
                pixels = None
                if tif.ome_metadata:
                    root = ET.fromstring(tif.ome_metadata)
                    pixels = next(
                        (element for element in root.iter() if element.tag.endswith("Pixels")),
                        None,
                    )
                actual_t = pixels.get("TimeIncrement") if pixels is not None else None
                if actual_t is None:
                    errors.append(f"{record['path']}: OME TimeIncrement is missing")
                elif abs(float(actual_t) - expected_t) > max(1e-6, expected_t * 1e-5):
                    errors.append(
                        f"{record['path']}: TimeIncrement is {actual_t} s, "
                        f"expected {expected_t} s"
                    )
    except Exception as exc:
        errors.append(f"{record['path']}: cannot read TIFF ({exc})")
    return errors


def _record_is_valid(project_dir: Path, record: dict) -> bool:
    path = project_dir / record["path"]
    return path.is_file() and not _validate_tiff(path, record)


def _write_file_index(project_dir: Path, manifest: dict) -> None:
    rows = []
    for series, channel, record in _iter_file_records(manifest):
        rows.append(
            {
                "series_id": series["id"],
                "series_index": series["index"],
                "series_name": series["name"],
                "dimension_type": series["dimension_type"],
                "channel_index": channel["index"],
                "physical_label": channel.get("physical_label"),
                "analysis_label": channel.get("analysis_label"),
                "axes": record["axes"],
                "z_count": record["z_count"],
                "t_count": record["t_count"],
                "time_increment_s": record.get("time_increment_s"),
                "page_count": record["page_count"],
                "container": record["container"],
                "shape": "x".join(str(value) for value in record["shape"]),
                "dtype": record["dtype"],
                "relative_path": record["path"],
                "size_bytes": record.get("size_bytes"),
                "status": record["status"],
            }
        )
    _write_csv(project_dir / "metadata/files.csv", rows)


def _write_intensity_summary(project_dir: Path, manifest: dict) -> None:
    rows = []
    for series, channel, record in _iter_file_records(manifest):
        array = tifffile.imread(project_dir / record["path"])
        bit_depth = 16 if record["dtype"] == "uint16" else 8
        maximum = (2**bit_depth) - 1
        rows.append(
            {
                "series_id": series["id"],
                "series_index": series["index"],
                "series_name": series["name"],
                "channel_index": channel["index"],
                "physical_label": channel.get("physical_label"),
                "analysis_label": channel.get("analysis_label"),
                "axes": record["axes"],
                "z_count": record["z_count"],
                "t_count": record["t_count"],
                "relative_path": record["path"],
                "dtype": str(array.dtype),
                "pixel_count": int(array.size),
                "min": float(np.min(array)),
                "p01": float(np.percentile(array, 1)),
                "p05": float(np.percentile(array, 5)),
                "mean": float(np.mean(array)),
                "median": float(np.median(array)),
                "p95": float(np.percentile(array, 95)),
                "p99": float(np.percentile(array, 99)),
                "max": float(np.max(array)),
                "zero_fraction": float(np.mean(array == 0)),
                "saturated_fraction": float(np.mean(array >= maximum)),
            }
        )
    _write_csv(project_dir / "metadata/intensity_summary.csv", rows)


def inspect_channels(
    lif_path: Path,
    user_assignments: dict[int, str] | None = None,
) -> dict:
    lif_path = lif_path.resolve()
    if not lif_path.is_file() or lif_path.suffix.lower() != ".lif":
        raise LifToolError(f"input is not a readable .lif file: {lif_path}")
    lif = LifFile(str(lif_path))
    try:
        metadata = [
            extract_metadata(
                image,
                lif.xml_root,
                str(lif_path),
                index,
            )
            for index, image in enumerate(lif.get_iter_image())
        ]
        report = summarize_resolved_channels(
            metadata,
            user_assignments=user_assignments,
        )
        report["source_file"] = lif_path.name
        report["pixel_reader"] = {
            "name": LifFile.reader_name,
            "version": LifFile.reader_version,
        }
        return report
    finally:
        _close_lif(lif)


def build_conversion_plan(
    lif_path: Path,
    *,
    channel_overrides: dict[int, str] | None = None,
    plate_calibration: dict | None = None,
    include_intensity: bool = False,
    lif_factory=LifFile,
) -> dict:
    """Inspect one LIF without reading pixels and describe the planned project."""
    lif_path = lif_path.resolve()
    channel_overrides = channel_overrides or {}
    if not lif_path.is_file() or lif_path.suffix.lower() != ".lif":
        raise LifToolError(f"input is not a readable .lif file: {lif_path}")

    lif = lif_factory(str(lif_path))
    try:
        images = list(lif.get_iter_image())
        metadata = [
            extract_metadata(image, lif.xml_root, str(lif_path), index)
            for index, image in enumerate(images)
        ]
        channel_resolution = summarize_resolved_channels(
            metadata,
            user_assignments=channel_overrides,
        )
        _apply_channel_resolution(metadata, channel_resolution)
        channel_directories, warnings = _channel_layout(metadata)

        planned_series = []
        uncompressed_pixel_bytes = 0
        plane_count = 0
        output_unit_count = 0
        for item in metadata:
            specs = _file_specs(item, channel_directories)
            specs_by_channel = {int(spec["channel_index"]): spec for spec in specs}
            outputs = []
            for channel in item["channel_info"]:
                channel_index = int(channel["index"])
                spec = dict(specs_by_channel[channel_index])
                spec.pop("status", None)
                output_unit_count += 1
                plane_count += int(spec["page_count"])
                uncompressed_pixel_bytes += (
                    int(np.prod(spec["shape"])) * np.dtype(spec["dtype"]).itemsize
                )
                outputs.append(
                    {
                        "channel_index": channel_index,
                        "physical_label": channel.get("physical_label")
                        or channel.get("label"),
                        "source_lut": channel.get("lut"),
                        "source_display_settings": channel.get(
                            "source_display_settings"
                        ),
                        "identity": channel.get("identity"),
                        "file": spec,
                    }
                )
            planned_series.append(
                {
                    "series_index": item["series_index"],
                    "series_name": item["series_name"],
                    "dimension_type": item["dimension_type"],
                    "dimensions": item["dimensions"],
                    "pixel_size": item["pixel_size"],
                    "stage_position": item["stage_position"],
                    "outputs": outputs,
                }
            )

        dimension_counts = Counter(item["dimension_type"] for item in metadata)
        workflow = channel_resolution["workflow"]
        required_actions = []
        if workflow["requires_acknowledgement"]:
            required_actions.append("review_acquisition_variants")
        optional_actions = []
        if any(
            channel["modality"] == "fluorescence"
            and channel["confirmed_assignment"] is None
            for channel in channel_resolution["channels"]
        ):
            optional_actions.append("confirm_channel_identity")

        compact_resolution = {
            key: value
            for key, value in channel_resolution.items()
            if key not in {"series_channels", "workflow_review"}
        }
        return {
            "schema_name": "candida_lif_conversion_plan",
            "schema_version": "0.1",
            "created_at": _iso_now(),
            "software": {
                "name": SOFTWARE_NAME,
                "version": SOFTWARE_VERSION,
                "pixel_reader": {
                    "name": LifFile.reader_name,
                    "version": LifFile.reader_version,
                },
            },
            "inspection": {
                "scope": "metadata_only",
                "pixels_read": False,
                "full_file_sha256": "deferred_until_conversion",
            },
            "source": {
                "filename": lif_path.name,
                "size_bytes": lif_path.stat().st_size,
                "metadata_sha256": hashlib.sha256(_xml_bytes(lif)).hexdigest(),
            },
            "status": workflow["status"],
            "required_actions": required_actions,
            "optional_actions": optional_actions,
            "summary": {
                "series_count": len(metadata),
                "dimension_type_counts": dict(sorted(dimension_counts.items())),
                "channel_keys": channel_resolution["consistency"]["channel_keys"],
                "output_unit_count": output_unit_count,
                "plane_count": plane_count,
                "estimated_uncompressed_pixel_bytes": uncompressed_pixel_bytes,
                "estimate_note": (
                    "Pixel payload only; TIFF metadata and deflate compression are excluded."
                ),
            },
            "output_policy": {
                "suggested_directory_name": _default_output(lif_path, "_tiff").name,
                "layout": "channel-first",
                "two_dimensional_format": "TIFF",
                "multidimensional_format": "OME-TIFF",
                "compression": "deflate",
                "master_image_representation": "native-intensity grayscale",
                "display_transform_applied": False,
                "metadata_files": _metadata_paths(
                    plate_calibration,
                    include_intensity,
                ),
            },
            "options": {
                "channel_overrides": {
                    f"C{index}": role
                    for index, role in sorted(channel_overrides.items())
                },
                "include_intensity": include_intensity,
                "plate_calibration_used": plate_calibration is not None,
                "plate_calibration_sha256": (
                    _json_sha256(plate_calibration)
                    if plate_calibration is not None
                    else None
                ),
            },
            "channel_directories": {
                f"C{index}": path for index, path in channel_directories.items()
            },
            "channel_resolution": compact_resolution,
            "workflow_review": channel_resolution["workflow_review"],
            "series": planned_series,
            "warnings": warnings,
        }
    finally:
        _close_lif(lif)


def compact_conversion_plan(plan: dict) -> str:
    summary = plan["summary"]
    workflow = plan["channel_resolution"]["workflow"]
    dimension_text = ", ".join(
        f"{key}={value}" for key, value in summary["dimension_type_counts"].items()
    )
    lines = [
        f"Plan: {workflow['route']} ({workflow['status']})",
        f"Series: {summary['series_count']} ({dimension_text})",
        f"Outputs: {summary['output_unit_count']} TIFF unit(s), "
        f"{summary['plane_count']} plane(s)",
        "Estimated uncompressed pixels: "
        f"{summary['estimated_uncompressed_pixel_bytes']} bytes",
        f"Suggested directory: {plan['output_policy']['suggested_directory_name']}",
    ]
    if plan["required_actions"]:
        lines.append("Required: " + ", ".join(plan["required_actions"]))
    if plan["optional_actions"]:
        lines.append("Optional: " + ", ".join(plan["optional_actions"]))
    return "\n".join(lines)


def validate_project(
    project_dir: Path,
    require_complete: bool = True,
    source_lif: Path | None = None,
) -> dict:
    project_dir = project_dir.resolve()
    errors = []
    warnings = []
    manifest_path = project_dir / "manifest.json"
    if not manifest_path.is_file():
        return {
            "ok": False,
            "checked_at": _iso_now(),
            "files_checked": 0,
            "errors": ["manifest.json is missing"],
            "warnings": [],
        }
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "ok": False,
            "checked_at": _iso_now(),
            "files_checked": 0,
            "errors": [f"cannot read manifest.json: {exc}"],
            "warnings": [],
        }

    if manifest.get("schema_name") != SCHEMA_NAME:
        errors.append(f"unexpected schema_name: {manifest.get('schema_name')!r}")
    if manifest.get("schema_version") not in SUPPORTED_SCHEMA_VERSIONS:
        errors.append(f"unsupported schema_version: {manifest.get('schema_version')!r}")
    if require_complete and manifest.get("project_status") != "complete":
        errors.append(f"project_status is {manifest.get('project_status')!r}, expected 'complete'")

    declared_paths = set()
    for key, value in (manifest.get("metadata_files") or {}).items():
        if not _safe_relative_path(value):
            errors.append(f"metadata_files.{key} is not a safe relative path: {value!r}")
            continue
        declared_paths.add(value)
        if not (project_dir / value).is_file():
            errors.append(f"declared metadata file is missing: {value}")

    source_xml_path = (manifest.get("metadata_files") or {}).get("source_xml")
    expected_xml_hash = (manifest.get("source") or {}).get("metadata_sha256")
    if source_xml_path and expected_xml_hash and (project_dir / source_xml_path).is_file():
        actual_xml_hash = hashlib.sha256((project_dir / source_xml_path).read_bytes()).hexdigest()
        if actual_xml_hash != expected_xml_hash:
            errors.append("metadata/source_metadata.xml differs from the source XML fingerprint")

    options = manifest.get("options") or {}
    if options.get("plate_calibration_used"):
        calibration_path = (manifest.get("metadata_files") or {}).get("plate_calibration")
        expected_calibration_hash = options.get("plate_calibration_sha256")
        if not calibration_path or not (project_dir / calibration_path).is_file():
            errors.append("plate calibration was used but its project copy is missing")
        elif not expected_calibration_hash:
            warnings.append("plate calibration fingerprint is missing from this alpha manifest")
        else:
            try:
                saved_calibration = json.loads(
                    (project_dir / calibration_path).read_text(encoding="utf-8")
                )
                if _json_sha256(saved_calibration) != expected_calibration_hash:
                    errors.append("saved plate calibration differs from its manifest fingerprint")
            except (OSError, json.JSONDecodeError) as exc:
                errors.append(f"cannot validate saved plate calibration: {exc}")

    files_checked = 0
    pixel_arrays_compared = 0
    pixel_planes_compared = 0
    expected_tiffs = set()
    for series, _channel, record in _iter_file_records(manifest):
        value = record.get("path", "")
        if not _safe_relative_path(value):
            errors.append(f"unsafe TIFF path in {series.get('id')}: {value!r}")
            continue
        expected_tiffs.add(value)
        path = project_dir / value
        if not path.is_file():
            errors.append(f"declared TIFF is missing: {value}")
            continue
        files_checked += 1
        errors.extend(_validate_tiff(path, record))
        if record.get("status") != "complete":
            errors.append(f"{value}: file status is {record.get('status')!r}")

    actual_tiffs = {
        path.relative_to(project_dir).as_posix()
        for path in project_dir.rglob("*.tif")
        if ".tmp" not in path.name
    }
    temporary_artifacts = [
        path.relative_to(project_dir).as_posix()
        for path in project_dir.rglob("*")
        if path.is_file() and ".tmp" in path.name
    ]
    for artifact in sorted(temporary_artifacts):
        errors.append(f"temporary conversion artifact remains: {artifact}")
    for unexpected in sorted(actual_tiffs - expected_tiffs):
        errors.append(f"undeclared or duplicate TIFF found: {unexpected}")
    for missing in sorted(expected_tiffs - actual_tiffs):
        if f"declared TIFF is missing: {missing}" not in errors:
            errors.append(f"declared TIFF is missing: {missing}")

    for series in manifest.get("series", []):
        if series.get("status") != "complete":
            errors.append(
                f"{series.get('id', 'unknown series')}: status is {series.get('status')!r}"
            )

    source_verified = None
    if source_lif is not None:
        source_lif = source_lif.resolve()
        lif = None
        try:
            lif = LifFile(str(source_lif))
            actual_source = _source_signature(source_lif, lif)
            if actual_source != manifest.get("source"):
                errors.append("provided source LIF does not match the manifest source signature")
                source_verified = False
            else:
                images = list(lif.get_iter_image())
                for series, _channel, record in _iter_file_records(manifest):
                    index = int(series["index"])
                    if index >= len(images):
                        errors.append(f"{series['id']}: source series index is out of range")
                        continue
                    with tifffile.TiffFile(project_dir / record["path"]) as tif:
                        pages = tif.series[0].pages
                        coordinates = list(_frame_coordinates(record))
                        if len(pages) != len(coordinates):
                            errors.append(
                                f"{record['path']}: cannot compare source pixels because "
                                "the TIFF page count differs"
                            )
                            continue
                        for page, (t, z) in zip(pages, coordinates):
                            source_plane = np.asarray(
                                images[index].get_frame(
                                    z=z,
                                    t=t,
                                    c=int(record["channel_index"]),
                                ),
                                dtype=np.dtype(record["dtype"]),
                            )
                            if not np.array_equal(source_plane, page.asarray()):
                                errors.append(
                                    f"{record['path']}: exported pixels differ from source "
                                    f"at T={t}, Z={z}"
                                )
                                break
                            pixel_planes_compared += 1
                    pixel_arrays_compared += 1
                source_verified = not any(
                    "source" in error or "pixels differ" in error for error in errors
                )
        except Exception as exc:
            errors.append(f"cannot perform source pixel verification: {exc}")
            source_verified = False
        finally:
            if lif is not None:
                _close_lif(lif)

    return {
        "ok": not errors,
        "checked_at": _iso_now(),
        "files_checked": files_checked,
        "source_verified": source_verified,
        "pixel_arrays_compared": pixel_arrays_compared,
        "pixel_planes_compared": pixel_planes_compared,
        "errors": errors,
        "warnings": warnings,
    }


def convert_lif(
    lif_path: Path,
    output_dir: Path,
    *,
    resume: bool = False,
    channel_overrides: dict[int, str] | None = None,
    plate_calibration: dict | None = None,
    include_intensity: bool = False,
    allow_review: bool = False,
    lif_factory=LifFile,
    progress_callback: Callable[[str], None] | None = None,
    cancel_callback: Callable[[], bool] | None = None,
) -> Path:
    lif_path = lif_path.resolve()
    output_dir = output_dir.resolve()
    partial_dir = output_dir.with_name(f"{output_dir.name}.partial")
    channel_overrides = channel_overrides or {}

    if not lif_path.is_file() or lif_path.suffix.lower() != ".lif":
        raise LifToolError(f"input is not a readable .lif file: {lif_path}")
    if output_dir.exists():
        raise LifToolError(f"output already exists; refusing to overwrite: {output_dir}")
    if partial_dir.exists() and not resume:
        raise LifToolError(
            f"partial output exists: {partial_dir}; inspect it and use --resume to continue"
        )
    if resume and not partial_dir.exists():
        raise LifToolError(f"no partial output exists to resume: {partial_dir}")

    def progress(message: str) -> None:
        if progress_callback is not None:
            progress_callback(message)

    def check_cancelled() -> None:
        if cancel_callback is not None and cancel_callback():
            raise ConversionCancelled("conversion cancelled by user")

    progress(f"Fingerprinting and inspecting {lif_path.name}")
    lif = lif_factory(str(lif_path))
    try:
        source = _source_signature(
            lif_path,
            lif,
            progress_callback=lambda done, total: progress(
                f"Fingerprint progress: {done}/{total} bytes"
            ),
            cancel_callback=cancel_callback,
        )
        images = list(lif.get_iter_image())
        metadata = [
            extract_metadata(
                image,
                lif.xml_root,
                str(lif_path),
                index,
            )
            for index, image in enumerate(images)
        ]
        channel_resolution = summarize_resolved_channels(
            metadata,
            user_assignments=channel_overrides,
        )
        workflow = channel_resolution["workflow"]
        if workflow["requires_acknowledgement"] and not allow_review:
            reasons = ", ".join(workflow["reason_codes"])
            raise LifToolError(
                "review required before conversion: "
                f"{reasons}; inspect with 'liftool channels' and explicitly "
                "acknowledge the review to continue"
            )
        workflow["acknowledged"] = bool(
            workflow["requires_acknowledgement"] and allow_review
        )
        workflow["acknowledgement_source"] = (
            "explicit_override" if workflow["acknowledged"] else None
        )
        if workflow["acknowledged"]:
            review = channel_resolution["workflow_review"]
            review["decision"] = {
                "status": "accepted",
                "action": "continue_all",
                "source": "explicit_override",
                "accepted_reason_codes": list(workflow["reason_codes"]),
                "reviewed_variant_group_ids": list(review["variant_group_ids"]),
                "note": None,
            }
        _apply_channel_resolution(metadata, channel_resolution)
        expected = _new_manifest(
            source,
            metadata,
            channel_overrides,
            plate_calibration,
            include_intensity,
            channel_resolution,
        )
        estimated_pixel_bytes = sum(
            int(np.prod(record["shape"])) * np.dtype(record["dtype"]).itemsize
            for _series, _channel, record in _iter_file_records(expected)
        )
        storage = output_storage_preflight(output_dir, estimated_pixel_bytes)
        if storage["errors"]:
            raise LifToolError(
                "output storage preflight failed: " + " ".join(storage["errors"])
            )
    except BaseException:
        _close_lif(lif)
        raise
    total_files = sum(
        1 for _series, _channel, _record in _iter_file_records(expected)
    )
    progress(f"Found {len(images)} series and {total_files} TIFF output unit(s)")

    try:
        if resume:
            try:
                previous = json.loads(
                    (partial_dir / "manifest.json").read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError) as exc:
                raise LifToolError(
                    f"cannot resume without a valid partial manifest: {exc}"
                ) from exc
            if previous.get("source") != source:
                raise LifToolError(
                    "partial output belongs to a different or changed source LIF"
                )
            if previous.get("schema_version") != SCHEMA_VERSION:
                raise LifToolError(
                    "partial output uses a different manifest schema and cannot be resumed"
                )
            if previous.get("options") != expected.get("options"):
                raise LifToolError("resume options differ from the original conversion")
            expected["created_at"] = previous.get("created_at", expected["created_at"])
            expected["warnings"] = previous.get("warnings", [])
            _append_log(partial_dir / "logs/conversion.log", "resuming conversion")
        else:
            partial_dir.mkdir(parents=True, exist_ok=False)
            _append_log(
                partial_dir / "logs/conversion.log",
                f"started conversion of {lif_path.name}",
            )
    except BaseException:
        _close_lif(lif)
        raise

    manifest = expected
    _write_manifest(partial_dir, manifest)

    try:
        _write_metadata_outputs(
            partial_dir,
            lif_path,
            lif,
            metadata,
            plate_calibration,
            include_intensity,
            channel_resolution,
        )
        completed_files = 0
        progress_interval = max(1, total_files // 20)
        total_planes = sum(
            int(record["page_count"])
            for _series, _channel, record in _iter_file_records(manifest)
        )
        completed_planes = 0
        plane_progress_interval = max(1, (total_planes + 99) // 100)
        last_reported_planes = 0

        def report_plane_progress(current_planes: int) -> None:
            nonlocal last_reported_planes
            if (
                current_planes == total_planes
                or current_planes - last_reported_planes >= plane_progress_interval
            ):
                progress(f"Plane progress: {current_planes}/{total_planes}")
                last_reported_planes = current_planes

        for series_number, (image, series) in enumerate(
            zip(images, manifest["series"]),
            start=1,
        ):
            check_cancelled()
            progress(
                f"Series {series_number}/{len(images)}: "
                f"{series['id']} ({series['dimension_type']})"
            )
            _append_log(
                partial_dir / "logs/conversion.log",
                f"processing {series['id']} ({series['dimension_type']})",
            )
            for _series, _channel, record in _iter_file_records({"series": [series]}):
                check_cancelled()
                record_planes = int(record["page_count"])
                if _record_is_valid(partial_dir, record):
                    record["status"] = "complete"
                    record["size_bytes"] = (partial_dir / record["path"]).stat().st_size
                else:
                    planes_before_record = completed_planes
                    _write_tiff_atomic(
                        partial_dir,
                        image,
                        record,
                        check_cancelled=check_cancelled,
                        plane_progress_callback=lambda done, _total: report_plane_progress(
                            planes_before_record + done
                        ),
                    )
                    immediate_errors = _validate_tiff(partial_dir / record["path"], record)
                    if immediate_errors:
                        raise LifToolError("; ".join(immediate_errors))
                    record["status"] = "complete"
                    record["size_bytes"] = (partial_dir / record["path"]).stat().st_size
                completed_planes += record_planes
                report_plane_progress(completed_planes)
                completed_files += 1
                _write_manifest(partial_dir, manifest)
                if (
                    completed_files == total_files
                    or completed_files % progress_interval == 0
                ):
                    progress(f"TIFF progress: {completed_files}/{total_files}")
            series["status"] = "complete"
            _write_manifest(partial_dir, manifest)

        _write_file_index(partial_dir, manifest)
        if include_intensity:
            progress("Computing optional intensity summary")
            _write_intensity_summary(partial_dir, manifest)
        manifest["project_status"] = "ready_for_validation"
        _write_manifest(partial_dir, manifest)
        progress("Validating project structure and TIFF metadata")
        validation = validate_project(partial_dir, require_complete=False)
        manifest["validation"] = validation
        if not validation["ok"]:
            manifest["project_status"] = "validation_failed"
            manifest["errors"].extend(validation["errors"])
            _write_manifest(partial_dir, manifest)
            raise LifToolError(
                f"validation failed with {len(validation['errors'])} error(s); "
                f"partial output kept at {partial_dir}"
            )
        manifest["project_status"] = "complete"
        manifest["completed_at"] = _iso_now()
        _write_manifest(partial_dir, manifest)
        _append_log(partial_dir / "logs/conversion.log", "conversion and validation complete")
        os.replace(partial_dir, output_dir)
        final_validation = validate_project(output_dir, require_complete=True)
        if not final_validation["ok"]:
            raise LifToolError(
                "post-move validation failed: " + "; ".join(final_validation["errors"])
            )
        progress("Conversion complete")
        return output_dir
    except BaseException as exc:
        if partial_dir.exists():
            if isinstance(exc, (KeyboardInterrupt, ConversionCancelled)):
                manifest["project_status"] = "interrupted"
            elif manifest.get("project_status") != "validation_failed":
                manifest["project_status"] = "failed"
            message = f"{type(exc).__name__}: {exc}"
            if message not in manifest["errors"]:
                manifest["errors"].append(message)
            _write_manifest(partial_dir, manifest)
            _append_log(partial_dir / "logs/conversion.log", message)
        raise
    finally:
        _close_lif(lif)


def _replace_windows_invalid_characters(value: str) -> str:
    result = []
    replacing = False
    for character in unicodedata.normalize("NFC", value):
        invalid = ord(character) < 32 or character in WINDOWS_INVALID_FILENAME_CHARACTERS
        if invalid:
            if not replacing and (not result or result[-1] != "_"):
                result.append("_")
            replacing = True
        else:
            result.append(character)
            replacing = False
    return "".join(result)


def _truncate_output_component(stem: str, suffix: str) -> str:
    candidate = f"{stem}{suffix}"
    if len(candidate.encode("utf-8")) <= MAX_OUTPUT_COMPONENT_BYTES:
        return candidate
    digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()[:10]
    ending = f"__{digest}{suffix}"
    budget = MAX_OUTPUT_COMPONENT_BYTES - len(ending.encode("utf-8"))
    prefix = ""
    used = 0
    for character in stem:
        encoded = character.encode("utf-8")
        if used + len(encoded) > budget:
            break
        prefix += character
        used += len(encoded)
    prefix = prefix.rstrip(" .") or "lif"
    return f"{prefix}{ending}"


def portable_output_name(lif_path: Path, suffix: str) -> str:
    """Return a readable output name that remains safe on Linux and Windows."""
    stem = _replace_windows_invalid_characters(lif_path.stem).rstrip(" .") or "lif"
    candidate = _truncate_output_component(stem, suffix)
    if candidate.rstrip(" .").upper() in WINDOWS_RESERVED_FILENAMES:
        candidate = f"_{candidate}"
    return candidate


def portable_path_collision_key(path: Path) -> tuple[str, ...]:
    """Normalize a path for conservative cross-platform collision checks."""
    resolved = path.expanduser().resolve(strict=False)
    return tuple(
        unicodedata.normalize("NFC", part).rstrip(" .").casefold()
        for part in resolved.parts
    )


def estimated_output_storage_bytes(pixel_bytes: int) -> int:
    pixel_bytes = max(0, int(pixel_bytes))
    proportional = (
        pixel_bytes * OUTPUT_PROPORTIONAL_OVERHEAD_PERCENT + 99
    ) // 100
    return pixel_bytes + max(OUTPUT_FIXED_OVERHEAD_BYTES, proportional)


def output_storage_preflight(output: Path, pixel_bytes: int) -> dict:
    """Inspect output filesystem capacity and permissions without writing."""
    output = output.expanduser().resolve(strict=False)
    candidate = output.parent
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    required = estimated_output_storage_bytes(pixel_bytes)
    errors = []
    warnings = []
    result = {
        "status": "ready",
        "checked_parent": str(candidate),
        "estimated_pixel_bytes": max(0, int(pixel_bytes)),
        "required_bytes": required,
        "free_bytes": None,
        "total_bytes": None,
        "device_id": None,
        "errors": errors,
        "warnings": warnings,
        "estimate_note": (
            "Uncompressed pixel estimate plus max(64 MiB, 5%) overhead; "
            "resume does not subtract existing partial files."
        ),
    }
    if not candidate.exists() or not candidate.is_dir():
        errors.append(
            f"The nearest existing output parent is not a directory: {candidate}."
        )
        result["status"] = "blocked"
        return result
    if not os.access(candidate, os.W_OK | os.X_OK):
        errors.append(f"The output parent is not writable: {candidate}.")
    try:
        usage = shutil.disk_usage(candidate)
        result["free_bytes"] = int(usage.free)
        result["total_bytes"] = int(usage.total)
        result["device_id"] = int(candidate.stat().st_dev)
        if usage.free < required:
            errors.append(
                "Insufficient free space: "
                f"approximately {required} bytes required, {usage.free} bytes free."
            )
        elif usage.free - required < MIN_POST_CONVERSION_FREE_BYTES:
            warnings.append(
                "Low remaining space: less than 1 GiB is expected after conversion."
            )
    except OSError as exc:
        warnings.append(f"Free disk space could not be determined: {exc}.")
    if errors:
        result["status"] = "blocked"
    return result


def _default_output(lif_path: Path, suffix: str) -> Path:
    return lif_path.parent / portable_output_name(lif_path, suffix)


def prompt_channel_assignments(
    report: dict,
    initial: dict[int, str] | None = None,
    *,
    input_func=input,
    output_func=print,
) -> dict[int, str]:
    """Interactively confirm, replace, clear, or explicitly leave channel identity unknown."""
    assignments = dict(initial or {})
    output_func(compact_channel_table(report))
    output_func(
        "\nConfirm channels. Enter keeps the current unconfirmed result; "
        "use a number, dye name/id, b=brightfield, u=unknown, or -=clear."
    )
    for channel in report["channels"]:
        index = int(channel["channel_key"][1:])
        candidates = [
            item for item in channel["stain_candidates"] if item["kind"] == "stain"
        ]
        numbered = ", ".join(
            f"{number}={item['id']}" for number, item in enumerate(candidates, start=1)
        )
        current = assignments.get(index, "unconfirmed")
        details = f"; {numbered}" if numbered else ""
        answer = input_func(
            f"{channel['channel_key']} [{current}{details}]: "
        ).strip()
        if not answer:
            continue
        if answer == "-":
            assignments.pop(index, None)
            continue
        if answer.isdigit() and 1 <= int(answer) <= len(candidates):
            assignments[index] = candidates[int(answer) - 1]["id"]
            continue
        shortcuts = {"b": "brightfield", "u": "unknown"}
        assignments[index] = canonical_assignment_id(shortcuts.get(answer.lower(), answer))
    return assignments


def _add_channel_roles(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--channel-role",
        action="append",
        default=[],
        metavar="C0=bodipy",
        help=(
            "confirm a dye/modality assignment; accepts registry ids or familiar names "
            "such as BODIPY, Nile Red, PI, brightfield, or unknown; repeat as needed"
        ),
    )
    parser.add_argument(
        "--ask-channel-roles",
        action="store_true",
        help="interactively review channel candidates before writing output",
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="lif2tiff", description=__doc__)
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {SOFTWARE_VERSION}",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser("inspect", help="extract metadata without TIFF conversion")
    inspect_parser.add_argument("lif", type=Path)
    inspect_parser.add_argument("-o", "--output", type=Path)
    inspect_parser.add_argument("--no-intensity", action="store_true")
    inspect_parser.add_argument("--plate-calibration", type=Path)

    channels_parser = subparsers.add_parser(
        "channels",
        help="diagnose channel identity and acquisition consistency without conversion",
    )
    channels_parser.add_argument("lif", type=Path)
    channels_parser.add_argument(
        "--json-output",
        type=Path,
        help="optional new JSON report path",
    )
    _add_channel_roles(channels_parser)

    plan_parser = subparsers.add_parser(
        "plan",
        help="build a metadata-only conversion plan without reading image pixels",
    )
    plan_parser.add_argument("lif", type=Path)
    plan_parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="optional new JSON plan path",
    )
    plan_parser.add_argument("--include-intensity", action="store_true")
    plan_parser.add_argument("--plate-calibration", type=Path)
    _add_channel_roles(plan_parser)

    convert_parser = subparsers.add_parser("convert", help="convert one LIF into a validated project")
    convert_parser.add_argument("lif", type=Path)
    convert_parser.add_argument("-o", "--output", type=Path)
    convert_parser.add_argument("--resume", action="store_true")
    convert_parser.add_argument("--include-intensity", action="store_true")
    convert_parser.add_argument("--plate-calibration", type=Path)
    convert_parser.add_argument(
        "--allow-review",
        action="store_true",
        help="continue after explicitly reviewing a non-standard acquisition",
    )
    _add_channel_roles(convert_parser)

    validate_parser = subparsers.add_parser("validate", help="validate an existing conversion project")
    validate_parser.add_argument("project", type=Path)
    validate_parser.add_argument(
        "--source",
        type=Path,
        help="optional original LIF for exact exported-pixel comparison",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        if args.command == "inspect":
            lif_path = args.lif.resolve()
            output = (args.output or _default_output(lif_path, "_inspection")).resolve()
            if output.exists():
                raise LifToolError(f"output already exists; refusing to overwrite: {output}")
            calibration = _load_calibration(args.plate_calibration)
            inspect_lif(
                lif_path,
                output,
                include_intensity=not args.no_intensity,
                plate_calibration=calibration,
            )
            print(output)
            return 0

        if args.command == "convert":
            lif_path = args.lif.resolve()
            output = (args.output or _default_output(lif_path, "_tiff")).resolve()
            overrides = parse_channel_assignments(args.channel_role)
            if args.ask_channel_roles:
                if not sys.stdin.isatty():
                    raise LifToolError("--ask-channel-roles requires an interactive terminal")
                preview = inspect_channels(lif_path, user_assignments=overrides)
                overrides = prompt_channel_assignments(preview, overrides)
            calibration = _load_calibration(args.plate_calibration)
            result = convert_lif(
                lif_path,
                output,
                resume=args.resume,
                channel_overrides=overrides,
                plate_calibration=calibration,
                include_intensity=args.include_intensity,
                allow_review=args.allow_review,
                progress_callback=lambda message: print(message, flush=True),
            )
            print(result)
            return 0

        if args.command == "plan":
            overrides = parse_channel_assignments(args.channel_role)
            if args.ask_channel_roles:
                if not sys.stdin.isatty():
                    raise LifToolError("--ask-channel-roles requires an interactive terminal")
                preview = inspect_channels(args.lif, user_assignments=overrides)
                overrides = prompt_channel_assignments(preview, overrides)
            calibration = _load_calibration(args.plate_calibration)
            plan = build_conversion_plan(
                args.lif,
                channel_overrides=overrides,
                plate_calibration=calibration,
                include_intensity=args.include_intensity,
            )
            print(compact_conversion_plan(plan))
            if args.output is not None:
                output = args.output.resolve()
                if output.exists():
                    raise LifToolError(
                        f"output already exists; refusing to overwrite: {output}"
                    )
                _write_text_atomic(output, _json_text(plan))
                print(f"\nJSON: {output}")
            return 0

        if args.command == "channels":
            overrides = parse_channel_assignments(args.channel_role)
            report = inspect_channels(args.lif, user_assignments=overrides)
            if args.ask_channel_roles:
                if not sys.stdin.isatty():
                    raise LifToolError("--ask-channel-roles requires an interactive terminal")
                overrides = prompt_channel_assignments(report, overrides)
                report = inspect_channels(args.lif, user_assignments=overrides)
            print(compact_channel_table(report))
            if args.json_output is not None:
                output = args.json_output.resolve()
                if output.exists():
                    raise LifToolError(
                        f"output already exists; refusing to overwrite: {output}"
                    )
                _write_text_atomic(output, _json_text(report))
                print(f"\nJSON: {output}")
            return 0

        validation = validate_project(
            args.project,
            require_complete=True,
            source_lif=args.source,
        )
        print(_json_text(validation), end="")
        return 0 if validation["ok"] else 1
    except (LifToolError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
