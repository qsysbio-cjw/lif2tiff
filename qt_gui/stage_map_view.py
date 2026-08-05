#!/usr/bin/env python3
"""Interactive Qt stage map for metadata-only LIF preflight."""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

from PySide6.QtCore import QEvent, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QCheckBox,
    QGraphicsScene,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


PALETTE = (
    "#147d92",
    "#d55e00",
    "#3b7d23",
    "#8b5aa3",
    "#b88a00",
    "#2474b5",
    "#b34b6b",
    "#52706f",
)


def load_stage_calibration(path: Path | None = None) -> dict | None:
    resource_root = (
        Path(sys._MEIPASS)
        if getattr(sys, "frozen", False)
        else Path(__file__).resolve().parent
    )
    candidate = path or resource_root / "cellvis_384_stage_calibration.json"
    try:
        return json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def expand_plate_grid(calibration: dict | None) -> tuple[list[dict], list[dict]]:
    if not calibration:
        return [], []
    plate = calibration.get("plate") or {}
    x_axis = calibration.get("x_axis") or {}
    y_axis = calibration.get("y_axis") or {}

    def expand(axis: dict, count: int, labels: bool) -> list[dict]:
        fit = axis.get("fit") or {}
        first_index = int(axis.get("first_grid_index", 1))
        first_edge = float(fit["first_left_or_top_boundary_um"])
        pitch = float(fit["pitch_um"])
        opening = float(fit["well_opening_um"])
        rows = []
        for index in range(1, count + 1):
            left = first_edge + (index - first_index) * pitch
            rows.append(
                {
                    "index": index,
                    "label": chr(64 + index) if labels else str(index),
                    "start_um": left,
                    "center_um": left + opening / 2,
                    "end_um": left + opening,
                }
            )
        return rows

    try:
        return (
            expand(x_axis, int(plate["columns"]), False),
            expand(y_axis, int(plate["rows"]), True),
        )
    except (KeyError, TypeError, ValueError):
        return [], []


def stage_map_points(plan: dict, source: Path) -> list[dict]:
    points = []
    for series in plan.get("series", []):
        stage = series.get("stage_position") or {}
        try:
            x_um = float(stage["x_m"]) * 1e6
            y_um = float(stage["y_m"]) * 1e6
        except (KeyError, TypeError, ValueError):
            continue
        if not math.isfinite(x_um) or not math.isfinite(y_um):
            continue
        z_m = stage.get("z_m")
        try:
            z_um = float(z_m) * 1e6 if z_m is not None else None
        except (TypeError, ValueError):
            z_um = None
        dimensions = series.get("dimensions") or {}
        pixel_size = series.get("pixel_size") or {}
        try:
            width_um = float(dimensions["x"]) * float(pixel_size["x_um_per_px"])
            height_um = float(dimensions["y"]) * float(pixel_size["y_um_per_px"])
        except (KeyError, TypeError, ValueError):
            width_um = height_um = 0.0
        points.append(
            {
                "source": source.name,
                "series_index": int(series["series_index"]),
                "series_name": str(series["series_name"]),
                "x_um": x_um,
                "y_um": y_um,
                "z_um": z_um,
                "width_um": width_um,
                "height_um": height_um,
            }
        )
    return points


def _cosmetic_pen(color: str, width: float = 1.0) -> QPen:
    pen = QPen(QColor(color), width)
    pen.setCosmetic(True)
    return pen


