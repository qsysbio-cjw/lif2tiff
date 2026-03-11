#!/usr/bin/env python3
"""LIF to TIFF + metadata converter for Leica confocal microscopy files."""

import datetime
import argparse
import json
import logging
import os
import re
import shutil
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import tifffile
from readlif.reader import LifFile

logger = logging.getLogger(__name__)

# LUT name → human-readable label mapping
LUT_LABEL_MAP = {
    "gray": "brightfield",
    "grey": "brightfield",
    "green": "green",
    "red": "red",
    "blue": "blue",
    "cyan": "cyan",
    "magenta": "magenta",
    "yellow": "yellow",
}


def sanitize_name(name):
    """Make a string safe for use as a filename/directory name."""
    # Remove leading numbering like "1. " or "2. "
    name = re.sub(r"^\d+\.\s*", "", name)
    # Remove file extension
    name = re.sub(r"\.(lif|LIF)$", "", name)
    # Replace unsafe characters with underscore
    name = re.sub(r"[^\w\-.]", "_", name)
    # Collapse multiple underscores
    name = re.sub(r"_+", "_", name)
    return name.strip("_")


def get_channel_label(idx, lut_names=None):
    """Get human-readable label for a channel index, derived from LUT name."""
    if lut_names and idx < len(lut_names):
        lut = lut_names[idx].lower()
        return LUT_LABEL_MAP.get(lut, lut)
    return f"ch{idx}"


def _parse_timestamps(ts_text):
    """Parse space-separated hex timestamps from TimeStampList text."""
    if not ts_text:
        return []
    result = []
    EPOCH_DIFF = 116444736000000000
    for token in ts_text.strip().split():
        try:
            filetime = int(token, 16)
            timestamp_s = (filetime - EPOCH_DIFF) / 1e7
            dt = datetime.datetime.utcfromtimestamp(timestamp_s)
            result.append(dt.strftime("%Y-%m-%dT%H:%M:%SZ"))
        except (ValueError, OSError, OverflowError):
            pass
    return result


def extract_xml_metadata(xml_root, series_name):
    """Extract detailed metadata from LIF XML for a specific series."""
    result = {"laser_settings": [], "channel_descriptions": []}

    for image_elem in xml_root.iter("Element"):
        name_attr = image_elem.get("Name", "")
        if name_attr != series_name:
            continue

        # ATLConfocalSettingDefinition — grab all attributes as base
        for atl in image_elem.iter("ATLConfocalSettingDefinition"):
            for key in atl.attrib:
                if key not in result:
                    result[key] = atl.get(key)

        # Active laser lines only (IsVisible='1')
        for laser in image_elem.iter("LaserLineSetting"):
            if laser.get("IsVisible") != "1":
                continue
            entry = {
                "wavelength_nm": float(laser.get("LaserLine", 0)),
                "intensity_percent": float(laser.get("IntensityDev", 0)),
            }
            result["laser_settings"].append(entry)

        # Build detector lookup by name: name -> {gain, type, ...}
        # Use the first occurrence of each detector name that is active
        detector_by_name = {}
        for detector in image_elem.iter("Detector"):
            name = detector.get("Name", "")
            if name in detector_by_name:
                continue
            detector_by_name[name] = {
                "name": name,
                "type": detector.get("Type"),
                "scan_type": detector.get("ScanType"),
                "gain": float(detector.get("Gain", 0)),
                "offset": float(detector.get("Offset", 0)),
                "detection_range_begin_nm": detector.get("DetectionRangeBegin"),
                "detection_range_end_nm": detector.get("DetectionRangeEnd"),
                "acquisition_mode": detector.get("AcquisitionModeName"),
                "dye_name": detector.get("DyeName") or None,
            }

        # ChannelProperty gives per-channel mapping: order = C0, C1, C2...
        # Each channel has DetectorName and DyeName
        channel_props = []
        current = {}
        for cp in image_elem.iter("ChannelProperty"):
            key_elem = cp.find("Key")
            val_elem = cp.find("Value")
            if key_elem is None or val_elem is None:
                continue
            k, v = key_elem.text, val_elem.text
            if k == "ChannelGroup" and current:
                channel_props.append(current)
                current = {}
            current[k] = v
        if current:
            channel_props.append(current)

        # For each channel, attach the matching detector info
        result["channel_detector_map"] = []
        for ch_prop in channel_props:
            det_name = ch_prop.get("DetectorName")
            det_info = detector_by_name.get(det_name, {}) if det_name else {}
            result["channel_detector_map"].append({
                "detector_name": det_name,
                "dye_name": ch_prop.get("DyeName") or None,
                "sequential_index": ch_prop.get("SequentialSettingIndex"),
                "detector_type": det_info.get("type"),
                "scan_type": det_info.get("scan_type"),
                "gain": det_info.get("gain"),
                "offset": det_info.get("offset"),
                "detection_range_begin_nm": det_info.get("detection_range_begin_nm"),
                "detection_range_end_nm": det_info.get("detection_range_end_nm"),
                "acquisition_mode": det_info.get("acquisition_mode"),
            })

        # Channel descriptions — collect in order (sorted by BytesInc)
        ch_descs = list(image_elem.iter("ChannelDescription"))
        ch_descs_sorted = sorted(ch_descs, key=lambda e: int(e.get("BytesInc", 0)))
        for ch_desc in ch_descs_sorted:
            entry = {}
            for attr in ["LUTName", "Min", "Max"]:
                val = ch_desc.get(attr)
                if val is not None:
                    entry[attr] = val
            if entry:
                result["channel_descriptions"].append(entry)

        # Timestamps from TimeStampList
        for ts_list in image_elem.iter("TimeStampList"):
            timestamps = _parse_timestamps(ts_list.text)
            if timestamps:
                result["acquisition_timestamps"] = timestamps
            break

        break  # Found the matching series

    return result


