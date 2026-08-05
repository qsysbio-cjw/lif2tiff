#!/usr/bin/env python3
"""
批量验证 LIF → TIFF 转换输出的完整性和正确性
"""
import argparse
import json
import logging
from pathlib import Path
import sys

import numpy as np
import tifffile


def validate_tiff(tiff_path, expected_shape=None, expected_dtype=None):
    """验证单个 TIFF 文件"""
    issues = []

    try:
        with tifffile.TiffFile(tiff_path) as tif:
            # 读取图像
            img = tif.asarray()

            # 检查形状
            if expected_shape and img.shape != expected_shape:
                issues.append(f"Shape mismatch: got {img.shape}, expected {expected_shape}")

            # 检查数据类型
            if expected_dtype and img.dtype != expected_dtype:
                issues.append(f"Dtype mismatch: got {img.dtype}, expected {expected_dtype}")

            # 检查是否全黑或全白
            if img.size > 0:
                min_val, max_val = img.min(), img.max()
                if min_val == max_val:
                    issues.append(f"Constant image: all pixels = {min_val}")
                elif min_val == 0 and max_val == 0:
                    issues.append("All black")
                elif img.dtype == np.uint8 and min_val == 255 and max_val == 255:
                    issues.append("All white (uint8)")
                elif img.dtype == np.uint16 and min_val == 65535 and max_val == 65535:
                    issues.append("All white (uint16)")

            return {
                "valid": len(issues) == 0,
                "shape": img.shape,
                "dtype": str(img.dtype),
                "min": float(min_val) if img.size > 0 else None,
                "max": float(max_val) if img.size > 0 else None,
                "mean": float(img.mean()) if img.size > 0 else None,
                "issues": issues
            }

    except Exception as e:
        return {
            "valid": False,
            "shape": None,
            "dtype": None,
            "min": None,
            "max": None,
            "mean": None,
            "issues": [f"Failed to read: {str(e)}"]
        }


def validate_series(series_dir):
    """验证单个 series 目录"""
    metadata_path = series_dir / f"{series_dir.name}_metadata.json"

    if not metadata_path.exists():
        return {
            "series": series_dir.name,
            "valid": False,
            "issues": ["Missing metadata.json"],
            "channels": []
        }

    # 读取元数据
    with open(metadata_path) as f:
        metadata = json.load(f)

    dims = metadata.get("dimensions", {})
    expected_shape = (dims.get("y", 1024), dims.get("x", 1024))

    # 验证每个通道
    channel_results = []
    all_valid = True

    for ch_info in metadata.get("channel_info", []):
        ch_idx = ch_info["index"]
        ch_label = ch_info["label"]
        bit_depth = ch_info.get("bit_depth", 8)
        expected_dtype = np.uint8 if bit_depth == 8 else np.uint16

        tiff_path = series_dir / f"{series_dir.name}_C{ch_idx}_{ch_label}.tif"

        if not tiff_path.exists():
            channel_results.append({
                "channel": f"C{ch_idx}_{ch_label}",
                "valid": False,
                "issues": ["File not found"]
            })
            all_valid = False
            continue

        result = validate_tiff(tiff_path, expected_shape, expected_dtype)
        result["channel"] = f"C{ch_idx}_{ch_label}"
        channel_results.append(result)

        if not result["valid"]:
            all_valid = False

    return {
        "series": series_dir.name,
        "valid": all_valid,
        "channels": channel_results
    }


def main():
    parser = argparse.ArgumentParser(description="验证 LIF → TIFF 转换输出")
    parser.add_argument("output_dir", help="输出目录（如 ./output）")
    parser.add_argument("-o", "--report", default="validation_report.json", help="验证报告输出路径")
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

    # 收集所有 LIF 目录
    lif_dirs = [d for d in output_dir.iterdir() if d.is_dir()]

    if not lif_dirs:
        logging.error(f"No LIF directories found in {output_dir}")
        sys.exit(1)

    logging.info(f"Found {len(lif_dirs)} LIF directories")

    # 验证所有 series
    all_results = []
    total_series = 0
    valid_series = 0

    for lif_dir in sorted(lif_dirs):
        logging.info(f"Validating {lif_dir.name}...")

        # 跳过 by_channel 目录
        series_dirs = [d for d in lif_dir.iterdir() if d.is_dir() and d.name != "by_channel"]

        for series_dir in sorted(series_dirs):
            result = validate_series(series_dir)
            result["lif"] = lif_dir.name
            all_results.append(result)

            total_series += 1
            if result["valid"]:
                valid_series += 1
            else:
                logging.warning(f"  {series_dir.name}: FAILED")
                for ch in result.get("channels", []):
                    if not ch.get("valid", True):
                        for issue in ch.get("issues", []):
                            logging.warning(f"    {ch['channel']}: {issue}")

    # 生成报告
    report = {
        "summary": {
            "total_series": total_series,
            "valid_series": valid_series,
            "failed_series": total_series - valid_series,
            "success_rate": valid_series / total_series if total_series > 0 else 0
        },
        "results": all_results
    }

    report_path = Path(args.report)
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)

    logging.info(f"\nValidation complete:")
    logging.info(f"  Total series: {total_series}")
    logging.info(f"  Valid: {valid_series}")
    logging.info(f"  Failed: {total_series - valid_series}")
    logging.info(f"  Success rate: {report['summary']['success_rate']:.1%}")
    logging.info(f"\nReport saved to: {report_path}")

    if valid_series < total_series:
        sys.exit(1)


if __name__ == "__main__":
    main()
