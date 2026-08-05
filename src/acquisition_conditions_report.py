#!/usr/bin/env python3
"""Summarize distinct Leica acquisition settings for one catalog person."""

from __future__ import annotations

import argparse
import csv
import html
import json
import math
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path


SERIES_PROTOCOL_FIELDS = (
    "x_px",
    "y_px",
    "channel_count",
    "pixel_size_x_um",
    "pixel_size_y_um",
    "bit_depths",
    "objective_name",
    "magnification",
    "numerical_aperture",
    "immersion",
    "scan_mode",
    "line_averaging",
    "frame_averaging",
    "pinhole_um",
    "pinhole_airy",
    "zoom",
    "scan_speed",
    "scan_direction",
    "pixel_dwell_time_us",
    "tld_mode",
)

CHANNEL_PROTOCOL_FIELDS = (
    "channel_index",
    "physical_label",
    "analysis_label",
    "stain",
    "lut",
    "bit_depth",
    "detector_name",
    "detector_type",
    "scan_type",
    "gain",
    "offset",
    "acquisition_mode",
    "dye_name_from_leica",
    "sequential_index",
    "sequential_setting_name",
    "excitation_wavelengths_nm",
    "excitation_intensities_percent",
    "emission_window_begin_nm",
    "emission_window_end_nm",
)


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _number(value: object) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _rounded(value: object, digits: int) -> object:
    number = _number(value)
    return round(number, digits) if number is not None else value


def _normalized_value(field: str, value: object) -> object:
    if field in {"gain", "offset", "excitation_intensities_percent", "emission_window_begin_nm", "emission_window_end_nm", "pinhole_um"}:
        if field == "excitation_intensities_percent":
            return ";".join(
                str(_rounded(item, 1)) for item in str(value or "").split(";") if item
            )
        return _rounded(value, 1)
    if field in {"pinhole_airy", "zoom", "pixel_dwell_time_us"}:
        return _rounded(value, 3)
    if field in {"pixel_size_x_um", "pixel_size_y_um"}:
        return _rounded(value, 6)
    return value


def _protocol_signature(series: dict[str, str], channels: list[dict[str, str]], normalized: bool) -> str:
    series_values = {
        field: _normalized_value(field, series.get(field, "")) if normalized else series.get(field, "")
        for field in SERIES_PROTOCOL_FIELDS
    }
    channel_values = []
    for channel in sorted(channels, key=lambda row: int(row["channel_index"])):
        channel_values.append(
            {
                field: _normalized_value(field, channel.get(field, ""))
                if normalized
                else channel.get(field, "")
                for field in CHANNEL_PROTOCOL_FIELDS
            }
        )
    return json.dumps({"series": series_values, "channels": channel_values}, ensure_ascii=False, sort_keys=True)


def _fmt(value: object, digits: int = 1) -> str:
    number = _number(value)
    if number is None:
        return str(value or "")
    if digits == 0:
        return str(int(round(number)))
    return f"{number:.{digits}f}".rstrip("0").rstrip(".")


def _top_values(values: list[object], digits: int = 1, limit: int = 5) -> str:
    normalized = [_fmt(value, digits) for value in values if str(value or "")]
    return ", ".join(f"{value} (n={count})" for value, count in Counter(normalized).most_common(limit)) or "none"


def _range_median(values: list[object], digits: int = 1) -> str:
    numbers = [number for value in values if (number := _number(value)) is not None]
    if not numbers:
        return "none"
    return f"{_fmt(min(numbers), digits)}-{_fmt(max(numbers), digits)}; median {_fmt(statistics.median(numbers), digits)}"


