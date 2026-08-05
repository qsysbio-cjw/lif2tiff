#!/usr/bin/env python3
"""Generate a self-contained interactive physical stage/plate map."""

from __future__ import annotations

import json
import math
import statistics
from pathlib import Path


# ANSI/SLAS 384-well geometry: distance from an outer-well center to the
# corresponding physical plate edge. This preserves the substantial plate rim.
PLATE_EDGE_OFFSET_X_UM = 12_130.0
PLATE_EDGE_OFFSET_Y_UM = 8_990.0


def _number(value):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _annotate_well_overlap(
    points: list[dict],
    fov_w: float,
    fov_h: float,
    x_grid: list[dict],
    y_grid: list[dict],
) -> None:
    if not fov_w or not fov_h or not x_grid or not y_grid:
        return
    fov_area = fov_w * fov_h
    for point in points:
        fov_left = point["x_um"] - fov_w / 2
        fov_right = point["x_um"] + fov_w / 2
        fov_top = point["y_um"] - fov_h / 2
        fov_bottom = point["y_um"] + fov_h / 2
        best = None
        for column in x_grid:
            overlap_x = max(
                0.0,
                min(fov_right, column["right_boundary_um"])
                - max(fov_left, column["left_boundary_um"]),
            )
            if not overlap_x:
                continue
            for row in y_grid:
                overlap_y = max(
                    0.0,
                    min(fov_bottom, row["bottom_boundary_um"])
                    - max(fov_top, row["top_boundary_um"]),
                )
                area = overlap_x * overlap_y
                if area > 0 and (best is None or area > best[0]):
                    best = (area, f"{row['label']}{column['column']}")
        if best:
            point["best_well"] = best[1]
            point["inside_well_fraction"] = round(best[0] / fov_area, 4)


def _plate_geometry(x_grid: list[dict], y_grid: list[dict]) -> dict | None:
    if not x_grid or not y_grid:
        return None
    return {
        "minX": x_grid[0]["center_um"] - PLATE_EDGE_OFFSET_X_UM,
        "maxX": x_grid[-1]["center_um"] + PLATE_EDGE_OFFSET_X_UM,
        "minY": y_grid[0]["center_um"] - PLATE_EDGE_OFFSET_Y_UM,
        "maxY": y_grid[-1]["center_um"] + PLATE_EDGE_OFFSET_Y_UM,
    }


