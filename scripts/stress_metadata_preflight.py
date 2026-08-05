#!/usr/bin/env python3
"""Repeat production metadata planning without reading pixels or writing outputs."""

from __future__ import annotations

import argparse
import json
import resource
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path


AUDIT_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = AUDIT_ROOT.parent
for path in (PROJECT_ROOT / "src", PROJECT_ROOT / "qt_gui"):
    sys.path.insert(0, str(path))

from app import discover_lifs  # noqa: E402
from liftool import build_conversion_plan  # noqa: E402


def file_descriptor_count() -> int | None:
    proc = Path("/proc/self/fd")
    try:
        return len(list(proc.iterdir()))
    except OSError:
        return None


def inspect_one(source: Path) -> dict:
    started = time.perf_counter()
    try:
        plan = build_conversion_plan(source)
    except BaseException as exc:
        return {
            "source": source.name,
            "source_parent": source.parent.name,
            "size_bytes": source.stat().st_size,
            "seconds": round(time.perf_counter() - started, 6),
            "status": "failed",
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
    dimensions = Counter(str(series.get("dimension_type", "unknown")) for series in plan["series"])
    summary = plan["summary"]
    return {
        "source": source.name,
        "source_parent": source.parent.name,
        "size_bytes": source.stat().st_size,
        "seconds": round(time.perf_counter() - started, 6),
        "status": "ok",
        "workflow_route": plan["channel_resolution"]["workflow"]["route"],
        "series_count": summary["series_count"],
        "output_unit_count": summary["output_unit_count"],
        "plane_count": summary["plane_count"],
        "estimated_uncompressed_pixel_bytes": summary[
            "estimated_uncompressed_pixel_bytes"
        ],
        "dimension_types": dict(sorted(dimensions.items())),
        "stage_position_count": sum(
            bool(series.get("stage_position")) for series in plan["series"]
        ),
        "suggested_directory_name": plan["output_policy"][
            "suggested_directory_name"
        ],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+", type=Path)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.repeat < 1:
        raise SystemExit("--repeat must be at least 1")
    output = args.output.expanduser().resolve()
    if output.exists():
        raise SystemExit(f"refusing to overwrite audit result: {output}")
    sources = discover_lifs(args.inputs)
    if not sources:
        raise SystemExit("no LIF files discovered")

    fd_before = file_descriptor_count()
    rss_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    started = time.perf_counter()
    runs = []
    for repeat_index in range(1, args.repeat + 1):
        for source in sources:
            record = inspect_one(source)
            record["repeat"] = repeat_index
            runs.append(record)
            print(
                f"[{repeat_index}/{args.repeat}] {record['status']:6s} "
                f"{record['seconds']:8.3f}s  {source.name}",
                flush=True,
            )

    elapsed = time.perf_counter() - started
    fd_after = file_descriptor_count()
    rss_after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    ok = [record for record in runs if record["status"] == "ok"]
    failures = [record for record in runs if record["status"] != "ok"]
    target_groups = defaultdict(list)
    for record in ok:
        if record["repeat"] != 1:
            continue
        key = (
            unicodedata.normalize("NFC", record["source_parent"]).casefold(),
            unicodedata.normalize(
                "NFC", record["suggested_directory_name"]
            ).rstrip(" .").casefold(),
        )
        target_groups[key].append(record)
    collisions = [
        {
            "source_parent": records[0]["source_parent"],
            "suggested_directory_name": records[0]["suggested_directory_name"],
            "count": len(records),
            "sources": [record["source"] for record in records],
        }
        for records in target_groups.values()
        if len(records) > 1
    ]
    payload = {
        "audit": "metadata-only production preflight",
        "pixel_data_read": False,
        "source_count": len(sources),
        "repeat": args.repeat,
        "run_count": len(runs),
        "success_count": len(ok),
        "failure_count": len(failures),
        "elapsed_seconds": round(elapsed, 6),
        "fd_before": fd_before,
        "fd_after": fd_after,
        "max_rss_kib_before": rss_before,
        "max_rss_kib_after": rss_after,
        "suggested_output_collisions": collisions,
        "runs": runs,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {output}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
