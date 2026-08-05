#!/usr/bin/env python3
"""Formal PySide6 GUI for the existing LIF2TIFF conversion core."""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import sys
import threading
from pathlib import Path

from PySide6.QtCore import QObject, QSignalBlocker, Qt, QTimer, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDockWidget,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)


ROOT = (
    Path(sys._MEIPASS)
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parents[1]
)
SRC = ROOT / "src"
BENCHMARK = ROOT / "viewer"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(BENCHMARK))

from channel_resolver import load_registry  # noqa: E402
from liffile_adapter import LifFileAdapter  # noqa: E402
from liftool import (  # noqa: E402
    ConversionCancelled,
    MIN_POST_CONVERSION_FREE_BYTES,
    SOFTWARE_VERSION,
    build_conversion_plan,
    convert_lif,
    output_storage_preflight,
    portable_path_collision_key,
    validate_project,
)
from presentation_export import (  # noqa: E402
    export_presentations,
    presentation_frame_count,
)
from tiff_project_adapter import (  # noqa: E402
    TiffProjectAdapter,
    is_tiff_project,
    project_directory,
)
from viewer import ViewerWindow  # noqa: E402
from stage_map_view import StageMapPage, load_stage_calibration, stage_map_points  # noqa: E402


APP_NAME = "LIF2TIFF Qt Workbench"


def discover_lifs(inputs: list[Path]) -> list[Path]:
    discovered: dict[Path, None] = {}
    for raw in inputs:
        path = raw.expanduser().resolve()
        if path.is_file() and path.suffix.lower() == ".lif":
            discovered[path] = None
        elif path.is_dir():
            for root, directories, filenames in os.walk(path, followlinks=False):
                directories.sort(key=str.casefold)
                for filename in sorted(filenames, key=str.casefold):
                    if filename.lower().endswith(".lif"):
                        discovered[Path(root, filename).resolve()] = None
    return list(discovered)


def series_table_rows(plan: dict) -> list[tuple[int, str, str, str]]:
    return [
        (
            int(series["series_index"]) + 1,
            str(series["series_name"]),
            str(series["dimension_type"]),
            f"{series['dimensions']['x']} x {series['dimensions']['y']}",
        )
        for series in plan.get("series", [])
    ]


def suggested_assignment(channel: dict) -> str | None:
    inferred = channel.get("inferred_dye") or {}
    if inferred.get("id"):
        return str(inferred["id"])
    if channel.get("modality") == "brightfield":
        return "brightfield"
    return None


def unconfirmed_channel_keys(plan: dict, channel_overrides: dict[int, str]) -> list[str]:
    return [
        channel["channel_key"]
        for channel in plan["channel_resolution"]["channels"]
        if int(channel["channel_key"][1:]) not in channel_overrides
        and not channel.get("confirmed_assignment")
    ]


def dry_run_report(
    plan: dict,
    output: Path | None,
    *,
    review_acknowledged: bool,
    resume: bool,
    channel_overrides: dict[int, str],
) -> dict:
    errors: list[str] = []
    warnings: list[str] = []
    workflow = plan["channel_resolution"]["workflow"]
    if workflow["route"] == "review" and not review_acknowledged:
        errors.append("Acquisition review has not been acknowledged.")
    if output is None:
        errors.append("No output directory is selected.")
        output_state = "missing"
    elif output.exists():
        errors.append("The selected complete output already exists.")
        output_state = "complete_exists"
    else:
        partial = output.with_name(f"{output.name}.partial")
        if partial.exists() and not resume:
            errors.append("A matching .partial project exists but Resume is disabled.")
            output_state = "partial_blocked"
        elif partial.exists():
            output_state = "partial_resume"
            warnings.append("The matching .partial project will be validated before resume.")
        elif resume:
            errors.append("Resume is enabled but no matching .partial project exists.")
            output_state = "resume_missing"
        else:
            output_state = "new"
    storage = None
    if output is not None and output_state != "complete_exists":
        storage = output_storage_preflight(
            output,
            int(plan["summary"]["estimated_uncompressed_pixel_bytes"]),
        )
        errors.extend(storage["errors"])
        warnings.extend(storage["warnings"])
    unconfirmed = unconfirmed_channel_keys(plan, channel_overrides)
    if unconfirmed:
        errors.append(
            "Every channel requires explicit confirmation; unresolved: "
            + ", ".join(unconfirmed)
            + "."
        )
    return {
        "status": "blocked" if errors else "ready",
        "errors": errors,
        "warnings": warnings,
        "workflow_route": workflow["route"],
        "output_state": output_state,
        "storage": storage,
        "unconfirmed_channels": unconfirmed,
        **plan["summary"],
    }


def find_batch_output_collisions(
    items: list[tuple[Path, Path | None]],
) -> list[dict]:
    groups: dict[tuple[str, ...], dict] = {}
    for source, output in items:
        if output is None:
            continue
        group = groups.setdefault(
            portable_path_collision_key(output),
            {"output": output, "sources": [], "targets": []},
        )
        group["sources"].append(source)
        group["targets"].append((source, output))
    return [group for group in groups.values() if len(group["sources"]) > 1]


def _human_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"


def apply_batch_storage_constraints(
    reports: list[tuple[Path, Path | None, dict]],
) -> None:
    groups: dict[int, dict] = {}
    for source, _output, report in reports:
        storage = report.get("storage") or {}
        device_id = storage.get("device_id")
        free_bytes = storage.get("free_bytes")
        if device_id is None or free_bytes is None:
            continue
        group = groups.setdefault(
            int(device_id),
            {"free_bytes": int(free_bytes), "required_bytes": 0, "sources": []},
        )
        group["free_bytes"] = min(group["free_bytes"], int(free_bytes))
        group["required_bytes"] += int(storage["required_bytes"])
        group["sources"].append(source)

    by_source = {source: report for source, _, report in reports}
    for group in groups.values():
        if len(group["sources"]) < 2:
            continue
        required = int(group["required_bytes"])
        free = int(group["free_bytes"])
        if required > free:
            message = (
                "Selected batch has insufficient combined free space on one "
                f"filesystem: {_human_bytes(required)} required, "
                f"{_human_bytes(free)} free."
            )
            for source in group["sources"]:
                report = by_source[source]
                report["errors"].append(message)
                report["status"] = "blocked"
        elif free - required < MIN_POST_CONVERSION_FREE_BYTES:
            message = (
                "Selected batch is expected to leave less than 1 GiB free on "
                "one filesystem."
            )
            for source in group["sources"]:
                report = by_source[source]
                if message not in report["warnings"]:
                    report["warnings"].append(message)


class ConversionBridge(QObject):
    inputs_ready = Signal(object)
    plan_ready = Signal(object)
    plan_failed = Signal(object)
    project_ready = Signal(object)
    project_failed = Signal(object)
    preview_ready = Signal(object)
    preview_failed = Signal(object)
    stage_plans_ready = Signal(object)
    item_status = Signal(object)
    progress = Signal(str)
    completed = Signal(object)
    failed = Signal(object)
    presentation_progress = Signal(str)
    presentation_completed = Signal(object)
    presentation_failed = Signal(object)


