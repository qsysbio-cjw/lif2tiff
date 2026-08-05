#!/usr/bin/env python3
"""Isolated PySide6 benchmark for multidimensional Leica LIF viewing."""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

import numpy as np
from PySide6.QtCore import QEvent, QSignalBlocker, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QGraphicsView,
    QGroupBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)


PROJECT_ROOT = (
    Path(sys._MEIPASS)
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parents[1]
)
SOURCE_DIR = PROJECT_ROOT / "src"
sys.path.insert(0, str(SOURCE_DIR))

from liffile_adapter import LifFileAdapter  # noqa: E402
from liftool import build_conversion_plan  # noqa: E402


COLORS = {
    "window": "#edf0f2",
    "panel": "#ffffff",
    "preview": "#121619",
    "border": "#c7ced4",
    "text": "#20282f",
    "muted": "#65717b",
    "accent": "#176b5b",
}


def clamp(value: float, lower: float, upper: float) -> float:
    return min(max(float(value), lower), upper)


def lut_color(label: str | None, index: int) -> np.ndarray:
    text = (label or "").lower()
    options = (
        ("green", (0.10, 1.00, 0.30)),
        ("red", (1.00, 0.12, 0.10)),
        ("blue", (0.15, 0.45, 1.00)),
        ("cyan", (0.10, 0.90, 1.00)),
        ("magenta", (1.00, 0.15, 0.90)),
        ("yellow", (1.00, 0.90, 0.10)),
    )
    for token, color in options:
        if token in text:
            return np.asarray(color, dtype=np.float32)
    return np.asarray(options[index % len(options)][1], dtype=np.float32)


def normalize(frame: np.ndarray, black: float, white: float, gamma: float) -> np.ndarray:
    if white <= black:
        white = black + 1.0
    values = (frame.astype(np.float32) - black) / (white - black)
    values = np.clip(values, 0.0, 1.0)
    gamma = max(float(gamma), 1e-6)
    if gamma != 1.0:
        values = np.power(values, 1.0 / gamma)
    return values


