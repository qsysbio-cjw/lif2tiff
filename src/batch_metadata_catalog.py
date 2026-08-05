#!/usr/bin/env python3
"""Batch-inspect LIF files and build one portable HTML metadata catalog."""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import re
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import quote

from lif2tiff_project import sanitize_name
from lif_metadata_inspect import inspect_lif
from stage_map import _annotate_well_overlap


REQUIRED_OUTPUTS = (
    "metadata.json",
    "series_metadata.csv",
    "channel_metadata.csv",
    "stage_positions.csv",
    "metadata_report.md",
    "stage_map.html",
    "source_metadata.xml",
)


def _natural_key(value: str) -> list[tuple[int, object]]:
    return [
        (0, int(part)) if part.isdigit() else (1, part.casefold())
        for part in re.split(r"(\d+)", value)
    ]


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def _float(value: object) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _well_key(well: str) -> tuple[int, int]:
    match = re.fullmatch(r"([A-Z]+)(\d+)", well)
    if not match:
        return (999, 999)
    row = 0
    for char in match.group(1):
        row = row * 26 + ord(char) - ord("A") + 1
    return (row, int(match.group(2)))


def _compact_wells(wells: list[str]) -> str:
    ordered = sorted(set(wells), key=_well_key)
    if not ordered:
        return "none"
    parsed = [(well, *_well_key(well)) for well in ordered]
    rows = [row for _, row, _ in parsed]
    columns = [column for _, _, column in parsed]
    if len(set(columns)) == 1 and rows == list(range(min(rows), max(rows) + 1)):
        first, last = ordered[0], ordered[-1]
        return first if first == last else f"{first}-{last}"
    if len(set(rows)) == 1 and columns == list(range(min(columns), max(columns) + 1)):
        first, last = ordered[0], ordered[-1]
        return first if first == last else f"{first}-{last}"
    if len(ordered) <= 8:
        return ", ".join(ordered)
    return f"{', '.join(ordered[:6])}, ... ({len(ordered)} wells)"


def _complete(output_dir: Path) -> bool:
    return all((output_dir / name).is_file() and (output_dir / name).stat().st_size for name in REQUIRED_OUTPUTS)


def _channel_summary(rows: list[dict[str, str]]) -> str:
    channels: dict[int, dict[str, str]] = {}
    for row in rows:
        try:
            index = int(row.get("channel_index", ""))
        except ValueError:
            continue
        channels.setdefault(index, row)
    labels = []
    for index, row in sorted(channels.items()):
        physical = row.get("physical_label") or row.get("label") or "unknown"
        analysis = row.get("analysis_label") or ""
        suffix = f"/{analysis}" if analysis and analysis not in {physical, "unknown"} else ""
        labels.append(f"C{index} {physical}{suffix}")
    return "; ".join(labels) or "none"