class PresentationExportDialog(QDialog):
    def __init__(
        self,
        plan: dict,
        visible_channels: set[int],
        use_lut: bool,
        output_parent: Path,
        output_name: str,
        parent=None,
    ):
        super().__init__(parent)
        self.plan = plan
        self.setWindowTitle("Batch overlay export")
        self.setMinimumWidth(440)
        layout = QVBoxLayout(self)

        channels_group = QGroupBox("Channels")
        channels_layout = QVBoxLayout(channels_group)
        channel_labels: dict[int, str] = {}
        for series in plan.get("series", []):
            for output in series.get("outputs", []):
                index = int(output["channel_index"])
                channel_labels.setdefault(
                    index,
                    output.get("analysis_label")
                    or output.get("physical_label")
                    or f"channel {index}",
                )
        self.channel_checks: dict[int, QCheckBox] = {}
        for index, label in sorted(channel_labels.items()):
            check = QCheckBox(f"C{index:02d}  {label}")
            check.setChecked(index in visible_channels)
            check.toggled.connect(self._refresh_estimate)
            channels_layout.addWidget(check)
            self.channel_checks[index] = check
        layout.addWidget(channels_group)

        form = QFormLayout()
        self.scope_combo = QComboBox()
        self.scope_combo.addItem("Current frame", "current_frame")
        self.scope_combo.addItem("All series, first Z/T plane", "all_series_first_plane")
        self.scope_combo.addItem("All Z/T planes", "all_planes")
        self.scope_combo.setCurrentIndex(1)
        self.scope_combo.currentIndexChanged.connect(self._refresh_estimate)
        form.addRow("Scope", self.scope_combo)
        self.contrast_combo = QComboBox()
        self.contrast_combo.addItem("Current adjusted display", "current_adjusted")
        self.contrast_combo.addItem("Leica source display", "source_display")
        self.contrast_combo.addItem("Full acquired range", "full_range")
        form.addRow("Contrast", self.contrast_combo)
        self.lut_check = QCheckBox("Use channel LUT colors")
        self.lut_check.setChecked(use_lut)
        form.addRow(self.lut_check)
        output_parent_row = QHBoxLayout()
        self.output_parent_edit = QLineEdit(str(output_parent))
        self.output_parent_edit.textChanged.connect(self._refresh_estimate)
        output_parent_row.addWidget(self.output_parent_edit, 1)
        output_browse = QPushButton("Browse")
        output_browse.clicked.connect(self._choose_output_parent)
        output_parent_row.addWidget(output_browse)
        form.addRow("Output parent", output_parent_row)
        self.output_name_edit = QLineEdit(output_name)
        self.output_name_edit.textChanged.connect(self._refresh_estimate)
        form.addRow("New folder", self.output_name_edit)
        layout.addLayout(form)
        self.estimate_label = QLabel()
        self.estimate_label.setWordWrap(True)
        layout.addWidget(self.estimate_label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Export")
        buttons.accepted.connect(self._accept_if_valid)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._refresh_estimate()

    def selected_channels(self) -> list[int]:
        return [index for index, check in self.channel_checks.items() if check.isChecked()]

    def _refresh_estimate(self, *_args) -> None:
        count = presentation_frame_count(
            self.plan,
            self.scope_combo.currentData(),
            self.selected_channels(),
        )
        self.estimate_label.setText(
            f"Planned PNG files: {count} | Selected channels: "
            f"{len(self.selected_channels())}\nDestination: {self.output_path()}"
        )

    def _choose_output_parent(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self,
            "Choose an existing output parent folder",
            self.output_parent_edit.text(),
        )
        if folder:
            self.output_parent_edit.setText(folder)

    def output_path(self) -> Path:
        return (
            Path(self.output_parent_edit.text()).expanduser()
            / self.output_name_edit.text().strip()
        ).resolve()

    def _accept_if_valid(self) -> None:
        if not self.selected_channels():
            QMessageBox.warning(self, APP_NAME, "Select at least one channel.")
            return
        parent = Path(self.output_parent_edit.text()).expanduser()
        name = self.output_name_edit.text().strip()
        if not parent.is_dir():
            QMessageBox.warning(self, APP_NAME, "Output parent must be an existing folder.")
            return
        if not name or Path(name).name != name or name in {".", ".."}:
            QMessageBox.warning(self, APP_NAME, "New folder must be one folder name.")
            return
        output = self.output_path()
        if output.exists() or output.with_name(f"{output.name}.partial").exists():
            QMessageBox.warning(self, APP_NAME, "The output folder or its partial already exists.")
            return
        self.accept()


class QtWorkbench(ViewerWindow):
    def __init__(self) -> None:
        super().__init__(None)
        self.setWindowTitle(f"{APP_NAME} {SOFTWARE_VERSION}")
        self.resize(1560, 900)
        self.path_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        self.setAcceptDrops(True)
        self.input_items: dict[Path, QListWidgetItem] = {}
        self.channel_overrides_by_source: dict[Path, dict[int, str]] = {}
        self.review_acknowledged_by_source: dict[Path, bool] = {}
        self.channel_overrides: dict[int, str] = {}
        self.cancel_event = threading.Event()
        self.conversion_thread: threading.Thread | None = None
        self.presentation_thread: threading.Thread | None = None
        self.lif_lock = threading.Lock()
        self.plan_token = 0
        self.preview_token = 0
        self.pending_source: Path | None = None
        self.active_project: Path | None = None
        self.pending_frame_key: tuple[int, int, int] | None = None
        self.close_after_cancel = False
        self.plan_cache: dict[Path, dict] = {}
        self.stage_map_token = 0
        self.plate_calibration = load_stage_calibration()
        self.stage_map_page = StageMapPage()
        self.view_tabs.addTab(self.stage_map_page, "Stage Map")
        self.view_tabs.currentChanged.connect(self._viewer_tab_changed)
        self.bridge = ConversionBridge()
        self.bridge.inputs_ready.connect(self._add_discovered_inputs)
        self.bridge.plan_ready.connect(self._plan_ready)
        self.bridge.plan_failed.connect(self._plan_failed)
        self.bridge.project_ready.connect(self._project_ready)
        self.bridge.project_failed.connect(self._project_failed)
        self.bridge.preview_ready.connect(self._preview_ready)
        self.bridge.preview_failed.connect(self._preview_failed)
        self.bridge.stage_plans_ready.connect(self._stage_plans_ready)
        self.bridge.item_status.connect(self._batch_item_status)
        self.bridge.progress.connect(self._conversion_progress)
        self.bridge.completed.connect(self._conversion_completed)
        self.bridge.failed.connect(self._conversion_failed)
        self.bridge.presentation_progress.connect(self._presentation_progress)
        self.bridge.presentation_completed.connect(self._presentation_completed)
        self.bridge.presentation_failed.connect(self._presentation_failed)
        self._build_queue_dock()
        self._build_conversion_dock()
        self.statusBar().showMessage("Ready")

    def _viewer_tab_changed(self, index: int) -> None:
        if not hasattr(self, "tools_tabs") or not hasattr(self, "export_tools_index"):
            return
        image_active = self.view_tabs.widget(index) is self.view
        self.tools_tabs.setTabEnabled(self.display_tools_index, image_active)
        self.tools_tabs.setTabEnabled(self.export_tools_index, image_active)
        if not image_active and self.tools_tabs.currentIndex() == self.display_tools_index:
            self.tools_tabs.setCurrentIndex(self.convert_tools_index)

    def _build_queue_dock(self) -> None:
        dock = QDockWidget("Input queue", self)
        dock.setObjectName("input-queue")
        dock.setMinimumWidth(310)
        panel = QWidget()
        layout = QVBoxLayout(panel)
        actions = QHBoxLayout()
        self.add_files_button = QPushButton("Add Files")
        self.add_files_button.clicked.connect(self.choose_lif)
        self.add_folder_button = QPushButton("Add Folder")
        self.add_folder_button.clicked.connect(self.choose_folder)
        self.remove_button = QPushButton("Remove")
        self.remove_button.clicked.connect(self.remove_selected)
        self.clear_button = QPushButton("Clear All")
        self.clear_button.clicked.connect(self.clear_inputs)
        for button in (
            self.add_files_button,
            self.add_folder_button,
            self.remove_button,
            self.clear_button,
        ):
            actions.addWidget(button)
        layout.addLayout(actions)
        self.open_project_button = QPushButton("Open Converted Project")
        self.open_project_button.clicked.connect(self.choose_project)
        layout.addWidget(self.open_project_button)
        self.queue = QListWidget()
        self.queue.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.queue.currentItemChanged.connect(self._queue_item_changed)
        self.queue.itemSelectionChanged.connect(self._queue_selection_changed)
        layout.addWidget(self.queue, 1)
        self.queue_summary = QLabel("Drop LIF files or folders")
        layout.addWidget(self.queue_summary)

        series_group = QGroupBox("Series")
        series_layout = QVBoxLayout(series_group)
        self.series_table = QTableWidget(0, 4)
        self.series_table.setHorizontalHeaderLabels(["#", "Name", "Type", "X x Y"])
        self.series_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.series_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.series_table.verticalHeader().setVisible(False)
        header = self.series_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.series_table.itemSelectionChanged.connect(self._series_table_changed)
        series_layout.addWidget(self.series_table)
        layout.addWidget(series_group, 2)
        self.queue_panel = panel
        dock.setWidget(panel)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, dock)

    def _build_conversion_dock(self) -> None:
        dock = QDockWidget("Preflight and conversion", self)
        dock.setObjectName("conversion")
        dock.setMinimumWidth(280)
        panel = QWidget()
        layout = QVBoxLayout(panel)

        preflight = QGroupBox("Preflight")
        preflight_layout = QVBoxLayout(preflight)
        self.preflight_label = QLabel("Select a queued LIF.")
        self.preflight_label.setWordWrap(True)
        preflight_layout.addWidget(self.preflight_label)
        self.review_check = QCheckBox("I reviewed the acquisition variants")
        self.review_check.setEnabled(False)
        self.review_check.toggled.connect(self._review_toggled)
        preflight_layout.addWidget(self.review_check)
        layout.addWidget(preflight)

        identity = QGroupBox("Channel identity")
        identity_layout = QVBoxLayout(identity)
        self.identity_table = QTableWidget(0, 3)
        self.identity_table.setHorizontalHeaderLabels(["Channel", "Candidate", "Assignment"])
        self.identity_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.identity_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.identity_table.itemSelectionChanged.connect(self.identity_selection_changed)
        self.identity_table.verticalHeader().setVisible(False)
        identity_header = self.identity_table.horizontalHeader()
        identity_header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        identity_header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        identity_header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        identity_layout.addWidget(self.identity_table)
        self.identity_status = QLabel("Confirmed 0 / 0 channels")
        self.identity_status.setWordWrap(True)
        self.identity_status.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        identity_layout.addWidget(self.identity_status)
        assignment_row = QHBoxLayout()
        self.assignment_combo = QComboBox()
        registry = load_registry()
        self.assignment_combo.addItems(
            ["unconfirmed", "brightfield", "unknown"]
            + [entry["id"] for entry in registry["entries"] if entry.get("kind") == "stain"]
        )
        self.apply_button = QPushButton("Apply")
        self.apply_button.clicked.connect(self.apply_assignment)
        assignment_row.addWidget(self.assignment_combo, 1)
        assignment_row.addWidget(self.apply_button)
        identity_layout.addLayout(assignment_row)
        self.accept_suggestions_button = QPushButton("Accept suggestions")
        self.accept_suggestions_button.clicked.connect(self.accept_suggestions)
        self.accept_suggestions_button.setEnabled(False)
        identity_layout.addWidget(self.accept_suggestions_button)
        layout.addWidget(identity, 1)

        conversion = QGroupBox("Conversion")
        conversion_layout = QVBoxLayout(conversion)
        output_row = QHBoxLayout()
        self.output_edit = QLineEdit()
        self.output_edit.textChanged.connect(self._refresh_conversion_gate)
        self.browse_button = QPushButton("Browse")
        self.browse_button.clicked.connect(self.choose_output)
        output_row.addWidget(self.output_edit, 1)
        output_row.addWidget(self.browse_button)
        conversion_layout.addLayout(output_row)
        self.output_mode_label = QLabel("Single LIF: output path shown above")
        self.output_mode_label.setWordWrap(True)
        conversion_layout.addWidget(self.output_mode_label)
        self.resume_check = QCheckBox("Resume matching .partial project")
        self.resume_check.toggled.connect(self._refresh_conversion_gate)
        conversion_layout.addWidget(self.resume_check)
        button_row = QHBoxLayout()
        self.dry_run_button = QPushButton("Dry Run")
        self.dry_run_button.clicked.connect(self.show_dry_run)
        self.convert_button = QPushButton("Convert")
        self.convert_button.clicked.connect(self.start_conversion)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.cancel_conversion)
        self.cancel_button.setEnabled(False)
        button_row.addWidget(self.dry_run_button)
        button_row.addWidget(self.convert_button)
        button_row.addWidget(self.cancel_button)
        conversion_layout.addLayout(button_row)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        conversion_layout.addWidget(self.progress)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(500)
        self.log.setMaximumHeight(140)
        conversion_layout.addWidget(self.log)
        layout.addWidget(conversion)

        self.tools_tabs = QTabWidget()
        self.viewer_controls.setMinimumWidth(0)
        self.display_tools_index = self.tools_tabs.addTab(
            self.viewer_controls, "Display"
        )
        self.convert_tools_index = self.tools_tabs.addTab(panel, "Convert")
        export_panel = QWidget()
        export_layout = QVBoxLayout(export_panel)
        export_current = QPushButton("Export Current Composite PNG")
        export_current.clicked.connect(self.export_png)
        export_layout.addWidget(export_current)
        self.batch_overlay_button = QPushButton("Batch Overlay Export")
        self.batch_overlay_button.clicked.connect(self.show_presentation_export)
        export_layout.addWidget(self.batch_overlay_button)
        self.presentation_status = QLabel("Derived PNG exports do not modify quantitative TIFFs.")
        self.presentation_status.setWordWrap(True)
        export_layout.addWidget(self.presentation_status)
        export_layout.addStretch(1)
        self.export_tools_index = self.tools_tabs.addTab(export_panel, "Export")
        self.tools_tabs.setCurrentIndex(self.convert_tools_index)
        dock.setWindowTitle("Tools")
        dock.setWidget(self.tools_tabs)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
        self.resizeDocks([dock], [305], Qt.Orientation.Horizontal)

    def choose_lif(self) -> None:
        filenames, _ = QFileDialog.getOpenFileNames(
            self,
            "Add Leica LIF files",
            "",
            "Leica LIF (*.lif)",
        )
        if filenames:
            self.add_inputs([Path(filename) for filename in filenames])

    def choose_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Add a folder containing LIF files")
        if folder:
            self.add_inputs([Path(folder)])

    def choose_project(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Open converted TIFF project")
        if folder:
            self.open_project(Path(folder))

    def add_inputs(self, inputs: list[Path]) -> None:
        if not inputs or self._presentation_running():
            return
        if len(inputs) == 1 and is_tiff_project(inputs[0]):
            self.open_project(inputs[0])
            return
        self.queue_summary.setText("Scanning inputs for LIF files...")
        self.statusBar().showMessage("Scanning inputs")

        def worker() -> None:
            try:
                self.bridge.inputs_ready.emit(discover_lifs(inputs))
            except BaseException as exc:
                self.bridge.plan_failed.emit((None, None, exc))

        threading.Thread(target=worker, daemon=True).start()

    def _add_discovered_inputs(self, paths: list[Path]) -> None:
        new_items = []
        for path in paths:
            if path in self.input_items:
                continue
            item = QListWidgetItem(f"{path.name}\n{path.parent.name}  |  Queued")
            item.setData(Qt.ItemDataRole.UserRole, str(path))
            self.queue.addItem(item)
            self.input_items[path] = item
            new_items.append(item)
        self.queue_summary.setText(f"{len(self.input_items)} LIF file(s)")
        self.statusBar().showMessage(
            f"Added {len(new_items)} LIF file(s)" if new_items else "No new LIF files found"
        )
        if new_items and self.queue.currentItem() is None:
            self.queue.setCurrentItem(new_items[0])

    def _set_item_status(self, path: Path | None, status: str) -> None:
        if path is None or path not in self.input_items:
            return
        item = self.input_items[path]
        item.setText(f"{path.name}\n{path.parent.name}  |  {status}")

    def _queue_item_changed(self, current: QListWidgetItem | None, _previous=None) -> None:
        if current is None or self._conversion_running():
            return
        self.load_lif(Path(current.data(Qt.ItemDataRole.UserRole)))

    def _selected_stage_sources(self) -> list[Path]:
        selected = self.queue.selectedItems()
        if not selected and self.queue.currentItem() is not None:
            selected = [self.queue.currentItem()]
        return [
            Path(item.data(Qt.ItemDataRole.UserRole)).expanduser().resolve()
            for item in selected
        ]

    def _queue_selection_changed(self) -> None:
        QTimer.singleShot(0, self._selection_changed_deferred)

    def _selection_changed_deferred(self) -> None:
        self._refresh_stage_map()
        self._refresh_batch_controls()

    def _refresh_stage_map(self) -> None:
        if self.active_project is not None and self.plan is not None:
            self.stage_map_page.set_datasets(
                [
                    {
                        "source": self.active_project.name,
                        "points": stage_map_points(self.plan, self.active_project),
                    }
                ],
                self.plate_calibration,
            )
            return
        sources = self._selected_stage_sources()
        self.stage_map_token += 1
        token = self.stage_map_token
        if not sources:
            self.stage_map_page.clear()
            return
        missing = [source for source in sources if source not in self.plan_cache]
        if not missing:
            self._apply_stage_map(sources)
            return
        self.stage_map_page.show_loading(len(sources))

        def worker() -> None:
            plans = {}
            errors = {}
            for source in missing:
                try:
                    plans[source] = build_conversion_plan(source)
                except BaseException as exc:
                    errors[source] = str(exc)
            self.bridge.stage_plans_ready.emit((token, sources, plans, errors))

        threading.Thread(target=worker, daemon=True).start()

    def _stage_plans_ready(self, payload) -> None:
        token, sources, plans, errors = payload
        self.plan_cache.update(plans)
        self._refresh_batch_controls()
        if token != self.stage_map_token:
            return
        self._apply_stage_map(sources, errors)

    def _apply_stage_map(self, sources: list[Path], errors: dict | None = None) -> None:
        datasets = []
        for source in sources:
            plan = self.plan_cache.get(source)
            if plan is None:
                continue
            datasets.append({"source": source.name, "points": stage_map_points(plan, source)})
        self.stage_map_page.set_datasets(datasets, self.plate_calibration)
        if errors:
            self.statusBar().showMessage(
                f"Stage metadata unavailable for {len(errors)} selected LIF file(s)"
            )

    def load_lif(self, source: Path) -> None:
        if self._conversion_running() or self._presentation_running():
            return
        self.active_project = None
        if hasattr(self, "tools_tabs"):
            self.tools_tabs.setTabEnabled(self.convert_tools_index, True)
        source = source.expanduser().resolve()
        self.channel_overrides = self.channel_overrides_by_source.setdefault(source, {})
        self.plan_token += 1
        token = self.plan_token
        self.preview_token += 1
        self.pending_source = source
        self.pending_frame_key = None
        self.series_table.setRowCount(0)
        self._set_item_status(source, "Inspecting")
        self.path_label.setText(source.name)
        self.path_label.setToolTip(str(source))
        self.preflight_label.setText("Inspecting metadata...")
        self.dry_run_button.setEnabled(False)
        self.convert_button.setEnabled(False)
        self.statusBar().showMessage(f"Inspecting {source.name}")

        def worker() -> None:
            lif = None
            try:
                plan = build_conversion_plan(source)
                lif = LifFileAdapter(source)
                self.bridge.plan_ready.emit((token, source, plan, lif))
            except BaseException as exc:
                if lif is not None:
                    lif.close()
                self.bridge.plan_failed.emit((token, source, exc))

        threading.Thread(target=worker, daemon=True).start()

    def open_project(self, path: Path) -> None:
        if self._conversion_running() or self._presentation_running():
            return
        project = project_directory(path)
        self.plan_token += 1
        token = self.plan_token
        self.preview_token += 1
        self.pending_source = project
        self.pending_frame_key = None
        self.series_table.setRowCount(0)
        self.path_label.setText(project.name)
        self.path_label.setToolTip(str(project))
        self.preflight_label.setText("Validating converted project...")
        self.statusBar().showMessage(f"Opening {project.name}")

        def worker() -> None:
            adapter = None
            try:
                validation = validate_project(project)
                if not validation["ok"]:
                    raise RuntimeError("; ".join(validation["errors"]))
                adapter = TiffProjectAdapter(project)
                self.bridge.project_ready.emit(
                    (token, project, adapter.plan, adapter, validation)
                )
            except BaseException as exc:
                if adapter is not None:
                    adapter.close()
                self.bridge.project_failed.emit((token, project, exc))

        threading.Thread(target=worker, daemon=True).start()

    def _project_ready(self, payload) -> None:
        token, project, plan, adapter, validation = payload
        if token != self.plan_token or project != self.pending_source:
            adapter.close()
            return
        self.active_project = project
        with QSignalBlocker(self.queue):
            self.queue.clearSelection()
            self.queue.setCurrentItem(None)
        with self.lif_lock:
            super().apply_loaded_lif(project, plan, adapter)
        self.path_label.setText(project.name)
        self.path_label.setToolTip(str(project))
        self._populate_series_table()
        self._refresh_stage_map()
        self.preflight_label.setText(
            f"Converted project verified | {plan['summary']['series_count']} series | "
            f"{validation['files_checked']} TIFF files"
        )
        self.review_check.setChecked(False)
        self.review_check.setEnabled(False)
        with QSignalBlocker(self.resume_check):
            self.resume_check.setChecked(False)
        self.identity_table.setRowCount(0)
        self.identity_status.setText(
            "This is an already converted project. Select an original .lif file "
            "from the input queue to run a new conversion."
        )
        self.identity_status.setStyleSheet("color: #65717b;")
        self.output_edit.setEnabled(False)
        self.browse_button.setEnabled(False)
        self.accept_suggestions_button.setEnabled(False)
        self.dry_run_button.setEnabled(False)
        self.convert_button.setEnabled(False)
        self.convert_button.setToolTip(
            "Already converted projects can be viewed and exported, but cannot be converted again. "
            "Add or select an original .lif file first."
        )
        self.tools_tabs.setTabEnabled(self.convert_tools_index, True)
        self.tools_tabs.setCurrentIndex(self.display_tools_index)
        self.statusBar().showMessage("Converted project ready")

    def _project_failed(self, payload) -> None:
        token, project, exc = payload
        if token != self.plan_token or project != self.pending_source:
            return
        self.active_project = None
        self.preflight_label.setText(f"Project open failed: {exc}")
        self.statusBar().showMessage("Project open failed")
        QMessageBox.critical(self, APP_NAME, str(exc))

    def _plan_ready(self, payload: tuple[int, Path, dict, LifFileAdapter]) -> None:
        token, source, plan, lif = payload
        if token != self.plan_token or source != self.pending_source:
            lif.close()
            return
        with self.lif_lock:
            super().apply_loaded_lif(source, plan, lif)
        self.path_label.setText(source.name)
        self.path_label.setToolTip(str(source))
        self.plan_cache[source] = plan
        self.channel_overrides = self.channel_overrides_by_source.setdefault(source, {})
        self._refresh_stage_map()
        self._populate_series_table()
        self._set_item_status(source, "Ready")
        self._populate_preflight()
        self.statusBar().showMessage("Preflight complete")

    def _populate_series_table(self) -> None:
        if self.plan is None:
            return
        rows = series_table_rows(self.plan)
        with QSignalBlocker(self.series_table):
            self.series_table.setRowCount(len(rows))
            for row, values in enumerate(rows):
                for column, value in enumerate(values):
                    item = QTableWidgetItem(str(value))
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                    if column == 0:
                        item.setData(
                            Qt.ItemDataRole.UserRole,
                            int(self.plan["series"][row]["series_index"]),
                        )
                    self.series_table.setItem(row, column, item)
            if rows:
                self.series_table.selectRow(self.series_combo.currentIndex())

    def _series_table_changed(self) -> None:
        row = self.series_table.currentRow()
        if row < 0 or self.series_table.item(row, 0) is None:
            return
        series_index = self.series_table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        combo_index = self.series_combo.findData(series_index)
        if combo_index >= 0 and combo_index != self.series_combo.currentIndex():
            self.series_combo.setCurrentIndex(combo_index)

    def series_changed(self, index: int) -> None:
        super().series_changed(index)
        if hasattr(self, "series_table") and index >= 0 and index < self.series_table.rowCount():
            with QSignalBlocker(self.series_table):
                self.series_table.selectRow(index)

    def _plan_failed(self, payload) -> None:
        token, source, exc = payload
        if token is not None and token != self.plan_token:
            return
        if source is not None:
            self._set_item_status(source, "Error")
        self.preflight_label.setText(f"Preflight failed: {exc}")
        self.statusBar().showMessage("Preflight failed")

    def render_frame(self) -> None:
        if self.lif is None or not self.channel_states:
            return
        key = (
            self.current_series_index(),
            self.z_spin.value(),
            self.time_slider.value(),
        )
        if key == self.current_frame_key and self.current_frames:
            self.compose_current_frames()
            return
        if self.pending_frame_key is not None:
            return
        self.preview_token += 1
        token = self.preview_token
        self.pending_frame_key = key
        lif = self.lif
        channels = tuple(self.channel_states)
        series_index, z, t = key
        self.frame_label.setText(f"Loading Z={z}  T={t}...")

        def worker() -> None:
            try:
                with self.lif_lock:
                    if lif is not self.lif:
                        return
                    image = lif.images[series_index]
                    frames = {
                        channel: image.get_frame(z=z, t=t, c=channel)
                        for channel in channels
                    }
                self.bridge.preview_ready.emit((token, key, frames))
            except BaseException as exc:
                self.bridge.preview_failed.emit((token, key, exc))

        threading.Thread(target=worker, daemon=True).start()

    def _preview_ready(self, payload) -> None:
        token, key, frames = payload
        if token != self.preview_token or key != self.pending_frame_key:
            return
        self.pending_frame_key = None
        requested = (
            self.current_series_index(),
            self.z_spin.value(),
            self.time_slider.value(),
        )
        if key[:2] == requested[:2]:
            self.current_frame_key = key
            self.current_frames = frames
            self.compose_current_frames()
        if key != requested:
            QTimer.singleShot(0, self.render_frame)

    def _preview_failed(self, payload) -> None:
        token, key, exc = payload
        if token != self.preview_token or key != self.pending_frame_key:
            return
        self.pending_frame_key = None
        self.frame_label.setText(f"Preview failed: {exc}")
        self.statusBar().showMessage("Preview failed")

    def _populate_preflight(self) -> None:
        assert self.plan is not None and self.source is not None
        summary = self.plan["summary"]
        workflow = self.plan["channel_resolution"]["workflow"]
        size_mb = summary["estimated_uncompressed_pixel_bytes"] / (1024 * 1024)
        self.preflight_label.setText(
            f"{workflow['route'].upper()} / {workflow['status']}\n"
            f"{summary['series_count']} series | {summary['output_unit_count']} TIFF units | "
            f"{summary['plane_count']} planes\n~{size_mb:.1f} MiB uncompressed pixels"
        )
        review = workflow["route"] == "review"
        with QSignalBlocker(self.review_check):
            self.review_check.setEnabled(review)
            self.review_check.setChecked(
                self.review_acknowledged_by_source.get(self.source, False)
            )
        channels = self.plan["channel_resolution"]["channels"]
        self.identity_table.setRowCount(len(channels))
        for row, channel in enumerate(channels):
            key = channel["channel_key"]
            channel_index = int(key[1:])
            candidate = suggested_assignment(channel) or "-"
            confirmed = channel.get("confirmed_assignment")
            if isinstance(confirmed, dict):
                confirmed = confirmed.get("id")
            assignment = self.channel_overrides.get(channel_index, confirmed or "unconfirmed")
            for column, value in enumerate((key, candidate, assignment)):
                item = QTableWidgetItem(str(value))
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.identity_table.setItem(row, column, item)
        if channels:
            self.identity_table.selectRow(0)
        suggested = self.source.parent / self.plan["output_policy"]["suggested_directory_name"]
        self.output_edit.setText(str(suggested))
        self.dry_run_button.setEnabled(True)
        self._refresh_batch_controls()

    def _selected_conversion_sources(self) -> list[Path]:
        return self._selected_stage_sources()

    def _review_toggled(self, checked: bool) -> None:
        if self.source is not None:
            self.review_acknowledged_by_source[self.source] = checked
        self._refresh_conversion_gate()

    def _source_ready(self, source: Path) -> bool:
        plan = self.plan_cache.get(source)
        if plan is None:
            return False
        overrides = self.channel_overrides_by_source.get(source, {})
        if unconfirmed_channel_keys(plan, overrides):
            return False
        workflow = plan["channel_resolution"]["workflow"]
        return (
            workflow["route"] != "review"
            or self.review_acknowledged_by_source.get(source, False)
        )

    def _conversion_gate_ready(self) -> bool:
        sources = self._selected_conversion_sources()
        return (
            bool(sources)
            and all(self._source_ready(source) for source in sources)
            and all(
                report["status"] == "ready"
                for _, _, report in self._selected_dry_run_reports()
            )
        )

    def _refresh_batch_controls(self) -> None:
        if not hasattr(self, "identity_status"):
            return
        sources = self._selected_conversion_sources()
        multiple = len(sources) > 1
        busy = self._conversion_running()
        self.output_edit.setEnabled(not busy and not multiple)
        self.browse_button.setEnabled(not busy and not multiple)
        if multiple:
            self.output_mode_label.setText(
                f"Batch: {len(sources)} LIFs | per-source suggested _tiff output"
            )
        else:
            self.output_mode_label.setText("Single LIF: output path shown above")
        missing = [source for source in sources if source not in self.plan_cache]
        suggestion_count = 0
        for source in sources:
            plan = self.plan_cache.get(source)
            if plan is None:
                continue
            overrides = self.channel_overrides_by_source.get(source, {})
            suggestion_count += sum(
                suggested_assignment(channel) is not None
                and int(channel["channel_key"][1:]) not in overrides
                for channel in plan["channel_resolution"]["channels"]
            )
        self.accept_suggestions_button.setText(
            f"Accept {suggestion_count} suggestions"
            + (f" ({len(sources)} LIFs)" if multiple else "")
        )
        self.accept_suggestions_button.setEnabled(
            not busy and not missing and suggestion_count > 0
        )
        self.dry_run_button.setEnabled(not busy and bool(sources) and not missing)
        self._refresh_conversion_gate()

    def _refresh_conversion_gate(self) -> None:
        if not hasattr(self, "identity_status"):
            return
        resume_mode = self.resume_check.isChecked()
        self.convert_button.setText("Resume" if resume_mode else "Convert")
        sources = self._selected_conversion_sources()
        if not sources:
            self.identity_status.setText("Confirmed 0 / 0 channels")
            self.convert_button.setEnabled(False)
            self.convert_button.setToolTip(
                "Select one or more original .lif files from the input queue."
            )
            return
        missing = [source for source in sources if source not in self.plan_cache]
        if missing:
            self.identity_status.setText(
                f"Reading metadata for {len(missing)} / {len(sources)} selected LIFs"
            )
            self.identity_status.setStyleSheet("color: #65717b;")
            self.convert_button.setEnabled(False)
            self.convert_button.setToolTip(
                "Wait for metadata inspection to finish before converting."
            )
            return
        total_count = 0
        unconfirmed_keys = []
        review_pending = []
        for source in sources:
            plan = self.plan_cache[source]
            channels = plan["channel_resolution"]["channels"]
            total_count += len(channels)
            unresolved = unconfirmed_channel_keys(
                plan, self.channel_overrides_by_source.get(source, {})
            )
            unconfirmed_keys.extend(unresolved)
            if (
                plan["channel_resolution"]["workflow"]["route"] == "review"
                and not self.review_acknowledged_by_source.get(source, False)
            ):
                review_pending.append(source.name)
        confirmed_count = total_count - len(unconfirmed_keys)
        if unconfirmed_keys:
            if len(sources) == 1:
                detail = "Required: " + ", ".join(unconfirmed_keys)
            else:
                detail = f"Unresolved: {len(unconfirmed_keys)} channels; see Dry Run for each LIF"
            self.identity_status.setText(
                f"Selected {len(sources)} LIFs | Confirmed {confirmed_count} / {total_count} "
                f"channels | {detail}"
            )
            self.identity_status.setStyleSheet("color: #9b2c2c;")
            convert_reason = "Confirm every selected channel identity before converting."
        elif review_pending:
            self.identity_status.setText(
                f"Selected {len(sources)} LIFs | Channels {confirmed_count} / {total_count} "
                f"confirmed | Acquisition review required for {len(review_pending)} LIF(s)"
            )
            self.identity_status.setStyleSheet("color: #9b2c2c;")
            convert_reason = "Acknowledge the acquisition review before converting."
        else:
            reports = self._selected_dry_run_reports()
            blocked_reports = [
                report for _, _, report in reports if report["status"] != "ready"
            ]
            blocked = len(blocked_reports)
            if blocked:
                if len(sources) == 1 and blocked_reports[0]["errors"]:
                    convert_reason = blocked_reports[0]["errors"][0]
                    self.identity_status.setText(
                        f"Selected 1 LIF | Confirmed {confirmed_count} / {total_count} "
                        f"channels | Blocked: {convert_reason}"
                    )
                else:
                    convert_reason = (
                        "Dry Run is blocked. Open its report for the exact reason."
                    )
                    self.identity_status.setText(
                        f"Selected {len(sources)} LIFs | Confirmed {confirmed_count} / "
                        f"{total_count} channels | Dry Run blocked for {blocked} LIF(s)"
                    )
                self.identity_status.setStyleSheet("color: #9b2c2c;")
            else:
                self.identity_status.setText(
                    f"Selected {len(sources)} LIFs | Confirmed {confirmed_count} / "
                    f"{total_count} channels | Ready"
                )
                self.identity_status.setStyleSheet("color: #176b5b;")
                convert_reason = (
                    "Resume the matching interrupted .partial project."
                    if resume_mode
                    else "Convert the selected original .lif file(s)."
                )
        ready = not self._conversion_running() and self._conversion_gate_ready()
        self.convert_button.setEnabled(ready)
        self.convert_button.setToolTip(convert_reason)

    def apply_assignment(self) -> None:
        row = self.identity_table.currentRow()
        if row < 0:
            return
        key = self.identity_table.item(row, 0).text()
        channel = int(key[1:])
        value = self.assignment_combo.currentText()
        if value == "unconfirmed":
            self.channel_overrides.pop(channel, None)
        else:
            self.channel_overrides[channel] = value
        self.identity_table.item(row, 2).setText(value)
        self._refresh_conversion_gate()

    def accept_suggestions(self) -> None:
        sources = self._selected_conversion_sources()
        if not sources or any(source not in self.plan_cache for source in sources):
            return
        accepted = 0
        for source in sources:
            overrides = self.channel_overrides_by_source.setdefault(source, {})
            for channel in self.plan_cache[source]["channel_resolution"]["channels"]:
                value = suggested_assignment(channel)
                if value is None:
                    continue
                index = int(channel["channel_key"][1:])
                if index not in overrides:
                    overrides[index] = value
                    accepted += 1
        if self.source is not None:
            self.channel_overrides = self.channel_overrides_by_source.setdefault(
                self.source, {}
            )
            for row in range(self.identity_table.rowCount()):
                key = self.identity_table.item(row, 0).text()
                index = int(key[1:])
                self.identity_table.item(row, 2).setText(
                    self.channel_overrides.get(index, "unconfirmed")
                )
        self.identity_selection_changed()
        self._refresh_batch_controls()
        self.statusBar().showMessage(
            f"Accepted {accepted} channel suggestion(s) for {len(sources)} LIF file(s)"
        )

    def identity_selection_changed(self) -> None:
        row = self.identity_table.currentRow()
        if row < 0 or self.identity_table.item(row, 2) is None:
            return
        value = self.identity_table.item(row, 2).text()
        index = self.assignment_combo.findText(value)
        self.assignment_combo.setCurrentIndex(max(0, index))

    def choose_output(self) -> None:
        parent = QFileDialog.getExistingDirectory(self, "Choose output parent directory")
        if parent and self.plan is not None:
            self.output_edit.setText(
                str(Path(parent) / self.plan["output_policy"]["suggested_directory_name"])
            )

    def _current_output(self) -> Path | None:
        text = self.output_edit.text().strip()
        return Path(text).expanduser().resolve() if text else None

    def _output_for_source(self, source: Path, sources: list[Path]) -> Path | None:
        plan = self.plan_cache.get(source)
        if plan is None:
            return None
        if len(sources) == 1 and source == self.source:
            return self._current_output()
        return source.parent / plan["output_policy"]["suggested_directory_name"]

    def _selected_dry_run_reports(self) -> list[tuple[Path, Path | None, dict]]:
        sources = self._selected_conversion_sources()
        reports = []
        for source in sources:
            plan = self.plan_cache[source]
            output = self._output_for_source(source, sources)
            reports.append(
                (
                    source,
                    output,
                    dry_run_report(
                        plan,
                        output,
                        review_acknowledged=self.review_acknowledged_by_source.get(
                            source, False
                        ),
                        resume=self.resume_check.isChecked(),
                        channel_overrides=self.channel_overrides_by_source.get(
                            source, {}
                        ),
                    ),
                )
            )
        by_source = {source: report for source, _, report in reports}
        collisions = find_batch_output_collisions(
            [(source, output) for source, output, _ in reports]
        )
        for collision in collisions:
            targets = "; ".join(
                f"{source.name} -> {output.name}"
                for source, output in collision["targets"]
            )
            message = (
                "Batch output collision under Windows-compatible comparison: "
                f"{targets}."
            )
            for source in collision["sources"]:
                report = by_source[source]
                report["errors"].append(message)
                report["status"] = "blocked"
                report["batch_output_collision"] = True
        apply_batch_storage_constraints(reports)
        return reports

    def show_dry_run(self) -> None:
        sources = self._selected_conversion_sources()
        if not sources:
            QMessageBox.warning(self, APP_NAME, "Select one or more queued LIF files.")
            return
        missing = [source for source in sources if source not in self.plan_cache]
        if missing:
            QMessageBox.information(
                self,
                APP_NAME,
                "Metadata inspection is still running for selected LIF files.",
            )
            return
        reports = self._selected_dry_run_reports()
        blocked = sum(report["status"] != "ready" for _, _, report in reports)
        total_size = sum(
            report["estimated_uncompressed_pixel_bytes"]
            for _, _, report in reports
        )
        total_units = sum(report["output_unit_count"] for _, _, report in reports)
        total_planes = sum(report["plane_count"] for _, _, report in reports)
        lines = [
            f"BATCH DRY RUN: {'BLOCKED' if blocked else 'READY'}",
            "",
            f"Selected LIFs: {len(reports)} | Ready: {len(reports) - blocked} | Blocked: {blocked}",
            f"TIFF units: {total_units} | Planes: {total_planes}",
            f"Estimated uncompressed pixels: {total_size / (1024 * 1024):.1f} MiB",
        ]
        for index, (source, output, report) in enumerate(reports, 1):
            lines.extend(
                [
                    "",
                    f"[{index}/{len(reports)}] {report['status'].upper()} | {source.name}",
                    f"Output: {output}",
                    f"Workflow: {report['workflow_route']} | Series: {report['series_count']} | "
                    f"TIFF units: {report['output_unit_count']} | Planes: {report['plane_count']}",
                    f"Output state: {report['output_state']}",
                ]
            )
            storage = report.get("storage")
            if storage and storage.get("free_bytes") is not None:
                lines.append(
                    "Storage: "
                    f"~{_human_bytes(storage['required_bytes'])} required | "
                    f"{_human_bytes(storage['free_bytes'])} free"
                )
            if report["errors"]:
                lines.extend(f"BLOCK: {item}" for item in report["errors"])
            if report["warnings"]:
                lines.extend(f"WARN: {item}" for item in report["warnings"])
        lines.extend(["", "No image pixels were read and no output files were written."])
        dialog = QDialog(self)
        dialog.setWindowTitle("Selected LIFs Dry Run Report")
        dialog.resize(820, 600)
        layout = QVBoxLayout(dialog)
        text = QPlainTextEdit("\n".join(lines))
        text.setReadOnly(True)
        layout.addWidget(text)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()

    def start_conversion(self) -> None:
        sources = self._selected_conversion_sources()
        if not sources:
            QMessageBox.warning(self, APP_NAME, "Select one or more queued LIF files.")
            return
        if any(source not in self.plan_cache for source in sources):
            QMessageBox.information(
                self,
                APP_NAME,
                "Metadata inspection is still running for selected LIF files.",
            )
            return
        reports = self._selected_dry_run_reports()
        if any(report["status"] != "ready" for _, _, report in reports):
            QMessageBox.warning(
                self,
                APP_NAME,
                "Selected-LIF Dry Run is blocked. Review its report first.",
            )
            return
        jobs = []
        for source, output, report in reports:
            assert output is not None
            plan = self.plan_cache[source]
            jobs.append(
                {
                    "source": source,
                    "output": output,
                    "overrides": dict(
                        self.channel_overrides_by_source.get(source, {})
                    ),
                    "allow_review": (
                        plan["channel_resolution"]["workflow"]["route"] == "review"
                        and self.review_acknowledged_by_source.get(source, False)
                    ),
                    "output_units": int(report["output_unit_count"]),
                    "plane_count": int(report["plane_count"]),
                }
            )
        resume = self.resume_check.isChecked()
        self.cancel_event.clear()
        self.play_timer.stop()
        self.preview_token += 1
        self.pending_frame_key = None
        with self.lif_lock:
            if self.lif is not None:
                self.lif.close()
                self.lif = None
        self.log.clear()
        self.progress.setValue(0)
        self._set_conversion_busy(True)
        for job in jobs:
            self._set_item_status(job["source"], "Queued for conversion")
        self.statusBar().showMessage(
            f"Starting sequential conversion of {len(jobs)} LIF file(s)"
        )

        def worker() -> None:
            active_source = None
            try:
                outputs = []
                completed_units = 0
                completed_planes = 0
                total_units = sum(job["output_units"] for job in jobs)
                total_planes = sum(job["plane_count"] for job in jobs)
                for job_index, job in enumerate(jobs, 1):
                    if self.cancel_event.is_set():
                        raise ConversionCancelled("batch conversion cancelled")
                    active_source = job["source"]
                    self.bridge.item_status.emit((active_source, "Converting"))

                    def progress_callback(
                        message: str,
                        *,
                        batch_index=job_index,
                        completed_tiffs=completed_units,
                        completed_plane_count=completed_planes,
                    ) -> None:
                        plane_match = re.search(r"Plane progress: (\d+)/(\d+)", message)
                        if plane_match:
                            overall_planes = completed_plane_count + int(
                                plane_match.group(1)
                            )
                            self.bridge.progress.emit(
                                f"Batch {batch_index}/{len(jobs)} | {message} | "
                                f"Overall planes {overall_planes}/{total_planes}"
                            )
                        else:
                            tiff_match = re.search(r"TIFF progress: (\d+)/(\d+)", message)
                            if tiff_match:
                                overall_tiffs = completed_tiffs + int(tiff_match.group(1))
                                self.bridge.progress.emit(
                                    f"Batch {batch_index}/{len(jobs)} | {message} | "
                                    f"Overall TIFF {overall_tiffs}/{total_units}"
                                )
                            else:
                                self.bridge.progress.emit(
                                    f"Batch {batch_index}/{len(jobs)} | {message}"
                                )

                    result = convert_lif(
                        job["source"],
                        job["output"],
                        resume=resume,
                        channel_overrides=job["overrides"],
                        allow_review=job["allow_review"],
                        progress_callback=progress_callback,
                        cancel_callback=self.cancel_event.is_set,
                    )
                    outputs.append((job["source"], result))
                    completed_units += job["output_units"]
                    completed_planes += job["plane_count"]
                    self.bridge.item_status.emit((active_source, "Converted"))
                self.bridge.completed.emit(outputs)
            except BaseException as exc:
                self.bridge.failed.emit((exc, active_source))

        self.conversion_thread = threading.Thread(target=worker, daemon=True)
        self.conversion_thread.start()

    def cancel_conversion(self) -> None:
        if self._conversion_running():
            self.cancel_event.set()
            self.cancel_button.setEnabled(False)
            self.log.appendPlainText("Cancellation requested; finishing current TIFF unit.")

    def _conversion_running(self) -> bool:
        return self.conversion_thread is not None and self.conversion_thread.is_alive()

    def _conversion_progress(self, message: str) -> None:
        self.log.appendPlainText(message)
        self.statusBar().showMessage(message)
        match = re.search(r"Overall planes (\d+)/(\d+)", message)
        if match is None:
            match = re.search(r"Plane progress: (\d+)/(\d+)", message)
        if match:
            done, total = map(int, match.groups())
            self.progress.setValue(round(100 * done / total))

    def _restore_after_conversion(self) -> None:
        self.conversion_thread = None
        self._set_conversion_busy(False)
        if self.source is not None and not self.close_after_cancel:
            source = self.source
            self.load_lif(source)

    def _set_conversion_busy(self, busy: bool) -> None:
        self.centralWidget().setEnabled(not busy)
        self.viewer_controls.setEnabled(not busy)
        self.queue_panel.setEnabled(not busy)
        self.identity_table.setEnabled(not busy)
        self.assignment_combo.setEnabled(not busy)
        self.apply_button.setEnabled(not busy)
        self.accept_suggestions_button.setEnabled(False)
        self.output_edit.setEnabled(False)
        self.browse_button.setEnabled(False)
        self.resume_check.setEnabled(not busy)
        self.review_check.setEnabled(
            not busy
            and self.plan is not None
            and self.plan["channel_resolution"]["workflow"]["route"] == "review"
        )
        self.convert_button.setEnabled(False)
        self.dry_run_button.setEnabled(False)
        self.cancel_button.setEnabled(busy and not self.cancel_event.is_set())
        if not busy:
            self._refresh_batch_controls()

    def _batch_item_status(self, payload) -> None:
        source, status = payload
        self._set_item_status(source, status)

    def _conversion_completed(self, outputs: list[tuple[Path, Path]]) -> None:
        self.progress.setValue(100)
        for source, output in outputs:
            self.log.appendPlainText(f"Conversion complete: {source.name} -> {output}")
            self._set_item_status(source, "Converted")
        self._restore_after_conversion()
        if self.close_after_cancel:
            QTimer.singleShot(0, self.close)
        else:
            QMessageBox.information(
                self,
                APP_NAME,
                f"Conversion complete for {len(outputs)} LIF file(s).",
            )

    def _conversion_failed(self, payload) -> None:
        exc, active_source = payload
        if isinstance(exc, ConversionCancelled):
            self.log.appendPlainText("Conversion cancelled; .partial project retained.")
            self._set_item_status(active_source, "Partial")
            if not self.close_after_cancel:
                QMessageBox.information(self, APP_NAME, "Conversion cancelled safely.")
        else:
            self.log.appendPlainText(f"Conversion failed: {exc}")
            self._set_item_status(active_source, "Failed")
            if not self.close_after_cancel:
                QMessageBox.critical(self, APP_NAME, str(exc))
        self._restore_after_conversion()
        if self.close_after_cancel:
            QTimer.singleShot(0, self.close)

    def export_png(self) -> None:
        pixmap = self.view.item.pixmap()
        if pixmap.isNull():
            QMessageBox.information(self, APP_NAME, "Load an image preview first.")
            return
        filename, _ = QFileDialog.getSaveFileName(self, "Export displayed composite", "", "PNG (*.png)")
        if filename:
            path = Path(filename)
            if path.suffix.lower() != ".png":
                path = path.with_suffix(".png")
            pixmap.save(str(path), "PNG")

    def show_presentation_export(self) -> None:
        if self.plan is None or self.lif is None or self._presentation_running():
            QMessageBox.information(self, APP_NAME, "Load an acquisition or converted project first.")
            return
        visible = {
            channel for channel, state in self.channel_states.items() if state.get("visible")
        }
        timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        if self.active_project is not None:
            default_parent = self.active_project.parent
            source_name = self.active_project.name.removesuffix("_tiff")
        elif self.source is not None:
            default_parent = self.source.parent
            source_name = self.source.stem
        else:
            default_parent = Path.home()
            source_name = "presentation"
        dialog = PresentationExportDialog(
            self.plan,
            visible,
            self.use_lut.isChecked(),
            default_parent,
            f"{source_name}_presentation_{timestamp}",
            self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        output = dialog.output_path()
        scope = str(dialog.scope_combo.currentData())
        contrast_mode = str(dialog.contrast_combo.currentData())
        channels = dialog.selected_channels()
        current_states: dict[tuple[int, int], dict] = {}
        for series in self.plan["series"]:
            series_index = int(series["series_index"])
            for item in series["outputs"]:
                channel = int(item["channel_index"])
                state = self.series_channel_states.get((series_index, channel))
                if state is not None:
                    current_states[(series_index, channel)] = dict(state)
                elif channel in self.synced_contrast:
                    current_states[(series_index, channel)] = dict(
                        self.synced_contrast[channel]
                    )
        current_frame = (
            self.current_series_index(),
            self.z_spin.value(),
            self.time_slider.value(),
        )
        adapter = self.lif
        plan = self.plan
        use_lut = dialog.lut_check.isChecked()
        self.presentation_status.setText(f"Exporting to {output.name}...")
        self.batch_overlay_button.setEnabled(False)
        self.queue_panel.setEnabled(False)

        def worker() -> None:
            try:
                with self.lif_lock:
                    result = export_presentations(
                        adapter=adapter,
                        plan=plan,
                        output=output,
                        channels=channels,
                        scope=scope,
                        contrast_mode=contrast_mode,
                        use_lut=use_lut,
                        current_frame=current_frame,
                        current_states=current_states,
                        progress=self.bridge.presentation_progress.emit,
                    )
                self.bridge.presentation_completed.emit(result)
            except BaseException as exc:
                self.bridge.presentation_failed.emit(exc)

        self.presentation_thread = threading.Thread(target=worker, daemon=True)
        self.presentation_thread.start()

    def _presentation_running(self) -> bool:
        return self.presentation_thread is not None and self.presentation_thread.is_alive()

    def _presentation_progress(self, message: str) -> None:
        self.presentation_status.setText(message)
        self.statusBar().showMessage(message)

    def _presentation_completed(self, output: Path) -> None:
        self.presentation_thread = None
        self.presentation_status.setText(f"Export complete: {output.name}")
        self.batch_overlay_button.setEnabled(True)
        self.queue_panel.setEnabled(True)
        self.statusBar().showMessage("Presentation export complete")
        QMessageBox.information(self, APP_NAME, f"Presentation export complete:\n{output}")

    def _presentation_failed(self, exc: BaseException) -> None:
        self.presentation_thread = None
        self.presentation_status.setText(f"Export failed: {exc}")
        self.batch_overlay_button.setEnabled(True)
        self.queue_panel.setEnabled(True)
        self.statusBar().showMessage("Presentation export failed")
        QMessageBox.critical(self, APP_NAME, str(exc))

    def remove_selected(self) -> None:
        if self._conversion_running() or self._presentation_running():
            return
        selected = list(self.queue.selectedItems())
        active_removed = any(
            self.source == Path(item.data(Qt.ItemDataRole.UserRole)) for item in selected
        )
        blocker = QSignalBlocker(self.queue)
        for item in selected:
            path = Path(item.data(Qt.ItemDataRole.UserRole))
            self.input_items.pop(path, None)
            self.plan_cache.pop(path, None)
            self.channel_overrides_by_source.pop(path, None)
            self.review_acknowledged_by_source.pop(path, None)
            self.queue.takeItem(self.queue.row(item))
        del blocker
        self.queue_summary.setText(f"{len(self.input_items)} LIF file(s)")
        if active_removed:
            self._reset_active_input()
            if self.queue.count():
                self.queue.setCurrentRow(0)
        else:
            self._refresh_stage_map()

    def clear_inputs(self) -> None:
        if self._conversion_running() or self._presentation_running():
            return
        with QSignalBlocker(self.queue):
            self.queue.clear()
        self.input_items.clear()
        self.plan_cache.clear()
        self.channel_overrides_by_source.clear()
        self.review_acknowledged_by_source.clear()
        self.queue_summary.setText("Drop LIF files or folders")
        self._reset_active_input()

    def _reset_active_input(self) -> None:
        self.plan_token += 1
        self.preview_token += 1
        self.pending_source = None
        self.active_project = None
        self.pending_frame_key = None
        self.play_timer.stop()
        self.play_button.setText("Play")
        with self.lif_lock:
            if self.lif is not None:
                self.lif.close()
                self.lif = None
        self.source = None
        self.plan = None
        self.current_frames.clear()
        self.current_frame_key = None
        self.channel_states.clear()
        self.channel_checks.clear()
        self.channel_visibility.clear()
        self.series_channel_states.clear()
        self.synced_contrast.clear()
        self.series_playback_fps.clear()
        self.active_contrast_channel = None
        while self.channels_layout.count():
            child = self.channels_layout.takeAt(0)
            if child.widget() is not None and child.widget() is not self.use_lut:
                child.widget().deleteLater()
        self.channels_layout.addWidget(self.use_lut)
        self.series_combo.clear()
        self.series_table.setRowCount(0)
        self._clear_contrast_channel_buttons()
        self.view.clear_image()
        self.path_label.setText("No LIF loaded")
        self.path_label.setToolTip("")
        self.frame_label.setText("Z=0  T=0")
        self.z_spacing_um = None
        self.z_summary.setText("Z plane 1 / 1")
        self.time_increment_s = None
        self.configured_cycle_s = None
        self.display_time_interval_s = None
        with QSignalBlocker(self.time_spin):
            self.time_spin.setRange(1, 1)
            self.time_spin.setValue(1)
        self.time_total_label.setText("/ 1")
        self.time_summary.setText("Time unavailable")
        self.interval_label.setText("Time interval: unavailable")
        self.preflight_label.setText("Select a queued LIF.")
        self.review_check.setChecked(False)
        self.review_check.setEnabled(False)
        self.identity_table.setRowCount(0)
        self.identity_status.setText("Confirmed 0 / 0 channels")
        self.identity_status.setStyleSheet("")
        self.accept_suggestions_button.setText("Accept suggestions")
        self.accept_suggestions_button.setEnabled(False)
        self.output_edit.clear()
        self.output_mode_label.setText("Single LIF: output path shown above")
        self.dry_run_button.setEnabled(False)
        self.convert_button.setEnabled(False)
        self.convert_button.setToolTip(
            "Select one or more original .lif files from the input queue."
        )
        self.statusBar().showMessage("Ready")
        self.tools_tabs.setTabEnabled(self.convert_tools_index, True)
        self._refresh_stage_map()

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802 - Qt API
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802 - Qt API
        paths = [Path(url.toLocalFile()) for url in event.mimeData().urls() if url.isLocalFile()]
        projects = [path for path in paths if is_tiff_project(path)]
        if len(paths) == 1 and projects:
            self.open_project(projects[0])
        else:
            self.add_inputs(paths)
        event.acceptProposedAction()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt API
        if self._presentation_running():
            QMessageBox.information(
                self,
                APP_NAME,
                "A presentation export is still running. Wait for it to finish before closing.",
            )
            event.ignore()
            return
        if self._conversion_running():
            decision = QMessageBox.question(
                self,
                APP_NAME,
                "A conversion is running. Request safe cancellation and close after the current TIFF unit?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if decision == QMessageBox.StandardButton.Yes:
                self.close_after_cancel = True
                self.cancel_conversion()
                self.statusBar().showMessage("Waiting for safe cancellation before closing")
            event.ignore()
            return
        self.plan_token += 1
        self.preview_token += 1
        with self.lif_lock:
            if self.lif is not None:
                self.lif.close()
                self.lif = None
        event.accept()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {SOFTWARE_VERSION}",
    )
    parser.add_argument("inputs", nargs="*", type=Path)
    parser.add_argument("--smoke-test", action="store_true")
    parser.add_argument("--screenshot", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    app = QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(SOFTWARE_VERSION)
    window = QtWorkbench()
    window.show()
    if args.inputs:
        QTimer.singleShot(0, lambda: window.add_inputs(args.inputs))
    if args.smoke_test:
        QTimer.singleShot(300, app.quit)
    elif args.screenshot is not None:
        output = args.screenshot.expanduser().resolve()
        if output.exists():
            raise SystemExit(f"refusing to overwrite screenshot: {output}")
        attempts = 0

        def capture() -> None:
            nonlocal attempts
            attempts += 1
            if window.plan is not None and window.channel_states and window.current_frames:
                output.parent.mkdir(parents=True, exist_ok=True)
                window.grab().save(str(output))
                print(output)
                app.quit()
                return
            if attempts >= 120:
                print("Qt GUI acceptance screenshot timed out", file=sys.stderr)
                app.exit(1)
                return
            QTimer.singleShot(100, capture)

        QTimer.singleShot(500, capture)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