class StageGraphicsView(QGraphicsView):
    def __init__(self) -> None:
        super().__init__()
        self.setScene(QGraphicsScene(self))
        self.setBackgroundBrush(QColor("#f4f6f7"))
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        self.acquisition_bounds: QRectF | None = None
        self.plate_bounds: QRectF | None = None

    def _apply_zoom_factor(self, factor: float) -> None:
        current = self.transform().m11()
        target = min(max(current * factor, 0.0005), 2.0)
        applied = target / max(current, 1e-12)
        if applied != 1.0:
            self.scale(applied, applied)

    def wheelEvent(self, event) -> None:  # noqa: N802 - Qt API
        delta = event.pixelDelta().y() or event.angleDelta().y()
        if not delta:
            super().wheelEvent(event)
            return
        factor = math.exp(float(delta) * (0.004 if event.pixelDelta().y() else 0.0012))
        if event.inverted():
            factor = 1.0 / factor
        self._apply_zoom_factor(factor)
        event.accept()

    def event(self, event) -> bool:
        if event.type() == QEvent.Type.NativeGesture:
            if event.gestureType() == Qt.NativeGestureType.ZoomNativeGesture:
                self._apply_zoom_factor(max(0.01, 1.0 + float(event.value())))
                event.accept()
                return True
        return super().event(event)

    def fit_acquisition(self) -> None:
        if self.acquisition_bounds and not self.acquisition_bounds.isEmpty():
            self.fitInView(self.acquisition_bounds, Qt.AspectRatioMode.KeepAspectRatio)

    def fit_plate(self) -> None:
        if self.plate_bounds and not self.plate_bounds.isEmpty():
            self.fitInView(self.plate_bounds, Qt.AspectRatioMode.KeepAspectRatio)