def write_stage_map(
    path: Path,
    stage_rows: list[dict],
    series_rows: list[dict] | None = None,
    calibration: dict | None = None,
) -> None:
    series_by_index = {str(row.get("series_index")): row for row in (series_rows or [])}
    points = []
    for row in stage_rows:
        x = _number(row.get("stage_x_um"))
        y = _number(row.get("stage_y_um"))
        z = _number(row.get("stage_z_um"))
        if x is None or y is None:
            continue
        series = series_by_index.get(str(row.get("series_index")), {})
        points.append(
            {
                "series_index": int(row["series_index"]),
                "series_name": row.get("series_name"),
                "timestamp": row.get("timestamp_first_utc"),
                "x_um": x,
                "y_um": y,
                "z_um": z,
                "x_px": _number(series.get("x_px")),
                "y_px": _number(series.get("y_px")),
                "pixel_size_x_um": _number(series.get("pixel_size_x_um")),
                "pixel_size_y_um": _number(series.get("pixel_size_y_um")),
            }
        )
    if not points:
        path.write_text("<html><body><p>No stage positions found.</p></body></html>", encoding="utf-8")
        return

    fov_widths = [
        point["x_px"] * point["pixel_size_x_um"]
        for point in points
        if point["x_px"] and point["pixel_size_x_um"]
    ]
    fov_heights = [
        point["y_px"] * point["pixel_size_y_um"]
        for point in points
        if point["y_px"] and point["pixel_size_y_um"]
    ]
    fov_w = statistics.median(fov_widths) if fov_widths else 0.0
    fov_h = statistics.median(fov_heights) if fov_heights else 0.0

    x_grid = (calibration or {}).get("full_x_grid") or []
    full_y_grid = (calibration or {}).get("full_y_grid") or []
    if full_y_grid:
        y_grid = [
            {
                "label": row["label"],
                "row": row["row"],
                "top_boundary_um": row["top_boundary_um"],
                "center_um": row["center_um"],
                "bottom_boundary_um": row["bottom_boundary_um"],
                "absolute": True,
            }
            for row in full_y_grid
        ]
    else:
        y_grid = [
            {
                "label": row["label"],
                "top_boundary_um": row["left_or_top"]["fitted_um"],
                "center_um": (
                    row["left_or_top"]["fitted_um"]
                    + row["right_or_bottom"]["fitted_um"]
                )
                / 2,
                "bottom_boundary_um": row["right_or_bottom"]["fitted_um"],
                "absolute": False,
            }
            for row in ((calibration or {}).get("y_axis") or {}).get("measured_boundaries", [])
        ]
    _annotate_well_overlap(points, fov_w, fov_h, x_grid, y_grid)
    x_values = [point["x_um"] for point in points]
    y_values = [point["y_um"] for point in points]
    for column in x_grid:
        x_values.extend([column["left_boundary_um"], column["right_boundary_um"]])
    for row in y_grid:
        y_values.extend([row["top_boundary_um"], row["bottom_boundary_um"]])

    acquisition_x = [point["x_um"] for point in points]
    acquisition_y = [point["y_um"] for point in points]
    acquisition_bounds = {
        "minX": min(acquisition_x) - max(fov_w, 3000.0),
        "maxX": max(acquisition_x) + max(fov_w, 3000.0),
        "minY": min(acquisition_y) - max(fov_h, 300.0),
        "maxY": max(acquisition_y) + max(fov_h, 300.0),
    }
    plate_geometry = _plate_geometry(x_grid, y_grid)
    plate_bounds = plate_geometry or {
        "minX": min(x_values) - max(fov_w, 1000.0),
        "maxX": max(x_values) + max(fov_w, 1000.0),
        "minY": min(y_values) - max(fov_h, 1000.0),
        "maxY": max(y_values) + max(fov_h, 1000.0),
    }
    z_values = [point["z_um"] for point in points if point["z_um"] is not None]
    z_span = max(z_values) - min(z_values) if z_values else 0.0
    dimension_types = sorted(
        {str(row.get("dimension_type")) for row in (series_rows or []) if row.get("dimension_type")}
    )
    acquisition_mode = "/".join(dimension_types) if dimension_types else "unknown dimensions"
    z_note = "stage Z constant/absent" if not z_values or z_span < 1e-6 else "stage Z varies"
    mode = f"{acquisition_mode}; {z_note}"
    calibration_label = "loaded" if calibration else "none"
    x_fit = ((calibration or {}).get("x_axis") or {}).get("fit") or {}
    y_fit = ((calibration or {}).get("y_axis") or {}).get("fit") or {}
    reference_well = ((calibration or {}).get("reference_well") or {}).get("well", "NA")
    y_grid_label = "absolute A-P" if full_y_grid else "relative"
    y_description = (
        "White square openings are calibrated wells A1-P24; soft gray gutters represent divider material."
        if full_y_grid
        else "White openings use calibrated columns and relative Y positions; soft gray gutters represent divider material."
    )

    html = '''<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Stage Position and Plate Map</title>
  <style>
    html, body { width: 100%; height: 100%; margin: 0; }
    body { font-family: Arial, sans-serif; color: #222; display: flex; flex-direction: column; overflow: hidden; }
    .topbar { flex: 0 0 auto; padding: 14px 18px 9px; border-bottom: 1px solid #d5dadd; background: #fff; }
    h1 { font-size: 20px; margin: 0 0 9px; }
    .bar { display: flex; gap: 7px; flex-wrap: wrap; align-items: center; margin-bottom: 7px; }
    .pill { border: 1px solid #ccc; border-radius: 4px; padding: 4px 8px; background: #f7f7f7; font-size: 13px; }
    button { padding: 5px 9px; }
    .map-shell { position: relative; flex: 1 1 auto; min-height: 0; background: #f6f7f7; }
    svg { display: block; width: 100%; height: 100%; background: #f6f7f7; cursor: grab; touch-action: none; }
    svg:active { cursor: grabbing; }
    .plate-base { fill: #e3e7e9; stroke: #87959b; stroke-width: 2; }
    .well { fill: #ffffff; stroke: #aab5ba; stroke-width: 1; }
    .well.reference { fill: #f3faf8; stroke: #408475; stroke-width: 1.8; }
    .grid-label { font-size: 11px; fill: #46545a; font-weight: 600; }
    .row-label { font-size: 11px; fill: #46545a; font-weight: 600; }
    .fov { fill: rgba(24, 126, 164, 0.22); stroke: #12698d; stroke-width: 1.5; }
    .point { fill: #0e5877; stroke: white; stroke-width: 1; }
    .point-label { font-size: 11px; fill: #111; font-weight: 600; }
    .scale { stroke: #333; stroke-width: 3; }
    .small { color: #555; font-size: 12px; line-height: 1.4; }
    .description { margin: 0; }
    #status { position: absolute; right: 12px; bottom: 8px; margin: 0; padding: 3px 6px; background: rgba(255,255,255,0.88); border: 1px solid #d5dadd; }
    .legend { display: inline-flex; align-items: center; gap: 5px; font-size: 12px; color: #4b555a; }
    .swatch { width: 14px; height: 14px; border: 1px solid #9ba8ae; display: inline-block; }
    .swatch.well-swatch { background: white; }
    .swatch.divider-swatch { background: #e3e7e9; }
    .swatch.fov-swatch { background: rgba(24, 126, 164, 0.22); border-color: #12698d; }
  </style>
</head>
<body>
  <header class="topbar">
  <h1>Stage Position and Plate Map</h1>
  <div class="bar">
    <span class="pill">FOVs: __SERIES_COUNT__</span>
    <span class="pill">wells: __WELL_COUNT__</span>
    <span class="pill">FOV: __FOV_W__ um square</span>
    <span class="pill">pitch X/Y: __X_PITCH_MM__ / __Y_PITCH_MM__ mm</span>
    <span class="pill">anchor: __REFERENCE_WELL__</span>
    <span class="pill">__MODE__</span>
    <button id="fit-acquisition">Fit acquisition</button>
    <button id="fit-plate">Fit plate</button>
    <button id="reset">Reset pan/zoom</button>
    <span class="legend"><i class="swatch well-swatch"></i>well</span>
    <span class="legend"><i class="swatch divider-swatch"></i>divider</span>
    <span class="legend"><i class="swatch fov-swatch"></i>FOV</span>
  </div>
  <p class="small description">Coordinates and FOV rectangles use physical micrometers from the LIF metadata. __Y_DESCRIPTION__ Two-finger scroll pans; pinch or Ctrl+scroll zooms.</p>
  </header>
  <main class="map-shell">
    <svg id="svg"><g id="viewport"></g></svg>
    <p id="status" class="small"></p>
  </main>
  <script>
    const points = __POINTS__;
    const xGrid = __X_GRID__;
    const yGrid = __Y_GRID__;
    const acquisitionBounds = __ACQUISITION_BOUNDS__;
    const plateBounds = __PLATE_BOUNDS__;
    const plateGeometry = __PLATE_GEOMETRY__;
    const fovW = __FOV_W_RAW__;
    const fovH = __FOV_H_RAW__;
    const zSpan = __Z_SPAN__;
    const zoomSensitivity = 0.005;
    const svg = document.getElementById("svg");
    const viewport = document.getElementById("viewport");
    const status = document.getElementById("status");
    let bounds = xGrid.length && yGrid.length ? plateBounds : acquisitionBounds;
    let scale = 1, panX = 0, panY = 0, dragging = false, lastX = 0, lastY = 0;
    function canvasWidth() { return Math.max(svg.clientWidth, 320); }
    function canvasHeight() { return Math.max(svg.clientHeight, 320); }
    function baseScale() {
      return Math.min((canvasWidth() - 100) / (bounds.maxX - bounds.minX), (canvasHeight() - 100) / (bounds.maxY - bounds.minY));
    }
    function offsetX() { return (canvasWidth() - (bounds.maxX - bounds.minX) * baseScale() * scale) / 2; }
    function offsetY() { return (canvasHeight() - (bounds.maxY - bounds.minY) * baseScale() * scale) / 2; }
    function px(x) { return offsetX() + (x - bounds.minX) * baseScale() * scale + panX; }
    function py(y) { return offsetY() + (y - bounds.minY) * baseScale() * scale + panY; }
    function worldX(screenX) { return bounds.minX + (screenX - offsetX() - panX) / (baseScale() * scale); }
    function worldY(screenY) { return bounds.minY + (screenY - offsetY() - panY) / (baseScale() * scale); }
    function esc(s) { return String(s ?? "").replace(/[&<>]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c])); }
    function formatUm(value) { return value >= 1000 ? (value / 1000).toFixed(value % 1000 === 0 ? 0 : 1) + " mm" : Math.round(value) + " um"; }
    function scaleBar() {
      const rawUm = 180 / (baseScale() * scale);
      const power = Math.pow(10, Math.floor(Math.log10(rawUm)));
      const candidates = [1, 2, 5, 10].map(value => value * power);
      const valueUm = candidates.reduce((best, value) => Math.abs(value - rawUm) < Math.abs(best - rawUm) ? value : best);
      return {valueUm, widthPx: valueUm * baseScale() * scale};
    }
    function render() {
      const parts = [];
      const showPointLabels = bounds !== plateBounds || scale > 2;
      const viewMinX = worldX(0), viewMaxX = worldX(canvasWidth());
      const viewMinY = worldY(0), viewMaxY = worldY(canvasHeight());
      const visibleColumns = xGrid.filter(column => column.right_boundary_um >= viewMinX && column.left_boundary_um <= viewMaxX);
      const visibleRows = yGrid.filter(row => row.bottom_boundary_um >= viewMinY && row.top_boundary_um <= viewMaxY);
      if (plateGeometry) {
        const plateLeft = plateGeometry.minX;
        const plateRight = plateGeometry.maxX;
        const plateTop = plateGeometry.minY;
        const plateBottom = plateGeometry.maxY;
        parts.push(`<rect class="plate-base" x="${px(plateLeft)}" y="${py(plateTop)}" width="${(plateRight - plateLeft) * baseScale() * scale}" height="${(plateBottom - plateTop) * baseScale() * scale}"><title>Plate divider material</title></rect>`);
      }
      for (const row of visibleRows) {
        for (const column of visibleColumns) {
          const referenceClass = row.label === "E" && column.column === 13 ? " reference" : "";
          const wellName = `${row.label}${column.column}`;
          parts.push(`<rect class="well${referenceClass}" x="${px(column.left_boundary_um)}" y="${py(row.top_boundary_um)}" width="${(column.right_boundary_um - column.left_boundary_um) * baseScale() * scale}" height="${(row.bottom_boundary_um - row.top_boundary_um) * baseScale() * scale}"><title>${wellName}${referenceClass ? "; calibration reference" : ""}</title></rect>`);
        }
      }
      if (visibleRows.length) {
        const labelY = py(visibleRows[0].top_boundary_um) - 8;
        for (const column of visibleColumns) {
          parts.push(`<text class="grid-label" x="${px(column.center_um)}" y="${labelY}" text-anchor="middle">C${column.column}</text>`);
        }
      }
      if (visibleColumns.length) {
        const labelX = Math.max(16, px(visibleColumns[0].left_boundary_um) - 8);
        for (const row of visibleRows) {
          parts.push(`<text class="row-label" x="${labelX}" y="${py(row.center_um) + 4}" text-anchor="end">${esc(row.label)}</text>`);
        }
      }
      for (const point of points) {
        const x = px(point.x_um - fovW / 2), y = py(point.y_um - fovH / 2);
        const width = fovW * baseScale() * scale, height = fovH * baseScale() * scale;
        const cx = px(point.x_um), cy = py(point.y_um);
        const overlap = point.best_well ? ` | ${point.best_well}: ${(point.inside_well_fraction * 100).toFixed(1)}% of FOV inside well` : "";
        const title = `${point.series_name} | ${point.timestamp} | x=${point.x_um.toFixed(1)} um, y=${point.y_um.toFixed(1)} um, z=${(point.z_um ?? 0).toFixed(4)} um${overlap}`;
        parts.push(`<rect class="fov" x="${x}" y="${y}" width="${width}" height="${height}"><title>${esc(title)}</title></rect>`);
        parts.push(`<circle class="point" cx="${cx}" cy="${cy}" r="4"><title>${esc(title)}</title></circle>`);
        if (showPointLabels) parts.push(`<text class="point-label" x="${cx + 7}" y="${cy + 4}">${point.series_index + 1}</text>`);
      }
      const bar = scaleBar();
      const scaleY = canvasHeight() - 42;
      parts.push(`<line class="scale" x1="32" y1="${scaleY}" x2="${32 + bar.widthPx}" y2="${scaleY}"/>`);
      parts.push(`<line class="scale" x1="32" y1="${scaleY - 7}" x2="32" y2="${scaleY + 7}"/>`);
      parts.push(`<line class="scale" x1="${32 + bar.widthPx}" y1="${scaleY - 7}" x2="${32 + bar.widthPx}" y2="${scaleY + 7}"/>`);
      parts.push(`<text x="32" y="${scaleY + 25}" font-size="12">scale bar ${formatUm(bar.valueUm)}</text>`);
      viewport.innerHTML = parts.join("");
      status.textContent = `zoom ${scale.toFixed(2)}; pan(${panX.toFixed(0)}, ${panY.toFixed(0)}); Z span ${zSpan.toFixed(6)} um`;
    }
    svg.addEventListener("pointerdown", event => {
      dragging = true; lastX = event.clientX; lastY = event.clientY;
      svg.setPointerCapture(event.pointerId);
    });
    svg.addEventListener("pointerup", event => { dragging = false; svg.releasePointerCapture(event.pointerId); });
    svg.addEventListener("pointercancel", () => dragging = false);
    svg.addEventListener("pointermove", event => {
      if (!dragging) return;
      panX += event.clientX - lastX; panY += event.clientY - lastY;
      lastX = event.clientX; lastY = event.clientY; render();
    });
    svg.addEventListener("wheel", event => {
      event.preventDefault();
      const unit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? canvasHeight() : 1;
      const deltaX = event.deltaX * unit, deltaY = event.deltaY * unit;
      if (event.ctrlKey) {
        const rect = svg.getBoundingClientRect();
        const cursorX = event.clientX - rect.left, cursorY = event.clientY - rect.top;
        const focusX = worldX(cursorX), focusY = worldY(cursorY);
        scale = Math.max(0.2, Math.min(30, scale * Math.exp(-deltaY * zoomSensitivity)));
        panX = cursorX - offsetX() - (focusX - bounds.minX) * baseScale() * scale;
        panY = cursorY - offsetY() - (focusY - bounds.minY) * baseScale() * scale;
      } else {
        panX -= deltaX;
        panY -= deltaY;
      }
      render();
    }, {passive:false});
    function setBounds(nextBounds) { bounds = nextBounds; scale = 1; panX = 0; panY = 0; render(); }
    document.getElementById("fit-acquisition").addEventListener("click", () => setBounds(acquisitionBounds));
    document.getElementById("fit-plate").addEventListener("click", () => setBounds(plateBounds));
    document.getElementById("reset").addEventListener("click", () => { scale = 1; panX = 0; panY = 0; render(); });
    window.addEventListener("resize", render);
    render();
  </script>
</body>
</html>
'''
    replacements = {
        "__SERIES_COUNT__": str(len(points)),
        "__WELL_COUNT__": str(len(x_grid) * len(y_grid)),
        "__CALIBRATION_LABEL__": calibration_label,
        "__FOV_W__": f"{fov_w:.2f}",
        "__FOV_H__": f"{fov_h:.2f}",
        "__X_PITCH__": str(x_fit.get("pitch_um", "NA")),
        "__Y_PITCH__": str(y_fit.get("pitch_um", "NA")),
        "__X_PITCH_MM__": (
            f"{float(x_fit['pitch_um']) / 1000:.3f}" if x_fit.get("pitch_um") is not None else "NA"
        ),
        "__Y_PITCH_MM__": (
            f"{float(y_fit['pitch_um']) / 1000:.3f}" if y_fit.get("pitch_um") is not None else "NA"
        ),
        "__Y_GRID_LABEL__": y_grid_label,
        "__REFERENCE_WELL__": reference_well,
        "__MODE__": mode,
        "__Y_DESCRIPTION__": y_description,
        "__POINTS__": json.dumps(points, ensure_ascii=False),
        "__X_GRID__": json.dumps(x_grid, ensure_ascii=False),
        "__Y_GRID__": json.dumps(y_grid, ensure_ascii=False),
        "__ACQUISITION_BOUNDS__": json.dumps(acquisition_bounds),
        "__PLATE_BOUNDS__": json.dumps(plate_bounds),
        "__PLATE_GEOMETRY__": json.dumps(plate_geometry),
        "__FOV_W_RAW__": str(fov_w),
        "__FOV_H_RAW__": str(fov_h),
        "__Z_SPAN__": str(z_span),
    }
    for marker, value in replacements.items():
        html = html.replace(marker, value)
    path.write_text(html, encoding="utf-8")