def extract_metadata(image, xml_root, source_file, series_index):
    """Extract all metadata for a series."""
    dims = image.dims
    settings = {}
    try:
        settings = image.settings or {}
    except Exception:
        pass

    # Pixel size: scale is in px/µm, convert to µm/px
    scale = image.scale
    pixel_size_x = 1.0 / scale[0] if scale[0] else None
    pixel_size_y = 1.0 / scale[1] if scale[1] else None
    pixel_size_z = 1.0 / scale[2] if scale[2] else None

    # Determine dimension type
    has_z = dims.z > 1
    has_t = dims.t > 1
    if has_z and has_t:
        dim_type = "ZT"
    elif has_z:
        dim_type = "Z-stack"
    elif has_t:
        dim_type = "time-lapse"
    else:
        dim_type = "2D"

    # Parse pinhole from meters to µm
    pinhole_m = settings.get("Pinhole")
    pinhole_um = float(pinhole_m) * 1e6 if pinhole_m else None

    # XML metadata (needed for LUT names before building channel_info)
    xml_meta = extract_xml_metadata(xml_root, image.name)

    # Extract LUT names from channel descriptions (already sorted by BytesInc in extract_xml_metadata)
    lut_names = [ch.get("LUTName", "") for ch in xml_meta.get("channel_descriptions", [])]
    ch_det_map = xml_meta.get("channel_detector_map", [])

    # Build channel info — merge LUT, bit depth, and detector info per channel
    bit_depths = image.bit_depth
    if not isinstance(bit_depths, (list, tuple)):
        bit_depths = (bit_depths,) * image.channels

    channel_info = []
    for i in range(image.channels):
        det = ch_det_map[i] if i < len(ch_det_map) else {}
        channel_info.append({
            "index": i,
            "label": get_channel_label(i, lut_names),
            "lut": lut_names[i] if i < len(lut_names) else None,
            "bit_depth": bit_depths[i] if i < len(bit_depths) else None,
            "detector_name": det.get("detector_name"),
            "detector_type": det.get("detector_type"),
            "scan_type": det.get("scan_type"),
            "gain": det.get("gain"),
            "offset": det.get("offset"),
            "detection_range_begin_nm": det.get("detection_range_begin_nm"),
            "detection_range_end_nm": det.get("detection_range_end_nm"),
            "acquisition_mode": det.get("acquisition_mode"),
            "dye_name": det.get("dye_name"),
            "sequential_index": det.get("sequential_index"),
        })

    metadata = {
        "source_file": os.path.basename(source_file),
        "series_name": image.name,
        "series_index": series_index,
        "dimension_type": dim_type,
        "dimensions": {
            "x": dims.x,
            "y": dims.y,
            "z": dims.z,
            "t": dims.t,
            "channels": image.channels,
        },
        "pixel_size": {
            "x_um_per_px": round(pixel_size_x, 6) if pixel_size_x else None,
            "y_um_per_px": round(pixel_size_y, 6) if pixel_size_y else None,
            "z_um_per_px": round(pixel_size_z, 6) if pixel_size_z else None,
        },
        "bit_depth_per_channel": list(bit_depths),
        "channel_info": channel_info,
        "objective": {
            "name": settings.get("ObjectiveName", "").strip(),
            "magnification": settings.get("Magnification"),
            "numerical_aperture": settings.get("NumericalAperture"),
            "immersion": settings.get("Immersion"),
        },
        "confocal_settings": {
            "scan_mode": settings.get("ScanMode"),
            "line_averaging": int(settings["LineAverage"]) if "LineAverage" in settings else None,
            "frame_averaging": int(settings["FrameAverage"]) if "FrameAverage" in settings else None,
            "pinhole_um": round(pinhole_um, 2) if pinhole_um else None,
            "pinhole_airy": float(settings["PinholeAiry"]) if "PinholeAiry" in settings else None,
            "zoom": float(settings["Zoom"]) if "Zoom" in settings else None,
            "scan_speed": settings.get("ScanSpeed"),
            "scan_direction": settings.get("ScanDirectionXName"),
            "pixel_dwell_time_us": round(float(settings["PixelDwellTime"]) * 1e6, 3) if "PixelDwellTime" in settings else None,
        },
        "microscope": {
            "model": settings.get("MicroscopeModel"),
            "serial": settings.get("SystemSerialNumber"),
        },
        "stage_position": {
            "x_m": float(settings["StagePosX"]) if "StagePosX" in settings else None,
            "y_m": float(settings["StagePosY"]) if "StagePosY" in settings else None,
            "z_m": float(settings["ZPosition"]) if "ZPosition" in settings else None,
        },
        "laser_settings": xml_meta.get("laser_settings", []),
        "acquisition_timestamps": xml_meta.get("acquisition_timestamps", []),
        "optical_settings": {
            "refraction_index": float(settings["RefractionIndex"]) if "RefractionIndex" in settings else None,
            "tld_mode": settings.get("ActiveCS_SubModeForTLDName"),
            "emission_wavelength_for_pinhole_airy_nm": float(settings["EmissionWavelengthForPinholeAiryCalculation"]) if "EmissionWavelengthForPinholeAiryCalculation" in settings else None,
        },
    }

    return metadata


