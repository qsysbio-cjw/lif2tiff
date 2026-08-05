#!/usr/bin/env python3
"""Measure wavelength-tolerance effects on frozen LIF workflow routing."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
from collections import Counter, defaultdict
from pathlib import Path

from channel_resolver import OPTICAL_TOLERANCE_NM, assess_consistency


def read_groups(root: Path) -> dict[tuple[str, str], list[dict[str, str]]]:
    groups: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    paths = sorted(root.glob("*_acquisition_conditions/channel_conditions.csv"))
    if not paths:
        raise FileNotFoundError(f"no acquisition channel tables found under {root}")
    for path in paths:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                groups[(row["person"], row["source_relative_path"])].append(row)
    return groups


def audit(root: Path, tolerances: list[float]) -> dict:
    groups = read_groups(root)
    baseline = {}
    by_tolerance: dict[str, dict] = {}
    for key, rows in groups.items():
        baseline[key] = {
            issue["code"]
            for issue in assess_consistency(rows)["issues"]
            if issue["severity"] == "warning"
        }

    for tolerance in tolerances:
        records = []
        reason_counts = Counter()
        changed_to_standard = []
        changed_to_review = []
        for key, rows in sorted(groups.items()):
            reasons = {
                issue["code"]
                for issue in assess_consistency(
                    rows, optical_tolerance_nm=tolerance
                )["issues"]
                if issue["severity"] == "warning"
            }
            reason_counts.update(reasons)
            was_review = bool(baseline[key])
            is_review = bool(reasons)
            record = "/".join(key)
            if was_review and not is_review:
                changed_to_standard.append(record)
            elif not was_review and is_review:
                changed_to_review.append(record)
            if is_review:
                records.append({"record": record, "reason_codes": sorted(reasons)})
        key = f"{tolerance:g}"
        by_tolerance[key] = {
            "standard_lif": len(groups) - len(records),
            "review_lif": len(records),
            "review_reason_counts": dict(sorted(reason_counts.items())),
            "changed_from_selected_tolerance_to_standard": changed_to_standard,
            "changed_from_selected_tolerance_to_review": changed_to_review,
            "review_records": records,
        }

    return {
        "schema_name": "candida_optical_tolerance_audit",
        "schema_version": "0.1",
        "created_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        "snapshot_name": root.name,
        "lif_records": len(groups),
        "tolerance_unit": "nm",
        "comparison_rule": "absolute difference <= tolerance for excitation and emission values",
        "selected_tolerance_nm": OPTICAL_TOLERANCE_NM,
        "selected_tolerance_review_lif": sum(bool(value) for value in baseline.values()),
        "tolerances": by_tolerance,
    }


def markdown(report: dict) -> str:
    lines = [
        "# Optical Wavelength Tolerance Audit",
        "",
        f"Frozen LIF records: {report['lif_records']}",
        "",
        "The tested rule is an absolute tolerance for excitation and emission wavelengths. "
        "Gain, offset, and laser power remain informational variations.",
        "",
        f"Selected production tolerance: {report['selected_tolerance_nm']:g} nm",
        "",
        "| Tolerance | Standard | Review | Changed to standard vs selected | Changed to review |",
        "|---:|---:|---:|---:|---:|",
    ]
    for tolerance, result in report["tolerances"].items():
        lines.append(
            f"| {tolerance} nm | {result['standard_lif']} | {result['review_lif']} | "
            f"{len(result['changed_from_selected_tolerance_to_standard'])} | "
            f"{len(result['changed_from_selected_tolerance_to_review'])} |"
        )
    lines += ["", "## Differences From Selected Tolerance", ""]
    for tolerance, result in report["tolerances"].items():
        lines.append(f"### {tolerance} nm")
        lines.append("")
        to_standard = result["changed_from_selected_tolerance_to_standard"]
        to_review = result["changed_from_selected_tolerance_to_review"]
        lines.append(f"- Selected review -> tested tolerance standard: {len(to_standard)}")
        for record in to_standard:
            lines.append(f"  - `{record}`")
        lines.append(f"- Selected standard -> tested tolerance review: {len(to_review)}")
        for record in to_review:
            lines.append(f"  - `{record}`")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--tolerance", type=float, action="append", dest="tolerances")
    args = parser.parse_args()
    if args.output.exists():
        parser.error(f"output already exists: {args.output}")
    report = audit(args.snapshot, args.tolerances or [0.1, 0.5, 1.0])
    args.output.mkdir(parents=True)
    (args.output / "audit.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (args.output / "audit.md").write_text(markdown(report), encoding="utf-8")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
