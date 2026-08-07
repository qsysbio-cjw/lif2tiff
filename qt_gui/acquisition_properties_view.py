"""Native Qt view for recorded Leica acquisition metadata."""

from __future__ import annotations

import datetime as dt

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)


STATUS_LABELS = {
    "resolved_sequence": "Resolved from sequential setting",
    "current_fallback": "Read from current image setting",
    "recorded_conflict": "Recorded metadata conflict",
    "special_acquisition": "Special acquisition metadata",
    "missing": "Incomplete recorded metadata",
}


def _number(value, decimals: int = 3) -> str:
    if value in {None, ""}:
        return "Unavailable"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{number:.{decimals}f}".rstrip("0").rstrip(".")


def _recorded_timestamp(value: object) -> str:
    text = str(value)
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        china = dt.timezone(dt.timedelta(hours=8))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=china)
        else:
            parsed = parsed.astimezone(china)
        rendered = parsed.isoformat(sep=" ", timespec="milliseconds")[:-6]
        return f"{rendered} (UTC+8)"
    except ValueError:
        return text


def _timestamp_for_frame(timestamps: list, frame_index: int) -> str:
    if not timestamps:
        return "Unavailable"
    position = min(max(0, int(frame_index)), len(timestamps) - 1)
    return _recorded_timestamp(timestamps[position])


def acquisition_property_model(plan: dict | None, series_index: int, frame_index: int = 0) -> dict:
    if not plan or not plan.get("series"):
        return {}
    series = next(
        (
            item
            for item in plan["series"]
            if int(item.get("series_index", -1)) == int(series_index)
        ),
        None,
    )
    if series is None:
        return {}
    dims = series.get("dimensions") or {}
    pixel = series.get("pixel_size") or {}
    width_um = (
        float(dims["x"]) * float(pixel["x_um_per_px"])
        if dims.get("x") is not None and pixel.get("x_um_per_px") is not None
        else None
    )
    height_um = (
        float(dims["y"]) * float(pixel["y_um_per_px"])
        if dims.get("y") is not None and pixel.get("y_um_per_px") is not None
        else None
    )
    channels = []
    for output in series.get("outputs", []):
        properties = dict(output.get("acquisition_properties") or {})
        properties.update(
            {
                "index": int(output.get("channel_index", len(channels))),
                "physical_label": output.get("physical_label") or "Unlabeled",
                "lut": output.get("source_lut"),
                "identity": output.get("identity") or {},
            }
        )
        if not properties.get("metadata_resolution_status"):
            modality = ((properties.get("identity") or {}).get("modality") or {}).get("value")
            has_detector = bool(properties.get("detector_name"))
            has_emission = (
                properties.get("emission_window_begin_nm") is not None
                and properties.get("emission_window_end_nm") is not None
            )
            if has_detector and (modality == "brightfield" or has_emission):
                properties["metadata_resolution_status"] = (
                    "resolved_sequence"
                    if properties.get("sequential_setting_name") is not None
                    else "current_fallback"
                )
            else:
                properties["metadata_resolution_status"] = "missing"
        channels.append(properties)
    timestamps = series.get("acquisition_timestamps") or []
    timepoint_timestamps = series.get("timepoint_timestamps") or []
    return {
        "series_index": int(series.get("series_index", series_index)),
        "series_name": series.get("series_name") or f"Series {series_index + 1}",
        "dimension_type": series.get("dimension_type") or "Unknown",
        "dimensions": dims,
        "pixel_size": pixel,
        "physical_size_um": {"x": width_um, "y": height_um},
        "series_start_time": _recorded_timestamp(timestamps[0]) if timestamps else "Unavailable",
        "frame_time": _timestamp_for_frame(timepoint_timestamps, frame_index),
        "frame_index": int(frame_index),
        "timestamp_count": len(timestamps),
        "objective": series.get("objective") or {},
        "confocal_settings": series.get("confocal_settings") or {},
        "microscope": series.get("microscope") or {},
        "optical_settings": series.get("optical_settings") or {},
        "channels": channels,
    }


def _value_label(text: object, *, muted: bool = False) -> QLabel:
    label = QLabel(str(text))
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    label.setWordWrap(True)
    if muted:
        label.setStyleSheet("color: #65717b;")
    return label