def write_tiff(array, filepath, pixel_size_x=None, pixel_size_y=None, pixel_size_z=None):
    """Write numpy array as TIFF with metadata."""
    kwargs = {"compression": "lzw"}

    if pixel_size_x and pixel_size_y:
        # resolution in pixels per µm
        kwargs["resolution"] = (1.0 / pixel_size_x, 1.0 / pixel_size_y)
        kwargs["resolutionunit"] = None  # custom unit
        kwargs["imagej"] = True
        kwargs["metadata"] = {"unit": "um"}
        if pixel_size_z and array.ndim == 3:
            kwargs["metadata"]["spacing"] = pixel_size_z
            kwargs["metadata"]["axes"] = "ZYX"

    tifffile.imwrite(str(filepath), array, **kwargs)


def get_channel_info(lif_path):
    """Quick scan of a LIF file to get unique channel labels (no image export)."""
    lif = LifFile(str(lif_path))
    seen = {}
    for image in lif.get_iter_image():
        xml_meta = extract_xml_metadata(lif.xml_root, image.name)
        lut_names = [ch.get("LUTName", "") for ch in xml_meta.get("channel_descriptions", [])]
        for i in range(image.channels):
            label = get_channel_label(i, lut_names)
            if label not in seen:
                seen[label] = lut_names[i] if i < len(lut_names) else ""
    return seen  # {label: lut_name}