def _summarize_output(
    output_dir: Path,
    source_label: str,
    source_relative: Path,
    source_size: int,
    output_root: Path,
    calibration: dict | None,
) -> dict[str, object]:
    series = _read_csv(output_dir / "series_metadata.csv")
    channels = _read_csv(output_dir / "channel_metadata.csv")
    stage = _read_csv(output_dir / "stage_positions.csv")
    dimensions = Counter(row.get("dimension_type") or "unknown" for row in series)
    dimension_summary = "; ".join(
        f"{name}: {count}" for name, count in sorted(dimensions.items(), key=lambda item: _natural_key(item[0]))
    )

    points = []
    for row in stage:
        x = _float(row.get("stage_x_um"))
        y = _float(row.get("stage_y_um"))
        if x is not None and y is not None:
            points.append({"x_um": x, "y_um": y})

    wells: list[str] = []
    outside = 0
    if calibration and points and series:
        widths = [
            _float(row.get("x_px")) * _float(row.get("pixel_size_x_um"))
            for row in series
            if _float(row.get("x_px")) is not None and _float(row.get("pixel_size_x_um")) is not None
        ]
        heights = [
            _float(row.get("y_px")) * _float(row.get("pixel_size_y_um"))
            for row in series
            if _float(row.get("y_px")) is not None and _float(row.get("pixel_size_y_um")) is not None
        ]
        if widths and heights:
            _annotate_well_overlap(
                points,
                statistics.median(widths),
                statistics.median(heights),
                calibration.get("full_x_grid") or [],
                calibration.get("full_y_grid") or [],
            )
            wells = [point["best_well"] for point in points if point.get("best_well")]
            outside = sum(1 for point in points if not point.get("best_well"))

    if not points:
        qc = "no_stage"
    elif calibration and outside:
        qc = "review"
    else:
        qc = "ok"

    output_relative = output_dir.relative_to(output_root)
    source_metadata_sha256 = hashlib.sha256((output_dir / "source_metadata.xml").read_bytes()).hexdigest()
    timestamps = sorted(row.get("timestamp_first_utc") for row in series if row.get("timestamp_first_utc"))
    objectives = sorted(set(row.get("objective_name") for row in series if row.get("objective_name")))
    return {
        "person": source_label,
        "session": source_relative.parent.as_posix() or ".",
        "lif_name": source_relative.name,
        "source_relative_path": source_relative.as_posix(),
        "source_size_bytes": source_size,
        "series_count": len(series),
        "dimensions": dimension_summary,
        "channels": _channel_summary(channels),
        "stage_positions": len(points),
        "wells": _compact_wells(wells),
        "mapped_fovs": len(wells),
        "outside_fovs": outside,
        "qc": qc,
        "first_timestamp_utc": timestamps[0] if timestamps else "",
        "last_timestamp_utc": timestamps[-1] if timestamps else "",
        "objectives": "; ".join(objectives),
        "output_relative_path": output_relative.as_posix(),
        "source_metadata_sha256": source_metadata_sha256,
        "source_status": "normal",
        "source_note": "",
        "duplicate_group": "",
        "duplicate_of": "",
    }


