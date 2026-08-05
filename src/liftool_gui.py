#!/usr/bin/env python3
"""Linux desktop workbench for LIF preflight, preview, and conversion."""

from __future__ import annotations

import argparse
import os
import queue
import re
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import numpy as np
from PIL import Image, ImageGrab, ImageTk

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
except ImportError:  # The launcher supplies the sandbox-local optional dependency.
    DND_FILES = None
    TkinterDnD = None

from channel_resolver import load_registry
from liffile_adapter import LifFileAdapter
from liftool import (
    ConversionCancelled,
    LifToolError,
    build_conversion_plan,
    convert_lif,
)


APP_NAME = "LIF2TIFF Workbench"
PALETTE = {
    "window": "#eef1f4",
    "panel": "#f8f9fa",
    "surface": "#ffffff",
    "border": "#c9d0d7",
    "text": "#1d2730",
    "muted": "#5f6b76",
    "accent": "#176b5b",
    "accent_active": "#0f5648",
    "warning": "#8a5a00",
    "warning_bg": "#fff3cd",
    "preview": "#14181c",
}


def discover_lif_paths(inputs: list[Path]) -> list[Path]:
    """Return unique LIF files from explicit files and recursively scanned folders."""
    discovered: dict[Path, None] = {}
    for raw_path in inputs:
        path = raw_path.expanduser().resolve()
        if path.is_file():
            if path.suffix.lower() == ".lif":
                discovered[path] = None
            continue
        if not path.is_dir():
            continue
        for root, directories, filenames in os.walk(path, followlinks=False):
            directories.sort(key=str.casefold)
            for filename in sorted(filenames, key=str.casefold):
                if filename.lower().endswith(".lif"):
                    discovered[Path(root, filename).resolve()] = None
    return list(discovered)


def build_dry_run_report(
    plan: dict,
    output: Path | None,
    *,
    review_acknowledged: bool,
    resume: bool,
    channel_overrides: dict[int, str],
) -> dict:
    """Evaluate conversion readiness without reading pixels or writing output."""
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
            warnings.append("A matching .partial project will be checked for resume compatibility.")
            output_state = "partial_resume"
        else:
            output_state = "new"
    unconfirmed = [
        channel["channel_key"]
        for channel in plan["channel_resolution"]["channels"]
        if int(channel["channel_key"][1:]) not in channel_overrides
        and not channel.get("confirmed_assignment")
    ]
    if unconfirmed:
        warnings.append(f"Unconfirmed channel identities: {', '.join(unconfirmed)}.")
    summary = plan["summary"]
    return {
        "status": "blocked" if errors else "ready",
        "errors": errors,
        "warnings": warnings,
        "workflow_route": workflow["route"],
        "output_state": output_state,
        "series_count": summary["series_count"],
        "output_unit_count": summary["output_unit_count"],
        "plane_count": summary["plane_count"],
        "estimated_uncompressed_pixel_bytes": summary["estimated_uncompressed_pixel_bytes"],
        "unconfirmed_channels": unconfirmed,
    }


class RangeSlider(tk.Canvas):
    """Compact two-handle numeric range control."""

    def __init__(self, parent, *, command, **kwargs):
        super().__init__(
            parent,
            height=30,
            bg=PALETTE["surface"],
            highlightthickness=1,
            highlightbackground=PALETTE["border"],
            cursor="hand2",
            **kwargs,
        )
        self.command = command
        self.minimum = 0.0
        self.maximum = 255.0
        self.low = 0.0
        self.high = 255.0
        self.active_handle = "low"
        self.padding = 12
        self.bind("<Configure>", lambda _event: self._draw())
        self.bind("<Button-1>", self._press)
        self.bind("<B1-Motion>", self._drag)
        self.bind("<ButtonRelease-1>", lambda _event: self.configure(cursor="hand2"))

    def set_range(
        self,
        minimum: float,
        maximum: float,
        low: float,
        high: float,
    ) -> None:
        self.minimum = float(minimum)
        self.maximum = max(float(maximum), self.minimum + 1.0)
        self.low = min(max(float(low), self.minimum), self.maximum)
        self.high = min(max(float(high), self.low), self.maximum)
        self._draw()

    def set_values(self, low: float, high: float) -> None:
        self.low = min(max(float(low), self.minimum), self.maximum)
        self.high = min(max(float(high), self.minimum), self.maximum)
        if self.low > self.high:
            if self.active_handle == "low":
                self.low = self.high
            else:
                self.high = self.low
        self._draw()

    def _value_to_x(self, value: float) -> float:
        usable = max(1.0, self.winfo_width() - 2 * self.padding)
        fraction = (value - self.minimum) / (self.maximum - self.minimum)
        return self.padding + fraction * usable

    def _x_to_value(self, x: float) -> float:
        usable = max(1.0, self.winfo_width() - 2 * self.padding)
        fraction = min(max((x - self.padding) / usable, 0.0), 1.0)
        return self.minimum + fraction * (self.maximum - self.minimum)

    def _draw(self) -> None:
        self.delete("all")
        width = max(1, self.winfo_width())
        middle = 15
        low_x = self._value_to_x(self.low)
        high_x = self._value_to_x(self.high)
        self.create_rectangle(
            self.padding,
            middle - 3,
            width - self.padding,
            middle + 3,
            fill="#d7dde2",
            outline="",
        )
        self.create_rectangle(
            low_x,
            middle - 3,
            high_x,
            middle + 3,
            fill=PALETTE["accent"],
            outline="",
        )
        for x in (low_x, high_x):
            self.create_rectangle(
                x - 5,
                middle - 10,
                x + 5,
                middle + 10,
                fill=PALETTE["surface"],
                outline=PALETTE["accent"],
                width=2,
            )

    def _press(self, event) -> None:
        low_distance = abs(event.x - self._value_to_x(self.low))
        high_distance = abs(event.x - self._value_to_x(self.high))
        self.active_handle = "low" if low_distance <= high_distance else "high"
        self.configure(cursor="sb_h_double_arrow")
        self._move_active(event.x)

    def _drag(self, event) -> None:
        self._move_active(event.x)

    def _move_active(self, x: float) -> None:
        value = self._x_to_value(x)
        if self.active_handle == "low":
            self.low = min(value, self.high)
        else:
            self.high = max(value, self.low)
        self._draw()
        self.command(self.low, self.high)


def _percentile_limits(frame: np.ndarray) -> tuple[float, float]:
    lower, upper = np.percentile(frame, (1, 99))
    lower = float(lower)
    upper = float(upper)
    if upper <= lower:
        upper = lower + 1.0
    return lower, upper


def _normalize_frame(
    frame: np.ndarray,
    black: float,
    white: float,
    gamma: float = 1.0,
) -> np.ndarray:
    if white <= black:
        white = black + 1.0
    normalized = (frame.astype(np.float32) - float(black)) / float(white - black)
    normalized = np.clip(normalized, 0.0, 1.0)
    gamma = max(float(gamma), 1e-6)
    if gamma != 1.0:
        normalized = np.power(normalized, 1.0 / gamma)
    return normalized


def _validated_zoom_percent(value: str | float, fallback: float = 100.0) -> float:
    try:
        percent = float(value)
    except (TypeError, ValueError):
        percent = float(fallback)
    return min(max(percent, 5.0), 10000.0)


def _lut_color(label: str | None, channel_index: int) -> tuple[float, float, float]:
    text = (label or "").lower()
    for token, color in (
        ("green", (0.1, 1.0, 0.35)),
        ("red", (1.0, 0.15, 0.12)),
        ("blue", (0.2, 0.5, 1.0)),
        ("cyan", (0.1, 0.9, 1.0)),
        ("magenta", (1.0, 0.2, 0.9)),
        ("yellow", (1.0, 0.9, 0.1)),
    ):
        if token in text:
            return color
    fallback = (
        (0.1, 1.0, 0.35),
        (1.0, 0.15, 0.12),
        (0.2, 0.5, 1.0),
        (1.0, 0.9, 0.1),
    )
    return fallback[channel_index % len(fallback)]


def _render_single(
    frame: np.ndarray,
    black: float,
    white: float,
    *,
    color: tuple[float, float, float] | None = None,
    gamma: float = 1.0,
) -> Image.Image:
    normalized = _normalize_frame(frame, black, white, gamma)
    if color is None:
        rgb = np.repeat(normalized[..., None], 3, axis=2)
    else:
        rgb = normalized[..., None] * np.asarray(color, dtype=np.float32)
    return Image.fromarray(np.asarray(np.clip(rgb * 255.0, 0, 255), dtype=np.uint8))