def _channel_description(channel: dict[str, str]) -> str:
    index = channel.get("channel_index", "?")
    physical = channel.get("physical_label") or "unknown"
    dye = (channel.get("dye_name_from_leica") or "").removeprefix("Leica/")
    detector = channel.get("detector_name") or ""
    gain = _fmt(channel.get("gain"), 1)
    excitation = channel.get("excitation_wavelengths_nm") or ""
    intensities = channel.get("excitation_intensities_percent") or ""
    begin = channel.get("emission_window_begin_nm") or ""
    end = channel.get("emission_window_end_nm") or ""
    parts = [f"C{index} {physical}"]
    if dye:
        parts.append(dye)
    is_fluorescence = channel.get("inferred_modality") == "fluorescence_like"
    if is_fluorescence and excitation:
        pairs = [
            f"{_fmt(w, 1)}nm@{_fmt(i, 1)}%"
            for w, i in zip(excitation.split(";"), intensities.split(";"))
        ]
        parts.append("Ex " + "+".join(pairs))
    if is_fluorescence and begin and end:
        parts.append(f"Em {_fmt(begin, 1)}-{_fmt(end, 1)}nm")
    if detector:
        parts.append(f"{detector} gain {gain}")
    return " | ".join(parts)


def _collect(catalog_root: Path, person: str):
    catalog = [row for row in _read_csv(catalog_root / "catalog.csv") if row["person"] == person]
    series_rows = []
    channel_rows = []
    channels_by_series: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for catalog_row in catalog:
        output_dir = catalog_root / catalog_row["output_relative_path"]
        identity = {
            "person": person,
            "session": catalog_row["session"],
            "lif_name": catalog_row["lif_name"],
            "source_relative_path": catalog_row["source_relative_path"],
        }
        for row in _read_csv(output_dir / "series_metadata.csv"):
            combined = {**identity, **row}
            series_rows.append(combined)
        for row in _read_csv(output_dir / "channel_metadata.csv"):
            combined = {**identity, **row}
            channel_rows.append(combined)
            channels_by_series[(catalog_row["source_relative_path"], row["series_index"])].append(combined)
    return catalog, series_rows, channel_rows, channels_by_series


def _build_protocols(series_rows, channels_by_series):
    records = []
    normalized_members: dict[str, list[dict[str, str]]] = defaultdict(list)
    exact_signatures = set()
    for series in series_rows:
        key = (series["source_relative_path"], series["series_index"])
        channels = channels_by_series[key]
        normalized = _protocol_signature(series, channels, normalized=True)
        exact_signatures.add(_protocol_signature(series, channels, normalized=False))
        normalized_members[normalized].append(series)

    ordered = sorted(normalized_members.items(), key=lambda item: (-len(item[1]), item[0]))
    protocol_by_signature = {signature: f"P{index:03d}" for index, (signature, _) in enumerate(ordered, start=1)}
    protocol_rows = []
    for signature, members in ordered:
        protocol_id = protocol_by_signature[signature]
        representative = members[0]
        key = (representative["source_relative_path"], representative["series_index"])
        channels = sorted(channels_by_series[key], key=lambda row: int(row["channel_index"]))
        paths = sorted({member["source_relative_path"] for member in members})
        timestamps = sorted(member["timestamp_first_utc"] for member in members if member.get("timestamp_first_utc"))
        protocol_rows.append(
            {
                "protocol_id": protocol_id,
                "series_count": len(members),
                "lif_count": len(paths),
                "first_timestamp_utc": timestamps[0] if timestamps else "",
                "last_timestamp_utc": timestamps[-1] if timestamps else "",
                "image_size_px": f"{representative['x_px']}x{representative['y_px']}",
                "pixel_size_um": representative["pixel_size_x_um"],
                "objective": representative["objective_name"],
                "zoom": _fmt(representative["zoom"], 3),
                "pinhole_airy": _fmt(representative["pinhole_airy"], 3),
                "line_averaging": representative["line_averaging"],
                "scan_speed": representative["scan_speed"],
                "pixel_dwell_time_us": representative["pixel_dwell_time_us"],
                "channels": " || ".join(_channel_description(channel) for channel in channels),
                "example_lif": paths[0],
                "normalized_signature": signature,
            }
        )
        for member in members:
            records.append(
                {
                    "source_relative_path": member["source_relative_path"],
                    "series_index": member["series_index"],
                    "series_name": member["series_name"],
                    "dimension_type": member["dimension_type"],
                    "z_count": member["z_count"],
                    "t_count": member["t_count"],
                    "optical_protocol_id": protocol_id,
                }
            )
    return protocol_rows, records, len(exact_signatures)


