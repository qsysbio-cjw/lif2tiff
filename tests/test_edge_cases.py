from __future__ import annotations

import ast
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest


AUDIT_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = AUDIT_ROOT.parent
for path in (
    PROJECT_ROOT / "src",
    PROJECT_ROOT / "qt_gui",
    PROJECT_ROOT / "viewer",
):
    sys.path.insert(0, str(path))

from app import (  # noqa: E402
    QtWorkbench,
    apply_batch_storage_constraints,
    discover_lifs,
    dry_run_report,
    find_batch_output_collisions,
)
from liffile_adapter import LifImageAdapter  # noqa: E402
import liftool as liftool_module  # noqa: E402
from liftool import _default_output  # noqa: E402
from stage_map_view import expand_plate_grid, stage_map_points  # noqa: E402
from PySide6.QtCore import QSignalBlocker, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QDockWidget, QListWidgetItem  # noqa: E402


def sample_plan(*, route: str = "standard", suggestion: str | None = "nile_red") -> dict:
    inferred = {"id": suggestion} if suggestion else None
    return {
        "channel_resolution": {
            "workflow": {"route": route, "status": "ready"},
            "channels": [
                {
                    "channel_key": "C00",
                    "modality": "fluorescence",
                    "inferred_dye": inferred,
                    "confirmed_assignment": None,
                },
                {
                    "channel_key": "C01",
                    "modality": "brightfield",
                    "inferred_dye": None,
                    "confirmed_assignment": "brightfield",
                },
            ],
        },
        "summary": {
            "series_count": 1,
            "output_unit_count": 1,
            "plane_count": 1,
            "estimated_uncompressed_pixel_bytes": 1024,
        },
        "output_policy": {"suggested_directory_name": "sample_tiff"},
        "series": [],
    }


@pytest.fixture(scope="session")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    yield app


def test_recursive_discovery_handles_unicode_case_and_symlink_loop(tmp_path: Path):
    nested = tmp_path / "数据 folder"
    nested.mkdir()
    first = nested / "样品 A.LIF"
    second = tmp_path / "B.lif"
    ignored = tmp_path / "not_lif.txt"
    first.touch()
    second.touch()
    ignored.touch()
    (nested / "loop").symlink_to(tmp_path, target_is_directory=True)
    alias = tmp_path / "alias.lif"
    alias.symlink_to(first)

    found = discover_lifs([tmp_path, first, alias])

    assert len(found) == 2
    assert set(found) == {second.resolve(), first.resolve()}


def test_stage_map_skips_missing_or_nonfinite_xy_and_keeps_valid_point():
    plan = {
        "series": [
            {
                "series_index": 0,
                "series_name": "valid",
                "stage_position": {"x_m": 0.01, "y_m": 0.02, "z_m": "bad"},
                "dimensions": {"x": 512, "y": 256},
                "pixel_size": {"x_um_per_px": 0.2, "y_um_per_px": 0.3},
            },
            {
                "series_index": 1,
                "series_name": "nan",
                "stage_position": {"x_m": float("nan"), "y_m": 0.02},
            },
            {"series_index": 2, "series_name": "missing"},
        ]
    }

    points = stage_map_points(plan, Path("plate.lif"))

    assert len(points) == 1
    assert points[0]["series_name"] == "valid"
    assert points[0]["z_um"] is None
    assert points[0]["width_um"] == pytest.approx(102.4)
    assert points[0]["height_um"] == pytest.approx(76.8)


def test_adapter_accepts_derived_image_without_acquisition_timestamp():
    image = SimpleNamespace(
        name="FRET-Efficiency",
        path="FRET-Efficiency",
        sizes={"X": 8, "Y": 8},
        dtype=np.dtype("uint8"),
        coords={},
        attrs={},
        timestamps=None,
        dims=("Y", "X"),
        tilescan=None,
    )

    adapted = LifImageAdapter(image, source_series_index=0, mosaic_index=None)

    assert adapted.acquisition_timestamps == []