def _render_overlay(
    frames: dict[int, np.ndarray],
    channel_info: dict[int, dict],
    manual_channel: int | None = None,
    manual_limits: tuple[float, float] | None = None,
    *,
    visible_channels: set[int] | None = None,
    settings_by_channel: dict[int, dict] | None = None,
) -> Image.Image:
    selected = {
        index: frame
        for index, frame in frames.items()
        if visible_channels is None or index in visible_channels
    }
    if not selected:
        shape = next(iter(frames.values())).shape
        return Image.new("RGB", (shape[1], shape[0]), (0, 0, 0))
    shape = next(iter(selected.values())).shape
    rgb = np.zeros((*shape, 3), dtype=np.float32)
    fluorescence = []
    brightfield = []
    for index, frame in sorted(selected.items()):
        info = channel_info.get(index, {})
        modality = ((info.get("identity") or {}).get("modality") or {}).get("value")
        settings = (settings_by_channel or {}).get(index)
        if settings is not None:
            limits = (settings["black"], settings["white"])
            gamma = settings.get("gamma", 1.0)
        else:
            limits = (
                manual_limits
                if index == manual_channel and manual_limits is not None
                else _percentile_limits(frame)
            )
            gamma = 1.0
        normalized = _normalize_frame(frame, *limits, gamma)
        if modality == "brightfield":
            brightfield.append(normalized)
        else:
            fluorescence.append((index, normalized, info))
    if brightfield:
        base = np.mean(brightfield, axis=0)
        rgb += base[..., None] * 0.72
    for index, normalized, info in fluorescence:
        color = _lut_color(
            info.get("source_lut") or info.get("physical_label"),
            index,
        )
        rgb += normalized[..., None] * np.asarray(color, dtype=np.float32) * 0.9
    return Image.fromarray(np.asarray(np.clip(rgb * 255.0, 0, 255), dtype=np.uint8))


class LifToolApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_NAME)
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        window_width = min(1440, screen_width)
        window_height = min(880, max(640, screen_height - 80))
        self.root.geometry(f"{window_width}x{window_height}")
        self.root.minsize(min(1100, screen_width), min(700, window_height))
        self.root.configure(bg=PALETTE["window"])

        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.input_paths: list[Path] = []
        self.input_iid_by_path: dict[Path, str] = {}
        self.input_path_by_iid: dict[str, Path] = {}
        self.input_status: dict[Path, str] = {}
        self.next_input_iid = 0
        self.source_path: Path | None = None
        self.plan: dict | None = None
        self.preview_lif: LifFileAdapter | None = None
        self.preview_lock = threading.Lock()
        self.preview_token = 0
        self.current_frames: dict[int, np.ndarray] = {}
        self.current_channel_info: dict[int, dict] = {}
        self.channel_contrast: dict[tuple[Path, int, int], dict] = {}
        self.channel_visibility: dict[tuple[Path, int, int], bool] = {}
        self.layer_vars: dict[int, tk.BooleanVar] = {}
        self.last_rendered_t: int | None = None
        self.rendered_full: Image.Image | None = None
        self.rendered_tk: ImageTk.PhotoImage | None = None
        self.channel_overrides: dict[int, str] = {}
        self.cancel_event = threading.Event()
        self.busy_mode: str | None = None
        self.close_after_cancel = False
        self._updating_contrast = False
        self._updating_time = False
        self.time_scrub_job: str | None = None
        self.view_render_job: str | None = None
        self.view_refine_job: str | None = None
        self.playing = False
        self.playback_job: str | None = None
        self.zoom = 1.0
        self.pan_x = 0.0
        self.pan_y = 0.0
        self.pan_start: tuple[float, float] | None = None

        self.path_var = tk.StringVar(value="No LIF selected")
        self.input_summary_var = tk.StringVar(value="Drop LIF files or folders here")
        self.summary_var = tk.StringVar(value="Open one Leica LIF to begin.")
        self.status_var = tk.StringVar(value="Ready")
        self.series_detail_var = tk.StringVar(value="No series selected")
        self.channel_var = tk.StringVar()
        self.use_lut_var = tk.BooleanVar(value=True)
        self.contrast_mode_var = tk.StringVar(value="Source display")
        self.black_var = tk.DoubleVar(value=0.0)
        self.white_var = tk.DoubleVar(value=255.0)
        self.black_text_var = tk.StringVar(value="Black 0")
        self.white_text_var = tk.StringVar(value="White 255")
        self.z_var = tk.IntVar(value=0)
        self.t_var = tk.IntVar(value=0)
        self.playback_fps_var = tk.StringVar(value="2")
        self.zoom_text_var = tk.StringVar(value="Zoom 100%")
        self.zoom_percent_var = tk.StringVar(value="100")
        self.assignment_var = tk.StringVar(value="unconfirmed")
        self.review_var = tk.BooleanVar(value=False)
        self.review_text_var = tk.StringVar(value="No preflight result")
        self.output_var = tk.StringVar()
        self.resume_var = tk.BooleanVar(value=False)

        self._configure_styles()
        self._build_ui()
        self._register_drop_targets()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(80, self._poll_events)

    def _configure_styles(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(".", font=("DejaVu Sans", 10), foreground=PALETTE["text"])
        style.configure("TFrame", background=PALETTE["panel"])
        style.configure("Surface.TFrame", background=PALETTE["surface"])
        style.configure("Header.TFrame", background=PALETTE["surface"])
        style.configure(
            "Title.TLabel",
            background=PALETTE["surface"],
            foreground=PALETTE["text"],
            font=("DejaVu Sans", 16, "bold"),
        )
        style.configure(
            "Muted.TLabel",
            background=PALETTE["panel"],
            foreground=PALETTE["muted"],
        )
        style.configure(
            "Surface.TLabel",
            background=PALETTE["surface"],
            foreground=PALETTE["text"],
        )
        style.configure(
            "Warning.TLabel",
            background=PALETTE["warning_bg"],
            foreground=PALETTE["warning"],
            padding=8,
        )
        style.configure(
            "Accent.TButton",
            background=PALETTE["accent"],
            foreground="#ffffff",
            padding=(12, 7),
            font=("DejaVu Sans", 10, "bold"),
        )
        style.map(
            "Accent.TButton",
            background=[("active", PALETTE["accent_active"]), ("disabled", "#9aa5ae")],
        )
        style.configure("TButton", padding=(10, 6))
        style.configure("Treeview", rowheight=27, background="#ffffff", fieldbackground="#ffffff")
        style.configure("Treeview.Heading", font=("DejaVu Sans", 9, "bold"))
        style.configure("TLabelframe", background=PALETTE["panel"], bordercolor=PALETTE["border"])
        style.configure("TLabelframe.Label", background=PALETTE["panel"], font=("DejaVu Sans", 10, "bold"))

    def _build_ui(self) -> None:
        header = ttk.Frame(self.root, style="Header.TFrame", padding=(16, 10))
        header.pack(fill="x")
        ttk.Label(header, text=APP_NAME, style="Title.TLabel").pack(side="left")
        self.folder_button = ttk.Button(header, text="Add Folder", command=self._choose_folder)
        self.folder_button.pack(side="right")
        self.open_button = ttk.Button(header, text="Add Files", command=self._choose_lifs)
        self.open_button.pack(side="right", padx=(0, 7))
        ttk.Label(
            header,
            textvariable=self.path_var,
            style="Surface.TLabel",
            anchor="e",
        ).pack(side="right", padx=(16, 14), fill="x", expand=True)

        ttk.Separator(self.root).pack(fill="x")
        panes = ttk.Panedwindow(self.root, orient="horizontal")
        panes.pack(fill="both", expand=True, padx=10, pady=10)

        left = ttk.Frame(panes, padding=8)
        center = ttk.Frame(panes, padding=(4, 8))
        right = ttk.Frame(panes, padding=8)
        panes.add(left, weight=2)
        panes.add(center, weight=5)
        panes.add(right, weight=3)

        self._build_left_panel(left)
        self._build_viewer(center)
        self._build_right_panel(right)
        self._build_status_bar()

    def _build_left_panel(self, parent: ttk.Frame) -> None:
        input_box = ttk.Labelframe(parent, text="Input queue", padding=6)
        input_box.pack(fill="x", pady=(0, 10))
        input_header = ttk.Frame(input_box)
        input_header.pack(fill="x", pady=(0, 5))
        ttk.Label(
            input_header,
            textvariable=self.input_summary_var,
            style="Muted.TLabel",
        ).pack(side="left", fill="x", expand=True)
        self.remove_input_button = ttk.Button(
            input_header,
            text="Remove",
            command=self._remove_selected_inputs,
        )
        self.remove_input_button.pack(side="right")
        self.clear_input_button = ttk.Button(
            input_header,
            text="Clear All",
            command=self._clear_all_inputs,
        )
        self.clear_input_button.pack(side="right", padx=(0, 5))
        self.input_tree = ttk.Treeview(
            input_box,
            columns=("folder", "status"),
            show="tree headings",
            height=5,
            selectmode="extended",
        )
        self.input_tree.heading("#0", text="LIF")
        self.input_tree.heading("folder", text="Folder")
        self.input_tree.heading("status", text="Status")
        self.input_tree.column("#0", width=150, minwidth=100)
        self.input_tree.column("folder", width=145, minwidth=80)
        self.input_tree.column("status", width=72, anchor="center", stretch=False)
        input_scrollbar = ttk.Scrollbar(
            input_box,
            orient="vertical",
            command=self.input_tree.yview,
        )
        self.input_tree.configure(yscrollcommand=input_scrollbar.set)
        self.input_tree.pack(side="left", fill="x", expand=True)
        input_scrollbar.pack(side="right", fill="y")
        self.input_tree.bind("<<TreeviewSelect>>", self._on_input_selected)

        summary = ttk.Labelframe(parent, text="Preflight", padding=10)
        summary.pack(fill="x", pady=(0, 10))
        ttk.Label(
            summary,
            textvariable=self.summary_var,
            style="Muted.TLabel",
            justify="left",
            wraplength=300,
        ).pack(fill="x")

        series_box = ttk.Labelframe(parent, text="Series", padding=6)
        series_box.pack(fill="both", expand=True)
        self.series_tree = ttk.Treeview(
            series_box,
            columns=("index", "type", "size"),
            show="tree headings",
            selectmode="browse",
        )
        self.series_tree.heading("#0", text="Name")
        self.series_tree.heading("index", text="#")
        self.series_tree.heading("type", text="Type")
        self.series_tree.heading("size", text="X x Y")
        self.series_tree.column("#0", width=145, minwidth=105)
        self.series_tree.column("index", width=36, anchor="center", stretch=False)
        self.series_tree.column("type", width=66, anchor="center", stretch=False)
        self.series_tree.column("size", width=118, anchor="center", stretch=False)
        scrollbar = ttk.Scrollbar(series_box, orient="vertical", command=self.series_tree.yview)
        self.series_tree.configure(yscrollcommand=scrollbar.set)
        self.series_tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.series_tree.bind("<<TreeviewSelect>>", self._on_series_selected)

    def _build_viewer(self, parent: ttk.Frame) -> None:
        toolbar = ttk.Frame(parent, padding=(2, 0, 2, 8))
        toolbar.pack(fill="x")
        ttk.Button(toolbar, text="Export PNG", command=self._export_png).pack(side="right")
        ttk.Button(toolbar, text="Fit", command=self._reset_view).pack(side="right", padx=(0, 7))
        ttk.Label(toolbar, text="%").pack(side="right", padx=(3, 8))
        self.zoom_spin = ttk.Spinbox(
            toolbar,
            from_=5,
            to=10000,
            increment=25,
            width=7,
            textvariable=self.zoom_percent_var,
            command=self._zoom_percent_changed,
        )
        self.zoom_spin.pack(side="right")
        self.zoom_spin.bind("<Return>", self._zoom_percent_changed)
        self.zoom_spin.bind("<FocusOut>", self._zoom_percent_changed)
        ttk.Label(toolbar, text="Zoom").pack(side="right", padx=(0, 5))
        ttk.Label(toolbar, textvariable=self.series_detail_var, anchor="w").pack(
            side="left",
            fill="x",
            expand=True,
        )

        self.preview_canvas = tk.Canvas(
            parent,
            bg=PALETTE["preview"],
            highlightthickness=1,
            highlightbackground=PALETTE["border"],
        )
        self.preview_canvas.pack(fill="both", expand=True)
        self.preview_canvas.create_text(
            20,
            20,
            anchor="nw",
            fill="#aab3ba",
            text="Image preview",
            font=("DejaVu Sans", 12),
            tags="preview_message",
        )
        self.preview_canvas.bind("<Configure>", lambda _event: self._fit_rendered_image())
        self.preview_canvas.bind("<MouseWheel>", self._zoom_wheel)
        self.preview_canvas.bind("<Control-MouseWheel>", self._zoom_wheel)
        self.preview_canvas.bind(
            "<Button-4>",
            lambda event: self._zoom_at(event.x, event.y, 1 / 1.12, interactive=True),
        )
        self.preview_canvas.bind(
            "<Button-5>",
            lambda event: self._zoom_at(event.x, event.y, 1.12, interactive=True),
        )
        self.preview_canvas.bind("<ButtonPress-1>", self._pan_start_event)
        self.preview_canvas.bind("<B1-Motion>", self._pan_move_event)
        self.preview_canvas.bind("<ButtonRelease-1>", self._pan_end_event)

        controls = ttk.Frame(parent, padding=(2, 10, 2, 0))
        controls.pack(fill="x")

        self.layer_frame = ttk.Labelframe(controls, text="Visible channels", padding=5)
        self.layer_frame.pack(fill="x", pady=(0, 7))

        row1 = ttk.Frame(controls)
        row1.pack(fill="x", pady=(0, 7))
        ttk.Label(row1, text="Adjust").pack(side="left")
        self.channel_combo = ttk.Combobox(
            row1,
            textvariable=self.channel_var,
            state="readonly",
            width=17,
        )
        self.channel_combo.pack(side="left", padx=(7, 16))
        self.channel_combo.bind("<<ComboboxSelected>>", lambda _event: self._channel_changed())
        ttk.Checkbutton(
            row1,
            text="LUT",
            variable=self.use_lut_var,
            command=self._render_current,
        ).pack(side="left")

        time_row = ttk.Frame(controls)
        time_row.pack(fill="x", pady=(0, 7))
        ttk.Label(time_row, text="Z plane").pack(side="left")
        self.z_spin = ttk.Spinbox(time_row, from_=0, to=0, width=5, textvariable=self.z_var, command=self._request_preview)
        self.z_spin.pack(side="left")
        ttk.Label(time_row, text="Time").pack(side="left", padx=(14, 4))
        self.t_spin = ttk.Spinbox(time_row, from_=0, to=0, width=5, textvariable=self.t_var, command=self._request_preview)
        self.t_spin.pack(side="left")
        self.z_spin.bind("<Return>", lambda _event: self._request_preview())
        self.t_spin.bind("<Return>", lambda _event: self._request_preview())
        self.play_button = ttk.Button(time_row, text="Play", width=6, command=self._toggle_playback)
        self.play_button.pack(side="left", padx=(10, 4))
        self.play_button.state(["disabled"])
        ttk.Label(time_row, text="FPS").pack(side="left")
        self.playback_fps_entry = ttk.Entry(
            time_row,
            textvariable=self.playback_fps_var,
            width=5,
        )
        self.playback_fps_entry.pack(side="left", padx=(4, 0))
        self.playback_fps_entry.bind("<Return>", self._validate_fps)
        self.playback_fps_entry.bind("<FocusOut>", self._validate_fps)

        timeline_row = ttk.Frame(controls)
        timeline_row.pack(fill="x", pady=(0, 7))
        ttk.Label(timeline_row, text="Timeline").pack(side="left")
        self.time_scale = ttk.Scale(
            timeline_row,
            from_=0,
            to=0,
            orient="horizontal",
            command=self._time_scrubbed,
        )
        self.time_scale.pack(side="left", fill="x", expand=True, padx=(8, 0))

        row2 = ttk.Frame(controls)
        row2.pack(fill="x")
        self.contrast_combo = ttk.Combobox(
            row2,
            textvariable=self.contrast_mode_var,
            values=("Source display", "Full range", "Manual"),
            state="readonly",
            width=15,
        )
        self.contrast_combo.pack(side="left")
        self.contrast_combo.bind("<<ComboboxSelected>>", lambda _event: self._contrast_mode_changed())
        self.range_slider = RangeSlider(row2, command=self._range_changed)
        self.range_slider.pack(side="left", fill="x", expand=True, padx=(10, 0))

        value_row = ttk.Frame(controls)
        value_row.pack(fill="x", pady=(5, 0))
        ttk.Label(value_row, text="Black").pack(side="left")
        self.black_entry = ttk.Entry(value_row, textvariable=self.black_var, width=10)
        self.black_entry.pack(side="left", padx=(4, 18))
        ttk.Label(value_row, text="White").pack(side="left")
        self.white_entry = ttk.Entry(value_row, textvariable=self.white_var, width=10)
        self.white_entry.pack(side="left", padx=(4, 0))
        self.black_entry.bind("<Return>", self._contrast_entry_changed)
        self.black_entry.bind("<FocusOut>", self._contrast_entry_changed)
        self.white_entry.bind("<Return>", self._contrast_entry_changed)
        self.white_entry.bind("<FocusOut>", self._contrast_entry_changed)

    def _build_right_panel(self, parent: ttk.Frame) -> None:
        channel_box = ttk.Labelframe(parent, text="Channel identity", padding=6)
        channel_box.pack(fill="both", expand=True, pady=(0, 10))
        self.channel_tree = ttk.Treeview(
            channel_box,
            columns=("modality", "candidate", "assignment"),
            show="tree headings",
            height=7,
            selectmode="browse",
        )
        self.channel_tree.heading("#0", text="Channel")
        self.channel_tree.heading("modality", text="Modality")
        self.channel_tree.heading("candidate", text="Candidate")
        self.channel_tree.heading("assignment", text="Assignment")
        self.channel_tree.column("#0", width=50, stretch=False)
        self.channel_tree.column("modality", width=68, stretch=False)
        self.channel_tree.column("candidate", width=75)
        self.channel_tree.column("assignment", width=75)
        self.channel_tree.pack(fill="both", expand=True)
        self.channel_tree.bind("<<TreeviewSelect>>", self._on_identity_selected)

        assignment_row = ttk.Frame(channel_box, padding=(0, 7, 0, 0))
        assignment_row.pack(fill="x")
        self.assignment_combo = ttk.Combobox(
            assignment_row,
            textvariable=self.assignment_var,
            state="readonly",
        )
        self.assignment_combo.pack(side="left", fill="x", expand=True)
        ttk.Button(assignment_row, text="Apply", command=self._apply_assignment).pack(side="left", padx=(7, 0))

        review_box = ttk.Labelframe(parent, text="Acquisition review", padding=8)
        review_box.pack(fill="x", pady=(0, 10))
        self.review_label = ttk.Label(
            review_box,
            textvariable=self.review_text_var,
            style="Muted.TLabel",
            justify="left",
            wraplength=330,
        )
        self.review_label.pack(fill="x")
        self.review_check = ttk.Checkbutton(
            review_box,
            text="I reviewed the acquisition variants",
            variable=self.review_var,
        )
        self.review_check.pack(anchor="w", pady=(7, 0))
        self.review_check.state(["disabled"])

        output_box = ttk.Labelframe(parent, text="Conversion", padding=8)
        output_box.pack(fill="x")
        output_row = ttk.Frame(output_box)
        output_row.pack(fill="x")
        ttk.Entry(output_row, textvariable=self.output_var).pack(side="left", fill="x", expand=True)
        ttk.Button(output_row, text="Browse", command=self._choose_output).pack(side="left", padx=(7, 0))
        ttk.Checkbutton(
            output_box,
            text="Resume matching .partial project",
            variable=self.resume_var,
        ).pack(anchor="w", pady=(7, 8))
        actions = ttk.Frame(output_box)
        actions.pack(fill="x")
        actions.columnconfigure((0, 1, 2), weight=1, uniform="conversion-actions")
        self.dry_run_button = ttk.Button(
            actions,
            text="Dry Run",
            command=self._show_dry_run,
        )
        self.dry_run_button.grid(row=0, column=0, sticky="ew")
        self.convert_button = ttk.Button(
            actions,
            text="Convert",
            style="Accent.TButton",
            command=self._start_conversion,
        )
        self.convert_button.grid(row=0, column=1, sticky="ew", padx=5)
        self.cancel_button = ttk.Button(actions, text="Cancel", command=self._cancel_conversion)
        self.cancel_button.grid(row=0, column=2, sticky="ew")
        self.cancel_button.state(["disabled"])
        self.dry_run_button.state(["disabled"])
        self.convert_button.state(["disabled"])

    def _build_status_bar(self) -> None:
        status = ttk.Frame(self.root, style="Header.TFrame", padding=(12, 7))
        status.pack(fill="x", side="bottom")
        self.progress = ttk.Progressbar(status, mode="determinate", maximum=100, length=260)
        self.progress.pack(side="left")
        ttk.Label(status, textvariable=self.status_var, style="Surface.TLabel").pack(side="left", padx=(12, 0))
        self.log_button = ttk.Button(status, text="Show log", command=self._show_log)
        self.log_button.pack(side="right")
        self.log_lines: list[str] = []

    def _register_drop_targets(self) -> None:
        if DND_FILES is None:
            self.input_summary_var.set("Add LIF files or folders")
            return
        for widget in (self.root, self.input_tree):
            widget.drop_target_register(DND_FILES)
            widget.dnd_bind("<<Drop>>", self._on_drop)

    def _on_drop(self, event) -> str:
        paths = [Path(value) for value in self.root.tk.splitlist(event.data)]
        self.add_input_paths(paths)
        return getattr(event, "action", "copy")

    def _choose_lifs(self) -> None:
        selected = filedialog.askopenfilenames(
            title="Add Leica LIF files",
            filetypes=(("Leica LIF", "*.lif"), ("All files", "*")),
        )
        if selected:
            self.add_input_paths([Path(value) for value in selected])

    def _choose_folder(self) -> None:
        selected = filedialog.askdirectory(title="Add a folder containing Leica LIF files")
        if selected:
            self.add_input_paths([Path(selected)])

    def add_input_paths(self, inputs: list[Path]) -> None:
        if not inputs:
            return
        self.status_var.set("Scanning inputs for LIF files")

        def worker() -> None:
            try:
                discovered = discover_lif_paths(inputs)
                self.events.put(("inputs_discovered", discovered))
            except BaseException as exc:
                self.events.put(("input_scan_error", exc))

        threading.Thread(target=worker, daemon=True).start()

    def _add_discovered_inputs(self, paths: list[Path]) -> None:
        new_paths = [path for path in paths if path not in self.input_iid_by_path]
        for path in new_paths:
            iid = f"input-{self.next_input_iid}"
            self.next_input_iid += 1
            self.input_paths.append(path)
            self.input_iid_by_path[path] = iid
            self.input_path_by_iid[iid] = path
            self.input_status[path] = "Queued"
            self.input_tree.insert(
                "",
                "end",
                iid=iid,
                text=path.name,
                values=(path.parent.name or str(path.parent), "Queued"),
            )
        count = len(self.input_paths)
        self.input_summary_var.set(f"{count} LIF file{'s' if count != 1 else ''}")
        if not paths:
            self.status_var.set("No LIF files found in the supplied inputs")
        elif new_paths:
            self.status_var.set(f"Added {len(new_paths)} LIF file{'s' if len(new_paths) != 1 else ''}")
        else:
            self.status_var.set("All discovered LIF files were already in the queue")
        if new_paths and self.source_path is None and self.busy_mode is None:
            iid = self.input_iid_by_path[new_paths[0]]
            self.input_tree.selection_set(iid)
            self.input_tree.focus(iid)
            self.load_lif(new_paths[0])

    def _set_input_status(self, path: Path | None, status: str) -> None:
        if path is None or path not in self.input_iid_by_path:
            return
        self.input_status[path] = status
        iid = self.input_iid_by_path[path]
        values = list(self.input_tree.item(iid, "values"))
        values[1] = status
        self.input_tree.item(iid, values=values)

    def _on_input_selected(self, _event=None) -> None:
        if self.busy_mode:
            return
        iid = self.input_tree.focus()
        if not iid:
            selection = self.input_tree.selection()
            iid = selection[-1] if selection else ""
        path = self.input_path_by_iid.get(iid)
        if path is not None:
            self.load_lif(path)

    def _remove_selected_inputs(self) -> None:
        selected = list(self.input_tree.selection())
        if not selected:
            return
        selected_paths = [self.input_path_by_iid[iid] for iid in selected]
        if self.busy_mode and self.source_path in selected_paths:
            messagebox.showinfo(APP_NAME, "Wait for the current operation to finish.")
            return
        removing_current = self.source_path in selected_paths
        for iid, path in zip(selected, selected_paths):
            self.input_tree.delete(iid)
            self.input_paths.remove(path)
            self.input_iid_by_path.pop(path, None)
            self.input_path_by_iid.pop(iid, None)
            self.input_status.pop(path, None)
        self.input_summary_var.set(
            f"{len(self.input_paths)} LIF files" if self.input_paths else "Drop LIF files or folders here"
        )
        if removing_current:
            self._reset_active_input()
        if self.source_path is None and self.input_paths:
            next_path = self.input_paths[0]
            next_iid = self.input_iid_by_path[next_path]
            self.input_tree.selection_set(next_iid)
            self.input_tree.focus(next_iid)
            self.load_lif(next_path)

    def _clear_all_inputs(self) -> None:
        if not self.input_paths:
            return
        if self.busy_mode:
            messagebox.showinfo(APP_NAME, "Wait for the current operation to finish.")
            return
        for iid in self.input_tree.get_children():
            self.input_tree.delete(iid)
        self.input_paths.clear()
        self.input_iid_by_path.clear()
        self.input_path_by_iid.clear()
        self.input_status.clear()
        self.input_summary_var.set("Drop LIF files or folders here")
        self._reset_active_input()
        self.status_var.set("Input queue cleared; source files were not modified")

    def _reset_active_input(self) -> None:
        self._stop_playback()
        self._close_preview_source()
        self.source_path = None
        self.plan = None
        self.current_frames.clear()
        self.rendered_full = None
        self.path_var.set("No LIF selected")
        self.summary_var.set("Select a LIF from the input queue.")
        self.series_detail_var.set("No series selected")
        self.output_var.set("")
        self._clear_plan_views()
        self.preview_canvas.itemconfigure("preview_message", text="Image preview")

    def load_lif(self, path: Path) -> None:
        if self.busy_mode:
            messagebox.showinfo(APP_NAME, "Wait for the current operation to finish.")
            return
        path = path.expanduser().resolve()
        self._stop_playback()
        if path not in self.input_iid_by_path:
            self._add_discovered_inputs([path])
        if path == self.source_path and self.busy_mode == "plan":
            return
        if path == self.source_path and self.plan is not None:
            return
        self._close_preview_source()
        self.source_path = path
        self.plan = None
        self.channel_overrides.clear()
        self.path_var.set(str(path))
        self.summary_var.set("Inspecting metadata...")
        self.status_var.set("Building preflight plan")
        self._clear_plan_views()
        self._set_busy("plan")
        self._set_input_status(path, "Inspecting")

        def worker() -> None:
            try:
                plan = build_conversion_plan(path)
                preview_lif = LifFileAdapter(path)
                self.events.put(("plan_ready", (path, plan, preview_lif)))
            except BaseException as exc:
                self.events.put(("operation_error", (path, exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _clear_plan_views(self) -> None:
        for tree in (self.series_tree, self.channel_tree):
            for item in tree.get_children():
                tree.delete(item)
        self.channel_combo["values"] = ()
        self.channel_var.set("")
        self.preview_canvas.delete("preview_image")
        self.preview_canvas.itemconfigure("preview_message", text="Loading preflight...")
        self.review_text_var.set("No preflight result")
        self.review_var.set(False)
        self.review_check.state(["disabled"])

    def _populate_plan(self, plan: dict) -> None:
        self.plan = plan
        summary = plan["summary"]
        workflow = plan["channel_resolution"]["workflow"]
        dimensions = ", ".join(
            f"{key}: {value}" for key, value in summary["dimension_type_counts"].items()
        )
        estimated_mb = summary["estimated_uncompressed_pixel_bytes"] / (1024 * 1024)
        self.summary_var.set(
            f"{workflow['route'].upper()} / {workflow['status']}\n"
            f"{summary['series_count']} series | {dimensions}\n"
            f"{summary['output_unit_count']} outputs | {summary['plane_count']} planes\n"
            f"~{estimated_mb:.1f} MiB uncompressed pixels"
        )
        for series in plan["series"]:
            dims = series["dimensions"]
            self.series_tree.insert(
                "",
                "end",
                iid=str(series["series_index"]),
                text=series["series_name"],
                values=(
                    series["series_index"],
                    series["dimension_type"],
                    f"{dims['x']} x {dims['y']}",
                ),
            )

        for channel in plan["channel_resolution"]["channels"]:
            key = channel["channel_key"]
            candidate = (channel.get("inferred_dye") or {}).get("id", "-")
            assignment = self.channel_overrides.get(int(key[1:]), "unconfirmed")
            self.channel_tree.insert(
                "",
                "end",
                iid=key,
                text=key,
                values=(channel["modality"], candidate, assignment),
            )

        registry = load_registry()
        assignments = ["unconfirmed", "brightfield", "unknown"] + [
            entry["id"] for entry in registry["entries"] if entry.get("kind") == "stain"
        ]
        self.assignment_combo["values"] = assignments

        if workflow["route"] == "review":
            reasons = ", ".join(workflow["reason_codes"])
            groups = plan["workflow_review"]["groups"]
            group_text = ", ".join(
                f"{group['group_id']}: {group['series_count']}" for group in groups
            )
            self.review_text_var.set(f"Review required: {reasons}\nProtocol groups: {group_text}")
            self.review_label.configure(style="Warning.TLabel")
            self.review_check.state(["!disabled"])
        else:
            self.review_text_var.set(workflow["summary"])
            self.review_label.configure(style="Muted.TLabel")
            self.review_var.set(False)
            self.review_check.state(["disabled"])

        suggested = Path.cwd() / plan["output_policy"]["suggested_directory_name"]
        self.output_var.set(str(suggested))
        first = self.series_tree.get_children()
        if first:
            self.series_tree.selection_set(first[0])
            self.series_tree.focus(first[0])
            self._on_series_selected()

    def _on_series_selected(self, _event=None) -> None:
        if not self.plan:
            return
        self._stop_playback()
        selection = self.series_tree.selection()
        if not selection:
            return
        index = int(selection[0])
        series = self.plan["series"][index]
        dims = series["dimensions"]
        self.series_detail_var.set(
            f"{series['series_name']} | {series['dimension_type']} | "
            f"{dims['x']} x {dims['y']} | Z={dims['z']} T={dims['t']}"
        )
        values = []
        self.current_channel_info = {}
        self.layer_vars = {}
        for output in series["outputs"]:
            channel_index = int(output["channel_index"])
            label = output.get("physical_label") or f"channel {channel_index}"
            value = f"C{channel_index:02d}  {label}"
            values.append(value)
            self.current_channel_info[channel_index] = output
            key = self._contrast_key(channel_index, series_index=index)
            if key not in self.channel_contrast:
                self.channel_contrast[key] = self._default_channel_contrast(output)
            if key not in self.channel_visibility:
                self.channel_visibility[key] = True
            self.layer_vars[channel_index] = tk.BooleanVar(
                value=self.channel_visibility[key]
            )
        self.channel_combo["values"] = values
        if values:
            self.channel_var.set(values[0])
        self._rebuild_layer_controls()
        self._load_active_contrast()
        self.z_var.set(0)
        self.t_var.set(0)
        self.z_spin.configure(to=max(0, int(dims["z"]) - 1))
        self.t_spin.configure(to=max(0, int(dims["t"]) - 1))
        self._updating_time = True
        self.time_scale.configure(to=max(0, int(dims["t"]) - 1))
        self.time_scale.set(0)
        self._updating_time = False
        if int(dims["t"]) > 1:
            self.play_button.state(["!disabled"])
        else:
            self.play_button.state(["disabled"])
        self._reset_view()
        self._request_preview()

    def _contrast_key(
        self,
        channel_index: int,
        *,
        series_index: int | None = None,
    ) -> tuple[Path, int, int]:
        if self.source_path is None:
            raise RuntimeError("No source LIF selected")
        if series_index is None:
            selection = self.series_tree.selection()
            if not selection:
                raise RuntimeError("No series selected")
            series_index = int(selection[0])
        return (self.source_path, int(series_index), int(channel_index))

    def _default_channel_contrast(self, output: dict) -> dict:
        source = output.get("source_display_settings") or {}
        dtype = np.dtype((output.get("file") or {}).get("dtype", "uint8"))
        if np.issubdtype(dtype, np.integer):
            dtype_info = np.iinfo(dtype)
            fallback_min = float(dtype_info.min)
            fallback_max = float(dtype_info.max)
        else:
            fallback_min, fallback_max = 0.0, 1.0
        value_min = float(source.get("value_min", fallback_min))
        value_max = float(source.get("value_max", fallback_max))
        if value_max <= value_min:
            value_min, value_max = fallback_min, fallback_max
        source_black = min(max(float(source.get("black", value_min)), value_min), value_max)
        source_white = min(max(float(source.get("white", value_max)), source_black), value_max)
        return {
            "value_min": value_min,
            "value_max": value_max,
            "source_black": source_black,
            "source_white": source_white,
            "black": source_black,
            "white": source_white,
            "gamma": max(float(source.get("gamma", 1.0)), 1e-6),
            "mode": "Source display",
            "source": source.get("source", "dtype_range_fallback"),
        }

    def _rebuild_layer_controls(self) -> None:
        for child in self.layer_frame.winfo_children():
            child.destroy()
        for position, (channel_index, info) in enumerate(
            sorted(self.current_channel_info.items())
        ):
            label = info.get("physical_label") or f"channel {channel_index}"
            control = ttk.Checkbutton(
                self.layer_frame,
                text=f"C{channel_index:02d} {label}",
                variable=self.layer_vars[channel_index],
                command=lambda index=channel_index: self._toggle_layer(index),
            )
            control.grid(
                row=position // 3,
                column=position % 3,
                sticky="w",
                padx=(0, 12),
            )

    def _toggle_layer(self, channel_index: int) -> None:
        key = self._contrast_key(channel_index)
        self.channel_visibility[key] = self.layer_vars[channel_index].get()
        for value in self.channel_combo["values"]:
            if value.startswith(f"C{channel_index:02d}"):
                self.channel_var.set(value)
                break
        self._load_active_contrast()
        self._render_current()

    def _request_preview(self) -> None:
        if not self.plan or not self.preview_lif or self.busy_mode == "conversion":
            return
        selection = self.series_tree.selection()
        if not selection:
            return
        series_index = int(selection[0])
        z = int(self.z_var.get())
        t = int(self.t_var.get())
        self.preview_token += 1
        token = self.preview_token
        self.preview_canvas.itemconfigure("preview_message", text="Loading image...")

        def worker() -> None:
            try:
                with self.preview_lock:
                    if self.preview_lif is None:
                        return
                    image = self.preview_lif.images[series_index]
                    frames = {
                        channel: np.asarray(image.get_frame(z=z, t=t, c=channel))
                        for channel in range(image.channels)
                    }
                self.events.put(("preview_ready", (token, t, frames)))
            except BaseException as exc:
                self.events.put(("preview_error", (token, exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _current_timepoint_count(self) -> int:
        if not self.plan:
            return 1
        selection = self.series_tree.selection()
        if not selection:
            return 1
        return max(1, int(self.plan["series"][int(selection[0])]["dimensions"]["t"]))

    def _toggle_playback(self) -> None:
        if self.playing:
            self._stop_playback()
            return
        if self._current_timepoint_count() <= 1:
            return
        self.playing = True
        self.play_button.configure(text="Pause")
        self._advance_playback()

    def _advance_playback(self) -> None:
        self.playback_job = None
        if not self.playing or self.busy_mode == "conversion":
            return
        count = self._current_timepoint_count()
        self.t_var.set((int(self.t_var.get()) + 1) % count)
        self._request_preview()

    def _schedule_playback(self) -> None:
        if not self.playing:
            return
        if self.playback_job is not None:
            try:
                self.root.after_cancel(self.playback_job)
            except tk.TclError:
                pass
        try:
            fps = min(max(0.1, float(self.playback_fps_var.get())), 120.0)
        except ValueError:
            fps = 2.0
        self.playback_job = self.root.after(int(1000 / fps), self._advance_playback)

    def _validate_fps(self, _event=None) -> None:
        try:
            fps = min(max(float(self.playback_fps_var.get()), 0.1), 120.0)
        except ValueError:
            fps = 2.0
        self.playback_fps_var.set(f"{fps:g}")
        if self.playing:
            self._schedule_playback()

    def _time_scrubbed(self, value: str) -> None:
        if self._updating_time or not self.plan:
            return
        count = self._current_timepoint_count()
        timepoint = min(max(int(round(float(value))), 0), count - 1)
        self._updating_time = True
        self.time_scale.set(timepoint)
        self.t_var.set(timepoint)
        self._updating_time = False
        if self.time_scrub_job is not None:
            try:
                self.root.after_cancel(self.time_scrub_job)
            except tk.TclError:
                pass
        self.time_scrub_job = self.root.after(70, self._finish_time_scrub)

    def _finish_time_scrub(self) -> None:
        self.time_scrub_job = None
        self._request_preview()

    def _stop_playback(self) -> None:
        self.playing = False
        if self.playback_job is not None:
            try:
                self.root.after_cancel(self.playback_job)
            except tk.TclError:
                pass
            self.playback_job = None
        if self.time_scrub_job is not None:
            try:
                self.root.after_cancel(self.time_scrub_job)
            except tk.TclError:
                pass
            self.time_scrub_job = None
        if hasattr(self, "play_button"):
            self.play_button.configure(text="Play")

    def _selected_channel_index(self) -> int:
        match = re.match(r"C(\d+)", self.channel_var.get())
        return int(match.group(1)) if match else 0

    def _channel_changed(self) -> None:
        self._load_active_contrast()
        self._render_current()

    def _load_active_contrast(self) -> None:
        channel = self._selected_channel_index()
        try:
            settings = self.channel_contrast[self._contrast_key(channel)]
        except (KeyError, RuntimeError):
            return
        self._updating_contrast = True
        self.contrast_mode_var.set(settings["mode"])
        self.black_var.set(round(settings["black"], 3))
        self.white_var.set(round(settings["white"], 3))
        self.range_slider.set_range(
            settings["value_min"],
            settings["value_max"],
            settings["black"],
            settings["white"],
        )
        self._updating_contrast = False
        self._update_contrast_labels()

    def _contrast_mode_changed(self) -> None:
        channel = self._selected_channel_index()
        try:
            settings = self.channel_contrast[self._contrast_key(channel)]
        except (KeyError, RuntimeError):
            return
        mode = self.contrast_mode_var.get()
        if mode == "Source display":
            settings["black"] = settings["source_black"]
            settings["white"] = settings["source_white"]
        elif mode == "Full range":
            settings["black"] = settings["value_min"]
            settings["white"] = settings["value_max"]
        settings["mode"] = mode
        self._load_active_contrast()
        self._render_current()

    def _range_changed(self, black: float, white: float) -> None:
        if self._updating_contrast:
            return
        self._updating_contrast = True
        self.black_var.set(round(black, 3))
        self.white_var.set(round(white, 3))
        self._updating_contrast = False
        self.contrast_mode_var.set("Manual")
        self._store_active_contrast(black, white, "Manual")
        self._update_contrast_labels()
        self._render_current()

    def _contrast_entry_changed(self, _event=None) -> None:
        if self._updating_contrast:
            return
        try:
            black = float(self.black_entry.get())
            white = float(self.white_entry.get())
        except ValueError:
            self.black_var.set(round(self.range_slider.low, 3))
            self.white_var.set(round(self.range_slider.high, 3))
            return
        black = min(max(black, self.range_slider.minimum), self.range_slider.maximum)
        white = min(max(white, self.range_slider.minimum), self.range_slider.maximum)
        if white < black:
            black, white = white, black
        self._updating_contrast = True
        self.black_var.set(round(black, 3))
        self.white_var.set(round(white, 3))
        self.range_slider.set_values(black, white)
        self._updating_contrast = False
        self.contrast_mode_var.set("Manual")
        self._store_active_contrast(black, white, "Manual")
        self._update_contrast_labels()
        self._render_current()

    def _store_active_contrast(self, black: float, white: float, mode: str) -> None:
        try:
            settings = self.channel_contrast[
                self._contrast_key(self._selected_channel_index())
            ]
        except (KeyError, RuntimeError):
            return
        settings["black"] = float(black)
        settings["white"] = float(white)
        settings["mode"] = mode

    def _update_contrast_labels(self) -> None:
        self.black_text_var.set(f"Black {self.black_var.get():.1f}")
        self.white_text_var.set(f"White {self.white_var.get():.1f}")

    def _render_current(self) -> None:
        if not self.current_frames:
            return
        channel = self._selected_channel_index()
        visible = {
            index
            for index in self.current_frames
            if self.channel_visibility.get(self._contrast_key(index), True)
        }
        settings_by_channel = {
            index: self.channel_contrast[self._contrast_key(index)]
            for index in visible
            if self._contrast_key(index) in self.channel_contrast
        }
        if len(visible) > 1:
            image = _render_overlay(
                self.current_frames,
                self.current_channel_info,
                visible_channels=visible,
                settings_by_channel=settings_by_channel,
            )
        elif len(visible) == 1:
            shown_channel = next(iter(visible))
            frame = self.current_frames[shown_channel]
            settings = settings_by_channel[shown_channel]
            info = self.current_channel_info.get(shown_channel, {})
            modality = ((info.get("identity") or {}).get("modality") or {}).get("value")
            color = None
            if self.use_lut_var.get() and modality != "brightfield":
                color = _lut_color(
                    info.get("source_lut") or info.get("physical_label"),
                    shown_channel,
                )
            image = _render_single(
                frame,
                settings["black"],
                settings["white"],
                color=color,
                gamma=settings.get("gamma", 1.0),
            )
        else:
            shape = next(iter(self.current_frames.values())).shape
            image = Image.new("RGB", (shape[1], shape[0]), (0, 0, 0))
        self.rendered_full = image
        self._fit_rendered_image()

    def _reset_view(self) -> None:
        self.zoom = 1.0
        self.pan_x = 0.0
        self.pan_y = 0.0
        self._update_zoom_display()
        self._fit_rendered_image()

    def _zoom_wheel(self, event) -> str:
        if event.delta:
            magnitude = min(max(abs(float(event.delta)) / 120.0, 0.35), 3.0)
            factor = 1.12**magnitude
            if event.delta > 0:
                factor = 1.0 / factor
            self._zoom_at(event.x, event.y, factor, interactive=True)
        return "break"

    def _zoom_percent_changed(self, _event=None) -> None:
        percent = _validated_zoom_percent(self.zoom_percent_var.get(), self.zoom * 100)
        self.zoom_percent_var.set(f"{percent:g}")
        if self.rendered_full is None:
            self.zoom = percent / 100.0
            self._update_zoom_display()
            return
        factor = (percent / 100.0) / max(self.zoom, 1e-9)
        self._zoom_at(
            self.preview_canvas.winfo_width() / 2,
            self.preview_canvas.winfo_height() / 2,
            factor,
        )

    def _update_zoom_display(self) -> None:
        percent = self.zoom * 100.0
        self.zoom_text_var.set(f"Zoom {percent:.0f}%")
        self.zoom_percent_var.set(f"{percent:.0f}")

    def _zoom_at(
        self,
        x: float,
        y: float,
        factor: float,
        *,
        interactive: bool = False,
    ) -> str:
        if self.rendered_full is None:
            return "break"
        width = max(1, self.preview_canvas.winfo_width())
        height = max(1, self.preview_canvas.winfo_height())
        image_width, image_height = self.rendered_full.size
        base_scale = min(
            max(1, width - 24) / image_width,
            max(1, height - 24) / image_height,
        )
        old_scale = base_scale * self.zoom
        relative_x = (x - (width / 2 + self.pan_x)) / old_scale
        relative_y = (y - (height / 2 + self.pan_y)) / old_scale
        new_zoom = min(max(self.zoom * factor, 0.05), 100.0)
        new_scale = base_scale * new_zoom
        self.pan_x = x - width / 2 - relative_x * new_scale
        self.pan_y = y - height / 2 - relative_y * new_scale
        self.zoom = new_zoom
        self._update_zoom_display()
        if interactive:
            self._queue_view_render()
        else:
            self._fit_rendered_image()
        return "break"

    def _queue_view_render(self) -> None:
        if self.view_render_job is None:
            self.view_render_job = self.root.after(16, self._render_interactive_view)
        if self.view_refine_job is not None:
            try:
                self.root.after_cancel(self.view_refine_job)
            except tk.TclError:
                pass
        self.view_refine_job = self.root.after(120, self._render_refined_view)

    def _render_interactive_view(self) -> None:
        self.view_render_job = None
        self._fit_rendered_image(resample=Image.Resampling.NEAREST)

    def _render_refined_view(self) -> None:
        self.view_refine_job = None
        self._fit_rendered_image()

    def _pan_start_event(self, event) -> None:
        if self.rendered_full is None:
            return
        self.pan_start = (event.x, event.y)
        self.preview_canvas.configure(cursor="fleur")

    def _pan_move_event(self, event) -> None:
        if self.pan_start is None:
            return
        previous_x, previous_y = self.pan_start
        self.pan_x += event.x - previous_x
        self.pan_y += event.y - previous_y
        self.pan_start = (event.x, event.y)
        self._queue_view_render()

    def _pan_end_event(self, _event) -> None:
        self.pan_start = None
        self.preview_canvas.configure(cursor="")

    def _fit_rendered_image(
        self,
        *,
        resample: Image.Resampling = Image.Resampling.BILINEAR,
    ) -> None:
        if self.rendered_full is None:
            return
        width = max(100, self.preview_canvas.winfo_width())
        height = max(100, self.preview_canvas.winfo_height())
        image_width, image_height = self.rendered_full.size
        base_scale = min(
            max(1, width - 24) / image_width,
            max(1, height - 24) / image_height,
        )
        scale = max(base_scale * self.zoom, 1e-6)
        anchor_x = width / 2 + self.pan_x
        anchor_y = height / 2 + self.pan_y
        transformed = self.rendered_full.transform(
            (width, height),
            Image.Transform.AFFINE,
            (
                1 / scale,
                0,
                image_width / 2 - anchor_x / scale,
                0,
                1 / scale,
                image_height / 2 - anchor_y / scale,
            ),
            resample=resample,
            fillcolor=(20, 24, 28),
        )
        self.rendered_tk = ImageTk.PhotoImage(transformed)
        self.preview_canvas.delete("preview_image")
        self.preview_canvas.create_image(
            0,
            0,
            image=self.rendered_tk,
            anchor="nw",
            tags="preview_image",
        )
        self.preview_canvas.itemconfigure("preview_message", text="")

    def _on_identity_selected(self, _event=None) -> None:
        selection = self.channel_tree.selection()
        if not selection:
            return
        index = int(selection[0][1:])
        self.assignment_var.set(self.channel_overrides.get(index, "unconfirmed"))

    def _apply_assignment(self) -> None:
        selection = self.channel_tree.selection()
        if not selection:
            return
        key = selection[0]
        index = int(key[1:])
        value = self.assignment_var.get()
        if value == "unconfirmed":
            self.channel_overrides.pop(index, None)
        else:
            self.channel_overrides[index] = value
        current = list(self.channel_tree.item(key, "values"))
        current[2] = value
        self.channel_tree.item(key, values=current)

    def _choose_output(self) -> None:
        selected = filedialog.askdirectory(title="Choose output parent directory")
        if not selected:
            return
        name = (
            self.plan["output_policy"]["suggested_directory_name"]
            if self.plan
            else "lif_tiff"
        )
        self.output_var.set(str(Path(selected) / name))

    def _show_dry_run(self) -> None:
        if not self.source_path or not self.plan:
            messagebox.showerror(APP_NAME, "Select and inspect a LIF first.")
            return
        output_text = self.output_var.get().strip()
        output = Path(output_text).expanduser().resolve() if output_text else None
        report = build_dry_run_report(
            self.plan,
            output,
            review_acknowledged=self.review_var.get(),
            resume=self.resume_var.get(),
            channel_overrides=self.channel_overrides,
        )
        estimated_mb = report["estimated_uncompressed_pixel_bytes"] / (1024 * 1024)
        lines = [
            f"DRY RUN: {report['status'].upper()}",
            "",
            f"Source: {self.source_path.name}",
            f"Workflow: {report['workflow_route']}",
            f"Series: {report['series_count']}",
            f"TIFF units: {report['output_unit_count']}",
            f"Planes: {report['plane_count']}",
            f"Estimated uncompressed pixels: {estimated_mb:.1f} MiB",
            f"Output state: {report['output_state']}",
        ]
        if report["errors"]:
            lines.extend(("", "Blocking checks:"))
            lines.extend(f"- {message}" for message in report["errors"])
        if report["warnings"]:
            lines.extend(("", "Warnings:"))
            lines.extend(f"- {message}" for message in report["warnings"])
        lines.extend(("", "No image pixels were read and no output files were written."))

        window = tk.Toplevel(self.root)
        window.title("Dry Run Report")
        window.geometry("680x460")
        text = tk.Text(window, wrap="word", font=("DejaVu Sans Mono", 10), padx=14, pady=14)
        text.pack(fill="both", expand=True)
        text.insert("1.0", "\n".join(lines))
        text.configure(state="disabled")
        self.status_var.set(f"Dry run {report['status']}; no files written")

    def _start_conversion(self) -> None:
        if not self.source_path or not self.plan:
            messagebox.showerror(APP_NAME, "Open and inspect a LIF first.")
            return
        workflow = self.plan["channel_resolution"]["workflow"]
        if workflow["route"] == "review" and not self.review_var.get():
            messagebox.showwarning(APP_NAME, "Review and acknowledge the acquisition variants first.")
            return
        output_text = self.output_var.get().strip()
        if not output_text:
            messagebox.showerror(APP_NAME, "Choose an output directory.")
            return
        output = Path(output_text).expanduser().resolve()
        if output.exists():
            messagebox.showerror(APP_NAME, f"Output already exists:\n{output}")
            return
        partial = output.with_name(f"{output.name}.partial")
        resume = self.resume_var.get()
        if partial.exists() and not resume:
            messagebox.showwarning(
                APP_NAME,
                "A matching .partial directory exists. Enable Resume or choose another output.",
            )
            return

        self.cancel_event.clear()
        self._stop_playback()
        self._set_busy("conversion")
        self.progress.configure(mode="determinate", value=0)
        self.status_var.set("Starting conversion")
        self.log_lines.clear()
        self._close_preview_source()
        source = self.source_path
        overrides = dict(self.channel_overrides)
        allow_review = workflow["route"] == "review" and self.review_var.get()

        def progress(message: str) -> None:
            self.events.put(("conversion_progress", message))

        def worker() -> None:
            preview_lif = None
            try:
                result = convert_lif(
                    source,
                    output,
                    resume=resume,
                    channel_overrides=overrides,
                    allow_review=allow_review,
                    progress_callback=progress,
                    cancel_callback=self.cancel_event.is_set,
                )
                preview_lif = LifFileAdapter(source)
                self.events.put(("conversion_done", (result, preview_lif)))
            except BaseException as exc:
                try:
                    preview_lif = LifFileAdapter(source)
                except BaseException:
                    preview_lif = None
                self.events.put(("conversion_error", (exc, preview_lif)))

        threading.Thread(target=worker, daemon=True).start()

    def _cancel_conversion(self) -> None:
        if self.busy_mode != "conversion":
            return
        self.cancel_event.set()
        self.cancel_button.state(["disabled"])
        self.status_var.set("Cancellation requested; finishing current TIFF unit")

    def _export_png(self) -> None:
        if self.rendered_full is None:
            messagebox.showinfo(APP_NAME, "Load an image preview first.")
            return
        selected = filedialog.asksaveasfilename(
            title="Export displayed view",
            defaultextension=".png",
            filetypes=(("PNG image", "*.png"),),
        )
        if selected:
            self.rendered_full.save(selected, format="PNG")
            self.status_var.set(f"Exported {Path(selected).name}")

    def _set_busy(self, mode: str | None) -> None:
        self.busy_mode = mode
        if mode:
            self.open_button.state(["disabled"])
            self.convert_button.state(["disabled"])
            self.dry_run_button.state(["disabled"])
            self.play_button.state(["disabled"])
            if mode == "conversion":
                self.cancel_button.state(["!disabled"])
            else:
                self.cancel_button.state(["disabled"])
                self.progress.configure(mode="indeterminate")
                self.progress.start(12)
        else:
            self.progress.stop()
            self.progress.configure(mode="determinate")
            self.open_button.state(["!disabled"])
            if self.plan is not None:
                self.convert_button.state(["!disabled"])
                self.dry_run_button.state(["!disabled"])
            else:
                self.convert_button.state(["disabled"])
                self.dry_run_button.state(["disabled"])
            if self.plan is not None and self._current_timepoint_count() > 1:
                self.play_button.state(["!disabled"])
            else:
                self.play_button.state(["disabled"])
            self.cancel_button.state(["disabled"])

    def _poll_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "inputs_discovered":
                    self._add_discovered_inputs(payload)
                elif kind == "input_scan_error":
                    self.status_var.set("Input scan failed")
                    messagebox.showerror(APP_NAME, str(payload))
                elif kind == "plan_ready":
                    path, plan, preview_lif = payload
                    if path != self.source_path:
                        preview_lif.close()
                        continue
                    self.preview_lif = preview_lif
                    self._populate_plan(plan)
                    self._set_busy(None)
                    self._set_input_status(path, "Ready")
                    self.status_var.set("Preflight complete")
                elif kind == "preview_ready":
                    token, timepoint, frames = payload
                    if token == self.preview_token:
                        self.current_frames = frames
                        self.last_rendered_t = timepoint
                        self._updating_time = True
                        self.t_var.set(timepoint)
                        self.time_scale.set(timepoint)
                        self._updating_time = False
                        self._render_current()
                        self._schedule_playback()
                elif kind == "preview_error":
                    token, exc = payload
                    if token == self.preview_token:
                        self._stop_playback()
                        self.preview_canvas.itemconfigure("preview_message", text=f"Preview error: {exc}")
                elif kind == "operation_error":
                    path, exc = payload
                    self._set_busy(None)
                    self._set_input_status(path, "Error")
                    self.status_var.set("Preflight failed")
                    messagebox.showerror(APP_NAME, str(exc))
                elif kind == "conversion_progress":
                    message = str(payload)
                    self.log_lines.append(message)
                    self.status_var.set(message)
                    match = re.search(r"TIFF progress: (\d+)/(\d+)", message)
                    if match:
                        done, total = map(int, match.groups())
                        self.progress.configure(value=100.0 * done / total)
                elif kind == "conversion_done":
                    result, preview_lif = payload
                    self.preview_lif = preview_lif
                    self._set_busy(None)
                    self._set_input_status(self.source_path, "Converted")
                    self.progress.configure(value=100)
                    self.status_var.set("Conversion complete")
                    if self.close_after_cancel:
                        self._final_close()
                        return
                    messagebox.showinfo(APP_NAME, f"Conversion complete:\n{result}")
                elif kind == "conversion_error":
                    exc, preview_lif = payload
                    self.preview_lif = preview_lif
                    self._set_busy(None)
                    if self.close_after_cancel:
                        self._final_close()
                        return
                    if isinstance(exc, ConversionCancelled):
                        self._set_input_status(self.source_path, "Partial")
                        self.status_var.set("Conversion cancelled; partial project retained")
                        messagebox.showinfo(
                            APP_NAME,
                            "Conversion cancelled at a safe boundary. The .partial project can be resumed.",
                        )
                    else:
                        self._set_input_status(self.source_path, "Failed")
                        self.status_var.set("Conversion failed")
                        messagebox.showerror(APP_NAME, str(exc))
        except queue.Empty:
            pass
        self.root.after(80, self._poll_events)

    def _show_log(self) -> None:
        window = tk.Toplevel(self.root)
        window.title("Conversion log")
        window.geometry("800x480")
        text = tk.Text(window, wrap="word", font=("DejaVu Sans Mono", 9))
        text.pack(fill="both", expand=True)
        text.insert("1.0", "\n".join(self.log_lines) or "No conversion messages yet.")
        text.configure(state="disabled")

    def _close_preview_source(self) -> None:
        with self.preview_lock:
            if self.preview_lif is not None:
                self.preview_lif.close()
                self.preview_lif = None

    def _on_close(self) -> None:
        if self.busy_mode == "conversion":
            if not messagebox.askyesno(
                APP_NAME,
                "A conversion is running. Request cancellation and close after the current TIFF unit?",
            ):
                return
            self.close_after_cancel = True
            self._cancel_conversion()
            self.status_var.set("Waiting for safe cancellation before closing")
            return
        self._final_close()

    def _final_close(self) -> None:
        self._stop_playback()
        self._close_preview_source()
        self.root.destroy()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("lif", nargs="*", type=Path, help="LIF files or folders to queue")
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help="construct the window under a display server and exit",
    )
    parser.add_argument(
        "--acceptance-screenshot",
        type=Path,
        help="load the supplied LIF, save one GUI acceptance screenshot, and exit",
    )
    parser.add_argument(
        "--acceptance-view",
        choices=("single", "overlay"),
        default="single",
        help="preview mode used only with --acceptance-screenshot",
    )
    parser.add_argument(
        "--acceptance-playback",
        action="store_true",
        help="advance one real T plane and zoom before the acceptance screenshot",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = TkinterDnD.Tk() if TkinterDnD is not None else tk.Tk()
    app = LifToolApp(root)
    if args.smoke_test:
        root.update_idletasks()
        if TkinterDnD is None:
            raise RuntimeError("tkinterdnd2 is unavailable")
        dnd_version = root.tk.call("package", "present", "tkdnd")
        print(f"GUI smoke test passed; native drag-and-drop {dnd_version}")
        app._close_preview_source()
        root.destroy()
        return 0
    if args.acceptance_screenshot is not None:
        if not args.lif:
            raise SystemExit("--acceptance-screenshot requires at least one LIF file or folder")
        output = args.acceptance_screenshot.expanduser().resolve()
        if output.exists():
            raise SystemExit(f"refusing to overwrite screenshot: {output}")
        attempts = 0
        playback_started = False
        initial_timepoint: int | None = None

        def capture_when_ready() -> None:
            nonlocal attempts, playback_started, initial_timepoint
            attempts += 1
            if app.plan is not None and app.current_frames and app.busy_mode is None:
                if args.acceptance_playback and not playback_started:
                    if app._current_timepoint_count() <= 1:
                        raise RuntimeError("acceptance playback requires a time-lapse series")
                    initial_timepoint = app.last_rendered_t
                    app._toggle_playback()
                    playback_started = True
                    root.after(100, capture_when_ready)
                    return
                if (
                    args.acceptance_playback
                    and app.last_rendered_t == initial_timepoint
                ):
                    root.after(100, capture_when_ready)
                    return
                if args.acceptance_playback:
                    app._stop_playback()
                    app._zoom_at(
                        app.preview_canvas.winfo_width() / 2,
                        app.preview_canvas.winfo_height() / 2,
                        2.0,
                    )
                if args.acceptance_view == "single":
                    selected_channel = app._selected_channel_index()
                    for channel_index, variable in app.layer_vars.items():
                        visible = channel_index == selected_channel
                        variable.set(visible)
                        app.channel_visibility[
                            app._contrast_key(channel_index)
                        ] = visible
                else:
                    for channel_index, variable in app.layer_vars.items():
                        variable.set(True)
                        app.channel_visibility[
                            app._contrast_key(channel_index)
                        ] = True
                app._render_current()
                root.update_idletasks()
                x = root.winfo_rootx()
                y = root.winfo_rooty()
                width = root.winfo_width()
                height = root.winfo_height()
                output.parent.mkdir(parents=True, exist_ok=True)
                ImageGrab.grab(bbox=(x, y, x + width, y + height)).save(output)
                print(output)
                app._close_preview_source()
                root.destroy()
                return
            if attempts >= 300:
                app._close_preview_source()
                root.destroy()
                raise RuntimeError("GUI acceptance screenshot timed out")
            root.after(100, capture_when_ready)

        root.after(100, lambda: app.add_input_paths(args.lif))
        root.after(200, capture_when_ready)
        root.mainloop()
        return 0
    if args.lif:
        root.after(100, lambda: app.add_input_paths(args.lif))
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