def export_series(image, output_dir, metadata, dry_run=False, channels_filter=None):
    """Export a single series to TIFF files + metadata JSON."""
    dims = metadata["dimensions"]
    px = metadata["pixel_size"]
    dim_type = metadata["dimension_type"]

    os.makedirs(output_dir, exist_ok=True)

    # Write metadata JSON
    meta_path = output_dir / f"{image.name}_metadata.json"
    if not dry_run:
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(metadata, f, indent=2, ensure_ascii=False)
        logger.info("  Metadata: %s", meta_path.name)

    if dry_run:
        return

    n_channels = dims["channels"]
    n_z = dims["z"]
    n_t = dims["t"]

    for c in range(n_channels):
        label = metadata["channel_info"][c]["label"] if c < len(metadata["channel_info"]) else f"ch{c}"
        if channels_filter is not None and label not in channels_filter:
            continue
        bit_depth = metadata["bit_depth_per_channel"][c] if c < len(metadata["bit_depth_per_channel"]) else 8
        dtype = np.uint16 if bit_depth > 8 else np.uint8

        if dim_type == "2D":
            # Single 2D image per channel
            frame = image.get_frame(z=0, t=0, c=c)
            arr = np.array(frame, dtype=dtype)
            fname = f"{image.name}_C{c}_{label}.tif"
            write_tiff(arr, output_dir / fname, px["x_um_per_px"], px["y_um_per_px"])
            logger.info("  Exported: %s (%s)", fname, arr.shape)

        elif dim_type == "Z-stack":
            # Multi-page TIFF for Z-stack
            slices = []
            for z in range(n_z):
                frame = image.get_frame(z=z, t=0, c=c)
                slices.append(np.array(frame, dtype=dtype))
            arr = np.stack(slices, axis=0)  # (Z, Y, X)
            fname = f"{image.name}_C{c}_{label}_Zstack.tif"
            write_tiff(arr, output_dir / fname, px["x_um_per_px"], px["y_um_per_px"], px["z_um_per_px"])
            logger.info("  Exported: %s (%s)", fname, arr.shape)

        elif dim_type == "time-lapse":
            # One TIFF per timepoint
            for t in range(n_t):
                t_dir = output_dir / f"T{t:03d}"
                os.makedirs(t_dir, exist_ok=True)
                frame = image.get_frame(z=0, t=t, c=c)
                arr = np.array(frame, dtype=dtype)
                fname = f"{image.name}_T{t:03d}_C{c}_{label}.tif"
                write_tiff(arr, t_dir / fname, px["x_um_per_px"], px["y_um_per_px"])
            logger.info("  Exported: C%d_%s, %d timepoints", c, label, n_t)

        elif dim_type == "ZT":
            # Per-timepoint subdirectories, each with Z-stack TIFF
            for t in range(n_t):
                t_dir = output_dir / f"T{t:03d}"
                os.makedirs(t_dir, exist_ok=True)
                slices = []
                for z in range(n_z):
                    frame = image.get_frame(z=z, t=t, c=c)
                    slices.append(np.array(frame, dtype=dtype))
                arr = np.stack(slices, axis=0)
                fname = f"{image.name}_T{t:03d}_C{c}_{label}_Zstack.tif"
                write_tiff(arr, t_dir / fname, px["x_um_per_px"], px["y_um_per_px"], px["z_um_per_px"])
            logger.info("  Exported: C%d_%s, %d timepoints x %d Z-slices", c, label, n_t, n_z)


def _apply_lut(gray_path, label):
    """Apply color LUT to a grayscale uint8 TIFF, return RGB uint8 array or None."""
    LUT_RGB = {
        "green":   (0, 1, 0),
        "red":     (1, 0, 0),
        "blue":    (0, 0, 1),
        "cyan":    (0, 1, 1),
        "magenta": (1, 0, 1),
        "yellow":  (1, 1, 0),
    }
    coeff = LUT_RGB.get(label)
    if coeff is None:
        return None

    with tifffile.TiffFile(gray_path) as tif:
        img = tif.asarray()

    if img.dtype != np.uint8:
        logger.warning("  LUT skipped for %s: only uint8 supported (got %s)", gray_path.name, img.dtype)
        return None

    rgb = np.zeros((*img.shape, 3), dtype=np.uint8)
    for i, c in enumerate(coeff):
        if c:
            rgb[..., i] = img
    return rgb


def build_by_channel(lif_dir, dry_run=False):
    """Copy exported files into by_channel/ subdirectory grouped by channel/metadata."""
    by_channel_dir = lif_dir / "by_channel"

    # Collect all files from series directories
    for series_dir in sorted(lif_dir.iterdir()):
        if not series_dir.is_dir() or series_dir.name == "by_channel":
            continue
        for f in sorted(series_dir.iterdir()):
            if f.suffix == ".json":
                dest_dir = by_channel_dir / "metadata"
            elif f.suffix == ".tif":
                # Extract channel label from filename: ..._C0_green.tif -> green
                parts = f.stem.split("_")
                # Find the part after the CN part
                label = "unknown"
                for j, p in enumerate(parts):
                    if re.match(r"^C\d+$", p) and j + 1 < len(parts):
                        label = parts[j + 1]
                        break
                dest_dir = by_channel_dir / label
            else:
                continue

            if not dry_run:
                os.makedirs(dest_dir, exist_ok=True)
                shutil.copy2(f, dest_dir / f.name)

                # Extra LUT-colourised copy for fluorescence channels
                if f.suffix == ".tif" and label not in ("brightfield", "unknown"):
                    rgb = _apply_lut(f, label)
                    if rgb is not None:
                        lut_dir = by_channel_dir / f"{label}_lut"
                        os.makedirs(lut_dir, exist_ok=True)
                        tifffile.imwrite(str(lut_dir / f.name), rgb, compression="lzw")

    if not dry_run:
        logger.info("  by_channel/: organized into %s",
                    ", ".join(d.name for d in sorted(by_channel_dir.iterdir()) if d.is_dir()))


