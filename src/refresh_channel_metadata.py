#!/usr/bin/env python3
"""Refresh derived channel metadata from saved Leica XML without reading pixels."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from lif2tiff_project import extract_xml_metadata, infer_channel_assignment
from lif_metadata_inspect import _channel_rows, _write_csv, _write_report


DETECTOR_FIELDS = (
    "detector_name",
    "detector_type",
    "scan_type",
    "gain",
    "offset",
    "detection_range_begin_nm",
    "detection_range_end_nm",
    "acquisition_mode",
    "sequential_index",
    "sequential_setting_name",
    "excitation_settings",
    "emission_window_begin_nm",
    "emission_window_end_nm",
)


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def refresh_output(output_dir: Path) -> tuple[int, int]:
    metadata_path = output_dir / "metadata.json"
    xml_path = output_dir / "source_metadata.xml"
    if not metadata_path.is_file() or not xml_path.is_file():
        raise FileNotFoundError("metadata.json or source_metadata.xml is missing")

    bundle = json.loads(metadata_path.read_text(encoding="utf-8"))
    xml_root = ET.parse(xml_path).getroot()
    updated_series = 0
    unmatched_series = 0

    for metadata in bundle.get("series") or []:
        xml_metadata = extract_xml_metadata(xml_root, metadata.get("series_name"))
        detector_map = xml_metadata.get("channel_detector_map") or []
        channels = metadata.get("channel_info") or []
        if len(detector_map) < len(channels):
            unmatched_series += 1
            continue
        for index, channel in enumerate(channels):
            detector = detector_map[index]
            for field in DETECTOR_FIELDS:
                channel[field] = detector.get(field)
            channel["dye_name"] = detector.get("dye_name")
            assignment = infer_channel_assignment(
                channel.get("physical_label") or channel.get("label"),
                detector.get("dye_name"),
            )
            channel.update(assignment)
        metadata["laser_settings"] = xml_metadata.get("laser_settings") or []
        updated_series += 1

    bundle["schema_version"] = "0.3"
    bundle["channel_metadata_extraction"] = "sequential-setting-aware-v1"
    bundle["channel_metadata_refreshed_at"] = (
        dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    )

    all_channel_rows = []
    for metadata in bundle.get("series") or []:
        all_channel_rows.extend(_channel_rows(metadata))

    metadata_temp = metadata_path.with_name(f".{metadata_path.name}.refresh-{os.getpid()}")
    metadata_temp.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
    channel_temp = (output_dir / "channel_metadata.csv").with_name(
        f".channel_metadata.csv.refresh-{os.getpid()}"
    )
    _write_csv(channel_temp, all_channel_rows)
    report_temp = (output_dir / "metadata_report.md").with_name(
        f".metadata_report.md.refresh-{os.getpid()}"
    )
    _write_report(
        report_temp,
        Path(bundle.get("source_lif_name") or bundle.get("source_lif") or "source.lif"),
        _read_csv(output_dir / "series_metadata.csv"),
        all_channel_rows,
        _read_csv(output_dir / "intensity_summary.csv")
        if (output_dir / "intensity_summary.csv").is_file()
        else [],
    )

    metadata_temp.replace(metadata_path)
    channel_temp.replace(output_dir / "channel_metadata.csv")
    report_temp.replace(output_dir / "metadata_report.md")
    return updated_series, unmatched_series


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="Catalog root containing per-LIF metadata outputs")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    root = args.root.resolve()
    outputs = sorted({path.parent for path in root.rglob("metadata.json")})
    updated = 0
    unmatched = 0
    failed = []
    for index, output_dir in enumerate(outputs, start=1):
        try:
            series_count, unmatched_count = refresh_output(output_dir)
            updated += series_count
            unmatched += unmatched_count
            print(f"[{index}/{len(outputs)}] {output_dir.relative_to(root)}: {series_count} series")
        except Exception as exc:
            failed.append((output_dir.relative_to(root).as_posix(), str(exc)))
            print(f"ERROR {output_dir.relative_to(root)}: {exc}", file=sys.stderr)
    print(f"outputs={len(outputs)} updated_series={updated} unmatched_series={unmatched} failed={len(failed)}")
    return 1 if failed or unmatched else 0


if __name__ == "__main__":
    raise SystemExit(main())
