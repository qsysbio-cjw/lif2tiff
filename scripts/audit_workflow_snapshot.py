#!/usr/bin/env python3
"""Audit standard/review routing against a frozen acquisition snapshot."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
from collections import Counter, defaultdict
from pathlib import Path

from channel_resolver import assess_consistency, decide_workflow


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


def audit(root: Path) -> dict:
    groups = read_groups(root)
    consistency_counts = Counter()
    route_counts = Counter()
    workflow_status_counts = Counter()
    reason_counts = Counter()
    review_records = []
    channel_rows = 0
    for (person, source_relative_path), rows in sorted(groups.items()):
        channel_rows += len(rows)
        consistency = assess_consistency(rows)
        workflow = decide_workflow(consistency)
        consistency_counts[consistency["status"]] += 1
        route_counts[workflow["route"]] += 1
        workflow_status_counts[workflow["status"]] += 1
        if workflow["route"] == "review":
            reason_counts.update(workflow["reason_codes"])
            review_records.append(
                {
                    "record": f"{person}/{source_relative_path}",
                    "reason_codes": workflow["reason_codes"],
                    "issues": consistency["issues"],
                }
            )
    return {
        "schema_name": "candida_lif_workflow_audit",
        "schema_version": "0.1",
        "created_at": dt.datetime.now(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat(),
        "snapshot_name": root.name,
        "lif_records": len(groups),
        "channel_rows": channel_rows,
        "consistency_status_counts": dict(sorted(consistency_counts.items())),
        "workflow_route_counts": dict(sorted(route_counts.items())),
        "workflow_status_counts": dict(sorted(workflow_status_counts.items())),
        "review_reason_counts": dict(sorted(reason_counts.items())),
        "review_records": review_records,
    }


def markdown(report: dict) -> str:
    total = report["lif_records"]
    standard = report["workflow_route_counts"].get("standard", 0)
    review = report["workflow_route_counts"].get("review", 0)
    lines = [
        "# LIF Workflow Audit",
        "",
        "## Summary",
        "",
        f"- Frozen LIF records: {total}",
        f"- Channel rows: {report['channel_rows']}",
        f"- Standard route: {standard}/{total} ({standard / total:.1%})",
        f"- Review route: {review}/{total} ({review / total:.1%})",
        "",
        "## Workflow Status",
        "",
    ]
    for key, value in report["workflow_status_counts"].items():
        lines.append(f"- `{key}`: {value}")
    lines += ["", "## Review Reasons", ""]
    for key, value in report["review_reason_counts"].items():
        lines.append(f"- `{key}`: {value}")
    lines += ["", "## Review Records", ""]
    for item in report["review_records"]:
        reasons = ", ".join(item["reason_codes"])
        lines.append(f"- `{item['record']}`: {reasons}")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    if args.output.exists():
        parser.error(f"output already exists: {args.output}")
    report = audit(args.snapshot)
    args.output.mkdir(parents=True)
    (args.output / "audit.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (args.output / "audit.md").write_text(markdown(report), encoding="utf-8")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