def process_lif(lif_path, output_base_dir, dry_run=False, dump_xml=False,
                channels_filter=None, progress_callback=None):
    """Process a single LIF file."""
    lif_path = Path(lif_path)
    logger.info("Processing: %s", lif_path.name)

    lif = LifFile(str(lif_path))
    logger.info("  Found %d series", lif.num_images)

    # Dump XML if requested
    if dump_xml:
        xml_str = ET.tostring(lif.xml_root, encoding="unicode")
        xml_out = output_base_dir / f"{sanitize_name(lif_path.name)}_metadata.xml"
        os.makedirs(output_base_dir, exist_ok=True)
        with open(xml_out, "w", encoding="utf-8") as f:
            f.write(xml_str)
        logger.info("  XML dumped to: %s", xml_out)

    lif_dir = output_base_dir / sanitize_name(lif_path.name)

    for i, image in enumerate(lif.get_iter_image()):
        metadata = extract_metadata(image, lif.xml_root, str(lif_path), i)
        dims = metadata["dimensions"]
        px = metadata["pixel_size"]

        dim_desc = f"{dims['x']}x{dims['y']}"
        if dims["z"] > 1:
            dim_desc += f"x{dims['z']}Z"
        if dims["t"] > 1:
            dim_desc += f"x{dims['t']}T"
        dim_desc += f", {dims['channels']}ch, {metadata['dimension_type']}"

        px_desc = ""
        if px["x_um_per_px"]:
            px_desc = f", {px['x_um_per_px']:.4f} µm/px"

        logger.info("  Series %d/%d: \"%s\" (%s%s)",
                     i + 1, lif.num_images, image.name, dim_desc, px_desc)

        series_dir = lif_dir / image.name
        export_series(image, series_dir, metadata, dry_run=dry_run, channels_filter=channels_filter)
        if progress_callback:
            progress_callback(i + 1, lif.num_images,
                              f"Done: {image.name} ({i+1}/{lif.num_images})")

    if not dry_run:
        logger.info("  Building by_channel/ index...")
        build_by_channel(lif_dir)

    logger.info("  Done: %s\n", lif_path.name)


def main():
    parser = argparse.ArgumentParser(
        description="Convert Leica LIF files to TIFF + metadata JSON")
    parser.add_argument("input", nargs="+",
                        help="LIF file(s) or directory containing LIF files")
    parser.add_argument("-o", "--output-dir", default="./output",
                        help="Output directory (default: ./output)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show series info without exporting")
    parser.add_argument("--dump-xml", action="store_true",
                        help="Dump raw LIF XML metadata to file")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Enable verbose logging")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s",
    )

    output_dir = Path(args.output_dir)

    # Collect LIF files
    lif_files = []
    for inp in args.input:
        p = Path(inp)
        if p.is_dir():
            lif_files.extend(sorted(p.glob("*.lif")))
            lif_files.extend(sorted(p.glob("*.LIF")))
        elif p.is_file() and p.suffix.lower() == ".lif":
            lif_files.append(p)
        else:
            logger.warning("Skipping: %s (not a LIF file or directory)", inp)

    if not lif_files:
        logger.error("No LIF files found.")
        sys.exit(1)

    logger.info("Found %d LIF file(s)\n", len(lif_files))

    for lif_path in lif_files:
        try:
            process_lif(lif_path, output_dir, dry_run=args.dry_run,
                        dump_xml=args.dump_xml)
        except Exception as e:
            logger.error("Error processing %s: %s", lif_path.name, e)
            if args.verbose:
                import traceback
                traceback.print_exc()


if __name__ == "__main__":
    main()
