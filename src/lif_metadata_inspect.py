#!/usr/bin/env python3
"""Extract Leica LIF metadata into machine-readable tables and a short report."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import os
import statistics
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np

from liffile_adapter import LifFileAdapter as LifFile
from lif2tiff_project import extract_metadata, sanitize_name
from stage_map import write_stage_map


def _iso_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def _safe_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first_timestamp(metadata: dict) -> str | None:
    timestamps = metadata.get("acquisition_timestamps") or []
    return timestamps[0] if timestamps else None


def _infer_modality(channel: dict) -> str:
    detector_name = (channel.get("detector_name") or "").lower()
    scan_type = (channel.get("scan_type") or "").lower()
    label = (channel.get("label") or "").lower()
    if "trans" in detector_name or scan_type == "tld" or label == "brightfield":
        return "brightfield_like"
    if detector_name or label in {"green", "red", "blue", "cyan", "magenta", "yellow"}:
        return "fluorescence_like"
    return "unknown"


def _flatten_series(metadata: dict) -> dict:
    dims = metadata["dimensions"]
    px = metadata["pixel_size"]
    obj = metadata.get("objective", {})
    conf = metadata.get("confocal_settings", {})
    scope = metadata.get("microscope", {})
    stage = metadata.get("stage_position", {})
    optical = metadata.get("optical_settings", {})
    return {
        "source_file": metadata.get("source_file"),
        "series_index": metadata.get("series_index"),
        "source_series_index": metadata.get("source_series_index"),
        "mosaic_index": metadata.get("mosaic_index"),
        "series_name": metadata.get("series_name"),
        "dimension_type": metadata.get("dimension_type"),
        "x_px": dims.get("x"),
        "y_px": dims.get("y"),
        "z_count": dims.get("z"),
        "t_count": dims.get("t"),
        "channel_count": dims.get("channels"),
        "pixel_size_x_um": px.get("x_um_per_px"),
        "pixel_size_y_um": px.get("y_um_per_px"),
        "pixel_size_z_um": px.get("z_um_per_px"),
        "bit_depths": ";".join(str(x) for x in metadata.get("bit_depth_per_channel", [])),
        "timestamp_first_utc": _first_timestamp(metadata),
        "timestamp_count": len(metadata.get("acquisition_timestamps") or []),
        "stage_x_m": stage.get("x_m"),
        "stage_y_m": stage.get("y_m"),
        "stage_z_m": stage.get("z_m"),
        "objective_name": obj.get("name"),
        "magnification": obj.get("magnification"),
        "numerical_aperture": obj.get("numerical_aperture"),
        "immersion": obj.get("immersion"),
        "microscope_model": scope.get("model"),
        "microscope_serial": scope.get("serial"),
        "scan_mode": conf.get("scan_mode"),
        "line_averaging": conf.get("line_averaging"),
        "frame_averaging": conf.get("frame_averaging"),
        "pinhole_um": conf.get("pinhole_um"),
        "pinhole_airy": conf.get("pinhole_airy"),
        "zoom": conf.get("zoom"),
        "scan_speed": conf.get("scan_speed"),
        "scan_direction": conf.get("scan_direction"),
        "pixel_dwell_time_us": conf.get("pixel_dwell_time_us"),
        "refraction_index": optical.get("refraction_index"),
        "tld_mode": optical.get("tld_mode"),
    }


def _channel_rows(metadata: dict) -> list[dict]:
    rows = []
    lasers = metadata.get("laser_settings") or []
    laser_waves = ";".join(str(x.get("wavelength_nm")) for x in lasers)
    laser_intensities = ";".join(str(x.get("intensity_percent")) for x in lasers)
    for channel in metadata.get("channel_info") or []:
        excitation = channel.get("excitation_settings") or []
        rows.append(
            {
                "source_file": metadata.get("source_file"),
                "series_index": metadata.get("series_index"),
                "series_name": metadata.get("series_name"),
                "channel_index": channel.get("index"),
                "label": channel.get("label"),
                "physical_label": channel.get("physical_label") or channel.get("label"),
                "analysis_label": channel.get("analysis_label") or channel.get("label"),
                "stain": channel.get("stain"),
                "biological_role": channel.get("biological_role"),
                "assignment_source": channel.get("assignment_source"),
                "assignment_confidence": channel.get("assignment_confidence"),
                "inferred_modality": _infer_modality(channel),
                "lut": channel.get("lut"),
                "bit_depth": channel.get("bit_depth"),
                "detector_name": channel.get("detector_name"),
                "detector_type": channel.get("detector_type"),
                "scan_type": channel.get("scan_type"),
                "gain": channel.get("gain"),
                "offset": channel.get("offset"),
                "detection_range_begin_nm": channel.get("detection_range_begin_nm"),
                "detection_range_end_nm": channel.get("detection_range_end_nm"),
                "acquisition_mode": channel.get("acquisition_mode"),
                "dye_name_from_leica": channel.get("dye_name"),
                "sequential_index": channel.get("sequential_index"),
                "sequential_setting_name": channel.get("sequential_setting_name"),
                "excitation_wavelengths_nm": ";".join(str(x.get("wavelength_nm")) for x in excitation),
                "excitation_intensities_percent": ";".join(str(x.get("intensity_percent")) for x in excitation),
                "emission_window_begin_nm": channel.get("emission_window_begin_nm"),
                "emission_window_end_nm": channel.get("emission_window_end_nm"),
                "all_visible_laser_wavelengths_nm": laser_waves,
                "all_visible_laser_intensities_percent": laser_intensities,
            }
        )
    return rows


def _stage_row(metadata: dict) -> dict:
    stage = metadata.get("stage_position", {})
    return {
        "source_file": metadata.get("source_file"),
        "series_index": metadata.get("series_index"),
        "series_name": metadata.get("series_name"),
        "timestamp_first_utc": _first_timestamp(metadata),
        "stage_x_m": stage.get("x_m"),
        "stage_y_m": stage.get("y_m"),
        "stage_z_m": stage.get("z_m"),
        "stage_x_um": _safe_float(stage.get("x_m")) * 1e6 if stage.get("x_m") is not None else None,
        "stage_y_um": _safe_float(stage.get("y_m")) * 1e6 if stage.get("y_m") is not None else None,
        "stage_z_um": _safe_float(stage.get("z_m")) * 1e6 if stage.get("z_m") is not None else None,
    }


def _intensity_rows(image, metadata: dict) -> list[dict]:
    rows = []
    dims = metadata["dimensions"]
    bit_depths = metadata.get("bit_depth_per_channel") or []
    z_count = dims.get("z") or 1
    t_count = dims.get("t") or 1
    for channel in metadata.get("channel_info") or []:
        c = int(channel["index"])
        bit_depth = int(bit_depths[c]) if c < len(bit_depths) else int(channel.get("bit_depth") or 8)
        max_possible = (2**bit_depth) - 1
        values = []
        for t in range(t_count):
            for z in range(z_count):
                frame = np.asarray(image.get_frame(z=z, t=t, c=c))
                values.append(frame.reshape(-1))
        arr = np.concatenate(values) if len(values) > 1 else values[0]
        rows.append(
            {
                "source_file": metadata.get("source_file"),
                "series_index": metadata.get("series_index"),
                "series_name": metadata.get("series_name"),
                "channel_index": c,
                "label": channel.get("label"),
                "bit_depth": bit_depth,
                "dtype": str(arr.dtype),
                "pixel_count": int(arr.size),
                "min": float(np.min(arr)),
                "p01": float(np.percentile(arr, 1)),
                "p05": float(np.percentile(arr, 5)),
                "mean": float(np.mean(arr)),
                "median": float(np.median(arr)),
                "p95": float(np.percentile(arr, 95)),
                "p99": float(np.percentile(arr, 99)),
                "max": float(np.max(arr)),
                "zero_fraction": float(np.mean(arr == 0)),
                "saturated_fraction": float(np.mean(arr >= max_possible)),
            }
        )
    return rows


def _write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _time_span(series_rows: list[dict]) -> str:
    times = [r["timestamp_first_utc"] for r in series_rows if r.get("timestamp_first_utc")]
    if not times:
        return "NA"
    return f"{min(times)} to {max(times)}"


def _constant_or_values(rows: list[dict], key: str) -> str:
    values = sorted({str(r.get(key)) for r in rows if r.get(key) not in (None, "")})
    if not values:
        return "NA"
    if len(values) == 1:
        return values[0]
    return "; ".join(values[:8]) + (" ..." if len(values) > 8 else "")


def _write_report(
    path: Path,
    lif_path: Path,
    series_rows: list[dict],
    channel_rows: list[dict],
    intensity_rows: list[dict],
    include_intensity_file: bool | None = None,
) -> None:
    lines = [
        f"# LIF Metadata Report",
        "",
        f"- Source file: `{lif_path.name}`",
        f"- Series count: {len(series_rows)}",
        f"- Dimension types: {_constant_or_values(series_rows, 'dimension_type')}",
        f"- Image size: {_constant_or_values(series_rows, 'x_px')} x {_constant_or_values(series_rows, 'y_px')} px",
        f"- Channels per series: {_constant_or_values(series_rows, 'channel_count')}",
        f"- Pixel size: {_constant_or_values(series_rows, 'pixel_size_x_um')} um/px",
        f"- Time span: {_time_span(series_rows)}",
        "",
        "## Channels",
        "",
        "| C | Physical | Analysis | Stain | Biological role | Modality | Detector | Gain | Offset | Detection nm | Leica dye name | Assignment |",
        "|---|---|---|---|---|---|---|---:|---:|---|---|---|",
    ]
    seen = set()
    for row in channel_rows:
        key = (
            row["channel_index"],
            row["physical_label"],
            row["analysis_label"],
            row["detector_name"],
            row["gain"],
            row["offset"],
        )
        if key in seen:
            continue
        seen.add(key)
        lines.append(
            "| {channel_index} | {physical_label} | {analysis_label} | {stain} | {biological_role} | {inferred_modality} | {detector_name} | {gain} | {offset} | {detection_range_begin_nm}-{detection_range_end_nm} | {dye_name_from_leica} | {assignment_source}/{assignment_confidence} |".format(
                **row
            )
        )
    if intensity_rows:
        lines += [
            "",
            "## Intensity Summary",
            "",
            "| Channel | Mean range | P99 range | Saturated fraction max |",
            "|---|---:|---:|---:|",
        ]
        by_label = {}
        for row in intensity_rows:
            by_label.setdefault(row["label"], []).append(row)
        for label, rows in by_label.items():
            means = [r["mean"] for r in rows]
            p99s = [r["p99"] for r in rows]
            sats = [r["saturated_fraction"] for r in rows]
            lines.append(
                f"| {label} | {min(means):.3f}-{max(means):.3f} | {min(p99s):.3f}-{max(p99s):.3f} | {max(sats):.6f} |"
            )
    lines += [
        "",
        "## Output Files",
        "",
        "- `metadata.json`: full structured metadata for programmatic use",
        "- `series_metadata.csv`: one row per series",
        "- `channel_metadata.csv`: one row per series x channel",
        "- `stage_positions.csv`: one row per series with stage coordinates",
    ]
    has_intensity_file = (
        include_intensity_file
        if include_intensity_file is not None
        else bool(intensity_rows)
    )
    if has_intensity_file:
        lines.append(
            "- `intensity_summary.csv`: pixel intensity statistics for exported image units"
        )
    lines += [
        "- `stage_map.html`: interactive physical stage map with FOV rectangles and scale bar",
        "- `source_metadata.xml`: raw Leica XML metadata",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def inspect_lif(
    lif_path: Path,
    out_dir: Path,
    include_intensity: bool,
    plate_calibration: dict | None = None,
) -> None:
    lif = LifFile(str(lif_path))
    out_dir.mkdir(parents=True, exist_ok=False)

    (out_dir / "source_metadata.xml").write_text(
        ET.tostring(lif.xml_root, encoding="unicode"),
        encoding="utf-8",
    )

    all_metadata = []
    series_rows = []
    channel_rows = []
    stage_rows = []
    intensity_rows = []

    for index, image in enumerate(lif.get_iter_image()):
        metadata = extract_metadata(image, lif.xml_root, str(lif_path), index)
        all_metadata.append(metadata)
        series_rows.append(_flatten_series(metadata))
        channel_rows.extend(_channel_rows(metadata))
        stage_rows.append(_stage_row(metadata))
        if include_intensity:
            intensity_rows.extend(_intensity_rows(image, metadata))

    full = {
        "schema_name": "candida_lif_metadata_bundle",
        "schema_version": "0.2",
        "created_at": _iso_now(),
        "source_lif": lif_path.name,
        "source_lif_name": lif_path.name,
        "source_lif_size_bytes": os.path.getsize(lif_path),
        "series_count": lif.num_images,
        "series": all_metadata,
    }
    (out_dir / "metadata.json").write_text(json.dumps(full, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_csv(out_dir / "series_metadata.csv", series_rows)
    _write_csv(out_dir / "channel_metadata.csv", channel_rows)
    _write_csv(out_dir / "stage_positions.csv", stage_rows)
    if include_intensity:
        _write_csv(out_dir / "intensity_summary.csv", intensity_rows)
    _write_report(out_dir / "metadata_report.md", lif_path, series_rows, channel_rows, intensity_rows)
    write_stage_map(
        out_dir / "stage_map.html",
        stage_rows,
        series_rows,
        calibration=plate_calibration,
    )
    lif.close()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("lif", type=Path, help="Input Leica .lif file")
    parser.add_argument("--out", type=Path, help="Output metadata directory")
    parser.add_argument("--no-intensity", action="store_true", help="Skip reading image pixels for intensity statistics")
    parser.add_argument(
        "--plate-calibration",
        type=Path,
        help="Optional plate_calibration.json used to overlay calibrated columns/rows",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    lif_path = args.lif.resolve()
    if not lif_path.exists():
        print(f"error: LIF not found: {lif_path}", file=sys.stderr)
        return 2
    out_dir = args.out or (lif_path.parent / f"{sanitize_name(lif_path.name)}_metadata")
    if out_dir.exists():
        print(f"error: output directory already exists: {out_dir}", file=sys.stderr)
        print("choose a new --out directory to avoid accidental overwrite", file=sys.stderr)
        return 2
    plate_calibration = None
    if args.plate_calibration:
        try:
            plate_calibration = json.loads(args.plate_calibration.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"error: cannot read plate calibration: {exc}", file=sys.stderr)
            return 2
    inspect_lif(
        lif_path,
        out_dir.resolve(),
        include_intensity=not args.no_intensity,
        plate_calibration=plate_calibration,
    )
    print(out_dir.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