class AcquisitionPropertiesPage(QWidget):
    def __init__(self) -> None:
        super().__init__()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        outer.addWidget(self.scroll)
        self.content = QWidget()
        self.layout = QVBoxLayout(self.content)
        self.layout.setContentsMargins(18, 16, 18, 20)
        self.layout.setSpacing(12)
        self.scroll.setWidget(self.content)
        self.clear()

    def _reset(self) -> None:
        while self.layout.count():
            item = self.layout.takeAt(0)
            if item.widget() is not None:
                item.widget().hide()
                item.widget().deleteLater()

    def clear(self) -> None:
        self._reset()
        title = QLabel("Recorded Acquisition Properties")
        title.setStyleSheet("font-size: 19px; font-weight: 650;")
        self.layout.addWidget(title)
        self.layout.addWidget(
            _value_label("Select a LIF and Series to inspect its recorded metadata.", muted=True)
        )
        self.layout.addStretch(1)

    def set_model(self, model: dict) -> None:
        if not model:
            self.clear()
            return
        self._reset()
        title = QLabel("Recorded Acquisition Properties")
        title.setStyleSheet("font-size: 19px; font-weight: 650;")
        self.layout.addWidget(title)
        subtitle = _value_label(
            f"Series {model['series_index'] + 1}: {model['series_name']}", muted=True
        )
        self.layout.addWidget(subtitle)

        summary = QFrame()
        summary.setFrameShape(QFrame.Shape.StyledPanel)
        summary_layout = QGridLayout(summary)
        summary_layout.setHorizontalSpacing(26)
        summary_layout.setVerticalSpacing(7)
        dims = model["dimensions"]
        pixel = model["pixel_size"]
        physical = model["physical_size_um"]
        summary_values = [
            ("Acquired", model["series_start_time"]),
            ("Current frame time", model["frame_time"]),
            ("Acquisition", model["dimension_type"]),
            (
                "Dimensions",
                f"{dims.get('x', '?')} x {dims.get('y', '?')} px | "
                f"Z {dims.get('z', '?')} | T {dims.get('t', '?')}",
            ),
            (
                "Pixel size",
                f"{_number(pixel.get('x_um_per_px'))} x "
                f"{_number(pixel.get('y_um_per_px'))} um/px",
            ),
            (
                "Field size",
                f"{_number(physical.get('x'), 2)} x {_number(physical.get('y'), 2)} um",
            ),
        ]
        for position, (name, value) in enumerate(summary_values):
            row, column = divmod(position, 2)
            block = QWidget()
            block_layout = QVBoxLayout(block)
            block_layout.setContentsMargins(0, 0, 0, 0)
            block_layout.setSpacing(1)
            heading = QLabel(name)
            heading.setStyleSheet("color: #65717b; font-size: 11px;")
            block_layout.addWidget(heading)
            block_layout.addWidget(_value_label(str(value)))
            summary_layout.addWidget(block, row, column)
        summary_layout.setColumnStretch(0, 1)
        summary_layout.setColumnStretch(1, 1)
        self.layout.addWidget(summary)

        channels_title = QLabel("Channels")
        channels_title.setStyleSheet("font-size: 15px; font-weight: 650;")
        self.layout.addWidget(channels_title)
        for channel in model["channels"]:
            self.layout.addWidget(self._channel_card(channel))

        settings = QGroupBox("Acquisition settings")
        settings_form = QFormLayout(settings)
        objective = model["objective"]
        confocal = model["confocal_settings"]
        objective_text = objective.get("name") or "Unavailable"
        if objective.get("magnification"):
            objective_text += f" | {objective['magnification']}x"
        if objective.get("numerical_aperture"):
            objective_text += f" | NA {objective['numerical_aperture']}"
        if objective.get("immersion"):
            objective_text += f" | {objective['immersion']}"
        settings_form.addRow("Objective", _value_label(objective_text))
        settings_form.addRow("Zoom", _value_label(_number(confocal.get("zoom"))))
        pinhole = _number(confocal.get("pinhole_um"), 2)
        airy = _number(confocal.get("pinhole_airy"), 2)
        settings_form.addRow("Pinhole", _value_label(f"{pinhole} um | {airy} AU"))
        settings_form.addRow("Scan speed", _value_label(_number(confocal.get("scan_speed"))))
        settings_form.addRow(
            "Averaging",
            _value_label(
                f"Line {_number(confocal.get('line_averaging'), 0)} | "
                f"Frame {_number(confocal.get('frame_averaging'), 0)}"
            ),
        )
        settings_form.addRow(
            "Pixel dwell", _value_label(f"{_number(confocal.get('pixel_dwell_time_us'))} us")
        )
        self.layout.addWidget(settings)

        details = QGroupBox("Additional recorded metadata")
        details.setCheckable(True)
        details.setChecked(False)
        details_form = QFormLayout(details)
        microscope = model["microscope"]
        optical = model["optical_settings"]
        details_form.addRow("Microscope", _value_label(microscope.get("model") or "Unavailable"))
        details_form.addRow("Serial", _value_label(microscope.get("serial") or "Unavailable"))
        details_form.addRow("Scan mode", _value_label(confocal.get("scan_mode") or "Unavailable"))
        details_form.addRow("Scan direction", _value_label(confocal.get("scan_direction") or "Unavailable"))
        details_form.addRow("Refraction index", _value_label(_number(optical.get("refraction_index"))))
        for index in range(details_form.rowCount()):
            for role in (QFormLayout.ItemRole.LabelRole, QFormLayout.ItemRole.FieldRole):
                item = details_form.itemAt(index, role)
                if item and item.widget():
                    item.widget().setVisible(False)
        details.toggled.connect(
            lambda checked, form=details_form: self._set_form_rows_visible(form, checked)
        )
        self.layout.addWidget(details)
        self.layout.addStretch(1)

    @staticmethod
    def _set_form_rows_visible(form: QFormLayout, visible: bool) -> None:
        for index in range(form.rowCount()):
            for role in (QFormLayout.ItemRole.LabelRole, QFormLayout.ItemRole.FieldRole):
                item = form.itemAt(index, role)
                if item and item.widget():
                    item.widget().setVisible(visible)

    def _channel_card(self, channel: dict) -> QGroupBox:
        index = int(channel["index"])
        card = QGroupBox(f"C{index:02d}  {channel['physical_label']}")
        form = QFormLayout(card)
        status = channel.get("metadata_resolution_status") or "missing"
        if status not in {"resolved_sequence", "current_fallback"}:
            warning = QLabel(STATUS_LABELS.get(status, status))
            warning.setWordWrap(True)
            warning.setStyleSheet(
                "color: #8a4b08; background: #fff4df; border: 1px solid #e5b96d; "
                "padding: 5px; border-radius: 4px;"
            )
            form.addRow(warning)
        excitation = []
        for laser in channel.get("excitation_settings") or []:
            wavelength = _number(laser.get("wavelength_nm"), 1)
            intensity = _number(laser.get("intensity_percent"), 2)
            excitation.append(f"{wavelength} nm @ {intensity}%")
        form.addRow("Excitation", _value_label(", ".join(excitation) or "Unavailable"))
        begin = channel.get("emission_window_begin_nm")
        end = channel.get("emission_window_end_nm")
        identity = channel.get("identity") or {}
        modality = (identity.get("modality") or {}).get("value")
        if begin is not None and end is not None:
            emission = f"{_number(begin, 1)}-{_number(end, 1)} nm"
        elif modality == "brightfield":
            emission = "Not applicable (transmitted light)"
        else:
            emission = "Unavailable"
        form.addRow("Emission window", _value_label(emission))
        detector = channel.get("detector_name") or "Unavailable"
        if channel.get("detector_type"):
            detector += f" | {channel['detector_type']}"
        form.addRow("Detector", _value_label(detector))
        form.addRow(
            "Gain / offset",
            _value_label(f"{_number(channel.get('gain'))} / {_number(channel.get('offset'))}"),
        )
        form.addRow("Acquisition mode", _value_label(channel.get("acquisition_mode") or "Unavailable"))
        form.addRow("LUT", _value_label(channel.get("lut") or "Unavailable"))
        form.addRow("Bit depth", _value_label(_number(channel.get("bit_depth"), 0)))
        setting = channel.get("sequential_setting_name") or "Current image setting"
        form.addRow("Recorded setting", _value_label(setting, muted=True))
        return card