def format_elapsed(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    hours = int(seconds // 3600)
    minutes = int(seconds % 3600 // 60)
    remainder = seconds % 60
    if hours:
        return f"{hours:d}:{minutes:02d}:{remainder:05.2f}"
    return f"{minutes:02d}:{remainder:05.2f}"


def recommended_playback_fps(frame_count: int, target_seconds: float = 25.0) -> int:
    """Choose an integer playback rate targeting a short whole-series preview."""
    estimate = int(math.floor(max(1, frame_count) / target_seconds + 0.5))
    return int(clamp(max(1, estimate), 1, 120))


class RangeSlider(QWidget):
    """Compact two-handle floating-point range control."""

    values_changed = Signal(float, float)

    def __init__(self) -> None:
        super().__init__()
        self.minimum = 0.0
        self.maximum = 255.0
        self.low = 0.0
        self.high = 255.0
        self.active = "low"
        self.padding = 10.0
        self.setMinimumHeight(34)

    def set_range(self, minimum: float, maximum: float, low: float, high: float) -> None:
        self.minimum = float(minimum)
        self.maximum = max(float(maximum), self.minimum + 1.0)
        self.set_values(low, high)

    def set_values(self, low: float, high: float, *, emit: bool = False) -> None:
        self.low = clamp(low, self.minimum, self.maximum)
        self.high = clamp(high, self.minimum, self.maximum)
        if self.low > self.high:
            self.low, self.high = self.high, self.low
        self.update()
        if emit:
            self.values_changed.emit(self.low, self.high)

    def _value_to_x(self, value: float) -> float:
        width = max(1.0, self.width() - 2 * self.padding)
        return self.padding + (value - self.minimum) / (self.maximum - self.minimum) * width

    def _x_to_value(self, x: float) -> float:
        width = max(1.0, self.width() - 2 * self.padding)
        fraction = clamp((x - self.padding) / width, 0.0, 1.0)
        return self.minimum + fraction * (self.maximum - self.minimum)

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt API
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        middle = self.height() / 2
        low_x, high_x = self._value_to_x(self.low), self._value_to_x(self.high)
        painter.setPen(
            QPen(QColor("#d4dbe0"), 6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        )
        painter.drawLine(int(self.padding), int(middle), int(self.width() - self.padding), int(middle))
        painter.setPen(
            QPen(QColor(COLORS["accent"]), 6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        )
        painter.drawLine(int(low_x), int(middle), int(high_x), int(middle))
        painter.setPen(QPen(QColor(COLORS["accent"]), 2))
        painter.setBrush(QColor("#ffffff"))
        for x in (low_x, high_x):
            painter.drawEllipse(int(x - 6), int(middle - 10), 12, 20)

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt API
        x = event.position().x()
        self.active = (
            "low"
            if abs(x - self._value_to_x(self.low)) <= abs(x - self._value_to_x(self.high))
            else "high"
        )
        self._move_active(x)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt API
        if event.buttons() & Qt.MouseButton.LeftButton:
            self._move_active(event.position().x())

    def _move_active(self, x: float) -> None:
        value = self._x_to_value(x)
        if self.active == "low":
            self.low = min(value, self.high)
        else:
            self.high = max(value, self.low)
        self.update()
        self.values_changed.emit(self.low, self.high)


class ImageView(QGraphicsView):
    zoom_changed = Signal(float)
    input_event = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.setScene(QGraphicsScene(self))
        self.item = QGraphicsPixmapItem()
        self.scene().addItem(self.item)
        self.setBackgroundBrush(QColor(COLORS["preview"]))
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.MinimalViewportUpdate)
        self.relative_zoom = 1.0
        self.invert_zoom = False
        self.event_count = 0
        self.has_image = False

    def set_image(self, image: QImage) -> None:
        first_image = not self.has_image
        self.item.setPixmap(QPixmap.fromImage(image))
        self.scene().setSceneRect(self.item.boundingRect())
        self.has_image = True
        if first_image:
            self.fit_image()

    def clear_image(self) -> None:
        self.item.setPixmap(QPixmap())
        self.scene().setSceneRect(0, 0, 1, 1)
        self.has_image = False
        self.relative_zoom = 1.0
        self.zoom_changed.emit(100.0)

    def fit_image(self) -> None:
        if not self.has_image:
            return
        self.resetTransform()
        self.fitInView(self.item, Qt.AspectRatioMode.KeepAspectRatio)
        self.relative_zoom = 1.0
        self.zoom_changed.emit(100.0)

    def set_zoom_percent(self, percent: float) -> None:
        if not self.has_image:
            return
        target = clamp(percent / 100.0, 0.05, 100.0)
        factor = target / max(self.relative_zoom, 1e-9)
        self.scale(factor, factor)
        self.relative_zoom = target
        self.zoom_changed.emit(target * 100.0)

    def _apply_zoom_factor(self, factor: float) -> None:
        target = clamp(self.relative_zoom * factor, 0.05, 100.0)
        factor = target / max(self.relative_zoom, 1e-9)
        if factor == 1.0:
            return
        self.scale(factor, factor)
        self.relative_zoom = target
        self.zoom_changed.emit(target * 100.0)

    def wheelEvent(self, event) -> None:  # noqa: N802 - Qt API
        pixel_delta = event.pixelDelta().y()
        angle_delta = event.angleDelta().y()
        if pixel_delta:
            delta = float(pixel_delta)
            source = f"pixelDelta={pixel_delta}"
            factor = math.exp(delta * 0.004)
        elif angle_delta:
            delta = float(angle_delta)
            source = f"angleDelta={angle_delta}"
            factor = math.exp(delta * math.log(1.12) / 120.0)
        else:
            event.ignore()
            return
        if event.inverted():
            factor = 1.0 / factor
            source += " inverted"
        if self.invert_zoom:
            factor = 1.0 / factor
            source += " app-inverted"
        self._apply_zoom_factor(factor)
        self.event_count += 1
        self.input_event.emit(f"Qt wheel #{self.event_count}: {source}")
        event.accept()

    def event(self, event) -> bool:
        if event.type() == QEvent.Type.NativeGesture:
            gesture_type = event.gestureType()
            if gesture_type == Qt.NativeGestureType.ZoomNativeGesture:
                factor = max(0.01, 1.0 + float(event.value()))
                if self.invert_zoom:
                    factor = 1.0 / factor
                self._apply_zoom_factor(factor)
                self.event_count += 1
                self.input_event.emit(
                    f"Qt native pinch #{self.event_count}: value={event.value():.4f}"
                )
                event.accept()
                return True
        return super().event(event)


class ViewerWindow(QMainWindow):
    def __init__(self, source: Path | None = None) -> None:
        super().__init__()
        self.setWindowTitle("Qt LIF Viewer Benchmark")
        self.resize(1380, 860)
        self.lif: LifFileAdapter | None = None
        self.plan: dict | None = None
        self.source: Path | None = None
        self.load_error: BaseException | None = None
        self.channel_states: dict[int, dict] = {}
        self.channel_checks: dict[int, QCheckBox] = {}
        self.channel_visibility: dict[int, bool] = {}
        self.series_channel_states: dict[tuple[int, int], dict] = {}
        self.synced_contrast: dict[int, dict] = {}
        self.active_contrast_channel: int | None = None
        self.contrast_channel_buttons: dict[int, QRadioButton] = {}
        self.current_frames: dict[int, np.ndarray] = {}
        self.current_frame_key: tuple[int, int, int] | None = None
        self.time_increment_s: float | None = None
        self.configured_cycle_s: float | None = None
        self.display_time_interval_s: float | None = None
        self.z_spacing_um: float | None = None
        self.series_playback_fps: dict[int, float] = {}
        self.play_timer = QTimer(self)
        self.play_timer.timeout.connect(self.advance_time)
        self._building_controls = False

        self._build_ui()
        self._apply_style()
        if source is not None:
            QTimer.singleShot(0, lambda: self.load_lif(source))

    def _build_ui(self) -> None:
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(10, 10, 10, 8)
        root.setSpacing(8)

        header = QHBoxLayout()
        self.path_label = QLabel("No LIF loaded")
        self.path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        header.addWidget(self.path_label, 1)
        open_button = QPushButton("Open LIF")
        open_button.clicked.connect(self.choose_lif)
        header.addWidget(open_button)
        root.addLayout(header)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.viewer_splitter = splitter
        self.view = ImageView()
        self.view.zoom_changed.connect(self._zoom_from_view)
        self.view.input_event.connect(self._input_event_received)
        self.view_tabs = QTabWidget()
        self.view_tabs.addTab(self.view, "Image")
        splitter.addWidget(self.view_tabs)
        self.viewer_controls = self._build_controls()
        splitter.addWidget(self.viewer_controls)
        splitter.setStretchFactor(0, 1)
        splitter.setSizes([980, 360])
        root.addWidget(splitter, 1)

        footer = QHBoxLayout()
        self.event_label = QLabel("Input events will appear here")
        self.event_label.setStyleSheet(f"color: {COLORS['muted']};")
        footer.addWidget(self.event_label, 1)
        self.frame_label = QLabel("Z=0  T=0")
        footer.addWidget(self.frame_label)
        root.addLayout(footer)
        self.setCentralWidget(central)

    def _build_controls(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setMinimumWidth(350)
        panel = QWidget()
        layout = QVBoxLayout(panel)

        acquisition = QGroupBox("Acquisition")
        acquisition_form = QFormLayout(acquisition)
        self.series_combo = QComboBox()
        self.series_combo.currentIndexChanged.connect(self.series_changed)
        acquisition_form.addRow("Series", self.series_combo)
        self.z_spin = QSpinBox()
        self.z_spin.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.z_spin.valueChanged.connect(self._z_spin_changed)
        self.z_summary = QLabel("Z plane 1 / 1")
        acquisition_form.addRow(self.z_summary)
        self.z_slider = QSlider(Qt.Orientation.Horizontal)
        self.z_slider.valueChanged.connect(self._z_slider_changed)
        acquisition_form.addRow(self.z_slider)
        layout.addWidget(acquisition)

        time_group = QGroupBox("Time")
        time_layout = QVBoxLayout(time_group)
        time_position = QHBoxLayout()
        time_position.addWidget(QLabel("Frame"))
        self.time_spin = QSpinBox()
        self.time_spin.setRange(1, 1)
        self.time_spin.setKeyboardTracking(False)
        self.time_spin.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.time_spin.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.time_spin.valueChanged.connect(self._time_spin_changed)
        time_position.addWidget(self.time_spin)
        self.time_total_label = QLabel("/ 1")
        time_position.addWidget(self.time_total_label)
        time_position.addStretch(1)
        time_layout.addLayout(time_position)
        self.time_summary = QLabel("Time 00:00.00 / 00:00.00")
        self.time_summary.setWordWrap(True)
        time_layout.addWidget(self.time_summary)
        self.time_slider = QSlider(Qt.Orientation.Horizontal)
        self.time_slider.valueChanged.connect(self._time_slider_changed)
        time_layout.addWidget(self.time_slider)
        time_row = QHBoxLayout()
        self.play_button = QPushButton("Play")
        self.play_button.clicked.connect(self.toggle_playback)
        time_row.addWidget(self.play_button)
        self.fps_spin = QDoubleSpinBox()
        self.fps_spin.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.fps_spin.setRange(0.1, 120.0)
        self.fps_spin.setDecimals(2)
        self.fps_spin.setSingleStep(0.5)
        self.fps_spin.setKeyboardTracking(False)
        self.fps_spin.setValue(2.0)
        self.fps_spin.setToolTip("Playback speed; acquisition timing is unchanged")
        self.fps_spin.valueChanged.connect(self._fps_changed)
        time_row.addWidget(self.fps_spin)
        time_row.addWidget(QLabel("FPS"))
        time_layout.addLayout(time_row)
        self.interval_label = QLabel("Time interval: unavailable")
        self.interval_label.setWordWrap(True)
        time_layout.addWidget(self.interval_label)
        layout.addWidget(time_group)

        self.channels_group = QGroupBox("Visible channels")
        self.channels_layout = QVBoxLayout(self.channels_group)
        self.use_lut = QCheckBox("Use source LUT colors")
        self.use_lut.setChecked(True)
        self.use_lut.toggled.connect(self.render_frame)
        self.channels_layout.addWidget(self.use_lut)
        layout.addWidget(self.channels_group)

        contrast = QGroupBox("Active channel contrast")
        contrast_form = QFormLayout(contrast)
        self.contrast_channel_widget = QWidget()
        self.contrast_channel_layout = QGridLayout(self.contrast_channel_widget)
        self.contrast_channel_layout.setContentsMargins(0, 0, 0, 0)
        self.contrast_channel_group = QButtonGroup(self)
        self.contrast_channel_group.setExclusive(True)
        contrast_form.addRow("Channel", self.contrast_channel_widget)
        self.sync_contrast = QCheckBox("Sync across series")
        self.sync_contrast.setChecked(True)
        self.sync_contrast.toggled.connect(self._contrast_sync_toggled)
        contrast_form.addRow(self.sync_contrast)
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["Source display", "Full range", "Manual"])
        self.mode_combo.currentTextChanged.connect(self.contrast_mode_changed)
        contrast_form.addRow("Mode", self.mode_combo)
        self.range_slider = RangeSlider()
        self.range_slider.values_changed.connect(self.range_contrast_changed)
        contrast_form.addRow(self.range_slider)
        self.black_spin = QDoubleSpinBox()
        self.white_spin = QDoubleSpinBox()
        for control in (self.black_spin, self.white_spin):
            control.setDecimals(3)
            control.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
            control.valueChanged.connect(self.manual_contrast_changed)
        contrast_form.addRow("Black", self.black_spin)
        contrast_form.addRow("White", self.white_spin)
        self.reset_contrast_button = QPushButton("Reset LIF display")
        self.reset_contrast_button.clicked.connect(self.reset_lif_contrast)
        contrast_form.addRow(self.reset_contrast_button)
        layout.addWidget(contrast)

        navigation = QGroupBox("Navigation")
        navigation_form = QFormLayout(navigation)
        self.zoom_spin = QDoubleSpinBox()
        self.zoom_spin.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.zoom_spin.setRange(5.0, 10000.0)
        self.zoom_spin.setDecimals(0)
        self.zoom_spin.setValue(100.0)
        self.zoom_spin.setSuffix(" %")
        self.zoom_spin.valueChanged.connect(self._zoom_from_control)
        navigation_form.addRow("Zoom", self.zoom_spin)
        fit_button = QPushButton("Fit")
        fit_button.clicked.connect(self.view.fit_image)
        navigation_form.addRow("", fit_button)
        self.invert_zoom = QCheckBox("Invert zoom direction")
        self.invert_zoom.toggled.connect(self._invert_zoom_changed)
        navigation_form.addRow("", self.invert_zoom)
        layout.addWidget(navigation)

        layout.addStretch(1)
        scroll.setWidget(panel)
        return scroll

    def _apply_style(self) -> None:
        self.setStyleSheet(
            f"""
            QMainWindow, QWidget {{ background: {COLORS['window']}; color: {COLORS['text']}; }}
            QGroupBox {{
                background: {COLORS['panel']}; border: 1px solid {COLORS['border']};
                border-radius: 4px; margin-top: 10px; padding-top: 7px;
                font-weight: 600;
            }}
            QGroupBox::title {{ subcontrol-origin: margin; left: 8px; padding: 0 3px; }}
            QPushButton {{ background: #f7f8f9; border: 1px solid {COLORS['border']};
                border-radius: 3px; padding: 6px 10px; }}
            QPushButton:hover {{ border-color: {COLORS['accent']}; }}
            QComboBox, QSpinBox, QDoubleSpinBox {{ background: white; border: 1px solid
                {COLORS['border']}; border-radius: 3px; padding: 4px; }}
            QScrollArea {{ border: 0; }}
            """
        )

    def choose_lif(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(self, "Open Leica LIF", "", "Leica LIF (*.lif)")
        if filename:
            self.load_lif(Path(filename))

    def load_lif(self, source: Path) -> None:
        source = source.expanduser().resolve()
        self.load_error = None
        self.path_label.setText(str(source))
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            plan = build_conversion_plan(source)
            lif = LifFileAdapter(source)
        except BaseException as exc:
            self.load_error = exc
            self.event_label.setText(f"Load failed: {exc}")
            return
        finally:
            QApplication.restoreOverrideCursor()
        self.apply_loaded_lif(source, plan, lif)

    def apply_loaded_lif(self, source: Path, plan: dict, lif: LifFileAdapter) -> None:
        resolved_source = source.expanduser().resolve()
        if self.source != resolved_source:
            self.channel_visibility.clear()
            self.series_channel_states.clear()
            self.synced_contrast.clear()
            self.series_playback_fps.clear()
            self.active_contrast_channel = None
        if self.lif is not None and self.lif is not lif:
            self.lif.close()
        self.source = resolved_source
        self.plan = plan
        self.lif = lif
        self.load_error = None
        self.path_label.setText(str(self.source))
        self.current_frames.clear()
        self.current_frame_key = None
        with QSignalBlocker(self.series_combo):
            self.series_combo.clear()
            for series in self.plan["series"]:
                self.series_combo.addItem(
                    f"{series['series_index'] + 1:03d}  {series['series_name']}",
                    series["series_index"],
                )
        if self.series_combo.count():
            self.series_combo.setCurrentIndex(0)
            self.series_changed(0)

    def current_series_index(self) -> int:
        return int(self.series_combo.currentData() or 0)

    def series_changed(self, _index: int) -> None:
        if self.plan is None or self.lif is None or self.series_combo.currentIndex() < 0:
            return
        self.play_timer.stop()
        self.play_button.setText("Play")
        series_index = self.current_series_index()
        series = self.plan["series"][series_index]
        dims = series["dimensions"]
        self.z_spin.setRange(0, max(0, int(dims["z"]) - 1))
        self.z_slider.setRange(0, max(0, int(dims["z"]) - 1))
        self.z_slider.setEnabled(int(dims["z"]) > 1)
        self.time_slider.setRange(0, max(0, int(dims["t"]) - 1))
        self.time_spin.setRange(1, max(1, int(dims["t"])))
        self.z_spin.setValue(0)
        self.z_slider.setValue(0)
        raw_z_spacing = series.get("pixel_size", {}).get("z_um_per_px")
        self.z_spacing_um = float(raw_z_spacing) if raw_z_spacing is not None else None
        self._update_z_readout()
        self.time_slider.setValue(0)
        self.time_spin.setValue(1)
        increments = [
            output.get("file", {}).get("time_increment_s")
            for output in series["outputs"]
            if output.get("file", {}).get("time_increment_s") is not None
        ]
        self.time_increment_s = float(increments[0]) if increments else None
        raw_cycle = self.lif.images[series_index].settings.get("CycleTime")
        try:
            self.configured_cycle_s = float(raw_cycle) if raw_cycle is not None else None
        except (TypeError, ValueError):
            self.configured_cycle_s = None
        self.display_time_interval_s = (
            self.configured_cycle_s
            if self.configured_cycle_s is not None
            else self.time_increment_s
        )
        self._update_interval_label()
        frame_count = int(dims["t"])
        playback_fps = self.series_playback_fps.setdefault(
            series_index,
            float(recommended_playback_fps(frame_count)),
        )
        with QSignalBlocker(self.fps_spin):
            self.fps_spin.setValue(playback_fps)
        self._update_timer_interval()
        self._update_time_readout()
        self.current_frames.clear()
        self.current_frame_key = None

        self._building_controls = True
        while self.channels_layout.count():
            child = self.channels_layout.takeAt(0)
            if child.widget() is not None and child.widget() is not self.use_lut:
                child.widget().deleteLater()
        self.channels_layout.addWidget(self.use_lut)
        self.channel_states.clear()
        self.channel_checks.clear()
        self._clear_contrast_channel_buttons()
        for output in series["outputs"]:
            index = int(output["channel_index"])
            label = output.get("physical_label") or f"channel {index}"
            source = output.get("source_display_settings") or {}
            dtype = np.dtype(output["file"]["dtype"])
            dtype_max = float(np.iinfo(dtype).max) if np.issubdtype(dtype, np.integer) else 1.0
            value_min = float(source.get("value_min", 0.0))
            value_max = float(source.get("value_max", dtype_max))
            state_key = (series_index, index)
            state = self.series_channel_states.get(state_key)
            if state is None:
                state = {
                    "output": output,
                    "visible": self.channel_visibility.get(index, True),
                    "value_min": value_min,
                    "value_max": value_max,
                    "source_black": float(source.get("black", value_min)),
                    "source_white": float(source.get("white", value_max)),
                    "black": float(source.get("black", value_min)),
                    "white": float(source.get("white", value_max)),
                    "gamma": float(source.get("gamma", 1.0)),
                    "source_gamma": float(source.get("gamma", 1.0)),
                    "mode": "Source display",
                }
                self.series_channel_states[state_key] = state
            else:
                state["output"] = output
                state["visible"] = self.channel_visibility.get(index, state["visible"])
            if self.sync_contrast.isChecked() and index in self.synced_contrast:
                self._apply_contrast_preset(state, self.synced_contrast[index])
            self.channel_visibility.setdefault(index, bool(state["visible"]))
            self.channel_states[index] = state
            check = QCheckBox(f"C{index:02d}  {label}")
            check.setChecked(bool(state["visible"]))
            check.toggled.connect(lambda visible, channel=index: self.channel_toggled(channel, visible))
            self.channels_layout.addWidget(check)
            self.channel_checks[index] = check
            radio = QRadioButton(f"C{index:02d}")
            radio.setToolTip(label)
            radio.toggled.connect(
                lambda checked, channel=index: self.contrast_channel_selected(channel, checked)
            )
            self.contrast_channel_group.addButton(radio, index)
            self.contrast_channel_layout.addWidget(
                radio,
                len(self.contrast_channel_buttons) // 3,
                len(self.contrast_channel_buttons) % 3,
            )
            self.contrast_channel_buttons[index] = radio
        if self.channel_states:
            selected = self.active_contrast_channel
            if selected not in self.channel_states:
                selected = min(self.channel_states)
            self.active_contrast_channel = selected
            self.contrast_channel_buttons[selected].setChecked(True)
        self._building_controls = False
        self.active_channel_changed()
        self.render_frame()

    def active_channel_index(self) -> int:
        if self.active_contrast_channel in self.channel_states:
            return int(self.active_contrast_channel)
        return min(self.channel_states, default=0)

    def _clear_contrast_channel_buttons(self) -> None:
        while self.contrast_channel_layout.count():
            self.contrast_channel_layout.takeAt(0)
        for button in self.contrast_channel_buttons.values():
            self.contrast_channel_group.removeButton(button)
            button.deleteLater()
        self.contrast_channel_buttons.clear()

    def contrast_channel_selected(self, channel: int, checked: bool) -> None:
        if not checked:
            return
        self.active_contrast_channel = channel
        self.active_channel_changed()

    def active_channel_changed(self, _index: int | None = None) -> None:
        if self._building_controls or not self.channel_states:
            return
        state = self.channel_states[self.active_channel_index()]
        blockers = [
            QSignalBlocker(self.mode_combo),
            QSignalBlocker(self.black_spin),
            QSignalBlocker(self.white_spin),
        ]
        self.mode_combo.setCurrentText(state["mode"])
        for control in (self.black_spin, self.white_spin):
            control.setRange(state["value_min"], state["value_max"])
        self.black_spin.setValue(state["black"])
        self.white_spin.setValue(state["white"])
        self.range_slider.set_range(
            state["value_min"],
            state["value_max"],
            state["black"],
            state["white"],
        )
        del blockers

    def channel_toggled(self, channel: int, visible: bool) -> None:
        self.channel_states[channel]["visible"] = visible
        self.channel_visibility[channel] = visible
        self.render_frame()

    def contrast_mode_changed(self, mode: str) -> None:
        if self._building_controls or not self.channel_states:
            return
        state = self.channel_states[self.active_channel_index()]
        state["mode"] = mode
        if mode == "Source display":
            state["black"], state["white"] = state["source_black"], state["source_white"]
        elif mode == "Full range":
            state["black"], state["white"] = state["value_min"], state["value_max"]
        self._propagate_active_contrast()
        self.active_channel_changed()
        self.render_frame()

    def manual_contrast_changed(self) -> None:
        if self._building_controls or not self.channel_states:
            return
        state = self.channel_states[self.active_channel_index()]
        black, white = self.black_spin.value(), self.white_spin.value()
        if white <= black:
            return
        state["black"], state["white"], state["mode"] = black, white, "Manual"
        self._propagate_active_contrast()
        with QSignalBlocker(self.mode_combo):
            self.mode_combo.setCurrentText("Manual")
        self.range_slider.set_values(black, white)
        self.render_frame()

    def range_contrast_changed(self, black: float, white: float) -> None:
        if self._building_controls or not self.channel_states or white <= black:
            return
        state = self.channel_states[self.active_channel_index()]
        state["black"], state["white"], state["mode"] = black, white, "Manual"
        self._propagate_active_contrast()
        blockers = [
            QSignalBlocker(self.mode_combo),
            QSignalBlocker(self.black_spin),
            QSignalBlocker(self.white_spin),
        ]
        self.mode_combo.setCurrentText("Manual")
        self.black_spin.setValue(black)
        self.white_spin.setValue(white)
        del blockers
        self.render_frame()

    def _contrast_sync_toggled(self, enabled: bool) -> None:
        if not enabled:
            self.synced_contrast.clear()

    def _contrast_preset(self, state: dict) -> dict:
        span = max(float(state["value_max"]) - float(state["value_min"]), 1e-12)
        return {
            "mode": state["mode"],
            "black_fraction": (float(state["black"]) - float(state["value_min"])) / span,
            "white_fraction": (float(state["white"]) - float(state["value_min"])) / span,
        }

    def _apply_contrast_preset(self, state: dict, preset: dict) -> None:
        mode = preset["mode"]
        state["mode"] = mode
        if mode == "Source display":
            state["black"] = state["source_black"]
            state["white"] = state["source_white"]
            state["gamma"] = state["source_gamma"]
        elif mode == "Full range":
            state["black"] = state["value_min"]
            state["white"] = state["value_max"]
        else:
            span = float(state["value_max"]) - float(state["value_min"])
            state["black"] = float(state["value_min"]) + preset["black_fraction"] * span
            state["white"] = float(state["value_min"]) + preset["white_fraction"] * span

    def _propagate_active_contrast(self) -> None:
        if not self.sync_contrast.isChecked() or not self.channel_states:
            return
        channel = self.active_channel_index()
        preset = self._contrast_preset(self.channel_states[channel])
        self.synced_contrast[channel] = preset
        for (_series, state_channel), state in self.series_channel_states.items():
            if state_channel == channel:
                self._apply_contrast_preset(state, preset)

    def reset_lif_contrast(self) -> None:
        self.synced_contrast.clear()
        for state in self.series_channel_states.values():
            state["mode"] = "Source display"
            state["black"] = state["source_black"]
            state["white"] = state["source_white"]
            state["gamma"] = state["source_gamma"]
        self.active_channel_changed()
        self.render_frame()

    def _time_slider_changed(self, value: int) -> None:
        with QSignalBlocker(self.time_spin):
            self.time_spin.setValue(value + 1)
        self._update_time_readout()
        self.render_frame()

    def _z_slider_changed(self, value: int) -> None:
        with QSignalBlocker(self.z_spin):
            self.z_spin.setValue(value)
        self._update_z_readout()
        self.render_frame()

    def _z_spin_changed(self, value: int) -> None:
        with QSignalBlocker(self.z_slider):
            self.z_slider.setValue(value)
        self._update_z_readout()
        self.render_frame()

    def _update_z_readout(self) -> None:
        plane_index = self.z_slider.value()
        plane_count = self.z_slider.maximum() + 1
        text = f"Z plane {plane_index + 1} / {plane_count}"
        if self.z_spacing_um is not None and plane_count > 1:
            current = plane_index * self.z_spacing_um
            total = (plane_count - 1) * self.z_spacing_um
            text += f"   |   Relative depth {current:g} / {total:g} um"
        self.z_summary.setText(text)

    def _time_spin_changed(self, value: int) -> None:
        with QSignalBlocker(self.time_slider):
            self.time_slider.setValue(value - 1)
        self._update_time_readout()
        self.render_frame()

    def _update_time_readout(self) -> None:
        frame_index = self.time_slider.value()
        frame_count = self.time_slider.maximum() + 1
        if self.display_time_interval_s is None:
            timing = "Time unavailable"
        else:
            current = frame_index * self.display_time_interval_s
            total = max(0, frame_count - 1) * self.display_time_interval_s
            timing = f"Time {format_elapsed(current)} / {format_elapsed(total)}"
        self.time_total_label.setText(f"/ {frame_count}")
        self.time_summary.setText(timing)

    def toggle_playback(self) -> None:
        if self.play_timer.isActive():
            self.play_timer.stop()
            self.play_button.setText("Play")
        elif self.time_slider.maximum() > 0:
            self._update_timer_interval()
            self.play_timer.start()
            self.play_button.setText("Pause")

    def _update_timer_interval(self) -> None:
        self.play_timer.setInterval(max(1, round(1000.0 / self.fps_spin.value())))

    def _fps_changed(self, value: float) -> None:
        self._update_timer_interval()
        if self.plan is not None and self.series_combo.currentIndex() >= 0:
            self.series_playback_fps[self.current_series_index()] = float(value)

    def _update_interval_label(self) -> None:
        text = (
            f"Time interval: {self.display_time_interval_s:g} s"
            if self.display_time_interval_s is not None
            else "Time interval: unavailable"
        )
        self.interval_label.setText(text)

    def advance_time(self) -> None:
        if getattr(self, "pending_frame_key", None) is not None:
            return
        maximum = self.time_slider.maximum()
        self.time_slider.setValue((self.time_slider.value() + 1) % (maximum + 1))

    def render_frame(self) -> None:
        if self.lif is None or not self.channel_states:
            return
        series_index = self.current_series_index()
        image = self.lif.images[series_index]
        z, t = self.z_spin.value(), self.time_slider.value()
        key = (series_index, z, t)
        if key != self.current_frame_key:
            self.current_frames = {
                channel: np.asarray(image.get_frame(z=z, t=t, c=channel))
                for channel in self.channel_states
            }
            self.current_frame_key = key
        self.compose_current_frames()

    def compose_current_frames(self) -> None:
        if self.lif is None or not self.channel_states or not self.current_frames:
            return
        series_index = self.current_series_index()
        image = self.lif.images[series_index]
        z, t = self.z_spin.value(), self.time_slider.value()
        rgb = None
        visible_total = sum(
            1 for state in self.channel_states.values() if state["visible"]
        )
        visible_count = 0
        for channel, state in sorted(self.channel_states.items()):
            if not state["visible"]:
                continue
            frame = self.current_frames[channel]
            values = normalize(frame, state["black"], state["white"], state["gamma"])
            if rgb is None:
                rgb = np.zeros((*values.shape, 3), dtype=np.float32)
            output = state["output"]
            modality = ((output.get("identity") or {}).get("modality") or {}).get("value")
            if modality == "brightfield":
                rgb += values[..., None] * (1.0 if visible_total == 1 else 0.72)
            else:
                color = (
                    lut_color(
                        output.get("source_lut") or output.get("physical_label"),
                        channel,
                    )
                    if self.use_lut.isChecked()
                    else np.ones(3, dtype=np.float32)
                )
                rgb += values[..., None] * color * 0.9
            visible_count += 1
        if rgb is None:
            height, width = int(image.dims.y), int(image.dims.x)
            rgb8 = np.zeros((height, width, 3), dtype=np.uint8)
        else:
            rgb8 = np.ascontiguousarray(np.clip(rgb * 255.0, 0, 255).astype(np.uint8))
        height, width = rgb8.shape[:2]
        qimage = QImage(
            rgb8.data,
            width,
            height,
            int(rgb8.strides[0]),
            QImage.Format.Format_RGB888,
        ).copy()
        self.view.set_image(qimage)
        displayed = self.current_frame_key or (series_index, z, t)
        displayed_series = self.plan["series"][displayed[0]]
        displayed_dims = displayed_series["dimensions"]
        self.frame_label.setText(
            f"Displayed Z {displayed[1] + 1}/{displayed_dims['z']} | "
            f"T {displayed[2] + 1}/{displayed_dims['t']} | "
            f"Channels {visible_count}"
        )

    def _zoom_from_view(self, percent: float) -> None:
        with QSignalBlocker(self.zoom_spin):
            self.zoom_spin.setValue(percent)

    def _zoom_from_control(self, percent: float) -> None:
        self.view.set_zoom_percent(percent)

    def _invert_zoom_changed(self, checked: bool) -> None:
        self.view.invert_zoom = checked

    def _input_event_received(self, text: str) -> None:
        self.event_label.setText(text)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt API
        if self.lif is not None:
            self.lif.close()
        event.accept()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("lif", nargs="?", type=Path)
    parser.add_argument("--smoke-test", action="store_true")
    parser.add_argument("--screenshot", type=Path)
    parser.add_argument("--zoom", type=float, default=200.0)
    parser.add_argument("--timepoint", type=int, default=1)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    app = QApplication(sys.argv[:1])
    app.setApplicationName("Qt LIF Viewer Benchmark")
    window = ViewerWindow(args.lif)
    window.show()
    if args.smoke_test:
        QTimer.singleShot(250, app.quit)
    elif args.screenshot is not None:
        output = args.screenshot.expanduser().resolve()
        if output.exists():
            raise SystemExit(f"refusing to overwrite screenshot: {output}")

        attempts = 0

        def capture() -> None:
            nonlocal attempts
            attempts += 1
            if window.load_error is not None:
                print(f"load failed: {window.load_error}", file=sys.stderr)
                app.exit(1)
                return
            if window.lif is None or not window.channel_states:
                if attempts >= 100:
                    print("screenshot acceptance timed out", file=sys.stderr)
                    app.exit(1)
                    return
                QTimer.singleShot(100, capture)
                return
            window.time_slider.setValue(min(args.timepoint, window.time_slider.maximum()))
            window.view.set_zoom_percent(args.zoom)
            output.parent.mkdir(parents=True, exist_ok=True)
            window.grab().save(str(output))
            print(output)
            app.quit()

        QTimer.singleShot(800, capture)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