@pytest.mark.parametrize(
    "calibration",
    [
        None,
        {},
        {"plate": {"rows": 16, "columns": 24}},
        {
            "plate": {"rows": "bad", "columns": 24},
            "x_axis": {},
            "y_axis": {},
        },
    ],
)
def test_malformed_plate_calibration_fails_closed(calibration):
    assert expand_plate_grid(calibration) == ([], [])


def test_dry_run_state_matrix_does_not_write(tmp_path: Path):
    plan = sample_plan()
    output = tmp_path / "new"
    ready = dry_run_report(
        plan,
        output,
        review_acknowledged=False,
        resume=False,
        channel_overrides={0: "unknown"},
    )
    assert ready["status"] == "ready"
    assert not output.exists()

    partial = output.with_name("new.partial")
    partial.mkdir()
    blocked = dry_run_report(
        plan,
        output,
        review_acknowledged=False,
        resume=False,
        channel_overrides={0: "unknown"},
    )
    resumed = dry_run_report(
        plan,
        output,
        review_acknowledged=False,
        resume=True,
        channel_overrides={0: "unknown"},
    )
    assert blocked["output_state"] == "partial_blocked"
    assert blocked["status"] == "blocked"
    assert resumed["output_state"] == "partial_resume"
    assert resumed["status"] == "ready"


def test_review_and_channel_confirmation_are_independent_gates(tmp_path: Path):
    report = dry_run_report(
        sample_plan(route="review", suggestion=None),
        tmp_path / "out",
        review_acknowledged=False,
        resume=False,
        channel_overrides={},
    )
    assert report["status"] == "blocked"
    assert len(report["errors"]) == 2
    assert any("review" in error.lower() for error in report["errors"])
    assert any("confirmation" in error.lower() for error in report["errors"])


def test_right_tools_dock_starts_compact_but_remains_user_resizable(qapp):
    window = QtWorkbench()
    try:
        window.identity_status.setText("X" * 2000)
        window.show()
        qapp.processEvents()
        tools = [dock for dock in window.findChildren(QDockWidget) if dock.windowTitle() == "Tools"]
        assert len(tools) == 1
        initial_width = tools[0].width()
        assert initial_width <= max(520, round(window.width() * 0.35))
        assert tools[0].minimumWidth() < tools[0].maximumWidth()
        assert tools[0].maximumWidth() > 1000
    finally:
        window.close_after_cancel = True
        window.close()
        qapp.processEvents()


@pytest.mark.skipif(
    os.name == "nt",
    reason="Windows cannot place case-only filename variants in one directory",
)
def test_gui_batch_dry_run_and_conversion_gate_block_output_collision(qapp, tmp_path: Path):
    window = QtWorkbench()
    first = tmp_path / "Sample.lif"
    second = tmp_path / "sample.lif"
    try:
        with QSignalBlocker(window.queue):
            for source, output_name in (
                (first, "Sample_tiff"),
                (second, "sample_tiff"),
            ):
                item = QListWidgetItem(source.name)
                item.setData(Qt.ItemDataRole.UserRole, str(source))
                window.queue.addItem(item)
                window.input_items[source] = item
                plan = sample_plan()
                plan["output_policy"]["suggested_directory_name"] = output_name
                window.plan_cache[source] = plan
                window.channel_overrides_by_source[source] = {0: "nile_red"}
                item.setSelected(True)
        reports = window._selected_dry_run_reports()
        window._refresh_conversion_gate()

        assert len(reports) == 2
        assert all(report["status"] == "blocked" for _, _, report in reports)
        assert all(report.get("batch_output_collision") for _, _, report in reports)
        assert all(
            any("Batch output collision" in error for error in report["errors"])
            for _, _, report in reports
        )
        assert not window.convert_button.isEnabled()
    finally:
        window.close_after_cancel = True
        window.close()
        qapp.processEvents()


