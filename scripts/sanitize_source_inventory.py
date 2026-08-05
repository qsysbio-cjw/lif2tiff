#!/usr/bin/env python3
"""Create a collision-test inventory without operator or directory identities."""

from __future__ import annotations

import argparse
import csv
import hashlib
import re
from pathlib import Path, PurePosixPath


FIELDS = (
    "person",
    "source_relative_path",
    "source_size_bytes",
    "series_count",
    "source_status",
)


def opaque_directory(value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    return f"directory_{digest}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit(f"refusing to overwrite: {args.output}")

    with args.source.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    people = {
        person: f"operator_{index:02d}"
        for index, person in enumerate(
            sorted({row["person"] for row in rows}, key=str.casefold), start=1
        )
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        for row in rows:
            source = PurePosixPath(row["source_relative_path"])
            directories = [opaque_directory(part) for part in source.parts[:-1]]
            filename = re.sub(
                re.escape(row["person"]),
                people[row["person"]],
                source.name,
                flags=re.IGNORECASE,
            )
            anonymized = PurePosixPath(*directories, filename).as_posix()
            writer.writerow(
                {
                    "person": people[row["person"]],
                    "source_relative_path": anonymized,
                    "source_size_bytes": row["source_size_bytes"],
                    "series_count": row["series_count"],
                    "source_status": row["source_status"],
                }
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