class StageMapPage(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.datasets: list[dict] = []
        self.calibration: dict | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        toolbar = QHBoxLayout()
        self.summary = QLabel("Select one or more queued LIF files.")
        self.summary.setWordWrap(True)
        toolbar.addWidget(self.summary, 1)
        fit_acquisition = QPushButton("Fit acquisition")
        fit_acquisition.clicked.connect(lambda: self.view.fit_acquisition())
        toolbar.addWidget(fit_acquisition)
        fit_plate = QPushButton("Fit 384-well plate")
        fit_plate.clicked.connect(lambda: self.view.fit_plate())
        toolbar.addWidget(fit_plate)
        self.plate_overlay = QCheckBox("384-well overlay")
        self.plate_overlay.setChecked(True)
        self.plate_overlay.setToolTip(
            "Use the confirmed Cellvis/Leica calibration; disable for raw stage coordinates"
        )
        self.plate_overlay.toggled.connect(self._render_current)
        toolbar.addWidget(self.plate_overlay)
        layout.addLayout(toolbar)
        self.legend = QLabel("")
        self.legend.setTextFormat(Qt.TextFormat.RichText)
        self.legend.setWordWrap(True)
        layout.addWidget(self.legend)
        self.view = StageGraphicsView()
        layout.addWidget(self.view, 1)

    def show_loading(self, count: int) -> None:
        self.summary.setText(f"Reading stage metadata for {count} selected LIF file(s)...")

    def clear(self) -> None:
        self.datasets = []
        self.calibration = None
        self.view.scene().clear()
        self.view.acquisition_bounds = None
        self.view.plate_bounds = None
        self.summary.setText("Select one or more queued LIF files.")
        self.legend.clear()

    def set_datasets(self, datasets: list[dict], calibration: dict | None) -> None:
        self.datasets = datasets
        self.calibration = calibration
        self._render_current()

    def _render_current(self) -> None:
        self._draw_datasets(
            self.datasets,
            self.calibration if self.plate_overlay.isChecked() else None,
        )

    def _draw_datasets(self, datasets: list[dict], calibration: dict | None) -> None:
        scene = self.view.scene()
        scene.clear()
        x_grid, y_grid = expand_plate_grid(calibration)
        all_points = [point for dataset in datasets for point in dataset["points"]]
        if not all_points:
            self.summary.setText(
                f"{len(datasets)} selected LIF file(s); no usable X/Y stage positions found."
            )
            self.legend.clear()
            return

        plate_rect = None
        if x_grid and y_grid:
            offsets = (calibration or {}).get("plate_edge_offset_um") or {}
            left = x_grid[0]["center_um"] - float(offsets.get("x", 12130.0))
            right = x_grid[-1]["center_um"] + float(offsets.get("x", 12130.0))
            top = y_grid[0]["center_um"] - float(offsets.get("y", 8990.0))
            bottom = y_grid[-1]["center_um"] + float(offsets.get("y", 8990.0))
            plate_rect = QRectF(left, top, right - left, bottom - top)
            scene.addRect(
                plate_rect,
                _cosmetic_pen("#76858c", 1.5),
                QBrush(QColor("#dce2e5")),
            )
            for row in y_grid:
                for column in x_grid:
                    well = QRectF(
                        column["start_um"],
                        row["start_um"],
                        column["end_um"] - column["start_um"],
                        row["end_um"] - row["start_um"],
                    )
                    item = scene.addRect(
                        well,
                        _cosmetic_pen("#a7b2b7"),
                        QBrush(QColor("#ffffff")),
                    )
                    item.setToolTip(f"Well {row['label']}{column['label']}")
            font = QFont()
            font.setPixelSize(850)
            for column in x_grid:
                label = scene.addText(column["label"], font)
                label.setDefaultTextColor(QColor("#46545a"))
                label.setPos(column["center_um"] - 300, y_grid[0]["start_um"] - 1550)
            for row in y_grid:
                label = scene.addText(row["label"], font)
                label.setDefaultTextColor(QColor("#46545a"))
                label.setPos(x_grid[0]["start_um"] - 1500, row["center_um"] - 500)

        acquisition_rect: QRectF | None = None
        legend_parts = []
        for dataset_index, dataset in enumerate(datasets):
            color = QColor(PALETTE[dataset_index % len(PALETTE)])
            fill = QColor(color)
            fill.setAlpha(72)
            pen = QPen(color, 2.0)
            pen.setCosmetic(True)
            legend_parts.append(
                f'<span style="color:{color.name()}; font-weight:600">&#9632;</span> '
                f'{dataset["source"]}'
            )
            for point in dataset["points"]:
                width = max(float(point["width_um"]), 80.0)
                height = max(float(point["height_um"]), 80.0)
                rect = QRectF(
                    point["x_um"] - width / 2,
                    point["y_um"] - height / 2,
                    width,
                    height,
                )
                acquisition_rect = rect if acquisition_rect is None else acquisition_rect.united(rect)
                z_text = "unavailable" if point["z_um"] is None else f'{point["z_um"]:.2f} um'
                tooltip = (
                    f'{point["source"]}\nSeries {point["series_index"] + 1}: '
                    f'{point["series_name"]}\nX {point["x_um"]:.2f} um | '
                    f'Y {point["y_um"]:.2f} um | Z {z_text}\n'
                    f'FOV {point["width_um"]:.2f} x {point["height_um"]:.2f} um'
                )
                item = scene.addRect(rect, pen, QBrush(fill))
                item.setToolTip(tooltip)
                center = scene.addEllipse(
                    point["x_um"] - 45,
                    point["y_um"] - 45,
                    90,
                    90,
                    _cosmetic_pen("#ffffff", 1.0),
                    QBrush(color),
                )
                center.setToolTip(tooltip)
                number_font = QFont()
                number_font.setPixelSize(280)
                number = scene.addText(str(point["series_index"] + 1), number_font)
                number.setDefaultTextColor(color.darker(135))
                number.setPos(point["x_um"] + width / 2 + 55, point["y_um"] - 170)
                number.setToolTip(tooltip)

        margin = max(
            500.0,
            max(max(point["width_um"], point["height_um"]) for point in all_points),
        )
        assert acquisition_rect is not None
        acquisition_rect = acquisition_rect.adjusted(-margin, -margin, margin, margin)
        self.view.acquisition_bounds = acquisition_rect
        self.view.plate_bounds = plate_rect
        scene_bounds = acquisition_rect if plate_rect is None else acquisition_rect.united(plate_rect)
        scene.setSceneRect(scene_bounds)
        positioned = len(all_points)
        calibration_text = (
            "confirmed Cellvis 384-well calibration"
            if plate_rect is not None
            else "raw stage coordinates only"
        )
        self.summary.setText(
            f"{len(datasets)} selected LIF file(s) | {positioned} positioned series | "
            f"{calibration_text}"
        )
        self.legend.setText(" &nbsp;&nbsp; ".join(legend_parts))
        self.view.fit_acquisition()