def test_windows_specs_are_syntactically_valid_and_bundle_runtime_data():
    gui_spec = (
        PROJECT_ROOT / "packaging/windows/LIF2TIFF-GUI.spec"
    ).read_text(encoding="utf-8")
    cli_spec = (
        PROJECT_ROOT / "packaging/windows/LIF2TIFF-CLI.spec"
    ).read_text(encoding="utf-8")
    ast.parse(gui_spec)
    ast.parse(cli_spec)
    for resource in (
        "channel_registry.json",
        "protocol_registry.json",
    ):
        assert resource in gui_spec
        assert resource in cli_spec
    assert "cellvis_384_stage_calibration.json" in gui_spec
    assert "COLLECT(" in gui_spec
    assert 'console=True' in cli_spec


def test_batch_output_collision_is_detected_with_windows_case_rules(tmp_path: Path):
    first = tmp_path / "Sample.lif"
    second = tmp_path / "sample.lif"
    outputs = [_default_output(source, "_tiff") for source in (first, second)]
    collisions = find_batch_output_collisions(list(zip((first, second), outputs)))

    assert outputs[0].name != outputs[1].name
    assert len(collisions) == 1
    assert collisions[0]["sources"] == [first, second]


def test_dry_run_blocks_unwritable_output_parent(tmp_path: Path, monkeypatch):
    parent = tmp_path / "readonly"
    parent.mkdir()
    real_access = liftool_module.os.access
    monkeypatch.setattr(
        liftool_module.os,
        "access",
        lambda path, mode: (
            False if Path(path) == parent else real_access(path, mode)
        ),
    )

    report = dry_run_report(
        sample_plan(),
        parent / "out",
        review_acknowledged=False,
        resume=False,
        channel_overrides={0: "unknown"},
    )

    assert report["status"] == "blocked"
    assert any("not writable" in error for error in report["errors"])
    assert not (parent / "out").exists()


def test_dry_run_checks_nearest_existing_parent_without_writing(tmp_path: Path):
    output = tmp_path / "missing" / "nested" / "out"

    report = dry_run_report(
        sample_plan(),
        output,
        review_acknowledged=False,
        resume=False,
        channel_overrides={0: "unknown"},
    )

    assert report["status"] == "ready"
    assert report["storage"]["checked_parent"] == str(tmp_path)
    assert not (tmp_path / "missing").exists()


def test_dry_run_blocks_when_free_space_is_insufficient(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        liftool_module.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=100, used=90, free=10),
    )

    report = dry_run_report(
        sample_plan(),
        tmp_path / "out",
        review_acknowledged=False,
        resume=False,
        channel_overrides={0: "unknown"},
    )

    assert report["status"] == "blocked"
    assert any("Insufficient free space" in error for error in report["errors"])


def test_batch_storage_is_aggregated_per_filesystem(tmp_path: Path, monkeypatch):
    free = 100 * 1024 * 1024
    monkeypatch.setattr(
        liftool_module.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=free * 2, used=free, free=free),
    )
    sources = [tmp_path / "a.lif", tmp_path / "b.lif"]
    reports = [
        (
            source,
            tmp_path / f"out_{index}",
            dry_run_report(
                sample_plan(),
                tmp_path / f"out_{index}",
                review_acknowledged=False,
                resume=False,
                channel_overrides={0: "unknown"},
            ),
        )
        for index, source in enumerate(sources)
    ]
    assert all(report["status"] == "ready" for _, _, report in reports)

    apply_batch_storage_constraints(reports)

    assert all(report["status"] == "blocked" for _, _, report in reports)
    assert all(
        any("combined free space" in error for error in report["errors"])
        for _, _, report in reports
    )


@pytest.mark.xfail(
    strict=True,
    reason="Resume is currently one global switch, so new and partial jobs cannot share a batch.",
)
def test_mixed_new_and_partial_outputs_can_share_one_batch(tmp_path: Path):
    plan = sample_plan()
    new_output = tmp_path / "new"
    partial_output = tmp_path / "partial"
    partial_output.with_name("partial.partial").mkdir()
    reports = [
        dry_run_report(
            plan,
            output,
            review_acknowledged=False,
            resume=True,
            channel_overrides={0: "unknown"},
        )
        for output in (new_output, partial_output)
    ]
    assert all(report["status"] == "ready" for report in reports)
