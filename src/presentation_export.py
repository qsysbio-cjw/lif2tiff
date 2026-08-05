"""Non-destructive batch rendering for presentation-oriented channel overlays."""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from typing import Callable

import numpy as np
from PIL import Image

from lif2tiff_project import sanitize_name


class PresentationExportError(RuntimeError):
    """A presentation export cannot be completed safely."""


def _normalize(frame: np.ndarray, black: float, white: float, gamma: float) -> np.ndarray:
    if white <= black:
        white = black + 1.0
    values = np.clip((frame.astype(np.float32) - black) / (white - black), 0.0, 1.0)
    gamma = max(float(gamma), 1e-6)
    return np.power(values, 1.0 / gamma) if gamma != 1.0 else values


def _lut_color(label: str | None, index: int) -> np.ndarray:
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


def compose_overlay(
    frames: dict[int, np.ndarray],
    render_states: dict[int, dict],
    *,
    use_lut: bool,
) -> np.ndarray:
    rgb = None
    selected_count = len(render_states)
    for channel, state in sorted(render_states.items()):
        values = _normalize(
            frames[channel], state["black"], state["white"], state.get("gamma", 1.0)
        )
        if rgb is None:
            rgb = np.zeros((*values.shape, 3), dtype=np.float32)
        if state.get("modality") == "brightfield":
            rgb += values[..., None] * (1.0 if selected_count == 1 else 0.72)
        else:
            color = (
                _lut_color(state.get("lut") or state.get("physical_label"), channel)
                if use_lut
                else np.ones(3, dtype=np.float32)
            )
            rgb += values[..., None] * color * 0.9
    if rgb is None:
        raise PresentationExportError("no selected channel is present")
    return np.ascontiguousarray(np.clip(rgb * 255.0, 0, 255).astype(np.uint8))


def presentation_frame_count(
    plan: dict, scope: str, channels: list[int] | None = None
) -> int:
    if scope == "current_frame":
        return 1
    selected = set(channels or [])
    series_items = [
        series
        for series in plan.get("series", [])
        if not selected
        or selected.intersection(
            int(output["channel_index"]) for output in series.get("outputs", [])
        )
    ]
    if scope == "all_series_first_plane":
        return len(series_items)
    if scope == "all_planes":
        return sum(
            int(series["dimensions"]["z"]) * int(series["dimensions"]["t"])
            for series in series_items
        )
    raise PresentationExportError(f"unsupported presentation scope: {scope}")


def _render_state(output: dict, mode: str, current: dict | None) -> dict:
    source = output.get("source_display_settings") or {}
    dtype = np.dtype(output["file"]["dtype"])
    dtype_max = float(np.iinfo(dtype).max) if np.issubdtype(dtype, np.integer) else 1.0
    value_min = float(source.get("value_min", 0.0))
    value_max = float(source.get("value_max", dtype_max))
    if mode == "current_adjusted" and current is not None:
        black = float(current["black"])
        white = float(current["white"])
        gamma = float(current.get("gamma", 1.0))
    elif mode == "full_range":
        black, white, gamma = value_min, value_max, 1.0
    else:
        black = float(source.get("black", value_min))
        white = float(source.get("white", value_max))
        gamma = float(source.get("gamma", 1.0))
    modality = ((output.get("identity") or {}).get("modality") or {}).get("value")
    return {
        "black": black,
        "white": white,
        "gamma": gamma,
        "modality": modality,
        "lut": output.get("source_lut"),
        "physical_label": output.get("physical_label"),
    }


def export_presentations(
    *,
    adapter,
    plan: dict,
    output: Path,
    channels: list[int],
    scope: str,
    contrast_mode: str,
    use_lut: bool,
    current_frame: tuple[int, int, int] | None = None,
    current_states: dict[tuple[int, int], dict] | None = None,
    progress: Callable[[str], None] | None = None,
) -> Path:
    output = output.expanduser().resolve()
    partial = output.with_name(f"{output.name}.partial")
    if output.exists() or partial.exists():
        raise PresentationExportError(f"refusing to overwrite existing export: {output}")
    if not channels:
        raise PresentationExportError("select at least one channel")
    if contrast_mode not in {"current_adjusted", "source_display", "full_range"}:
        raise PresentationExportError(f"unsupported contrast mode: {contrast_mode}")
    current_states = current_states or {}
    partial.mkdir(parents=True)
    exported = []
    try:
        if scope == "current_frame":
            if current_frame is None:
                raise PresentationExportError("current frame coordinates are missing")
            coordinates = [current_frame]
        else:
            coordinates = []
            for series in plan["series"]:
                index = int(series["series_index"])
                if scope == "all_series_first_plane":
                    coordinates.append((index, 0, 0))
                elif scope == "all_planes":
                    coordinates.extend(
                        (index, z, t)
                        for t in range(int(series["dimensions"]["t"]))
                        for z in range(int(series["dimensions"]["z"]))
                    )
                else:
                    raise PresentationExportError(f"unsupported presentation scope: {scope}")

        for number, (series_index, z, t) in enumerate(coordinates, 1):
            series = plan["series"][series_index]
            output_by_channel = {
                int(item["channel_index"]): item for item in series["outputs"]
            }
            selected = [channel for channel in channels if channel in output_by_channel]
            if not selected:
                continue
            frames = {
                channel: adapter.images[series_index].get_frame(z=z, t=t, c=channel)
                for channel in selected
            }
            states = {
                channel: _render_state(
                    output_by_channel[channel],
                    contrast_mode,
                    current_states.get((series_index, channel)),
                )
                for channel in selected
            }
            rgb = compose_overlay(frames, states, use_lut=use_lut)
            channel_text = "+".join(f"C{channel:02d}" for channel in selected)
            name = sanitize_name(str(series["series_name"])) or f"Series{series_index + 1:03d}"
            relative = Path(
                f"S{series_index + 1:03d}_{name}__{channel_text}__Z{z + 1:04d}_T{t + 1:04d}.png"
            )
            Image.fromarray(rgb).save(partial / relative, format="PNG")
            exported.append(
                {
                    "path": relative.as_posix(),
                    "series_index": series_index,
                    "series_name": series["series_name"],
                    "z_index": z,
                    "t_index": t,
                    "channels": selected,
                    "render_states": {
                        f"C{channel:02d}": {
                            "black": states[channel]["black"],
                            "white": states[channel]["white"],
                            "gamma": states[channel]["gamma"],
                            "modality": states[channel]["modality"],
                            "lut": states[channel]["lut"],
                        }
                        for channel in selected
                    },
                }
            )
            if progress is not None:
                progress(f"Presentation frame {number}/{len(coordinates)}")

        if not exported:
            raise PresentationExportError(
                "the selected channels are not present in the requested frame scope"
            )

        recipe = {
            "schema_name": "candida_presentation_export",
            "schema_version": "0.1",
            "created_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
            "source": {
                "filename": (plan.get("source") or {}).get("filename"),
                "project_directory_name": (plan.get("project") or {}).get("directory_name"),
            },
            "scope": scope,
            "channels": channels,
            "contrast_mode": contrast_mode,
            "use_lut": bool(use_lut),
            "exported_frame_count": len(exported),
            "files": exported,
        }
        (partial / "presentation_recipe.json").write_text(
            json.dumps(recipe, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(partial, output)
    except BaseException:
        raise
    return output
