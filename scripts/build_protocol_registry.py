#!/usr/bin/env python3
"""Build the portable channel-protocol registry from a frozen audit table."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path


FAMILY_TO_DYE = {
    "nile_red_acquisition": "nile_red",
    "bodipy_acquisition": "bodipy_493_503",
    "fm4_64_acquisition": "fm4_64",
    "pi_acquisition": "propidium_iodide",
    "cmac_acquisition": "cmac",
    "disc3_5_acquisition": "disc3_5",
    "sytox_green_acquisition": "sytox_green",
    "er_tracker_red_probable_acquisition": "er_tracker_red_probable",
}

SIGNATURE_FIELDS = (
    "excitation_nm",
    "laser_intensity_percent",
    "emission_begin_nm",
    "emission_end_nm",
    "gain",
    "offset",
    "detector",
    "detector_type",
    "scan_type",
    "acquisition_mode",
    "bit_depth",
)


def build_registry(
    source: Path,
    *,
    schema_name: str,
    frozen_through: str | None,
) -> dict:
    with source.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    groups = []
    for row in rows:
        family = row["protocol_families"]
        groups.append(
            {
                "id": row["tolerant_optical_group_id"],
                "acquisition_family": family,
                "candidate_dye_id": FAMILY_TO_DYE.get(family),
                "channel_count": int(row["channel_count"]),
                "series_count": int(row["series_count"]),
                "lif_count": int(row["lif_count"]),
                "first_date": row["first_date"] or None,
                "last_date": row["last_date"] or None,
                "signature": {field: row[field] for field in SIGNATURE_FIELDS},
            }
        )
    return {
        "schema_name": schema_name,
        "schema_version": "0.1",
        "frozen_through": frozen_through,
        "source_table": source.name,
        "interpretation": (
            "Matches identify a historical acquisition-parameter family and a "
            "candidate dye; they do not prove that the reagent was present."
        ),
        "tolerance_policy": {
            "excitation_nm": "0.1 nm",
            "laser_intensity_percent": "0.05 percentage point",
            "emission_boundaries_nm": "0.1 nm",
            "gain": "0.1",
            "offset": "0.1",
            "categorical_fields": "exact",
        },
        "groups": groups,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument(
        "--schema-name",
        default="lif2tiff_acquisition_protocol_registry",
    )
    parser.add_argument("--frozen-through")
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(f"refusing to overwrite: {args.output}")
    registry = build_registry(
        args.source,
        schema_name=args.schema_name,
        frozen_through=args.frozen_through,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(registry, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {len(registry['groups'])} protocol groups to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
