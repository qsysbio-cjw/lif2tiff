#!/usr/bin/env python3
"""Build a plate/stage calibration from deliberately acquired boundary positions."""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
from pathlib import Path

import numpy as np


def _read_csv(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _float(row: dict, key: str) -> float:
    value = row.get(key)
    if value in (None, ""):
        raise ValueError(f"missing {key} for {row.get('series_name', 'unknown series')}")
    return float(value)


def _select_series(rows: list[dict], first: int, count: int) -> list[dict]:
    by_number = {int(row["series_index"]) + 1: row for row in rows}
    numbers = list(range(first, first + count))
    missing = [number for number in numbers if number not in by_number]
    if missing:
        raise ValueError(f"missing 1-based series numbers: {missing}")
    return [by_number[number] for number in numbers]


def _row_label(index: int) -> str:
    """Convert a 1-based plate row index to A, B, ..., Z, AA, ..."""
    if index < 1:
        raise ValueError(f"plate row index must be positive, got {index}")
    label = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        label = chr(ord("A") + remainder) + label
    return label


def _fit_paired_boundaries(
    rows: list[dict],
    coordinate_key: str,
    first_grid_index: int | None,
    label_prefix: str,
) -> dict:
    if len(rows) % 2:
        raise ValueError("paired boundary calibration requires an even series count")

    coordinates = np.asarray([_float(row, coordinate_key) for row in rows], dtype=float)
    pair_count = len(rows) // 2
    design = []
    for pair_index in range(pair_count):
        design.append([1.0, float(pair_index), 0.0])
        design.append([1.0, float(pair_index), 1.0])
    design_array = np.asarray(design, dtype=float)
    coefficients, _, _, _ = np.linalg.lstsq(design_array, coordinates, rcond=None)
    first_left_um, pitch_um, opening_um = coefficients
    fitted = design_array @ coefficients
    residuals = coordinates - fitted

    measured_openings = [
        coordinates[index + 1] - coordinates[index]
        for index in range(0, len(coordinates), 2)
    ]
    same_edge_pitches = [
        coordinates[index + 2] - coordinates[index]
        for index in range(len(coordinates) - 2)
    ]

    boundaries = []
    for pair_index in range(pair_count):
        grid_index = first_grid_index + pair_index if first_grid_index is not None else None
        if grid_index is None:
            display_label = f"relative_{label_prefix.lower()}{pair_index + 1}"
        elif label_prefix == "R":
            display_label = _row_label(grid_index)
        else:
            display_label = f"{label_prefix}{grid_index}"
        left_offset = pair_index * 2
        right_offset = left_offset + 1
        boundaries.append(
            {
                "grid_index": grid_index,
                "label": display_label,
                "left_or_top": {
                    "series_number": int(rows[left_offset]["series_index"]) + 1,
                    "series_name": rows[left_offset]["series_name"],
                    "measured_um": round(float(coordinates[left_offset]), 3),
                    "fitted_um": round(float(fitted[left_offset]), 3),
                    "residual_um": round(float(residuals[left_offset]), 3),
                },
                "right_or_bottom": {
                    "series_number": int(rows[right_offset]["series_index"]) + 1,
                    "series_name": rows[right_offset]["series_name"],
                    "measured_um": round(float(coordinates[right_offset]), 3),
                    "fitted_um": round(float(fitted[right_offset]), 3),
                    "residual_um": round(float(residuals[right_offset]), 3),
                },
                "measured_center_um": round(
                    float((coordinates[left_offset] + coordinates[right_offset]) / 2), 3
                ),
            }
        )

    result = {
        "pair_count": pair_count,
        "first_grid_index": first_grid_index,
        "fit": {
            "first_left_or_top_boundary_um": round(float(first_left_um), 3),
            "pitch_um": round(float(pitch_um), 3),
            "well_opening_um": round(float(opening_um), 3),
            "divider_thickness_um": round(float(pitch_um - opening_um), 3),
            "rms_residual_um": round(float(math.sqrt(np.mean(residuals**2))), 3),
            "max_abs_residual_um": round(float(np.max(np.abs(residuals))), 3),
        },
        "robust_summaries": {
            "median_measured_opening_um": round(statistics.median(measured_openings), 3),
            "median_same_edge_pitch_um": (
                round(statistics.median(same_edge_pitches), 3)
                if same_edge_pitches
                else None
            ),
        },
        "measured_boundaries": boundaries,
    }
    return result


def _full_x_grid(axis: dict, column_count: int) -> list[dict]:
    first_column = axis["first_grid_index"]
    fit = axis["fit"]
    first_left = fit["first_left_or_top_boundary_um"]
    pitch = fit["pitch_um"]
    opening = fit["well_opening_um"]
    grid = []
    for column in range(1, column_count + 1):
        left = first_left + (column - first_column) * pitch
        right = left + opening
        grid.append(
            {
                "column": column,
                "left_boundary_um": round(left, 3),
                "center_um": round((left + right) / 2, 3),
                "right_boundary_um": round(right, 3),
            }
        )
    return grid


def _full_y_grid(axis: dict, row_count: int) -> list[dict]:
    first_row = axis["first_grid_index"]
    if first_row is None:
        return []
    fit = axis["fit"]
    first_top = fit["first_left_or_top_boundary_um"]
    pitch = fit["pitch_um"]
    opening = fit["well_opening_um"]
    grid = []
    for row in range(1, row_count + 1):
        top = first_top + (row - first_row) * pitch
        bottom = top + opening
        grid.append(
            {
                "row": row,
                "label": _row_label(row),
                "top_boundary_um": round(top, 3),
                "center_um": round((top + bottom) / 2, 3),
                "bottom_boundary_um": round(bottom, 3),
            }
        )
    return grid


def _reference_well(x_rows: list[dict], y_rows: list[dict], x_axis: dict, y_axis: dict) -> dict | None:
    first_column = x_axis["first_grid_index"]
    first_row = y_axis["first_grid_index"]
    if first_column is None or first_row is None:
        return None

    x_boundary = x_axis["measured_boundaries"][0]
    y_boundary = y_axis["measured_boundaries"][0]
    x_left = x_boundary["left_or_top"]["measured_um"]
    x_right = x_boundary["right_or_bottom"]["measured_um"]
    y_top = y_boundary["left_or_top"]["measured_um"]
    y_bottom = y_boundary["right_or_bottom"]["measured_um"]
    x_scan_y = statistics.median(_float(row, "stage_y_um") for row in x_rows)
    y_scan_x = statistics.median(_float(row, "stage_x_um") for row in y_rows)

    return {
        "well": f"{_row_label(first_row)}{first_column}",
        "row": first_row,
        "row_label": _row_label(first_row),
        "column": first_column,
        "assignment": "confirmed_by_acquisition_method_and_cross_axis_coordinates",
        "cross_axis_validation": {
            "x_boundary_scan_y_um": round(x_scan_y, 3),
            "first_row_top_um": round(y_top, 3),
            "first_row_bottom_um": round(y_bottom, 3),
            "x_boundary_scan_inside_first_row": y_top <= x_scan_y <= y_bottom,
            "y_boundary_scan_x_um": round(y_scan_x, 3),
            "first_column_left_um": round(x_left, 3),
            "first_column_right_um": round(x_right, 3),
            "y_boundary_scan_inside_first_column": x_left <= y_scan_x <= x_right,
        },
    }


def _one_well_validation(
    stage_csv: Path,
    series_csv: Path | None,
    calibration: dict,
) -> dict:
    rows = _read_csv(stage_csv)
    xs = [_float(row, "stage_x_um") for row in rows]
    ys = [_float(row, "stage_y_um") for row in rows]
    sorted_y = sorted(ys)
    steps = [b - a for a, b in zip(sorted_y, sorted_y[1:])]

    fov_width_um = None
    fov_height_um = None
    if series_csv:
        series_rows = _read_csv(series_csv)
        widths = [float(row["x_px"]) * float(row["pixel_size_x_um"]) for row in series_rows]
        heights = [float(row["y_px"]) * float(row["pixel_size_y_um"]) for row in series_rows]
        fov_width_um = statistics.median(widths)
        fov_height_um = statistics.median(heights)

    center_y = statistics.mean(ys)
    y_boundaries = calibration["y_axis"]["measured_boundaries"]
    nearest_y = min(y_boundaries, key=lambda item: abs(item["measured_center_um"] - center_y))

    result = {
        "source_lif": rows[0].get("source_file"),
        "series_count": len(rows),
        "x_span_um": round(max(xs) - min(xs), 3),
        "y_center_um": round(center_y, 3),
        "y_center_span_um": round(max(ys) - min(ys), 3),
        "median_center_step_um": round(statistics.median(steps), 3) if steps else None,
        "nearest_y_label": nearest_y["label"],
        "nearest_y_assignment": "absolute" if nearest_y.get("grid_index") is not None else "relative",
        "distance_to_relative_y_center_um": round(
            center_y - nearest_y["measured_center_um"], 3
        ),
    }
    if fov_width_um is not None and fov_height_um is not None:
        step = statistics.median(steps) if steps else None
        result.update(
            {
                "fov_width_um": round(fov_width_um, 3),
                "fov_height_um": round(fov_height_um, 3),
                "estimated_adjacent_overlap_um": round(fov_height_um - step, 3)
                if step is not None
                else None,
                "estimated_adjacent_overlap_fraction": round(
                    (fov_height_um - step) / fov_height_um, 4
                )
                if step is not None
                else None,
                "edge_to_edge_covered_span_um": round(
                    max(ys) - min(ys) + fov_height_um, 3
                ),
            }
        )
    return result


def build_calibration(args: argparse.Namespace) -> dict:
    rows = _read_csv(args.stage_csv)
    source_files = sorted({row.get("source_file") for row in rows})
    if len(source_files) != 1:
        raise ValueError(f"expected one source LIF in stage CSV, found: {source_files}")

    x_rows = _select_series(rows, args.x_first_series, args.x_series_count)
    y_rows = _select_series(rows, args.y_first_series, args.y_series_count)
    x_axis = _fit_paired_boundaries(
        x_rows,
        "stage_x_um",
        args.x_first_column,
        "C",
    )
    y_axis = _fit_paired_boundaries(
        y_rows,
        "stage_y_um",
        args.y_first_row,
        "R",
    )

    calibration = {
        "schema_name": "candida_plate_stage_calibration",
        "schema_version": "0.2",
        "status": "active",
        "source_lif": source_files[0],
        "plate": {
            "format": "384_well",
            "rows": args.plate_rows,
            "columns": args.plate_columns,
            "well_shape": "square",
        },
        "coordinate_convention": {
            "stage_x_increases_toward": "higher_plate_column_number",
            "stage_y_increases_toward": "higher_plate_row_number",
            "stage_position_represents": "fov_center",
            "boundary_measurement_method": "divider edge manually centered in FOV",
        },
        "x_axis": x_axis,
        "y_axis": y_axis,
        "full_x_grid": _full_x_grid(x_axis, args.plate_columns),
        "full_y_grid": _full_y_grid(y_axis, args.plate_rows),
        "limitations": [
            "Absolute stage registration may shift when a plate is re-seated; future runs need residual-based QC.",
            "Boundary coordinates include manual centering uncertainty.",
        ],
    }
    reference_well = _reference_well(x_rows, y_rows, x_axis, y_axis)
    if reference_well:
        calibration["reference_well"] = reference_well
    else:
        calibration["limitations"].insert(
            0,
            "Y pitch is calibrated, but absolute A-P row labels require one known row anchor.",
        )
    if args.one_well_stage_csv:
        calibration["one_well_validation"] = _one_well_validation(
            args.one_well_stage_csv,
            args.one_well_series_csv,
            calibration,
        )
    return calibration


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage_csv", type=Path, help="stage_positions.csv from x-y calibration LIF")
    parser.add_argument("--out", type=Path, required=True, help="New calibration JSON path")
    parser.add_argument("--x-first-series", type=int, default=1)
    parser.add_argument("--x-series-count", type=int, default=16)
    parser.add_argument("--x-first-column", type=int, default=13)
    parser.add_argument("--y-first-series", type=int, default=17)
    parser.add_argument("--y-series-count", type=int, default=20)
    parser.add_argument("--y-first-row", type=int, help="Optional 1-based absolute row index")
    parser.add_argument("--plate-rows", type=int, default=16)
    parser.add_argument("--plate-columns", type=int, default=24)
    parser.add_argument("--one-well-stage-csv", type=Path)
    parser.add_argument("--one-well-series-csv", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if args.out.exists():
        print(f"error: output already exists: {args.out}", file=sys.stderr)
        return 2
    try:
        calibration = build_calibration(args)
    except (OSError, ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(calibration, indent=2, ensure_ascii=False), encoding="utf-8")
    print(args.out.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
