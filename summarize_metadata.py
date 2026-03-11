#!/usr/bin/env python3
"""
汇总所有 series 的元数据到 CSV 表格
"""
import argparse
import csv
import json
import logging
from pathlib import Path
import sys


def extract_key_fields(metadata):
    """从 metadata.json 提取关键字段"""
    dims = metadata.get("dimensions", {})
    pixel_size = metadata.get("pixel_size", {})
    objective = metadata.get("objective", {})
    confocal = metadata.get("confocal_settings", {})
    microscope = metadata.get("microscope", {})
    stage = metadata.get("stage_position", {})

    # 提取激光信息
    lasers = metadata.get("laser_settings", [])
    laser_wavelengths = [l["wavelength_nm"] for l in lasers]
    laser_intensities = [l["intensity_percent"] for l in lasers]

    # 提取通道信息
    channels = metadata.get("channel_info", [])
    detector_names = [ch.get("detector_name", "") for ch in channels]
    detector_gains = [ch.get("gain", None) for ch in channels]

    # 提取时间戳（第一个）
    timestamps = metadata.get("acquisition_timestamps", [])
    first_timestamp = timestamps[0] if timestamps else None

    row = {
        "source_file": metadata.get("source_file", ""),
        "series_name": metadata.get("series_name", ""),
        "series_index": metadata.get("series_index", ""),
        "dimension_type": metadata.get("dimension_type", ""),

        # 尺寸
        "width": dims.get("x", ""),
        "height": dims.get("y", ""),
        "z_slices": dims.get("z", ""),
        "time_points": dims.get("t", ""),
        "channels": dims.get("channels", ""),

        # 像素尺寸
        "pixel_size_x_um": pixel_size.get("x_um_per_px", ""),
        "pixel_size_y_um": pixel_size.get("y_um_per_px", ""),
        "pixel_size_z_um": pixel_size.get("z_um_per_px", ""),

        # 物镜
        "objective": objective.get("name", ""),
        "magnification": objective.get("magnification", ""),
        "na": objective.get("numerical_aperture", ""),
        "immersion": objective.get("immersion", ""),

        # 共聚焦设置
        "scan_mode": confocal.get("scan_mode", ""),
        "line_averaging": confocal.get("line_averaging", ""),
        "frame_averaging": confocal.get("frame_averaging", ""),
        "pinhole_um": confocal.get("pinhole_um", ""),
        "pinhole_airy": confocal.get("pinhole_airy", ""),
        "zoom": confocal.get("zoom", ""),
        "scan_speed": confocal.get("scan_speed", ""),
        "scan_direction": confocal.get("scan_direction", ""),
        "pixel_dwell_time_us": confocal.get("pixel_dwell_time_us", ""),

        # 显微镜
        "microscope_model": microscope.get("model", ""),
        "microscope_serial": microscope.get("serial", ""),

        # Stage 位置
        "stage_x_m": stage.get("x_m", ""),
        "stage_y_m": stage.get("y_m", ""),
        "stage_z_m": stage.get("z_m", ""),

        # 激光
        "laser_wavelengths_nm": "; ".join(map(str, laser_wavelengths)),
        "laser_intensities_pct": "; ".join(map(str, laser_intensities)),

        # 探测器
        "detectors": "; ".join(detector_names),
        "detector_gains": "; ".join(str(g) if g is not None else "" for g in detector_gains),

        # 时间
        "acquisition_time": first_timestamp or "",
    }

    return row


def main():
    parser = argparse.ArgumentParser(description="汇总元数据到 CSV")
    parser.add_argument("output_dir", help="输出目录（如 ./output）")
    parser.add_argument("-o", "--csv", default="metadata_summary.csv", help="CSV 输出路径")
    parser.add_argument("-v", "--verbose", action="store_true", help="详细日志")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s"
    )

    output_dir = Path(args.output_dir)
    if not output_dir.exists():
        logging.error(f"Output directory not found: {output_dir}")
        sys.exit(1)

    # 收集所有 metadata.json
    metadata_files = []

    for lif_dir in sorted(output_dir.iterdir()):
        if not lif_dir.is_dir():
            continue

        for series_dir in sorted(lif_dir.iterdir()):
            if not series_dir.is_dir() or series_dir.name == "by_channel":
                continue

            metadata_path = series_dir / f"{series_dir.name}_metadata.json"
            if metadata_path.exists():
                metadata_files.append(metadata_path)

    if not metadata_files:
        logging.error(f"No metadata files found in {output_dir}")
        sys.exit(1)

    logging.info(f"Found {len(metadata_files)} metadata files")

    # 提取所有字段
    rows = []
    for metadata_path in metadata_files:
        with open(metadata_path) as f:
            metadata = json.load(f)
        row = extract_key_fields(metadata)
        rows.append(row)

    # 写入 CSV
    if rows:
        fieldnames = list(rows[0].keys())

        csv_path = Path(args.csv)
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

        logging.info(f"Summary saved to: {csv_path}")
        logging.info(f"Total series: {len(rows)}")

        # 简单统计
        pixel_sizes = [float(r["pixel_size_x_um"]) for r in rows if r["pixel_size_x_um"]]
        if pixel_sizes:
            logging.info(f"Pixel size range: {min(pixel_sizes):.4f} - {max(pixel_sizes):.4f} µm/px")

        laser_intensities_all = []
        for r in rows:
            if r["laser_intensities_pct"]:
                laser_intensities_all.extend([float(x) for x in r["laser_intensities_pct"].split("; ")])
        if laser_intensities_all:
            logging.info(f"Laser intensity range: {min(laser_intensities_all):.2f} - {max(laser_intensities_all):.2f}%")


if __name__ == "__main__":
    main()
