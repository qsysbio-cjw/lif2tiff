#!/usr/bin/env python3
"""Compatibility adapter exposing liffile through the converter's image API."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import liffile
import numpy as np


RECORDED_TIMEZONE_OFFSET = "+08:00"


def _recorded_datetime_text(value) -> str:
    """Convert Leica's UTC FILETIME instant to the instrument's UTC+8 timezone."""
    local_value = value + np.timedelta64(8, "h")
    text = np.datetime_as_string(local_value, unit="ms")
    if text.endswith("Z"):
        text = text[:-1]
    return f"{text}{RECORDED_TIMEZONE_OFFSET}"


def _recorded_text_or_raw(value) -> str:
    """Apply the recorded timezone whenever the value is a usable instant.

    Timestamp arrays that do not match the plane layout still hold real
    instants, so they must not fall back to naive UTC text: a bare value
    reads as eight hours earlier than the acquisition actually happened.
    """
    if isinstance(value, np.datetime64) and not np.isnat(value):
        return _recorded_datetime_text(value)
    return str(value)


def _first_mapping(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        return next((item for item in value if isinstance(item, dict)), {})
    return {}


class LifImageAdapter:
    """One liffile image, optionally restricted to one mosaic position."""

    def __init__(self, image, source_series_index: int, mosaic_index: int | None):
        self._image = image
        self.xml_element = getattr(image, "xml_element", None)
        self.source_series_index = source_series_index
        self.mosaic_index = mosaic_index
        self.xml_name = image.name
        self.name = image.path
        if mosaic_index is not None:
            self.name = f"{self.name} [M{mosaic_index:03d}]"

        sizes = image.sizes
        self.dims = SimpleNamespace(
            x=int(sizes.get("X", 1)),
            y=int(sizes.get("Y", 1)),
            z=int(sizes.get("Z", 1)),
            t=int(sizes.get("T", 1)),
        )
        self.channels = int(sizes.get("C", 1))
        bit_depth = int(np.dtype(image.dtype).itemsize * 8)
        self.bit_depth = tuple(bit_depth for _ in range(self.channels))
        self.numpy_dtype = np.dtype(image.dtype)
        self.scale = (
            self._pixels_per_um("X"),
            self._pixels_per_um("Y"),
            self._pixels_per_um("Z"),
        )
        self.time_points_s = [
            float(value) for value in image.coords.get("T", np.array([], dtype=float))
        ]
        self.acquisition_timestamps = self._timestamps()
        self.timepoint_timestamps = self._timepoint_timestamps()

        hardware = _first_mapping(image.attrs.get("HardwareSetting"))
        settings = _first_mapping(hardware.get("ATLConfocalSettingDefinition"))
        self.settings = dict(settings)
        if mosaic_index is not None and image.tilescan is not None:
            tile = image.tilescan.tiles[mosaic_index]
            self.settings.update(
                {
                    "StagePosX": float(tile["pos_x"]),
                    "StagePosY": float(tile["pos_y"]),
                    "ZPosition": float(tile["pos_z"]),
                }
            )

    def _timestamps(self) -> list[str]:
        values = np.asarray(self._image.timestamps)
        if not values.size:
            return []
        if values.dtype.kind != "M":
            flattened = values.reshape(-1)
            if all(value is None for value in flattened):
                return []
            return [_recorded_text_or_raw(value) for value in flattened]
        plane_axes = [axis for axis in self._image.dims if axis not in {"Y", "X"}]
        plane_shape = tuple(int(self._image.sizes[axis]) for axis in plane_axes)
        if values.size != int(np.prod(plane_shape)):
            return [_recorded_text_or_raw(value) for value in values.reshape(-1)]
        values = values.reshape(plane_shape)
        if self.mosaic_index is not None:
            selection = tuple(
                self.mosaic_index if axis == "M" else slice(None)
                for axis in plane_axes
            )
            values = values[selection]
        result = []
        for value in values.reshape(-1):
            result.append(_recorded_text_or_raw(value))
        return result

    def _timepoint_timestamps(self) -> list[str]:
        """Return the first recorded plane timestamp for each T coordinate."""
        values = np.asarray(self._image.timestamps)
        if not values.size or values.dtype.kind != "M":
            return []
        plane_axes = [axis for axis in self._image.dims if axis not in {"Y", "X"}]
        plane_shape = tuple(int(self._image.sizes[axis]) for axis in plane_axes)
        if values.size != int(np.prod(plane_shape)):
            return []
        values = values.reshape(plane_shape)
        if self.mosaic_index is not None and "M" in plane_axes:
            mosaic_axis = plane_axes.index("M")
            values = np.take(values, self.mosaic_index, axis=mosaic_axis)
            plane_axes.pop(mosaic_axis)
        if "T" not in plane_axes:
            values = values.reshape(-1)[:1]
        else:
            time_axis = plane_axes.index("T")
            values = np.moveaxis(values, time_axis, 0)
            values = values.reshape(values.shape[0], -1)[:, 0]
        result = []
        for value in values:
            result.append(_recorded_text_or_raw(value))
        return result

    def _pixels_per_um(self, axis: str) -> float:
        values = self._image.coords.get(axis)
        if values is None or len(values) < 2:
            return 0.0
        spacing_um = abs(float(values[1] - values[0])) * 1e6
        return 1.0 / spacing_um if spacing_um else 0.0

    def get_frame(self, *, z: int, t: int, c: int):
        indices = {}
        for axis, index in (("T", t), ("Z", z), ("C", c)):
            if axis in self._image.dims:
                indices[axis] = index
            elif index:
                raise IndexError(f"{axis} index {index} is invalid for {self.name}")
        if "M" in self._image.dims:
            if self.mosaic_index is None:
                raise RuntimeError(f"mosaic index is required for {self.name}")
            indices["M"] = self.mosaic_index
        frame = np.asarray(self._image.frame(**indices))
        frame = np.squeeze(frame)
        expected = (self.dims.y, self.dims.x)
        if frame.shape != expected:
            raise RuntimeError(
                f"liffile returned frame shape {frame.shape} for {self.name}, "
                f"expected {expected}"
            )
        return frame


class LifFileAdapter:
    """LifFile facade with deterministic mosaic expansion."""

    reader_name = "liffile"
    reader_version = liffile.__version__

    def __init__(self, path: str | Path):
        # Leica may retain singleton dimensions whose BytesInc is not a valid
        # stride after channel expansion. Liffile's default squeezing removes
        # only those length-one axes; the adapter restores them as size 1.
        self._lif = liffile.LifFile(Path(path), squeeze=True)
        self.xml_root = self._lif.xml_element
        self.xml_bytes = self._lif.xml_header().encode("utf-8")
        self.images = []
        for source_index, image in enumerate(self._lif.images):
            mosaic_count = int(image.sizes.get("M", 1))
            mosaic_indices = range(mosaic_count) if mosaic_count > 1 else (None,)
            self.images.extend(
                LifImageAdapter(image, source_index, mosaic_index)
                for mosaic_index in mosaic_indices
            )
        self.num_images = len(self.images)

    def get_iter_image(self):
        return iter(self.images)

    def close(self) -> None:
        self._lif.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()
        return False