def _unique_value_rows(series_rows, channel_rows):
    output = []
    series_fields = (
        "dimension_type", "x_px", "y_px", "z_count", "t_count", "channel_count",
        "pixel_size_x_um", "pixel_size_y_um", "pixel_size_z_um", "bit_depths",
        "objective_name", "line_averaging", "frame_averaging", "pinhole_um",
        "pinhole_airy", "zoom", "scan_speed", "scan_direction", "pixel_dwell_time_us", "tld_mode",
    )
    channel_fields = (
        "physical_label", "analysis_label", "dye_name_from_leica", "detector_name",
        "gain", "offset", "excitation_wavelengths_nm", "excitation_intensities_percent",
        "emission_window_begin_nm", "emission_window_end_nm", "sequential_setting_name",
    )
    for scope, rows, fields in (("series", series_rows, series_fields), ("channel", channel_rows, channel_fields)):
        for field in fields:
            grouped: dict[str, set[str]] = defaultdict(set)
            counts: Counter[str] = Counter()
            for row in rows:
                value = row.get(field, "")
                if not value:
                    continue
                counts[value] += 1
                grouped[value].add(row["source_relative_path"])
            for value, count in counts.most_common():
                output.append({"scope": scope, "field": field, "value": value, "count": count, "lif_count": len(grouped[value])})
    return output


def _fluorescence_summary(channel_rows):
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in channel_rows:
        if row.get("inferred_modality") == "fluorescence_like":
            grouped[row.get("dye_name_from_leica") or "Unspecified fluorescence preset"].append(row)
    summaries = []
    for dye, rows in sorted(grouped.items()):
        laser_pairs = []
        for row in rows:
            for wavelength, intensity in zip(
                (row.get("excitation_wavelengths_nm") or "").split(";"),
                (row.get("excitation_intensities_percent") or "").split(";"),
            ):
                if wavelength and intensity:
                    laser_pairs.append(f"{_fmt(wavelength, 1)} nm @ {_fmt(intensity, 1)}%")
        emission = [
            f"{_fmt(row['emission_window_begin_nm'], 1)}-{_fmt(row['emission_window_end_nm'], 1)} nm"
            for row in rows
            if row.get("emission_window_begin_nm") and row.get("emission_window_end_nm")
        ]
        summaries.append(
            {
                "dye": dye.removeprefix("Leica/"),
                "instances": len(rows),
                "lif_count": len({row["source_relative_path"] for row in rows}),
                "luts": ", ".join(sorted({row["physical_label"] for row in rows})),
                "detectors": ", ".join(sorted({row["detector_name"] for row in rows if row.get("detector_name")})),
                "laser_common": _top_values(laser_pairs, limit=5),
                "gain_range": _range_median([row["gain"] for row in rows], 1),
                "gain_common": _top_values([row["gain"] for row in rows], 1, 5),
                "emission_common": _top_values(emission, limit=5),
            }
        )
    return summaries


