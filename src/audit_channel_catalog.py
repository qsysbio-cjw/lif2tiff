#!/usr/bin/env python3
"""Audit channel resolver behavior against an extracted metadata catalog."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
from collections import Counter
from pathlib import Path

from channel_resolver import assess_consistency, resolve_channel


def audit_catalog(root: Path) -> dict:
    root = root.resolve()
    status_counts = Counter()
    modality_counts = Counter()
    modality_confidence_counts = Counter()
    stain_candidate_counts = Counter()
    stain_source_counts = Counter()
    instrument_preset_counts = Counter()
    warning_lifs = []
    channel_rows = 0
    files = sorted(root.rglob("channel_metadata.csv"))

    for path in files:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            rows = list(csv.DictReader(handle))
        channel_rows += len(rows)
        consistency = assess_consistency(rows)
        status_counts[consistency["status"]] += 1
        relative = path.parent.relative_to(root).as_posix()
        if consistency["status"] == "warning":
            warning_lifs.append(
                {
                    "metadata_record": relative,
                    "issues": consistency["issues"],
                }
            )

        for row in rows:
            result = resolve_channel(
                row,
                series_name=row.get("series_name", ""),
                source_name=f"{relative} {row.get('source_file', '')}",
            )
            modality_counts[result["modality"]["value"]] += 1
            modality_confidence_counts[
                f"{result['modality']['value']}:{result['modality']['confidence']}"
            ] += 1
            for candidate in result["stain_candidates"]:
                if candidate["kind"] == "stain":
                    stain_candidate_counts[
                        f"{candidate['id']}:{candidate['confidence']}"
                    ] += 1
                    stain_source_counts[
                        f"{candidate['id']}:{candidate['source']}"
                    ] += 1
                elif candidate["kind"] == "instrument_preset":
                    instrument_preset_counts[candidate["id"]] += 1

    return {
        "schema_version": "0.1",
        "created_at": dt.datetime.now(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat(),
        "catalog_name": root.name,
        "lif_metadata_records": len(files),
        "channel_rows": channel_rows,
        "consistency_status_counts": dict(sorted(status_counts.items())),
        "modality_counts": dict(sorted(modality_counts.items())),
        "modality_confidence_counts": dict(sorted(modality_confidence_counts.items())),
        "stain_candidate_counts": dict(sorted(stain_candidate_counts.items())),
        "stain_candidate_source_counts": dict(sorted(stain_source_counts.items())),
        "instrument_preset_counts": dict(sorted(instrument_preset_counts.items())),
        "warning_lifs": warning_lifs,
    }


def markdown_report(report: dict) -> str:
    total = report["lif_metadata_records"]
    stable = report["consistency_status_counts"].get("stable", 0)
    variation = report["consistency_status_counts"].get("variation", 0)
    warnings = report["consistency_status_counts"].get("warning", 0)
    no_warning = stable + variation
    lines = [
        "# Channel Identity Audit",
        "",
        "## Summary",
        "",
        f"- LIF metadata records: {total}",
        f"- Channel rows: {report['channel_rows']}",
        f"- Stable: {stable}",
        f"- Variation only: {variation}",
        f"- Warning: {warnings}",
        f"- Standard-mode compatible without protocol warning: "
        f"{no_warning}/{total} ({no_warning / total:.1%})",
        "",
        "## Modality",
        "",
    ]
    for key, value in report["modality_confidence_counts"].items():
        lines.append(f"- `{key}`: {value}")
    lines += ["", "## Stain Candidates", ""]
    if report["stain_candidate_counts"]:
        for key, value in report["stain_candidate_counts"].items():
            lines.append(f"- `{key}`: {value}")
    else:
        lines.append("- None")
    lines += ["", "## Warning Records", ""]
    for item in report["warning_lifs"]:
        messages = "; ".join(issue["message"] for issue in item["issues"])
        lines.append(f"- `{item['metadata_record']}`: {messages}")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("catalog", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    if args.output.exists():
        parser.error(f"output already exists: {args.output}")
    report = audit_catalog(args.catalog)
    args.output.mkdir(parents=True)
    (args.output / "audit.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (args.output / "audit.md").write_text(
        markdown_report(report),
        encoding="utf-8",
    )
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