def _annotate_sources(rows: list[dict[str, object]], annotations: dict) -> None:
    for item in annotations.get("relocated_prefixes", []):
        current = str(item.get("current", "")).rstrip("/")
        previous = str(item.get("previous", "")).rstrip("/")
        for row in rows:
            key = f"{row['person']}/{row['source_relative_path']}"
            if current and (key == current or key.startswith(f"{current}/")):
                row["source_status"] = "relocated"
                row["source_note"] = f"Relocated from {previous}"

    groups: dict[tuple[int, str], list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        groups[(int(row["source_size_bytes"]), str(row["source_metadata_sha256"]))].append(row)
    for members in groups.values():
        if len(members) < 2:
            continue
        members.sort(
            key=lambda row: (
                "副本" in str(row["lif_name"]) or "copy" in str(row["lif_name"]).casefold(),
                _natural_key(f"{row['person']}/{row['source_relative_path']}"),
            )
        )
        primary = members[0]
        primary_path = f"{primary['person']}/{primary['source_relative_path']}"
        group_id = str(primary["source_metadata_sha256"])[:12]
        for row in members:
            row["duplicate_group"] = group_id
        for row in members[1:]:
            row["source_status"] = "probable_duplicate"
            row["duplicate_of"] = primary_path
            row["source_note"] = f"Same file size and Leica XML metadata as {primary_path}"


def _write_catalog_csv(path: Path, rows: list[dict[str, object]]) -> None:
    fields = [
        "person",
        "session",
        "lif_name",
        "source_relative_path",
        "source_size_bytes",
        "series_count",
        "dimensions",
        "channels",
        "stage_positions",
        "wells",
        "mapped_fovs",
        "outside_fovs",
        "qc",
        "first_timestamp_utc",
        "last_timestamp_utc",
        "objectives",
        "output_relative_path",
        "source_metadata_sha256",
        "source_status",
        "source_note",
        "duplicate_group",
        "duplicate_of",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _write_index(
    path: Path,
    rows: list[dict[str, object]],
    failures: list[dict[str, str]],
    annotations: dict,
) -> None:
    people = sorted({str(row["person"]) for row in rows}, key=_natural_key)
    total_series = sum(int(row["series_count"]) for row in rows)
    total_size = sum(int(row["source_size_bytes"]) for row in rows)
    qc_counts = Counter(str(row["qc"]) for row in rows)
    mode_counts: Counter[str] = Counter()
    person_counts: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        for item in str(row["dimensions"]).split(";"):
            name, _, count = item.strip().partition(":")
            if name and count.strip().isdigit():
                mode_counts[name] += int(count)
        stats = person_counts[str(row["person"])]
        stats["lif"] += 1
        stats["series"] += int(row["series_count"])
        stats["bytes"] += int(row["source_size_bytes"])
        stats[str(row["qc"])] += 1
        stats["outside"] += int(row["outside_fovs"])
        stats[str(row["source_status"])] += 1

    table_rows = []
    for row in rows:
        output = Path(str(row["output_relative_path"]))
        map_href = quote((output / "stage_map.html").as_posix())
        report_href = quote((output / "metadata_report.md").as_posix())
        csv_href = quote((output / "series_metadata.csv").as_posix())
        search = " ".join(str(value) for value in row.values()).casefold()
        size_mib = int(row["source_size_bytes"]) / 1048576
        qc = str(row["qc"])
        source_status = str(row["source_status"])
        source_note = str(row["source_note"])
        table_rows.append(
            f'''<tr data-person="{html.escape(str(row['person']))}" data-qc="{html.escape(qc)}" data-source-status="{html.escape(source_status)}" data-search="{html.escape(search)}">
  <td>{html.escape(str(row['person']))}</td>
  <td>{html.escape(str(row['session']))}</td>
  <td class="lif">{html.escape(str(row['lif_name']))}</td>
  <td class="num">{size_mib:.1f}</td>
  <td class="num">{row['series_count']}</td>
  <td>{html.escape(str(row['dimensions']))}</td>
  <td>{html.escape(str(row['channels']))}</td>
  <td>{html.escape(str(row['wells']))}</td>
  <td><span class="status {html.escape(qc)}">{html.escape(qc)}</span>{f" <small>outside: {row['outside_fovs']}</small>" if row['outside_fovs'] else ""}</td>
  <td><span class="status {html.escape(source_status)}" title="{html.escape(source_note)}">{html.escape(source_status)}</span></td>
  <td class="links"><a href="{map_href}" target="_blank">Map</a><a href="{report_href}" target="_blank">Report</a><a href="{csv_href}" target="_blank">CSV</a></td>
</tr>'''
        )

    options = "".join(f'<option value="{html.escape(person)}">{html.escape(person)}</option>' for person in people)
    failure_html = ""
    if failures:
        items = "".join(
            f"<li><strong>{html.escape(item['source_relative_path'])}</strong>: {html.escape(item['error'])}</li>"
            for item in failures
        )
        failure_html = f"<details class=\"failures\"><summary>{len(failures)} files need attention</summary><ul>{items}</ul></details>"

    person_rows = "".join(
        f"<tr><td>{html.escape(person)}</td><td>{stats['lif']}</td><td>{stats['series']}</td>"
        f"<td>{stats['bytes'] / 1073741824:.2f}</td><td>{stats['ok']}</td><td>{stats['review']}</td>"
        f"<td>{stats['outside']}</td><td>{stats['probable_duplicate']}</td></tr>"
        for person, stats in sorted(person_counts.items(), key=lambda item: _natural_key(item[0]))
    )
    mode_summary = "".join(
        f"<span>{html.escape(name)}: {count} series</span>"
        for name, count in sorted(mode_counts.items(), key=lambda item: _natural_key(item[0]))
    )
    excluded = annotations.get("excluded_collections", [])
    excluded_count = sum(int(item.get("file_count", 0)) for item in excluded)
    relocated_count = sum(int(item.get("file_count", 0)) for item in annotations.get("relocated_prefixes", []))
    duplicate_count = sum(1 for row in rows if row["source_status"] == "probable_duplicate")
    reconciliation_items = "".join(
        f"<li><strong>{html.escape(str(item.get('path', '')))}</strong>: excluded {item.get('file_count', 0)} files; {html.escape(str(item.get('reason', '')))}</li>"
        for item in excluded
    )
    reconciliation_html = f'''<details class="reconciliation">
      <summary>Source reconciliation: {relocated_count} relocated, {duplicate_count} duplicate-like in catalog, {excluded_count} duplicate copies excluded</summary>
      <p class="note">Duplicate-like means equal file size and identical Leica XML metadata. It is a strong copy signal but not a full pixel-byte hash; no source file was deleted.</p>
      <ul>{reconciliation_items}</ul>
    </details>'''
    calibration_note = html.escape(
        str(
            annotations.get(
                "calibration_note",
                "Position QC is relative to the supplied calibration profile and does not establish the sample carrier type.",
            )
        )
    )
    condition_reports = sorted(path.parent.glob("*_acquisition_conditions/index.html"))
    report_links = "".join(
        f'<a href="{quote(report.relative_to(path.parent).as_posix())}">{html.escape(report.parent.name.removesuffix("_acquisition_conditions"))} acquisition conditions</a>'
        for report in condition_reports
    )
    report_nav = f'<nav class="report-nav">{report_links}</nav>' if report_links else ""

    document = f'''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>LIF Metadata Catalog</title>
  <style>
    :root {{ color-scheme: light; --line:#d7dcdf; --ink:#20282c; --muted:#647178; --accent:#176b87; }}
    * {{ box-sizing: border-box; }}
    body {{ margin:0; font-family:Arial,sans-serif; color:var(--ink); background:#f5f7f7; }}
    header {{ padding:18px 24px 14px; background:white; border-bottom:1px solid var(--line); }}
    h1 {{ margin:0 0 8px; font-size:24px; }}
    .summary {{ display:flex; gap:8px; flex-wrap:wrap; color:var(--muted); font-size:13px; }}
    .summary span {{ padding:4px 8px; border:1px solid var(--line); background:#f8f9f9; }}
    .report-nav {{ margin-top:9px; display:flex; gap:14px; flex-wrap:wrap; font-size:13px; }}
    .report-nav a {{ color:var(--accent); }}
    .overview {{ padding:10px 24px; background:white; border-bottom:1px solid var(--line); }}
    .overview summary {{ cursor:pointer; font-weight:600; margin-bottom:8px; }}
    .overview-grid {{ display:grid; grid-template-columns:minmax(420px,720px) 1fr; gap:22px; align-items:start; }}
    .overview table {{ width:100%; font-size:12px; }}
    .overview th {{ position:static; padding:5px 7px; }}
    .overview td {{ padding:5px 7px; }}
    .mode-summary {{ display:flex; gap:7px; flex-wrap:wrap; margin-bottom:8px; }}
    .mode-summary span {{ border:1px solid var(--line); padding:4px 7px; background:#f8f9f9; font-size:12px; }}
    .note {{ margin:0; color:var(--muted); font-size:12px; line-height:1.45; max-width:760px; }}
    .controls {{ display:grid; grid-template-columns:minmax(240px,1fr) 180px 150px auto; gap:8px; padding:12px 24px; background:#eef1f2; border-bottom:1px solid var(--line); }}
    input,select,button {{ min-height:34px; border:1px solid #b8c2c6; background:white; padding:6px 9px; font:inherit; }}
    button {{ cursor:pointer; }}
    main {{ padding:0 24px 24px; }}
    .table-wrap {{ overflow:auto; max-height:calc(100vh - 310px); min-height:320px; border:1px solid var(--line); border-top:0; background:white; }}
    table {{ width:100%; border-collapse:collapse; font-size:12px; }}
    th {{ position:sticky; top:0; z-index:1; text-align:left; background:#e8edef; border-bottom:1px solid #aeb9bd; padding:8px; white-space:nowrap; }}
    td {{ padding:7px 8px; border-bottom:1px solid #e1e5e7; vertical-align:top; }}
    tbody tr:hover {{ background:#f4fafb; }}
    td.lif {{ min-width:250px; max-width:430px; overflow-wrap:anywhere; }}
    td.num {{ text-align:right; font-variant-numeric:tabular-nums; }}
    td.links {{ white-space:nowrap; }}
    td.links a {{ color:var(--accent); margin-right:9px; }}
    .status {{ display:inline-block; padding:2px 6px; border:1px solid; font-weight:600; }}
    .status.ok {{ color:#276749; background:#edf8f1; border-color:#9bc8ad; }}
    .status.review {{ color:#8a4b08; background:#fff7e8; border-color:#dfbd7c; }}
    .status.no_stage {{ color:#6a475f; background:#f8f0f6; border-color:#c9a8bf; }}
    .status.normal {{ color:#4d5a60; background:#f5f7f7; border-color:#c7d0d4; }}
    .status.relocated {{ color:#315e75; background:#eef7fa; border-color:#9fc4d3; }}
    .status.probable_duplicate {{ color:#7b4e0c; background:#fff7e8; border-color:#d9b977; }}
    small {{ color:var(--muted); }}
    .failures {{ margin:12px 24px; color:#8b2f2f; }}
    .reconciliation {{ padding:8px 24px; background:#fafbfb; border-bottom:1px solid var(--line); font-size:12px; }}
    .reconciliation summary {{ cursor:pointer; font-weight:600; }}
    #visible-count {{ align-self:center; color:var(--muted); font-size:13px; white-space:nowrap; }}
    @media (max-width:900px) {{ .controls {{ grid-template-columns:1fr 1fr; }} .overview-grid {{ grid-template-columns:1fr; }} main {{ padding:0 8px 16px; }} header,.overview {{ padding-left:12px; }} }}
  </style>
</head>
<body>
  <header>
    <h1>LIF Metadata Catalog</h1>
    <div class="summary">
      <span>{len(rows)} LIF files</span><span>{total_series} series</span><span>{total_size / 1073741824:.2f} GiB source</span>
      <span>{qc_counts.get('ok', 0)} overlay match</span><span>{qc_counts.get('review', 0)} overlay review</span><span>{qc_counts.get('no_stage', 0)} without stage position</span>
    </div>
    {report_nav}
  </header>
  {failure_html}
  <details class="overview" open>
    <summary>Dataset overview</summary>
    <div class="overview-grid">
      <table>
        <thead><tr><th>Person</th><th>LIF</th><th>Series</th><th>GiB</th><th>Overlay match</th><th>Overlay review</th><th>Unmatched FOV</th><th>Duplicate-like</th></tr></thead>
        <tbody>{person_rows}</tbody>
      </table>
      <div>
        <div class="mode-summary">{mode_summary}</div>
        <p class="note"><strong>Position overlay is calibration-relative.</strong> {calibration_note} Review means that at least one FOV did not overlap a calibrated square-well opening; it does not mean that the LIF is corrupt. Map assignments describe measured geometry and do not infer sample identity or treatment.</p>
      </div>
    </div>
  </details>
  {reconciliation_html}
  <section class="controls">
    <input id="search" type="search" placeholder="Search person, session, LIF, channel, well...">
    <select id="person"><option value="">All people</option>{options}</select>
    <select id="qc"><option value="">All QC states</option><option value="ok">OK</option><option value="review">Review</option><option value="no_stage">No stage</option></select>
    <span id="visible-count"></span>
  </section>
  <main>
    <div class="table-wrap">
      <table>
        <thead><tr><th>Person</th><th>Session</th><th>LIF</th><th>MiB</th><th>Series</th><th>Dimensions</th><th>Channels</th><th>Mapped wells</th><th>Position overlay</th><th>Source</th><th>Open</th></tr></thead>
        <tbody id="catalog-body">{''.join(table_rows)}</tbody>
      </table>
    </div>
  </main>
  <script>
    const rows = [...document.querySelectorAll('#catalog-body tr')];
    const search = document.getElementById('search');
    const person = document.getElementById('person');
    const qc = document.getElementById('qc');
    const count = document.getElementById('visible-count');
    function filterRows() {{
      const query = search.value.trim().toLowerCase();
      let visible = 0;
      for (const row of rows) {{
        const show = (!query || row.dataset.search.includes(query)) && (!person.value || row.dataset.person === person.value) && (!qc.value || row.dataset.qc === qc.value);
        row.hidden = !show;
        if (show) visible += 1;
      }}
      count.textContent = `${{visible}} / ${{rows.length}} files`;
    }}
    search.addEventListener('input', filterRows); person.addEventListener('change', filterRows); qc.addEventListener('change', filterRows); filterRows();
  </script>
</body>
</html>'''
    path.write_text(document, encoding="utf-8")


def _parse_source(value: str) -> tuple[str, Path]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("source must be LABEL=/path/to/directory")
    label, raw_path = value.split("=", 1)
    path = Path(raw_path).expanduser().resolve()
    if not label.strip() or not path.is_dir():
        raise argparse.ArgumentTypeError(f"invalid source: {value}")
    return label.strip(), path


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", action="append", required=True, type=_parse_source, help="LABEL=/directory; repeatable")
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--plate-calibration", type=Path)
    parser.add_argument("--limit", type=int, help="Process only the first N discovered files")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    output_root = args.output_root.expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    calibration = None
    if args.plate_calibration:
        calibration = json.loads(args.plate_calibration.read_text(encoding="utf-8"))
    annotations_path = output_root / "source_annotations.json"
    annotations = json.loads(annotations_path.read_text(encoding="utf-8")) if annotations_path.is_file() else {}

    discovered: list[tuple[str, Path, Path]] = []
    for label, source_root in args.source:
        files = sorted(source_root.rglob("*.[lL][iI][fF]"), key=lambda path: _natural_key(path.as_posix()))
        discovered.extend((label, source_root, path) for path in files)
    if args.limit is not None:
        discovered = discovered[: args.limit]

    candidates: dict[Path, list[tuple[str, Path, Path]]] = defaultdict(list)
    for label, source_root, lif_path in discovered:
        relative = lif_path.relative_to(source_root)
        candidate = Path(sanitize_name(label)) / relative.parent / sanitize_name(lif_path.name)
        candidates[candidate].append((label, source_root, lif_path))

    rows: list[dict[str, object]] = []
    failures: list[dict[str, str]] = []
    for index, (label, source_root, lif_path) in enumerate(discovered, start=1):
        relative = lif_path.relative_to(source_root)
        candidate = Path(sanitize_name(label)) / relative.parent / sanitize_name(lif_path.name)
        if len(candidates[candidate]) > 1:
            digest = hashlib.sha1(relative.as_posix().encode()).hexdigest()[:8]
            candidate = candidate.with_name(f"{candidate.name}__{digest}")
        output_dir = output_root / candidate
        print(f"[{index}/{len(discovered)}] {label}/{relative.as_posix()}", flush=True)
        try:
            if _complete(output_dir):
                print("  reuse complete output", flush=True)
            elif output_dir.exists():
                raise RuntimeError("output directory exists but is incomplete")
            else:
                output_dir.parent.mkdir(parents=True, exist_ok=True)
                partial = output_dir.with_name(f"{output_dir.name}.partial")
                if partial.exists():
                    raise RuntimeError("partial output exists from an interrupted run")
                inspect_lif(lif_path, partial, include_intensity=False, plate_calibration=calibration)
                partial.rename(output_dir)
            rows.append(
                _summarize_output(
                    output_dir,
                    label,
                    relative,
                    lif_path.stat().st_size,
                    output_root,
                    calibration,
                )
            )
        except Exception as exc:  # continue cataloging independent LIF files
            message = str(exc).replace(str(source_root), "<SOURCE>")
            print(f"  ERROR: {message}", file=sys.stderr, flush=True)
            failures.append({"person": label, "source_relative_path": relative.as_posix(), "error": message})

    rows.sort(key=lambda row: (_natural_key(str(row["person"])), _natural_key(str(row["source_relative_path"]))))
    _annotate_sources(rows, annotations)
    _write_catalog_csv(output_root / "catalog.csv", rows)
    _write_index(output_root / "index.html", rows, failures, annotations)
    (output_root / "failures.json").write_text(json.dumps(failures, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"catalog: {output_root / 'index.html'}")
    print(f"complete: {len(rows)}; failed: {len(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