def _write_html(path, person, catalog, series_rows, channel_rows, protocols, exact_count, fluorescence):
    dimension_counts = Counter(row["dimension_type"] for row in series_rows)
    objective_counts = Counter(row["objective_name"] for row in series_rows)
    bf = [row for row in channel_rows if row.get("inferred_modality") == "brightfield_like"]
    fluor_rows = "".join(
        f"<tr><td>{html.escape(row['dye'])}</td><td>{row['instances']}</td><td>{row['lif_count']}</td>"
        f"<td>{html.escape(row['luts'])}</td><td>{html.escape(row['detectors'])}</td>"
        f"<td>{html.escape(row['laser_common'])}</td><td>{html.escape(row['gain_range'])}</td>"
        f"<td>{html.escape(row['gain_common'])}</td><td>{html.escape(row['emission_common'])}</td></tr>"
        for row in fluorescence
    )
    protocol_html = "".join(
        f'''<tr data-search="{html.escape(' '.join(str(value) for value in row.values()).casefold())}">
        <td>{row['protocol_id']}</td><td>{row['series_count']}</td><td>{row['lif_count']}</td>
        <td>{html.escape(row['image_size_px'])}</td><td>{html.escape(row['pixel_size_um'])}</td>
        <td>{html.escape(row['objective'])}</td><td>{html.escape(row['zoom'])}</td>
        <td>{html.escape(row['pinhole_airy'])}</td><td>{html.escape(row['line_averaging'])}</td>
        <td>{html.escape(row['scan_speed'])}</td><td class="channels">{html.escape(row['channels'])}</td>
        <td class="example">{html.escape(row['example_lif'])}</td></tr>'''
        for row in protocols
    )
    overview_rows = [
        ("Dimension types", ", ".join(f"{key}: {value}" for key, value in dimension_counts.items())),
        ("Image formats", _top_values([f"{row['x_px']}x{row['y_px']}" for row in series_rows], limit=10)),
        ("Channel counts", _top_values([row["channel_count"] for row in series_rows], 0, 10)),
        ("Objectives", ", ".join(f"{key}: {value}" for key, value in objective_counts.items())),
        ("Pixel size X (um/px)", _top_values([row["pixel_size_x_um"] for row in series_rows], 6, 12)),
        ("Zoom", _top_values([row["zoom"] for row in series_rows], 3, 12)),
        ("Pinhole (Airy)", _top_values([row["pinhole_airy"] for row in series_rows], 3, 12)),
        ("Scan speed", _top_values([row["scan_speed"] for row in series_rows], 0, 10)),
        ("Pixel dwell time (us)", _top_values([row["pixel_dwell_time_us"] for row in series_rows], 3, 10)),
        ("Line averaging", _top_values([row["line_averaging"] for row in series_rows], 0, 10)),
        ("Brightfield gain", f"{_range_median([row['gain'] for row in bf], 1)}; common: {_top_values([row['gain'] for row in bf], 1, 8)}"),
        ("Brightfield offset", _top_values([row["offset"] for row in bf], 1, 8)),
    ]
    overview_html = "".join(
        f"<tr><th>{html.escape(label)}</th><td>{html.escape(value)}</td></tr>" for label, value in overview_rows
    )
    document = f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(person)} Acquisition Conditions</title>
<style>
*{{box-sizing:border-box}} body{{margin:0;font-family:Arial,sans-serif;color:#20282c;background:#f5f7f7}} header{{padding:18px 24px;background:#fff;border-bottom:1px solid #d6dcdf}} h1{{margin:0 0 8px;font-size:24px}} h2{{font-size:18px;margin:22px 0 8px}} .stats{{display:flex;gap:8px;flex-wrap:wrap}} .stats span{{border:1px solid #d6dcdf;background:#f8f9f9;padding:4px 8px;font-size:13px}} main{{padding:0 24px 28px}} .note{{max-width:1200px;line-height:1.5;color:#536168;font-size:13px}} .links a{{margin-right:14px;color:#176b87}} table{{width:100%;border-collapse:collapse;background:white;font-size:12px}} th,td{{border-bottom:1px solid #e0e5e7;padding:7px 8px;text-align:left;vertical-align:top}} thead th{{position:sticky;top:0;background:#e8edef;z-index:1}} .overview th{{width:210px;background:#edf1f2}} .table-wrap{{overflow:auto;max-height:560px;border:1px solid #d6dcdf}} input{{width:100%;padding:8px 10px;border:1px solid #b8c2c6;margin-bottom:8px}} td.channels{{min-width:520px}} td.example{{min-width:260px;overflow-wrap:anywhere}} code{{font-size:12px}} @media(max-width:900px){{main{{padding:0 8px 20px}} header{{padding-left:12px}}}}
</style></head><body>
<header><h1>{html.escape(person)} Acquisition Conditions</h1><div class="stats"><span>{len(catalog)} LIF</span><span>{len(series_rows)} series</span><span>{len(channel_rows)} channel instances</span><span>{len(protocols)} normalized optical protocols</span><span>{exact_count} exact optical protocols</span></div></header>
<main>
<p class="note"><strong>Interpretation:</strong> Leica dye names are acquisition presets recorded by the microscope, not proof of the reagent present in the sample. In particular, ALEXA 488/546 presets may have been reused for Nile Red or other channels. Detector hardware range and the selected emission window are different fields; this report uses the sequential-setting-specific <code>MultiBand</code> window as the selected emission window.</p>
<p class="links"><a href="series_conditions.csv">All series settings CSV</a><a href="channel_conditions.csv">All channel settings CSV</a><a href="optical_protocols.csv">Normalized protocols CSV</a><a href="unique_values.csv">Every distinct value CSV</a></p>
<h2>Acquisition overview</h2><table class="overview">{overview_html}</table>
<h2>Fluorescence preset summary</h2><div class="table-wrap"><table><thead><tr><th>Leica preset</th><th>Channel instances</th><th>LIF</th><th>LUT</th><th>Detector</th><th>Common excitation</th><th>Gain range; median</th><th>Common gain</th><th>Common selected emission window</th></tr></thead><tbody>{fluor_rows}</tbody></table></div>
<h2>Normalized optical protocols</h2><p class="note">Stage position, timestamp, Z/T frame counts and biological treatment are excluded from the optical protocol signature. Numeric acquisition values are rounded only for grouping; exact raw values remain in the CSV files.</p>
<input id="search" type="search" placeholder="Search protocol, gain, excitation, emission, LIF..."><div class="table-wrap"><table><thead><tr><th>ID</th><th>Series</th><th>LIF</th><th>Image</th><th>um/px</th><th>Objective</th><th>Zoom</th><th>Pinhole AU</th><th>Line avg</th><th>Speed</th><th>Channels</th><th>Example</th></tr></thead><tbody id="protocols">{protocol_html}</tbody></table></div>
</main><script>const input=document.getElementById('search'),rows=[...document.querySelectorAll('#protocols tr')];input.addEventListener('input',()=>{{const q=input.value.trim().toLowerCase();for(const row of rows)row.hidden=q&&!row.dataset.search.includes(q)}});</script></body></html>'''
    path.write_text(document, encoding="utf-8")


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("catalog_root", type=Path)
    parser.add_argument("--person", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--update-existing", action="store_true")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv or sys.argv[1:])
    catalog_root = args.catalog_root.resolve()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=args.update_existing)
    catalog, series_rows, channel_rows, channels_by_series = _collect(catalog_root, args.person)
    if not catalog:
        raise SystemExit(f"no catalog rows for person: {args.person}")
    protocols, assignments, exact_count = _build_protocols(series_rows, channels_by_series)
    assignment_by_key = {
        (row["source_relative_path"], row["series_index"]): row["optical_protocol_id"]
        for row in assignments
    }
    for row in series_rows:
        row["optical_protocol_id"] = assignment_by_key[(row["source_relative_path"], row["series_index"])]
    _write_csv(output / "series_conditions.csv", series_rows)
    _write_csv(output / "channel_conditions.csv", channel_rows)
    _write_csv(output / "optical_protocols.csv", protocols)
    _write_csv(output / "unique_values.csv", _unique_value_rows(series_rows, channel_rows))
    fluorescence = _fluorescence_summary(channel_rows)
    summary = {
        "person": args.person,
        "lif_count": len(catalog),
        "series_count": len(series_rows),
        "channel_instance_count": len(channel_rows),
        "normalized_optical_protocol_count": len(protocols),
        "exact_optical_protocol_count": exact_count,
        "fluorescence_presets": fluorescence,
    }
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_html(output / "index.html", args.person, catalog, series_rows, channel_rows, protocols, exact_count, fluorescence)
    print(output / "index.html")
    print(json.dumps({key: value for key, value in summary.items() if key != "fluorescence_presets"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
